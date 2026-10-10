"""通过桌面端已暴露的 app-tools 目录投递完成消息，不调用隐藏宿主方法。"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import math
import os
import re
import struct
import threading
import time
import uuid

from .errors import HarnessError


MAX_FRAME_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
PIPE_PREFIX = "\\\\.\\pipe\\"
PROMPT_PREFIX = "DeepSeek Harness 自动完成通知。"
_retired_io = []
_retired_lock = threading.Lock()


class DeliveryError(HarnessError):
    """写出消息后无法确认接收时，调用方必须保留 uncertain，不能盲目重发。"""
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(code, message)
        self.uncertain = uncertain


def _invalid_json(*_args):
    raise ValueError("invalid JSON")


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _decode(data: bytes):
    try:
        return json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                          parse_constant=_invalid_json, parse_float=_float)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主响应格式无效。") from None


def _encode(value):
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise DeliveryError("COMPLETION_ARGUMENT_INVALID", "完成通知参数无法编码。") from None
    if not data or len(data) > MAX_FRAME_BYTES:
        raise DeliveryError("COMPLETION_ARGUMENT_INVALID", "完成通知超过宿主消息大小限制。")
    return struct.pack("<I", len(data)) + data


def _origin(origin):
    if not isinstance(origin, dict) or set(origin) != {"thread_id", "turn_id", "host_id", "pipe_path"}:
        raise DeliveryError("COMPLETION_ORIGIN_INVALID", "缺少可信的原对话归属。")
    try:
        valid_id = isinstance(origin["thread_id"], str) and str(uuid.UUID(origin["thread_id"])) == origin["thread_id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    pipe = origin["pipe_path"]
    if (not valid_id or origin["host_id"] != "local"
            or not isinstance(origin["turn_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", origin["turn_id"])
            or not isinstance(pipe, str) or not pipe.casefold().startswith(PIPE_PREFIX)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", pipe[len(PIPE_PREFIX):])):
        raise DeliveryError("COMPLETION_ORIGIN_INVALID", "完成通知只支持可信本机原对话。")
    return dict(origin)


def _tool(catalog, name):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主工具目录无效。")
    selected = [item for item in catalog["tools"] if isinstance(item, dict) and item.get("name") == name]
    if len(selected) != 1:
        raise DeliveryError("COMPLETION_HOST_CAPABILITY_UNAVAILABLE", "Codex 宿主没有提供所需对话工具。")
    value = selected[0]
    schema = value.get("inputSchema")
    # 只调用实际目录中的既有工具，并核对发送参数仍属于真实接口。
    required = {"threadId"} if name == "read_thread" else {"threadId", "prompt"}
    if (value.get("namespace") != "codex_app" or not isinstance(schema, dict)
            or schema.get("type", "object") != "object"
            or not isinstance(schema.get("properties"), dict)
            or not required.issubset(schema["properties"])
            or not isinstance(schema.get("required"), list)
            or not all(isinstance(key, str) for key in schema["required"])
            or not required.issubset(schema["required"])
            or not all(isinstance(schema["properties"][key], dict)
                       and schema["properties"][key].get("type") == "string" for key in required)
            or not set(schema["required"]).issubset({"threadId", "hostId", "prompt", "turnLimit", "includeOutputs", "maxOutputCharsPerItem"})):
        raise DeliveryError("COMPLETION_HOST_CAPABILITY_UNAVAILABLE", "Codex 宿主对话接口与当前契约不兼容。")
    return value


def _content(result, *, uncertain=False):
    if not isinstance(result, dict) or result.get("success") is not True:
        raise DeliveryError("COMPLETION_HOST_REJECTED", "Codex 宿主没有确认对话操作成功。", uncertain=uncertain)
    items = result.get("contentItems")
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get("type") != "inputText" or not isinstance(items[0].get("text"), str)):
        raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主缺少可核对的对话回执。", uncertain=uncertain)
    try:
        parsed = _decode(items[0]["text"].encode("utf-8"))
    except (DeliveryError, UnicodeError):
        raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主对话回执无效。", uncertain=uncertain) from None
    if not isinstance(parsed, dict):
        raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主对话回执无效。", uncertain=uncertain)
    return parsed


def _call(origin, tool, arguments, call_id):
    return {"arguments": arguments, "callerSource": "codex", "callId": call_id,
            "hostId": "local", "namespace": tool["namespace"], "threadId": origin["thread_id"],
            "tool": tool["name"], "turnId": origin["turn_id"]}


def _inspect(pipe, origin, catalog):
    tool = _tool(catalog, "read_thread")
    props = tool["inputSchema"]["properties"]
    arguments = {"threadId": origin["thread_id"]}
    if "hostId" in props:
        arguments["hostId"] = "local"
    for name, value in (("turnLimit", 1), ("includeOutputs", False), ("maxOutputCharsPerItem", 0)):
        if name in props:
            arguments[name] = value
    result = _content(pipe.request("tools/call", _call(origin, tool, arguments, "harness-inspect-" + uuid.uuid4().hex)))
    thread = result.get("thread")
    if (not isinstance(thread, dict) or thread.get("id") != origin["thread_id"]
            or thread.get("kind") != "codex" or thread.get("hostId") != "local"):
        raise DeliveryError("COMPLETION_ORIGIN_MISMATCH", "Codex 宿主未确认原本机对话归属。")
    return {"thread_id": thread["id"], "host_id": "local", "kind": "codex",
            "cwd": thread.get("cwd"), "status": thread.get("status")}


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class _WindowsPipe:
    """每次调用拥有自己的管道；每次读写均使用可取消的 overlapped I/O。"""
    def __init__(self, path, cancel, timeout=REQUEST_TIMEOUT_SECONDS):
        self.path = path
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout
        self.handle = None
        self.next_id = 0
        self.write_started = False
        if os.name != "nt":
            raise DeliveryError("COMPLETION_PLATFORM_UNAVAILABLE", "自动续跑宿主桥当前只支持 Windows。")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.api
        signatures = {
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "CreateEventW": ([wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "ReadFile": ([wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WriteFile": ([wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CancelIoEx": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "GetOverlappedResult": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            "WaitNamedPipeW": ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        # 极少数宿主故障可能延迟取消完成；保留仍被内核使用的缓冲区，后续连接回收。
        with _retired_lock:
            for entry in list(_retired_io):
                if api.WaitForSingleObject(entry[0], 0) == 0:
                    api.CloseHandle(entry[0])
                    _retired_io.remove(entry)

    def __enter__(self):
        while True:
            if self.cancel.is_set() or time.monotonic() >= self.deadline:
                raise DeliveryError("COMPLETION_HOST_UNAVAILABLE", "Codex 宿主通信连接超时。")
            # SECURITY_IDENTIFICATION 限制服务器模拟客户端；只连接已传入的本机管道。
            handle = self.api.CreateFileW(self.path, 0xC0000000, 0, None, 3, 0x40110000, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                return self
            if ctypes.get_last_error() != 231:
                raise DeliveryError("COMPLETION_HOST_UNAVAILABLE", "Codex 宿主通信连接不可用。")
            self.api.WaitNamedPipeW(self.path, 100)

    def __exit__(self, *_args):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def _transfer(self, buffer, size, *, writing=False):
        if self.cancel.is_set() or time.monotonic() >= self.deadline:
            raise DeliveryError("COMPLETION_HOST_TIMEOUT", "Codex 宿主通信已取消或超时。")
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise DeliveryError("COMPLETION_HOST_UNAVAILABLE", "无法建立 Codex 宿主通信等待。")
        overlapped = _Overlapped(hEvent=event)
        transferred = wintypes.DWORD()
        pending = False
        try:
            function = self.api.WriteFile if writing else self.api.ReadFile
            if writing:
                self.write_started = True
            result = function(self.handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            if not result:
                if ctypes.get_last_error() != 997:
                    raise DeliveryError("COMPLETION_HOST_DISCONNECTED", "Codex 宿主通信已关闭。")
                pending = True
                while True:
                    remaining = self.deadline - time.monotonic()
                    if self.cancel.is_set() or remaining <= 0:
                        raise DeliveryError("COMPLETION_HOST_TIMEOUT", "Codex 宿主通信响应超时。")
                    wait = self.api.WaitForSingleObject(event, min(100, max(1, math.ceil(remaining * 1000))))
                    if wait == 0:
                        break
                    if wait != 258:
                        raise DeliveryError("COMPLETION_HOST_DISCONNECTED", "Codex 宿主通信等待失败。")
                pending = False
                if not self.api.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
                    raise DeliveryError("COMPLETION_HOST_DISCONNECTED", "Codex 宿主通信已关闭。")
            if transferred.value == 0:
                raise DeliveryError("COMPLETION_HOST_DISCONNECTED", "Codex 宿主通信已关闭。")
            return transferred.value
        finally:
            if pending:
                # 取消后等待本地管道 I/O 的取消完成，缓冲区仍在作用域内；没有永久阻塞读线程。
                self.api.CancelIoEx(self.handle, ctypes.byref(overlapped))
                if self.api.WaitForSingleObject(event, 1000) != 0:
                    # 不无限等待取消，也不释放仍被内核使用的 OVERLAPPED/缓冲区。
                    with _retired_lock:
                        _retired_io.append((event, overlapped, buffer))
                    event = None
            if event is not None:
                self.api.CloseHandle(event)

    def _read(self, size):
        result = bytearray()
        while len(result) < size:
            buffer = ctypes.create_string_buffer(size - len(result))
            count = self._transfer(buffer, len(buffer))
            result.extend(buffer.raw[:count])
        return bytes(result)

    def request(self, method, params, *, mutating=False):
        if self.cancel.is_set() or time.monotonic() >= self.deadline:
            raise DeliveryError("COMPLETION_HOST_TIMEOUT", "Codex 宿主通信已取消或超时。")
        self.next_id += 1
        request_id = self.next_id
        frame = _encode({"id": request_id, "jsonrpc": "2.0", "method": method, "params": params})
        self.write_started = False
        try:
            offset = 0
            while offset < len(frame):
                buffer = ctypes.create_string_buffer(frame[offset:], len(frame) - offset)
                offset += self._transfer(buffer, len(buffer), writing=True)
            # 宿主可以广播通知；只读取本请求对应的回执，整个连接有单一时间边界。
            while True:
                size = struct.unpack("<I", self._read(4))[0]
                if not 1 <= size <= MAX_FRAME_BYTES:
                    raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主通信帧大小无效。")
                response = _decode(self._read(size))
                if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                    raise DeliveryError("COMPLETION_HOST_PROTOCOL_INVALID", "Codex 宿主通信回执无效。")
                if type(response.get("id")) is not int or response["id"] != request_id:
                    continue
                if "error" in response or "result" not in response:
                    raise DeliveryError("COMPLETION_HOST_REJECTED", "Codex 宿主没有确认对话请求成功。")
                return response["result"]
        except DeliveryError as error:
            error.uncertain = error.uncertain or (mutating and self.write_started)
            raise
        except Exception:
            raise DeliveryError("COMPLETION_DELIVERY_UNKNOWN", "无法确认 Codex 是否接收完成消息。",
                                uncertain=mutating and self.write_started) from None


def _inspect_sync(origin, timeout_seconds, cancel):
    with _WindowsPipe(origin["pipe_path"], cancel, timeout=timeout_seconds) as pipe:
        catalog = pipe.request("tools/list", {"threadStartKind": "all"})
        _tool(catalog, "send_message_to_thread")
        return _inspect(pipe, origin, catalog)


def _send_sync(origin, prompt, call_id, cancel):
    with _WindowsPipe(origin["pipe_path"], cancel) as pipe:
        catalog = pipe.request("tools/list", {"threadStartKind": "all"})
        tool = _tool(catalog, "send_message_to_thread")
        _inspect(pipe, origin, catalog)
        arguments = {"threadId": origin["thread_id"], "prompt": prompt}
        if "hostId" in tool["inputSchema"]["properties"]:
            arguments["hostId"] = "local"
        result = _content(pipe.request("tools/call", _call(origin, tool, arguments, call_id), mutating=True), uncertain=True)
        # 当前正式工具在 sendFollowUpMessage 被接收后返回 {threadId}；并无 status 字段。
        if (result.get("threadId") != origin["thread_id"]
                or set(result) - {"threadId", "status"}
                or not isinstance(result.get("status", "accepted"), str)
                or result.get("status", "accepted") not in {"accepted", "sent", "queued"}):
            raise DeliveryError("COMPLETION_RECEIPT_INVALID", "Codex 未返回原对话的明确消息接收回执。", uncertain=True)
        return {"status": "accepted", "thread_id": origin["thread_id"], "call_id": call_id}


async def _worker(function, *args):
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(function, *args, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise


async def inspect_origin(origin, *, timeout_seconds=5.0):
    """只读确认当前宿主提供续跑工具，且目标仍是原本机 Codex 对话。"""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= REQUEST_TIMEOUT_SECONDS:
        raise DeliveryError("COMPLETION_ARGUMENT_INVALID", "宿主只读检查的超时范围无效。")
    return await _worker(_inspect_sync, _origin(origin), timeout_seconds)


async def send_completion(origin, prompt, call_id):
    """只允许向 origin 自身投递固定完成模板，省略模型与权限覆盖参数。"""
    origin = _origin(origin)
    if (not isinstance(prompt, str) or not prompt.startswith(PROMPT_PREFIX) or "\x00" in prompt
            or len(prompt.encode("utf-8", errors="replace")) > 64 * 1024
            or not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", call_id)):
        raise DeliveryError("COMPLETION_ARGUMENT_INVALID", "完成通知只能使用受信模板和稳定事件编号。")
    return await _worker(_send_sync, origin, prompt, call_id)
