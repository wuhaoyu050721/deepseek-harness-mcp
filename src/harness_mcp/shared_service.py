"""本机共享后台：多个 stdio 前端复用同一持锁管理器和任务队列。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import inspect
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tomllib
import uuid
from typing import Any

from . import __version__
from .config import AppConfig, load_config
from .errors import HarnessError, error_payload
from .redaction import redact_value
from .shared_client import (
    AUTH_MAX_BYTES,
    RPC_MAX_BYTES,
    authentication_proof,
    channel_origin,
    read_frame,
    write_frame,
)


MAX_CONNECTIONS = 64
AUTH_TIMEOUT_SECONDS = 10
CHANNEL_DELIVERY_TIMEOUT_SECONDS = 15
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_NONSECRET_ENVIRONMENT = frozenset({
    "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "HOMEDRIVE",
    "HOMEPATH", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONUTF8",
    "PYTHONIOENCODING",
})
_RPC_ARGUMENTS = {
    "submit": frozenset({"project_id", "prompt", "client_request_id", "relative_paths", "timeout_seconds", "mode", "completion_origin"}),
    "status": frozenset({"job_id"}),
    "result": frozenset({"job_id", "cursor", "page_size", "view"}),
    "service_info": frozenset(),
    "preflight": frozenset({"project_id", "relative_paths", "mode"}),
    "cancel": frozenset({"job_id"}),
    "session_open": frozenset({"project_id", "client_request_id", "title", "mode", "relative_paths"}),
    "session_send": frozenset({"session_id", "prompt", "client_request_id", "timeout_seconds", "completion_origin"}),
    "session_list": frozenset({"project_id", "limit"}),
    "completion_watch": frozenset({"job_ids", "origin"}),
    "completion_list": frozenset({"origin", "state", "limit"}),
    "completion_ack": frozenset({"event_ids", "origin"}),
}


def _valid_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_256.fullmatch(value) is not None


def _protocol_error() -> HarnessError:
    return HarnessError("SHARED_PROTOCOL_ERROR", "本机共享连接报文不符合协议。")


def _check_nonsecret_environment(environment: Any, credential_env: str) -> dict:
    if not isinstance(environment, dict) or not set(environment) <= _NONSECRET_ENVIRONMENT or credential_env in environment or any(not isinstance(value, str) or "\x00" in value for value in environment.values()):
        raise _protocol_error()
    return environment


class _SharedService:
    def __init__(self, config: AppConfig, manager, token: str, config_digest: str, idle_seconds: float):
        self.config = config
        self.manager = manager
        self.token = token
        self.config_digest = config_digest
        self.idle_seconds = idle_seconds
        self.connections = 0
        self.sessions = 0
        self.tasks: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.last_activity = asyncio.get_running_loop().time()
        self.stopping = False
        self.credential: str | None = None
        self.completion_dispatcher = None
        self.channels = {}
        self._channel_registrations = {}
        self._writer_locks = {}
        self._completion_receipts = {}

    async def _write(self, writer, payload, *, max_bytes=RPC_MAX_BYTES):
        lock = self._writer_locks.setdefault(writer, asyncio.Lock())
        async with lock:
            await write_frame(writer, payload, max_bytes=max_bytes)

    async def send_completion(self, origin, prompt, call_id):
        from .codex_app_bridge import DeliveryError, send_completion
        if origin.get("client") != "claude_code":
            return await send_completion(origin, prompt, call_id)
        origin = channel_origin(origin)
        route = self.channels.get(origin["connection_id"])
        if route is None or route[0] != origin or route[1].is_closing():
            raise DeliveryError("CHANNEL_DISCONNECTED", "原 Claude 通道已断开；不会转发到另一个会话。")
        writer = route[1]
        delivery_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._completion_receipts[delivery_id] = (writer, future)
        written = False
        try:
            async def exchange():
                nonlocal written
                # 写锁、实际写出、回执共用时间边界，某个失联窗口不能阻塞其他投递。
                lock = self._writer_locks.setdefault(writer, asyncio.Lock())
                async with lock:
                    if writer.is_closing() or self.channels.get(origin["connection_id"]) != route:
                        raise DeliveryError("CHANNEL_DISCONNECTED", "原 Claude 通道已断开。")
                    written = True
                    await write_frame(writer, {"type": "completion_notification", "delivery_id": delivery_id,
                        "origin": origin, "prompt": prompt, "call_id": call_id}, max_bytes=RPC_MAX_BYTES)
                return await future
            accepted = await asyncio.wait_for(exchange(), CHANNEL_DELIVERY_TIMEOUT_SECONDS)
            if accepted is not True:
                raise DeliveryError("CHANNEL_WRITE_UNCONFIRMED", "无法确认 Claude channel 消息写出。", uncertain=True)
            return {"status": "channel_written", "client": "claude_code", "call_id": call_id}
        except DeliveryError:
            raise
        except (OSError, ConnectionError, asyncio.TimeoutError):
            raise DeliveryError("CHANNEL_DELIVERY_UNKNOWN", "无法确认 Claude channel 消息写出。", uncertain=written) from None
        finally:
            self._completion_receipts.pop(delivery_id, None)
            if not future.done():
                future.cancel()

    def _safe_error(self, error: Exception, *, stage="shared_rpc") -> dict:
        # IPC 令牌和模型凭证只留在受信内存；任何错误都不得回传原始异常。
        values = [self.token, *getattr(self.manager, "secrets", ())]
        if self.credential:
            values.append(self.credential)
        return redact_value(error_payload(error, stage=stage), values)

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connections >= MAX_CONNECTIONS or self.stopping:
            writer.close()
            return
        self.connections += 1
        self.writers.add(writer)
        task = asyncio.create_task(self._connection(reader, writer), name="harness-local-session")
        self.tasks.add(task)

        def finished(value: asyncio.Task) -> None:
            self.tasks.discard(value)
            if not value.cancelled():
                # 取走所有会话异常，防止事件循环打印可能包含敏感信息的堆栈。
                with suppress(BaseException):
                    value.exception()

        task.add_done_callback(finished)

    async def _authenticate(self, reader, writer) -> None:
        challenge = secrets.token_hex(32)
        await write_frame(writer, {"version": 1, "challenge": challenge, "config_digest": self.config_digest}, max_bytes=AUTH_MAX_BYTES)
        response = await read_frame(reader, max_bytes=AUTH_MAX_BYTES)
        if not isinstance(response, dict) or set(response) != {"version", "nonce", "config_digest", "proof"}:
            raise _protocol_error()
        if response["version"] != 1 or not _valid_hex(response["nonce"]) or not _valid_hex(response["proof"]):
            raise _protocol_error()
        if response["config_digest"] != self.config_digest:
            raise HarnessError("SHARED_CONFIG_MISMATCH", "共享后台的配置与本次接入配置不同，请先完成原实例任务。")
        expected = authentication_proof(self.token, "client", challenge, response["nonce"], self.config_digest)
        if not hmac.compare_digest(response["proof"], expected):
            raise HarnessError("SHARED_AUTH_FAILED", "本机共享连接认证失败。")
        await write_frame(writer, {
            "version": 1,
            "proof": authentication_proof(self.token, "server", challenge, response["nonce"], self.config_digest),
        }, max_bytes=AUTH_MAX_BYTES)
        # 只有相互证明本机后台身份后，前端才会发送可选模型环境凭证。
        attachment = await read_frame(reader, max_bytes=AUTH_MAX_BYTES)
        if not isinstance(attachment, dict) or set(attachment) not in ({"version", "model_environment"}, {"version", "model_environment", "channel"}) or attachment["version"] != 1:
            raise _protocol_error()
        origin = None
        if "channel" in attachment:
            if not self.config.completion.auto_continue:
                raise HarnessError("AUTO_CONTINUE_NOT_AUTHORIZED", "可信配置没有授权通道完成通知。")
            origin = channel_origin(attachment["channel"])
            if origin["connection_id"] in self._channel_registrations:
                raise HarnessError("CHANNEL_CONNECTION_CONFLICT", "原通道连接仍存在，不能替换。")
            # 在任何后续 await 之前占用身份，两个同时认证的 writer 不能覆盖彼此。
            self._channel_registrations[origin["connection_id"]] = writer
        environment = attachment["model_environment"]
        if not isinstance(environment, dict):
            raise _protocol_error()
        allowed = self.config.model.credential_env if self.config.model.credential_source == "env" else None
        if environment and (allowed is None or set(environment) != {allowed}):
            raise HarnessError("SHARED_ENV_NOT_ALLOWED", "共享前端只能提供可信配置指定的模型环境凭证。")
        if environment:
            value = environment[allowed]
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                raise _protocol_error()
            # 生命周期内固定首个有效凭证，防止另一个前端改变进行中任务的身份。
            previous = self.credential if self.credential is not None else os.environ.get(allowed)
            if previous is not None and not hmac.compare_digest(previous, value):
                raise HarnessError("SHARED_CREDENTIAL_MISMATCH", "共享后台已经使用另一模型凭证；本次连接未获接受。")
            self.credential = value
            self.manager._register_secret(value)
            os.environ[allowed] = value
        if self.stopping:
            raise HarnessError("SHARED_SERVICE_STOPPING", "共享后台正在确认停止，不接受新的连接。")
        await write_frame(writer, {"version": 1, "ready": True}, max_bytes=AUTH_MAX_BYTES)
        if origin is not None:
            self.channels[origin["connection_id"]] = (origin, writer)
        return origin

    async def _dispatch(self, request: dict, channel=None) -> dict:
        if not isinstance(request, dict) or set(request) != {"id", "method", "args"}:
            raise _protocol_error()
        request_id = request["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or (isinstance(request_id, str) and len(request_id) > 128):
            raise _protocol_error()
        method, arguments = request["method"], request["args"]
        if not isinstance(method, str) or method not in _RPC_ARGUMENTS or not isinstance(arguments, dict):
            raise _protocol_error()
        if not set(arguments) <= _RPC_ARGUMENTS[method]:
            raise _protocol_error()
        try:
            # 接口参数没有授予路由权限：Claude 身份必须等于该认证连接已登记的身份。
            origin = arguments.get("completion_origin", arguments.get("origin"))
            if origin is not None and (channel is not None or (isinstance(origin, dict) and origin.get("client") == "claude_code")):
                if origin != channel:
                    raise HarnessError("CHANNEL_TARGET_MISMATCH", "完成事件只能属于已认证的原通道连接。")
            result = getattr(self.manager, method)(**arguments)
            if inspect.isawaitable(result):
                result = await result
            if method == "service_info":
                result = {**result, "active_connections": self.sessions,
                          "pending_connections": max(0, self.connections - self.sessions),
                          "connection_mode": "shared", "runtime_version": self.config_digest}
            # 会话标题等成功字段也可能回显用户输入，统一应用精确凭证保护。
            protected = [self.token, *getattr(self.manager, "secrets", ())]
            if self.credential:
                protected.append(self.credential)
            if method == "result":
                # 仅真实 result 的进程内来源标记可保留源码；模型 JSON 同名字段无效。
                from .redaction import redact_verified_source_payload
                result = redact_verified_source_payload(result, protected)
            else:
                result = redact_value(result, protected)
            return {"id": request_id, "ok": True, "result": result}
        except Exception as error:
            return {"id": request_id, "ok": False, "error": self._safe_error(error)}

    async def _connection(self, reader, writer) -> None:
        attached = False
        origin = None
        try:
            origin = await asyncio.wait_for(self._authenticate(reader, writer), AUTH_TIMEOUT_SECONDS)
            self.sessions += 1
            attached = True
            self.last_activity = asyncio.get_running_loop().time()
            while True:
                request = await read_frame(reader, max_bytes=RPC_MAX_BYTES)
                if request.get("type") == "completion_receipt":
                    if (set(request) != {"type", "delivery_id", "accepted"} or not isinstance(request["delivery_id"], str)
                            or not re.fullmatch(r"[0-9a-f]{32}", request["delivery_id"]) or type(request["accepted"]) is not bool):
                        raise _protocol_error()
                    pending = self._completion_receipts.get(request["delivery_id"])
                    if pending is not None:
                        if pending[0] is not writer:
                            raise HarnessError("CHANNEL_TARGET_MISMATCH", "完成回执不属于原通道连接。")
                        if not pending[1].done():
                            pending[1].set_result(request["accepted"])
                    continue
                response = await self._dispatch(request, channel=origin)
                await self._write(writer, response)
                self.last_activity = asyncio.get_running_loop().time()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as error:
            with suppress(Exception):
                await self._write(writer, {"version": 1, "ready": False, "error": self._safe_error(error, stage="shared_rpc" if attached else "shared_attach")}, max_bytes=AUTH_MAX_BYTES)
        finally:
            if origin is not None and self.channels.get(origin["connection_id"], (None, None))[1] is writer:
                self.channels.pop(origin["connection_id"], None)
            for connection_id, registered_writer in tuple(self._channel_registrations.items()):
                if registered_writer is writer:
                    self._channel_registrations.pop(connection_id, None)
            for receipt_writer, future in tuple(self._completion_receipts.values()):
                if receipt_writer is writer and not future.done():
                    future.set_exception(ConnectionError("channel disconnected"))
            if attached:
                self.sessions -= 1
            self.connections -= 1
            self.writers.discard(writer)
            self.last_activity = asyncio.get_running_loop().time()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._writer_locks.pop(writer, None)

    async def wait_until_idle(self) -> None:
        poll = min(0.25, max(0.01, self.idle_seconds / 4))
        while True:
            await asyncio.sleep(poll)
            if self.stopping:
                if self.completion_dispatcher is not None:
                    await self.completion_dispatcher.close()
                    self.completion_dispatcher = None
                await self.manager.close()
                if self.manager.closed:
                    return
                await asyncio.sleep(1)
                continue
            if self.connections:
                continue
            if self.manager.store.has_pending():
                self.last_activity = asyncio.get_running_loop().time()
                continue
            if self.completion_dispatcher is not None and self.completion_dispatcher.has_pending():
                self.last_activity = asyncio.get_running_loop().time()
                continue
            if asyncio.get_running_loop().time() - self.last_activity < self.idle_seconds:
                continue
            self.stopping = True
            if self.completion_dispatcher is not None:
                await self.completion_dispatcher.close()
                self.completion_dispatcher = None
            await self.manager.close()
            if self.manager.closed:
                return
            # 停止不确定时保留 TaskManager、实例锁和归属；不开放新的模型任务。
            await asyncio.sleep(1)


async def serve(
    config: AppConfig,
    runtime_dir: Path,
    token: str,
    config_digest: str,
    idle_seconds: float = 30,
    manager_factory=None,
    nonsecret_environment: dict | None = None,
    config_path: Path | None = None,
) -> None:
    """启动一个独立后台；前端断开不触发管理器关闭或任务取消。"""
    from .shared_bootstrap import configuration_digest, prepare_private_runtime, write_private_json

    if manager_factory is None:
        from .conversation import SessionManager
        manager_factory = SessionManager

    if not _valid_hex(token) or not _valid_hex(config_digest):
        raise _protocol_error()
    if config_path is not None:
        config_path = Path(config_path).resolve()
        # 正式后台归属绑定原配置文件；无路径只用于已有直接注入的测试/嵌入调用。
        if configuration_digest(config, config_path) != config_digest:
            raise HarnessError("SHARED_CONFIG_MISMATCH", "共享后台启动配置来源或运行设置与已授权配置不同。")
    if isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float)) or not math.isfinite(idle_seconds) or not 0.01 <= idle_seconds <= 3600:
        raise _protocol_error()
    if nonsecret_environment is None:
        nonsecret_environment = {}
    _check_nonsecret_environment(nonsecret_environment, config.model.credential_env)
    runtime_dir = Path(runtime_dir).absolute()
    prepare_private_runtime(runtime_dir)
    runtime_dir = runtime_dir.resolve()
    original_environment = {name: os.environ.get(name) for name in {*nonsecret_environment, config.model.credential_env}}
    os.environ.update(nonsecret_environment)
    manager = None
    dispatcher = None
    server = None
    state = None
    try:
        manager = manager_factory(config)
        manager.config_path = config_path
        await manager.start()
        state = _SharedService(config, manager, token, config_digest, float(idle_seconds))
        if config.completion.auto_continue:
            from .completion import CompletionDispatcher
            dispatcher = CompletionDispatcher(manager, sender=state.send_completion)
            await dispatcher.start()
        state.completion_dispatcher = dispatcher
        server = await asyncio.start_server(state.accept, host="127.0.0.1", port=0, backlog=MAX_CONNECTIONS)
        port = server.sockets[0].getsockname()[1]
        write_private_json(runtime_dir / "registry.json", {
            "version": 1, "pid": os.getpid(), "host": "127.0.0.1", "port": port,
            "token": token, "config_digest": config_digest,
            "service_version": __version__, "data_dir": str(config.data_dir.resolve()),
        })
        state.last_activity = asyncio.get_running_loop().time()
        await state.wait_until_idle()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        if state is not None:
            for writer in tuple(state.writers):
                writer.close()
            for task in tuple(state.tasks):
                task.cancel()
            await asyncio.gather(*state.tasks, return_exceptions=True)
        if dispatcher is not None:
            await dispatcher.close()
        if manager is not None and not manager.closed:
            await manager.close()
        for name, value in original_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main() -> None:
    from .shared_bootstrap import configuration_digest, prepare_private_runtime, read_private_json, write_private_json

    parser = argparse.ArgumentParser(description="DeepSeek Harness MCP 受保护本机共享后台")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    args = parser.parse_args()
    bootstrap = None
    try:
        if not args.config.is_absolute() or not args.bootstrap.is_absolute():
            raise _protocol_error()
        prepare_private_runtime(args.bootstrap.parent)
        bootstrap = read_private_json(args.bootstrap)
        if bootstrap.get("version") != 1 or Path(bootstrap.get("config_path", "")).resolve() != args.config.resolve():
            raise _protocol_error()
        startup_id = bootstrap.get("startup_id")
        if not isinstance(startup_id, str) or re.fullmatch(r"[0-9a-f]{32}", startup_id) is None:
            raise _protocol_error()
        # WMI 后台不保证继承前端环境；先恢复白名单 home/path，再解析含 ~ 的配置。
        # 读取已受信配置的凭证变量名，保证它永不进入磁盘上的 bootstrap 环境。
        with args.config.open("rb") as stream:
            raw_config = tomllib.load(stream)
        credential_env = raw_config.get("model", {}).get("credential_env", "DEEPSEEK_API_KEY")
        environment = _check_nonsecret_environment(bootstrap.get("nonsecret_environment", {}), credential_env)
        os.environ.update(environment)
        config = load_config(args.config.resolve())
        digest = configuration_digest(config, args.config.resolve())
        if bootstrap.get("config_digest") != digest:
            raise HarnessError("SHARED_CONFIG_MISMATCH", "共享后台启动配置与已授权配置不同。")
        asyncio.run(serve(config, args.bootstrap.parent, bootstrap["token"], digest,
                          idle_seconds=bootstrap.get("idle_seconds", 30),
                          nonsecret_environment=bootstrap.get("nonsecret_environment", {}),
                          config_path=args.config.resolve()))
    except Exception as error:
        payload = error_payload(error, stage="startup")
        known = {"INSTANCE_LOCKED", "INVALID_CONFIG", "SHARED_CONFIG_MISMATCH", "SHARED_PROTOCOL_ERROR",
                 "SHARED_PRIVATE_PATH_UNSAFE", "SHARED_RUNTIME_UNSAFE", "DESKTOP_OS_UNSUPPORTED",
                 "SHARED_STORAGE_UNSAFE", "DESKTOP_RUNTIME_UNAVAILABLE", "SDK_VERSION_UNSUPPORTED"}
        code = payload["code"] if payload["code"] in known else "INTERNAL_ERROR"
        message = "共享后台启动失败，请查看本机环境诊断。"
        if isinstance(bootstrap, dict) and _valid_hex(bootstrap.get("config_digest")) and isinstance(bootstrap.get("startup_id"), str) and re.fullmatch(r"[0-9a-f]{32}", bootstrap["startup_id"]) is not None:
            with suppress(Exception):
                write_private_json(args.bootstrap.parent / "startup-error.json", {
                    "version": 1, "pid": os.getpid(), "parent_pid": os.getppid(),
                    "startup_id": bootstrap["startup_id"], "config_digest": bootstrap["config_digest"],
                    "code": code, "message": message,
                    **{key: payload[key] for key in ("diagnostic_id", "stage", "exception_type")},
                })
        print(f"{code}: {message} [diagnostic_id={payload['diagnostic_id']}, stage={payload['stage']}, exception_type={payload['exception_type']}]", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
