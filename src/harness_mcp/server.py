"""MCP 工具与主对话归属；stdout 仅由 SDK 输出协议。"""

import argparse
from contextlib import asynccontextmanager
import inspect
from pathlib import Path
import sys
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from . import __version__
from .config import AppConfig, load_config
from .errors import HarnessError, error_payload
from .redaction import redact_value
from .task_manager import TaskManager


class ToolReply(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool
    error: dict[str, Any] | None = None


def create_server(config: AppConfig, runner=None, *, manager=None, sessions: bool = False, claude_channel=None, config_path: Path | None = None) -> MCPServer:
    # 直接模式持有任务管理器；共享模式注入只连接后台的会话代理。
    if manager is None:
        manager = TaskManager(config, runner)
        manager.config_path = config_path
    if claude_channel is not None and hasattr(manager, "enable_channel"):
        async def channel_callback(payload):
            return await claude_channel.send_completion(payload["origin"], payload["prompt"], payload["call_id"])
        manager.enable_channel(claude_channel.origin(), channel_callback)

    @asynccontextmanager
    async def lifespan(_server):
        dispatcher = None
        try:
            await manager.start()
            if config.completion.auto_continue and isinstance(manager, TaskManager):
                from .completion import CompletionDispatcher
                dispatcher = CompletionDispatcher(manager, sender=claude_channel.send_completion if claude_channel is not None else None)
                await dispatcher.start()
            yield manager
        finally:
            if dispatcher is not None:
                await dispatcher.close()
            await manager.close()
            if claude_channel is not None:
                claude_channel.detach()

    server = MCPServer(
        "deepseek-harness-mcp", version=__version__, lifespan=lifespan,
        instructions=f"提交经本机授权的代码审查或副本修改任务。mode 默认 {config.default_mode}，可显式指定 review；edit 只修改副本，主 AI 复核合入。回执 auto_continue=true 时已绑定本客户端的完成通知机制，主 AI 读取完整 result 并处理后用 harness_completion_ack 确认；否则按建议间隔查询。Claude channel 还需启动客户端时登记渠道，channel_written 只证明已写出，acknowledged 才证明处理完成。取消只能通过 harness_cancel。通知不扩大原授权；失败或取消不自动重跑。",
        log_level="WARNING",
    )
    if claude_channel is not None:
        from .claude_channel import configure_channel
        configure_channel(server)

    def current_origin(ctx):
        if claude_channel is not None:
            if not config.completion.auto_continue:
                return None
            try:
                claude_channel.attach_context(ctx)
            except Exception:
                return None
            return claude_channel.origin()
        from .completion import origin_from_context
        return origin_from_context(ctx, config)

    async def accepted_origin(ctx):
        origin = current_origin(ctx)
        if origin is None:
            return None, "AUTO_CONTINUE_DISABLED" if not config.completion.auto_continue else "COMPLETION_CONTEXT_UNAVAILABLE"
        if claude_channel is not None:
            # 是否登记渠道由 Claude 启动参数决定；协议没有宿主送达 ACK。
            return origin, None
        from .codex_app_bridge import inspect_origin
        try:
            await inspect_origin(origin)
        except Exception as error:
            code = getattr(error, "code", "COMPLETION_HOST_UNAVAILABLE")
            return None, code
        return origin, None

    def automatic_receipt(origin, reason):
        value = {"auto_continue": origin is not None}
        if reason is not None:
            value["auto_continue_reason"] = reason
        if origin is not None and claude_channel is not None:
            value["notification_transport"] = "claude_channel"
            value["channel_registration"] = "required_at_client_startup"
        return value

    def reply(data=None, error=None):
        if error is not None:
            # 保留旧业务错误的两字段接口；未知异常和已有诊断使用固定入口阶段。
            stage = None if isinstance(error, HarnessError) and error.code != "INTERNAL_ERROR" and not error.details else "tool_request"
            return ToolReply(ok=False, error=redact_value(error_payload(error, stage=stage),manager.secrets))
        return ToolReply(ok=True, **data)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def harness_submit(project_id: str, prompt: str, client_request_id: str, relative_paths: list[str] | None = None, timeout_seconds: int | None = None, mode: Literal["review", "edit"] = config.default_mode, ctx: Context = None) -> ToolReply:
        """委派代码审查或修改授权副本，立即返回 job_id；edit 的实际差异需主 AI 合入，同编号重试不重放。"""
        try:
            origin, reason = await accepted_origin(ctx)
            extras = {"completion_origin": origin} if origin is not None else {}
            result = await manager.submit(project_id,prompt,client_request_id,relative_paths,timeout_seconds,mode,**extras)
            return reply({**result, **automatic_receipt(origin, reason)})
        except Exception as error:
            return reply(error=error)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def harness_status(job_id: str) -> ToolReply:
        """返回任务状态、阶段、真实耗时、最近活动和脱敏错误，不提供估算百分比。"""
        try:
            value = manager.status(job_id)
            return reply(await value if inspect.isawaitable(value) else value)
        except Exception as error:
            return reply(error=error)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def harness_result(job_id: str, cursor: str | None = None, page_size: int = 20, view: Literal["full", "summary", "patch", "files"] = "full") -> ToolReply:
        """按需读取 summary、patch、files 或兼容 full；游标绑定视图，质量与执行状态独立。"""
        try:
            value = manager.result(job_id,cursor,page_size,view)
            return reply(await value if inspect.isawaitable(value) else value)
        except Exception as error:
            return reply(error=error)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def harness_service_info() -> ToolReply:
        """查看当前/磁盘配置版本、是否需要重连，以及活动连接、排队和活动任务数量。"""
        try:
            value = manager.service_info()
            value = await value if inspect.isawaitable(value) else value
            if isinstance(manager, TaskManager):
                value = {**value, "active_connections": 1, "connection_mode": "direct"}
            return reply(value)
        except Exception as error:
            return reply(error=error)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def harness_preflight(project_id: str, relative_paths: list[str] | None = None, mode: Literal["review", "edit"] = config.default_mode) -> ToolReply:
        """提交前核对授权源码的凭证规则，失败仅返回脱敏路径/行号/规则；不调用模型、不修改项目。"""
        try:
            value = manager.preflight(project_id, relative_paths, mode)
            return reply(await value if inspect.isawaitable(value) else value)
        except Exception as error:
            return reply(error=error)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    async def harness_cancel(job_id: str) -> ToolReply:
        """幂等发起取消；运行任务确认执行器已停止后才进入 cancelled。"""
        try:
            return reply(await manager.cancel(job_id))
        except Exception as error:
            return reply(error=error)

    if sessions:
        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
        async def harness_session_open(project_id: str, client_request_id: str, title: str = "", mode: Literal["review", "edit"] = config.default_mode, relative_paths: list[str] | None = None) -> ToolReply:
            """创建绑定项目与范围的独立 DeepSeek 子代理对话；一个 Codex 对话可打开多个会话，不调用模型。"""
            try:
                return reply(await manager.session_open(project_id, client_request_id, title, mode, relative_paths))
            except Exception as error:
                return reply(error=error)

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
        async def harness_session_send(session_id: str, prompt: str, client_request_id: str, timeout_seconds: int | None = None, ctx: Context = None) -> ToolReply:
            """给子代理发任务或追问，返回 job_id；继承本会话有界历史。同会话须等上一轮结束，不同会话可并行。"""
            try:
                origin, reason = await accepted_origin(ctx)
                extras = {"completion_origin": origin} if origin is not None else {}
                result = await manager.session_send(session_id, prompt, client_request_id, timeout_seconds, **extras)
                return reply({**result, **automatic_receipt(origin, reason)})
            except Exception as error:
                return reply(error=error)

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
        async def harness_session_list(project_id: str | None = None, limit: int = 20) -> ToolReply:
            """找回独立子代理会话及近期作业编号；按项目筛选，随后用任务工具查状态、结果或取消。"""
            try:
                value = manager.session_list(project_id, limit)
                return reply(await value if inspect.isawaitable(value) else value)
            except Exception as error:
                return reply(error=error)

    if config.completion.auto_continue:
        async def invoke_completion(method, **arguments):
            value = getattr(manager, method)(**arguments)
            return await value if inspect.isawaitable(value) else value

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
        async def harness_watch(job_ids: list[str], ctx: Context) -> ToolReply:
            """为已有任务启用完成后自动通知原主对话，不重放模型；目标只取宿主元数据。"""
            try:
                origin, reason = await accepted_origin(ctx)
                if origin is None:
                    from .errors import HarnessError
                    raise HarnessError("AUTO_CONTINUE_UNAVAILABLE", "当前客户端无法绑定完成通知：" + reason)
                result = await invoke_completion("completion_watch", job_ids=job_ids, origin=origin)
                return reply({**result, **automatic_receipt(origin, reason)})
            except Exception as error:
                return reply(error=error)

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
        async def harness_completion_list(state: str | None = None, limit: int = 20, ctx: Context = None) -> ToolReply:
            """读取当前主对话的完成投递状态，delivered 与完整处理后的 acknowledged 分别记录。"""
            try:
                return reply(await invoke_completion("completion_list", origin=current_origin(ctx), state=state, limit=limit))
            except Exception as error:
                return reply(error=error)

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
        async def harness_completion_ack(event_ids: list[int], ctx: Context) -> ToolReply:
            """完整读取并处理任务后确认完成事件；通知送达或读取第一页不能代替确认。"""
            try:
                return reply(await invoke_completion("completion_ack", event_ids=event_ids, origin=current_origin(ctx)))
            except Exception as error:
                return reply(error=error)

    return server


def main() -> None:
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    parser = argparse.ArgumentParser(description="DeepSeek Harness MCP 本地 stdio 服务")
    parser.add_argument("--config", type=Path, required=True, help="本机可信 TOML 配置，不包含明文密钥")
    parser.add_argument("--shared", action="store_true", help="多个 stdio 会话连接同一个本机任务后台")
    parser.add_argument("--claude-channel", action="store_true", help="显式启用 Claude Code channel；客户端还须登记本渠道并使用兼容协议")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        channel = None
        if args.claude_channel:
            if not config.completion.auto_continue:
                from .errors import HarnessError
                raise HarnessError("AUTO_CONTINUE_DISABLED", "Claude channel 需要可信配置 completion.auto_continue=true。")
            from .claude_channel import ClaudeChannel
            channel = ClaudeChannel()
        if args.shared:
            from .shared_client import SharedClient
            create_server(config, manager=SharedClient(config, config_path=config_path), sessions=True, claude_channel=channel).run()
        else:
            create_server(config, claude_channel=channel, config_path=config_path).run()
    except Exception as error:
        payload = error_payload(error, stage="startup")
        print(f"{payload['code']}: {payload['message']} [diagnostic_id={payload['diagnostic_id']}, stage={payload['stage']}, exception_type={payload['exception_type']}]",file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

