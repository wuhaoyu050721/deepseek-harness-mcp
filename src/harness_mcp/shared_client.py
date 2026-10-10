"""stdio 会话的本机共享代理；断开客户端不会取消其他会话的任务。"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
from pathlib import Path

from .config import AppConfig
from .errors import HarnessError

AUTH_MAX_BYTES = 8192
RPC_MAX_BYTES = 16 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _json_constant(value):
    raise ValueError("non-finite JSON value")


async def read_frame(reader, *, max_bytes=AUTH_MAX_BYTES):
    """先校验长度再分配消息内存，拒绝无界或非对象 JSON。"""
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < length <= max_bytes:
        raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务消息长度无效。")
    try:
        value = json.loads(await reader.readexactly(length), object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (ValueError, UnicodeError):
        raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务消息格式无效。") from None
    if not isinstance(value, dict):
        raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务消息格式无效。")
    return value


async def write_frame(writer, payload, *, max_bytes=AUTH_MAX_BYTES):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(raw) <= max_bytes:
        raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务消息长度无效。")
    writer.write(struct.pack("!I", len(raw)) + raw)
    await writer.drain()


def authentication_proof(token, role, challenge, nonce, config_digest):
    if role not in {"client", "server"} or any(not isinstance(v, str) or not _HEX.fullmatch(v) for v in (token, challenge, nonce, config_digest)):
        raise HarnessError("SHARED_AUTH_FAILED", "共享服务身份验证失败。")
    message = json.dumps(["deepseek-harness-mcp", 1, role, challenge, nonce, config_digest], separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(bytes.fromhex(token), message, hashlib.sha256).hexdigest()


async def open_authenticated(registry):
    """验证双方角色和本次随机挑战后才允许传递模型凭证。"""
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", registry["port"]), 2)
        hello = await asyncio.wait_for(read_frame(reader), 2)
        if set(hello) != {"version", "challenge", "config_digest"} or hello.get("version") != 1 or hello.get("config_digest") != registry["config_digest"]:
            raise HarnessError("SHARED_AUTH_FAILED", "共享服务身份验证失败。")
        nonce = secrets.token_hex(32)
        proof = authentication_proof(registry["token"], "client", hello["challenge"], nonce, registry["config_digest"])
        await write_frame(writer, {"version": 1, "nonce": nonce, "config_digest": registry["config_digest"], "proof": proof})
        reply = await asyncio.wait_for(read_frame(reader), 2)
        expected = authentication_proof(registry["token"], "server", hello["challenge"], nonce, registry["config_digest"])
        if set(reply) != {"version", "proof"} or reply.get("version") != 1 or not isinstance(reply.get("proof"), str) or not hmac.compare_digest(reply["proof"], expected):
            raise HarnessError("SHARED_AUTH_FAILED", "共享服务身份验证失败。")
        return reader, writer
    except BaseException:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        raise


class SharedClient:
    def __init__(self, config: AppConfig, config_path: Path):
        self.config = config
        self.config_path = Path(config_path).absolute()
        self.secrets = []
        self.reader = self.writer = None
        self._request_lock = asyncio.Lock()
        self._sequence = 0

    async def start(self):
        from .shared_bootstrap import connect_or_start
        if self.writer is not None:
            return
        reader, writer, _ = await connect_or_start(self.config, self.config_path)
        self.reader, self.writer = reader, writer
        try:
            model_environment = {}
            if self.config.model.credential_source == "env":
                value = os.environ.get(self.config.model.credential_env, "")
                if value:
                    if "\x00" in value or len(value) > 4096:
                        raise HarnessError("CREDENTIAL_UNAVAILABLE", "所配置的模型凭证引用无效。")
                    self.secrets.append(value)
                    model_environment[self.config.model.credential_env] = value
            await write_frame(writer, {"version": 1, "model_environment": model_environment})
            ready = await asyncio.wait_for(read_frame(reader), 20)
            if ready.get("ready") is not True or ready.get("version") != 1:
                error = ready.get("error", {})
                code = error.get("code", "SHARED_BACKEND_UNAVAILABLE") if isinstance(error, dict) else "SHARED_BACKEND_UNAVAILABLE"
                if code == "SHARED_CREDENTIAL_MISMATCH":
                    raise HarnessError(code, "现有共享服务使用不同的模型凭证，请等待服务空闲退出后重新连接。")
                raise HarnessError("SHARED_BACKEND_UNAVAILABLE", "共享服务未完成初始化。")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        writer, self.writer = self.writer, None
        self.reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _call(self, method, args):
        async with self._request_lock:
            if self.writer is None or self.reader is None:
                raise HarnessError("SHARED_CONNECTION_CLOSED", "共享服务连接已关闭，请重新连接后查询原任务。")
            self._sequence += 1
            request_id = self._sequence
            try:
                await write_frame(self.writer, {"id": request_id, "method": method, "args": args}, max_bytes=RPC_MAX_BYTES)
                reply = await asyncio.wait_for(read_frame(self.reader, max_bytes=RPC_MAX_BYTES), 60)
                if reply.get("id") != request_id or not isinstance(reply.get("ok"), bool):
                    raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务响应格式无效。")
                if reply["ok"]:
                    if not isinstance(reply.get("result"), dict):
                        raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务响应格式无效。")
                    return reply["result"]
                error = reply.get("error")
                if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", error["code"]) or not isinstance(error.get("message"), str):
                    raise HarnessError("SHARED_PROTOCOL_INVALID", "共享服务响应格式无效。")
                raise HarnessError(error["code"], error["message"])
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                await self.close()
                raise HarnessError("SHARED_CONNECTION_LOST", "共享服务连接中断，操作结果尚不明确；重新连接后用原请求编号或作业编号查询，不能重复创建任务。") from None

    async def submit(self, project_id, prompt, client_request_id, relative_paths=None, timeout_seconds=None, mode="review"):
        return await self._call("submit", dict(project_id=project_id, prompt=prompt, client_request_id=client_request_id, relative_paths=relative_paths, timeout_seconds=timeout_seconds, mode=mode))

    async def status(self, job_id):
        return await self._call("status", {"job_id": job_id})

    async def result(self, job_id, cursor=None, page_size=20):
        return await self._call("result", dict(job_id=job_id, cursor=cursor, page_size=page_size))

    async def cancel(self, job_id):
        return await self._call("cancel", {"job_id": job_id})

    async def session_open(self, project_id, client_request_id, title="", mode="review", relative_paths=None):
        return await self._call("session_open", dict(project_id=project_id, client_request_id=client_request_id, title=title, mode=mode, relative_paths=relative_paths))

    async def session_send(self, session_id, prompt, client_request_id, timeout_seconds=None):
        return await self._call("session_send", dict(session_id=session_id, prompt=prompt, client_request_id=client_request_id, timeout_seconds=timeout_seconds))

    async def session_list(self, project_id=None, limit=20):
        return await self._call("session_list", dict(project_id=project_id, limit=limit))
