"""Claude Code 官方 channel 适配；写入 stdio 不等于主 AI 已处理结果。"""
from __future__ import annotations

import asyncio
import inspect
import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import HarnessError


CHANNEL_METHOD = "notifications/claude/channel"
CHANNEL_CAPABILITIES = {"claude/channel": {}}
# Claude 的 modern MCP 协议没有 unsolicited notification 路径。
# 只接受本项目 SDK 已知的 handshake 协议，不把未知未来版本当作可推送。
LEGACY_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


class ChannelDeliveryError(HarnessError):
    def __init__(self, code: str, message: str, *, uncertain: bool):
        super().__init__(code, message)
        self.uncertain = uncertain


class ChannelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=65536)
    # Claude 此处要求名为 meta 的普通属性，不能使用 MCP 的 _meta。
    meta: dict[str, str]


class ChannelNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["notifications/claude/channel"] = CHANNEL_METHOD
    params: ChannelParams


def _canonical_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid channel identity")
    parsed = str(UUID(value))
    if parsed != value:
        raise ValueError("invalid channel identity")
    return parsed


def configure_channel(server, *, enabled: bool = True) -> None:
    """为显式启用的 stdio 前端声明实验 capability，保留 SDK 原能力。"""
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    if not enabled:
        return
    lowlevel = getattr(server, "_lowlevel_server", server)
    if getattr(lowlevel, "_harness_claude_channel_configured", False):
        return
    original = lowlevel.create_initialization_options
    signature = inspect.signature(original)

    def initialization_options(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        experimental = dict(arguments.arguments.get("experimental_capabilities") or {})
        experimental["claude/channel"] = {}
        arguments.arguments["experimental_capabilities"] = experimental
        return original(*arguments.args, **arguments.kwargs)

    lowlevel.create_initialization_options = initialization_options
    lowlevel._harness_claude_channel_configured = True


class ClaudeChannel:
    """每个 stdio 前端只有一个私有路由，禁止目标参数改投另一窗口。

    connection_id 和 session_id 都是服务自己的连接范围标识，后者不声称
    是 Claude 的真实 session ID。Claude /clear 与交互 resume 可能继续使用
    原 MCP 进程，因此本适配保证窗口/连接隔离，不虚构跨会话切换恢复。
    """

    def __init__(self, *, connection_id: str | None = None, session_id: str | None = None):
        self.connection_id = _canonical_uuid(connection_id if connection_id is not None else str(uuid4()))
        self.session_id = _canonical_uuid(session_id if session_id is not None else str(uuid4()))
        self._session = None

    def origin(self) -> dict[str, str]:
        return {"client": "claude_code", "connection_id": self.connection_id, "session_id": self.session_id}

    def attach_context(self, ctx) -> dict[str, str]:
        """只留 SDK standalone connection；不使用请求结束后失效的输出流。"""
        self._session = None
        try:
            session = ctx.session
            protocol = session.protocol_version
        except (AttributeError, ValueError):
            raise ChannelDeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "当前请求没有 Claude channel 的 stdio 会话上下文。", uncertain=False) from None
        if not callable(getattr(session, "send_notification", None)) or protocol not in LEGACY_PROTOCOL_VERSIONS:
            raise ChannelDeliveryError("CLAUDE_CHANNEL_PROTOCOL_UNAVAILABLE", "Claude channel 需要 handshake 协议；请在启动 Claude Code 前设置 MCP_PROTOCOL_NEGOTIATION=legacy。", uncertain=False)
        self._session = session
        return self.origin()

    def detach(self) -> None:
        """前端结束后不再向旧会话写出；共享后台任务继续保存。"""
        self._session = None

    async def send_completion(self, origin, prompt: str, call_id: str) -> dict:
        """官方通知只写往当前已连接的前端；消费确认由 completion_ack 完成。"""
        if not isinstance(origin, dict) or origin != self.origin():
            raise ChannelDeliveryError("COMPLETION_TARGET_MISMATCH", "完成事件不属于当前 Claude channel 前端。", uncertain=False)
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise ChannelDeliveryError("CLAUDE_CHANNEL_INVALID_EVENT", "完成事件的投递编号无效。", uncertain=False)
        if self._session is None:
            raise ChannelDeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "Claude channel 尚未连接；事件仍保留在完成队列中。", uncertain=False)
        try:
            notification = ChannelNotification(params=ChannelParams(content=prompt, meta={"delivery_id": call_id}))
        except (ValueError, TypeError):
            raise ChannelDeliveryError("CLAUDE_CHANNEL_INVALID_EVENT", "完成事件的通知内容无效。", uncertain=False) from None
        try:
            # SDK related_request_id=None 使用 standalone channel，可在调用结束后写出。
            await asyncio.wait_for(self._session.send_notification(notification), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 写出途中断连或超时不保证未到达，禁止后台盲目重发。
            raise ChannelDeliveryError("CLAUDE_CHANNEL_DELIVERY_UNKNOWN", "Claude channel 写出结果不明，请从原任务编号恢复并确认完成事件。", uncertain=True) from None
        return {"status": "channel_written", "acknowledged": False}
