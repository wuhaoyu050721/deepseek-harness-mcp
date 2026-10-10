"""容器 PID 1 与 SDK 子进程入口：非 root、固定目录、整个任务硬时限。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .redaction import redact_value
from .supervisor import finite_number, read_json, write_json

TASK_FILE = Path("/control/task.json")
SOURCE_DIR = Path("/source")
HOME_DIR = Path("/home/harness")
OUTPUT_DIR = Path("/output")
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 126


def adapter_environment(request: dict) -> dict[str, str]:
    """SDK 会复制 Worker 环境；只让明确需要的字段进入该执行链。"""
    credential_env = request["model"]["credential_env"]
    allowed = {"PATH", "HOME", "DSH_HOME", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "LANG", credential_env}
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({"HOME": HOME_DIR.as_posix(), "DSH_HOME": HOME_DIR.as_posix(), "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def publish_result(result: dict, request: dict) -> None:
    """结果在容器内先脱敏并限制编码大小，宿主再独立复核与脱敏。"""
    secret = os.environ.get(request["model"]["credential_env"], "")
    result = redact_value(result, (secret,))
    limit = int(request["limits"]["max_output_bytes"]) + 64 * 1024
    raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(raw) > limit:
        result = {"raw_text": "", "finish_reason": result.get("finish_reason"), "usage": None, "error": {"code": "OUTPUT_LIMIT", "message": "Worker 编码结果超过配置上限，未发布完整报告。"}}
    write_json(OUTPUT_DIR / "result.json", result)
    # bind mount 保留容器 UID；专用 job 根目录在宿主是0700，报告已脱敏，
    # 单文件0644使普通宿主用户也可收集结果而无需赋予 Worker root。
    (OUTPUT_DIR / "result.json").chmod(0o644)


def adapter_main() -> int:
    os.umask(0o077)
    request = read_json(TASK_FILE)
    from .harness_adapter import run_harness

    def on_activity(stage: str) -> None:
        if stage in {"initializing", "reviewing", "finishing"}:
            write_json(OUTPUT_DIR / "activity.json", {"stage": stage, "at": time.time()})
            (OUTPUT_DIR / "activity.json").chmod(0o644)

    try:
        result = run_harness(request, SOURCE_DIR, HOME_DIR, on_activity)
    except BaseException:
        result = {"raw_text": "", "finish_reason": None, "usage": None, "error": {"code": "WORKER_FAILED", "message": "Worker 适配器未正常完成。"}}
    publish_result(result, request)
    return 1 if result.get("error") else 0


def stop_session(process: subprocess.Popen, grace: float = 1) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def supervise_worker() -> int:
    # 该 Worker 不能被宿主调用来悄然绕过容器隔离；只有镜像 PID 1 可执行。
    if sys.platform != "linux" or os.getpid() != 1 or os.getuid() != 10001:
        raise RuntimeError("Worker requires dedicated Linux container PID 1 UID10001")
    os.umask(0o077)
    request = read_json(TASK_FILE)
    deadline = finite_number(request["deadline_at"])
    monotonic_deadline = time.monotonic() + max(0, deadline - time.time())
    if monotonic_deadline <= time.monotonic():
        return EXIT_TIMEOUT
    cancelled = False

    def request_stop(_signum, _frame) -> None:
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    process = subprocess.Popen(
        [sys.executable, "-m", "harness_mcp.worker", "--adapter"],
        cwd=SOURCE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=adapter_environment(request),
        start_new_session=True,
    )
    while True:
        if cancelled or time.monotonic() >= monotonic_deadline:
            stop_session(process)
            return EXIT_CANCELLED if cancelled else EXIT_TIMEOUT
        result = process.poll()
        if result is not None:
            stop_session(process, grace=0.2)
            # PID 1 退出触发 namespace 清理；不能将单个 await 结束当作停止。
            return 0 if result == 0 else 1
        time.sleep(0.1)


def main() -> None:
    try:
        if sys.argv[1:] == ["--adapter"]:
            # 入口虽然可独立 import，但容器路径及身份不能由工具参数替换。
            if sys.platform != "linux" or os.getuid() != 10001:
                raise RuntimeError("adapter requires Linux Worker identity")
            code = adapter_main()
        elif not sys.argv[1:]:
            code = supervise_worker()
        else:
            code = 1
    except BaseException:
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
