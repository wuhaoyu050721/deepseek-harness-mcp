"""四个 MCP 工具；stdout 仅由 SDK 输出协议，任务由后台管理器执行。"""

import argparse
from contextlib import asynccontextmanager
import inspect
from pathlib import Path
import sys
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from . import __version__
from .config import AppConfig, load_config
from .errors import error_payload
from .redaction import redact_value
from .task_manager import TaskManager


class ToolReply(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool
    error: dict[str, Any] | None = None


def create_server(config: AppConfig, runner=None, *, manager=None, sessions: bool = False) -> MCPServer:
    # 直接模式持有任务管理器；共享模式注入只连接后台的会话代理。
    if manager is None:
        manager = TaskManager(config, runner)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            await manager.start()
            yield manager
        finally:
            await manager.close()

    server = MCPServer(
        "deepseek-harness-mcp", version=__version__, lifespan=lifespan,
        instructions="提交经本机授权的代码审查或副本修改任务。mode 默认 review；edit 只修改独立副本，result 返回实际差异，由主 AI 复核并合入原项目。submit 后按建议间隔查询；取消只能通过 harness_cancel；单次查询停止不取消后台任务。",
        log_level="WARNING",
    )

    def reply(data=None, error=None):
        if error is not None:
            return ToolReply(ok=False, error=redact_value(error_payload(error),manager.secrets))
        return ToolReply(ok=True, **data)

    @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def harness_submit(project_id: str, prompt: str, client_request_id: str, relative_paths: list[str] | None = None, timeout_seconds: int | None = None, mode: Literal["review", "edit"] = "review") -> ToolReply:
        """委派代码审查或修改授权副本，立即返回 job_id；edit 的实际差异需主 AI 合入，同编号重试不重放。"""
        try:
            return reply(await manager.submit(project_id,prompt,client_request_id,relative_paths,timeout_seconds,mode))
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
    async def harness_result(job_id: str, cursor: str | None = None, page_size: int = 20) -> ToolReply:
        """读取摘要、问题、报告文本及 edit 实际差异/后内容片段；沿 next_cursor 取完整修改，尚未保存时 ready=false。"""
        try:
            value = manager.result(job_id,cursor,page_size)
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
        async def harness_session_open(project_id: str, client_request_id: str, title: str = "", mode: Literal["review", "edit"] = "review", relative_paths: list[str] | None = None) -> ToolReply:
            """创建绑定项目与范围的独立 DeepSeek 子代理对话；一个 Codex 对话可打开多个会话，不调用模型。"""
            try:
                return reply(await manager.session_open(project_id, client_request_id, title, mode, relative_paths))
            except Exception as error:
                return reply(error=error)

        @server.tool(structured_output=True, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
        async def harness_session_send(session_id: str, prompt: str, client_request_id: str, timeout_seconds: int | None = None) -> ToolReply:
            """给子代理发任务或追问，返回 job_id；继承本会话有界历史。同会话须等上一轮结束，不同会话可并行。"""
            try:
                return reply(await manager.session_send(session_id, prompt, client_request_id, timeout_seconds))
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

    return server


def main() -> None:
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    parser = argparse.ArgumentParser(description="DeepSeek Harness MCP 本地 stdio 服务")
    parser.add_argument("--config", type=Path, required=True, help="本机可信 TOML 配置，不包含明文密钥")
    parser.add_argument("--shared", action="store_true", help="多个 stdio 会话连接同一个本机任务后台")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        if args.shared:
            from .shared_client import SharedClient
            create_server(config, manager=SharedClient(config, config_path=config_path), sessions=True).run()
        else:
            create_server(config).run()
    except Exception as error:
        payload = error_payload(error)
        print(f"{payload['code']}: {payload['message']}",file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

