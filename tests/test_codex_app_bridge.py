import asyncio
import ctypes
from ctypes import wintypes
import json
import os
import struct
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from harness_mcp import codex_app_bridge as bridge

WINDOWS_PIPE_CLASS = bridge._WindowsPipe


ORIGIN = {"thread_id": "01900000-0000-7000-8000-000000000001", "turn_id": "turn-origin",
          "host_id": "local", "pipe_path": "\\\\.\\pipe\\codex-test-fixture"}


def catalog():
    return {"tools": [
        {"name": "read_thread", "namespace": "codex_app", "inputSchema": {"type": "object", "properties": {
            "threadId": {"type": "string"}, "hostId": {"type": "string"}, "turnLimit": {"type": "integer"},
            "includeOutputs": {"type": "boolean"}, "maxOutputCharsPerItem": {"type": "integer"}}, "required": ["threadId"]}},
        {"name": "send_message_to_thread", "namespace": "codex_app", "inputSchema": {"type": "object", "properties": {
            "threadId": {"type": "string"}, "hostId": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["threadId", "prompt"]}},
    ]}


def content(value, success=True):
    return {"success": success, "contentItems": [{"type": "inputText", "text": json.dumps(value)}]}


def snapshot(**changes):
    return content({"schemaVersion": 1, "thread": {"id": ORIGIN["thread_id"], "kind": "codex",
                    "hostId": "local", "cwd": "fixture", "status": {"type": "idle"}, **changes}})


class FakePipe:
    calls = None
    responses = None

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class BridgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakePipe.calls = []
        FakePipe.responses = []
        self.mock = patch.object(bridge, "_WindowsPipe", FakePipe)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    async def test_inspection_checks_capability_and_same_local_thread(self):
        FakePipe.responses = [catalog(), snapshot()]
        result = await bridge.inspect_origin(ORIGIN)
        self.assertEqual(result["thread_id"], ORIGIN["thread_id"])
        self.assertEqual(result["cwd"], "fixture")
        self.assertEqual(FakePipe.calls[0], ("tools/list", {"threadStartKind": "all"}, {}))
        request = FakePipe.calls[1][1]
        self.assertEqual(request["tool"], "read_thread")
        self.assertEqual(request["arguments"]["threadId"], ORIGIN["thread_id"])
        self.assertEqual(request["callerSource"], "codex")

    async def test_invalid_inspection_timeout_is_rejected(self):
        for value in (True, 0, -1, float("nan"), 16, "5"):
            with self.subTest(timeout=value):
                with self.assertRaises(bridge.DeliveryError):
                    await bridge.inspect_origin(ORIGIN, timeout_seconds=value)

    async def test_cancelled_transport_cannot_begin_mutating_request(self):
        pipe = WINDOWS_PIPE_CLASS.__new__(WINDOWS_PIPE_CLASS)
        pipe.cancel = threading.Event()
        pipe.cancel.set()
        pipe.deadline = time.monotonic() + 5
        with self.assertRaises(bridge.DeliveryError) as caught:
            pipe.request("tools/call", {}, mutating=True)
        self.assertFalse(caught.exception.uncertain)

    async def test_send_requires_actual_receipt_and_preserves_model_permissions(self):
        FakePipe.responses = [catalog(), snapshot(), content({"threadId": ORIGIN["thread_id"]})]
        receipt = await bridge.send_completion(ORIGIN, bridge.PROMPT_PREFIX + " fixture", "completion-job-1")
        self.assertEqual(receipt, {"status": "accepted", "thread_id": ORIGIN["thread_id"], "call_id": "completion-job-1"})
        _, params, kwargs = FakePipe.calls[-1]
        self.assertTrue(kwargs["mutating"])
        self.assertEqual(params["threadId"], params["arguments"]["threadId"])
        self.assertEqual(params["turnId"], ORIGIN["turn_id"])
        self.assertEqual(set(params["arguments"]), {"threadId", "hostId", "prompt"})

    async def test_wrong_thread_kind_host_refuses_before_send(self):
        for changes in ({"id": str(uuid.uuid4())}, {"kind": "chatgpt"}, {"hostId": "remote"}):
            with self.subTest(changes=changes):
                FakePipe.calls = []
                FakePipe.responses = [catalog(), snapshot(**changes)]
                with self.assertRaises(bridge.DeliveryError) as caught:
                    await bridge.send_completion(ORIGIN, bridge.PROMPT_PREFIX, "call-1")
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(len(FakePipe.calls), 2)

    async def test_failed_or_ambiguous_send_is_not_accepted(self):
        for result in (content({"threadId": ORIGIN["thread_id"]}, False), content({}),
                       content({"threadId": str(uuid.uuid4())}), content({"threadId": ORIGIN["thread_id"], "status": "failed"}),
                       {"success": True, "contentItems": []}, content({"threadId": ORIGIN["thread_id"], "status": []}),
                       content({"threadId": ORIGIN["thread_id"], "unexpected": "ignored?"})):
            with self.subTest(result=result):
                FakePipe.responses = [catalog(), snapshot(), result]
                with self.assertRaises(bridge.DeliveryError) as caught:
                    await bridge.send_completion(ORIGIN, bridge.PROMPT_PREFIX, "call-1")
                self.assertTrue(caught.exception.uncertain)

    async def test_catalog_drift_does_not_send(self):
        for mutation in ("missing", "namespace", "required", "property"):
            with self.subTest(mutation=mutation):
                value = catalog()
                if mutation == "missing":
                    value["tools"].pop()
                elif mutation == "namespace":
                    value["tools"][1]["namespace"] = "other"
                elif mutation == "required":
                    value["tools"][1]["inputSchema"]["required"].append("newSecret")
                else:
                    value["tools"][1]["inputSchema"]["properties"]["prompt"] = None
                FakePipe.responses = [value]
                with self.assertRaises(bridge.DeliveryError) as caught:
                    await bridge.send_completion(ORIGIN, bridge.PROMPT_PREFIX, "call-1")
                self.assertFalse(caught.exception.uncertain)

    async def test_origin_and_prompt_are_validated_before_connect(self):
        for mutation in ({"pipe_path": "\\\\remote\\pipe\\test"}, {"pipe_path": "\\\\.\\pipe\\test\\nested"},
                         {"thread_id": "arbitrary"}, {"host_id": "remote"}, {"turn_id": "bad\nturn"}):
            with self.subTest(mutation=mutation), self.assertRaises(bridge.DeliveryError):
                await bridge.inspect_origin({**ORIGIN, **mutation})
        for prompt, call in (("arbitrary model content", "call-1"), (bridge.PROMPT_PREFIX, "call\n1")):
            with self.assertRaises(bridge.DeliveryError):
                await bridge.send_completion(ORIGIN, prompt, call)
        self.assertEqual(FakePipe.calls, [])

    async def test_read_inspection_call_ids_are_unique_even_for_same_event(self):
        FakePipe.responses = [catalog(), snapshot(), catalog(), snapshot()]
        await bridge.inspect_origin(ORIGIN)
        await bridge.inspect_origin(ORIGIN)
        self.assertNotEqual(FakePipe.calls[1][1]["callId"], FakePipe.calls[3][1]["callId"])

    def test_strict_utf8_duplicate_nonfinite_json(self):
        for raw in (b'\xff', b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(bridge.DeliveryError):
                bridge._decode(raw)
        with self.assertRaises(bridge.DeliveryError):
            bridge._encode({"a": float("nan")})


class NamedPipeFixture:
    """真实本机 pipe 只验证传输、碎片帧和取消，不连接 Codex 或发送消息。"""
    def __init__(self, responder):
        self.path = "\\\\.\\pipe\\harness-test-" + uuid.uuid4().hex
        self.responder = responder
        self.requests = []
        self.error = None
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                             wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        self.api.CreateNamedPipeW.restype = wintypes.HANDLE
        self.api.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        self.api.ConnectNamedPipe.restype = wintypes.BOOL
        self.api.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.api.ReadFile.restype = wintypes.BOOL
        self.api.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.api.WriteFile.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateNamedPipeW(self.path, 3, 0, 1, 65536, 65536, 1000, None)
        assert self.handle != ctypes.c_void_p(-1).value
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _read(self, count):
        output = b""
        while len(output) < count:
            buffer = ctypes.create_string_buffer(count - len(output))
            done = wintypes.DWORD()
            if not self.api.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(done), None) or not done.value:
                raise EOFError
            output += buffer.raw[:done.value]
        return output

    def _run(self):
        try:
            if not self.api.ConnectNamedPipe(self.handle, None) and ctypes.get_last_error() != 535:
                raise OSError("fixture connect failed")
            while True:
                count = struct.unpack("<I", self._read(4))[0]
                request = json.loads(self._read(count))
                self.requests.append(request)
                result = self.responder(request)
                if result is None:
                    break
                frame = bridge._encode({"jsonrpc": "2.0", "id": request["id"], "result": result})
                # 包头、UTF-8 内容和完整帧均刻意分段。
                for offset in range(0, len(frame), 7):
                    part = frame[offset:offset + 7]
                    done = wintypes.DWORD()
                    buffer = ctypes.create_string_buffer(part, len(part))
                    if not self.api.WriteFile(self.handle, buffer, len(part), ctypes.byref(done), None):
                        raise EOFError
        except EOFError:
            pass
        except Exception as error:
            self.error = error
        finally:
            self.api.CloseHandle(self.handle)

    def close(self):
        self.thread.join(2)
        assert not self.thread.is_alive(), "fixture pipe thread leaked"
        if self.error:
            raise self.error


@unittest.skipUnless(os.name == "nt", "Windows native named pipe transport")
class NativePipeTests(unittest.TestCase):
    def test_fragmented_frames_and_utf8(self):
        server = NamedPipeFixture(lambda _request: {"value": "本机管道碎片"})
        try:
            with bridge._WindowsPipe(server.path, threading.Event(), timeout=2) as pipe:
                self.assertEqual(pipe.request("tools/list", {}), {"value": "本机管道碎片"})
        finally:
            server.close()

    def test_read_timeout_cancels_overlapped_io_without_leaking_thread(self):
        def delayed(_request):
            time.sleep(0.2)
            return None
        server = NamedPipeFixture(delayed)
        started = time.monotonic()
        try:
            with bridge._WindowsPipe(server.path, threading.Event(), timeout=0.05) as pipe:
                with self.assertRaises(bridge.DeliveryError) as caught:
                    pipe.request("tools/list", {})
                self.assertEqual(caught.exception.code, "COMPLETION_HOST_TIMEOUT")
                self.assertFalse(caught.exception.uncertain)
        finally:
            server.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(bridge._retired_io, [])

    def test_after_send_disconnect_is_uncertain_and_not_retried(self):
        server = NamedPipeFixture(lambda _request: None)
        try:
            with bridge._WindowsPipe(server.path, threading.Event(), timeout=2) as pipe:
                with self.assertRaises(bridge.DeliveryError) as caught:
                    pipe.request("tools/call", {"fixture": True}, mutating=True)
                self.assertTrue(caught.exception.uncertain)
        finally:
            server.close()
        self.assertEqual(len(server.requests), 1)

    def test_missing_pipe_is_safe_to_retry(self):
        with self.assertRaises(bridge.DeliveryError) as caught:
            with bridge._WindowsPipe("\\\\.\\pipe\\harness-missing-" + uuid.uuid4().hex, threading.Event(), timeout=0.05):
                pass
        self.assertFalse(caught.exception.uncertain)


@unittest.skipUnless(os.name == "nt", "Windows native named pipe transport")
class NativeAsyncCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_cancel_stops_owned_overlapped_read(self):
        entered_read = threading.Event()

        def responder(request):
            if request["method"] == "tools/list":
                return catalog()
            entered_read.set()
            time.sleep(0.25)
            return None

        server = NamedPipeFixture(responder)
        origin = {**ORIGIN, "pipe_path": server.path}
        task = asyncio.create_task(bridge.inspect_origin(origin))
        try:
            deadline = time.monotonic() + 2
            while not entered_read.is_set():
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            server.close()
        self.assertEqual(bridge._retired_io, [])
