"""Windows 桌面 SDK Worker：在外层已持有的 JobObject 内运行一个任务。

stdout 只发布单个 UTF-8 JSON，stderr 由外层丢弃。凭证从允许列表环境读取，
不写入任务文件、结果或异常诊断；不提供文件系统访问隔离。
"""

from __future__ import annotations

from contextlib import redirect_stdout
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time

from .redaction import redact_value
from .errors import HarnessError, error_from_payload, error_payload


MAX_REQUEST_BYTES = 8 * 1024 * 1024


def owned_by_job(name: str) -> bool:
    """只核对自身归属，不创建第二层 Job，也不打开或操作旧进程。"""
    if os.name != "nt" or not isinstance(name, str) or not re.fullmatch(r"Local\\deepseek-harness-[0-9a-f]{32}-[0-9a-f]{32}", name):
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(0x0004, False, name)
    if not handle:
        return False
    try:
        member = wintypes.BOOL()
        return bool(kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle, ctypes.byref(member)) and member.value)
    finally:
        kernel.CloseHandle(handle)


def execute(request: dict) -> dict:
    """输入来自已冻结的 Runner 参数；异常只映射到固定公开信息。"""
    if not owned_by_job(request.get("_desktop_job_name")):
        return {"raw_text": "", "finish_reason": None, "usage": None,
                "error": {"code": "PROCESS_OWNERSHIP_INVALID", "message": "桌面 Worker 未处于本次任务的进程监管中。"}}
    deadline_timer = None
    activity_stop = threading.Event()
    activity_thread = None
    try:
        deadline = request["deadline_at"]
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError()
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"raw_text": "", "finish_reason": None, "usage": None,
                    "error": {"code": "TIMED_OUT", "message": "任务已达到总时限。"}}
        # 即使 MCP 事件循环暂停，独立 Worker 的定时线程也会结束直接父进程。
        # OwnedProcess 的通信线程随后终止其整个 JobObject，包括桌面 CLI 的后代。
        deadline_timer = threading.Timer(remaining, lambda: os._exit(124))
        deadline_timer.daemon = True
        deadline_timer.start()
        if request.get("executor", {}).get("backend") != "desktop":
            raise ValueError()
        mode = request.get("mode", "review")
        if not isinstance(mode, str) or mode not in {"review", "edit"}:
            raise HarnessError("INVALID_MODE", "执行模式必须为 review 或 edit。")
        snapshot = Path(request["snapshot_dir"]).resolve()
        home = Path(request["_desktop_home"]).resolve()
        workspace = snapshot
        if mode == "edit":
            value = request.get("workspace_dir")
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改模式需要独立工作副本的绝对路径。")
            workspace = Path(value).resolve()
            job_dir = Path(request["job_dir"]).resolve()
            if (
                not snapshot.is_relative_to(job_dir) or not workspace.is_relative_to(job_dir) or workspace == job_dir
                or workspace == snapshot or workspace.is_relative_to(snapshot) or snapshot.is_relative_to(workspace)
            ):
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改工作副本不属于本作业或与基线快照重叠。")
        if not snapshot.is_dir() or not workspace.is_dir() or not home.is_dir():
            raise HarnessError("INVALID_EXECUTOR_PATH", "执行代码目录不存在。")
        if any(home == directory or home.is_relative_to(directory) or directory.is_relative_to(home) for directory in (snapshot, workspace)):
            raise HarnessError("INVALID_EXECUTOR_PATH", "专用 home 必须与代码目录分开。")
        from .harness_adapter import run_harness
        activity_lock = threading.Lock()
        activity_stage = "initializing"

        def publish_activity(stage: str | None = None) -> None:
            nonlocal activity_stage
            with activity_lock:
                if stage is not None:
                    activity_stage = stage
                try:
                    temporary = home / "mcp-activity.tmp"
                    temporary.write_text(json.dumps({"stage": activity_stage, "at": time.time()}), encoding="utf-8")
                    os.replace(temporary, home / "mcp-activity.json")
                except OSError:
                    # Windows 读取方的短暂句柄可能阻止 replace。活动只是观察元数据，
                    # 本次丢弃、后续心跳重发，不能让可选写入中断真实 SDK 或重试模型。
                    pass

        def on_activity(stage: str) -> None:
            if stage in {"initializing", "reviewing", "editing", "finishing"}:
                publish_activity(stage)

        def activity_heartbeat() -> None:
            while not activity_stop.wait(2):
                try:
                    publish_activity()
                except OSError:
                    return

        # 心跳只发布受限阶段与时间；初始化/等待模型时也不保存原始通知。
        publish_activity()
        activity_thread = threading.Thread(target=activity_heartbeat, name="desktop-activity", daemon=True)
        activity_thread.start()

        # SDK/Python 的意外打印不能污染协议；runtime stderr 不进入持久日志。
        with redirect_stdout(sys.stderr):
            return run_harness(request, snapshot, home, on_activity)
    except HarnessError as error:
        return {"raw_text": "", "finish_reason": None, "usage": None,
                "error": error_payload(error, stage="model_execute")}
    except BaseException as error:
        diagnostic = error_payload(error, stage="model_execute")
        diagnostic.update(code="WORKER_FAILED", message="桌面 Worker 未正常完成；原始诊断未进入报告。")
        return {"raw_text": "", "finish_reason": None, "usage": None,
                "error": diagnostic}
    finally:
        activity_stop.set()
        if activity_thread:
            activity_thread.join(timeout=0.2)
        if deadline_timer:
            deadline_timer.cancel()


def encode_result(result: dict, request: dict) -> bytes:
    credential_env = request.get("model", {}).get("credential_env", "")
    secret = os.environ.get(credential_env, "") if isinstance(credential_env, str) else ""
    limit = request.get("limits", {}).get("max_output_bytes", 2 * 1024 * 1024)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 16 * 1024 * 1024:
        limit = 2 * 1024 * 1024
    if not isinstance(result, dict) or not isinstance(result.get("raw_text"), str):
        result = {"raw_text": "", "error": {"code": "EXECUTOR_PROTOCOL_ERROR", "message": "桌面 Worker 结果格式不正确。"}}
    error = result.get("error")
    if error is not None:
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            restored = HarnessError("EXECUTOR_PROTOCOL_ERROR", "桌面 Worker 错误格式不正确。")
        else:
            restored = error_from_payload(error)
            if restored.code == "PROTOCOL_ERROR" and error["code"] != "PROTOCOL_ERROR":
                restored = HarnessError("EXECUTOR_PROTOCOL_ERROR", "桌面 Worker 错误格式不正确。")
        error = error_payload(restored, stage="model_execute")
    # 固定白名单避免异常/SDK 扩展字段、字典键、未经核实的 usage 进入输出。
    timing = {}
    for name in ("initialization_seconds", "elapsed_seconds"):
        number = (result.get("timing") or {}).get(name) if isinstance(result.get("timing"), dict) else None
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
            timing[name] = number
    reason = result.get("finish_reason")
    safe = redact_value({"raw_text": result["raw_text"], "finish_reason": reason if isinstance(reason, str) else None,
                         "usage": None, "error": error, "timing": timing}, (secret,))
    raw = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(raw) > limit + 64 * 1024:
        raw = json.dumps({"raw_text": "", "finish_reason": None, "usage": None,
                          "error": {"code": "OUTPUT_LIMIT", "message": "Worker 结果超过安全上限，未发布完整报告。"}}, ensure_ascii=False).encode("utf-8")
    return raw


def main() -> None:
    request = {}
    result = {"raw_text": "", "finish_reason": None, "usage": None,
              "error": {"code": "EXECUTOR_PROTOCOL_ERROR", "message": "桌面 Worker 任务输入不正确。"}}
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        if sys.argv[1:]:
            raise ValueError()
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError()
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            request = {}
            raise ValueError()
        result = execute(request)
    except BaseException as error:
        diagnostic = error_payload(error, stage="worker_response")
        diagnostic.update(code="EXECUTOR_PROTOCOL_ERROR", message="桌面 Worker 任务输入不正确。")
        result["error"] = diagnostic
    try:
        sys.stdout.buffer.write(encode_result(result, request) + b"\n")
        sys.stdout.buffer.flush()
    except BaseException:
        raise SystemExit(1) from None
    raise SystemExit(1 if result.get("error") else 0)


if __name__ == "__main__":
    main()
