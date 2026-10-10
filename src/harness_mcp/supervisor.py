"""宿主独立 watchdog：MCP 崩溃或租约过期后停止整个专属容器。

控制目录不会挂进容器。模型既无法续租，也不能触及该宿主进程；所有
Docker 操作先核对持久 owner/run 标签，不按进程名或镜像名批量清理。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any

OWNER_LABEL = "io.deepseek.harness-mcp.owner"
RUN_LABEL = "io.deepseek.harness-mcp.run"
JOB_LABEL = "io.deepseek.harness-mcp.job"
SERVICE_LABEL = "io.deepseek.harness-mcp.service"
INSPECT_FORMAT = '{"id":{{json .Id}},"state":{{json .State}},"labels":{{json .Config.Labels}}}'


def read_json(path: Path, limit: int = 256 * 1024) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("control file exceeds limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("control file must be an object")
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid control number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid control number")
    return number


def lease_stop_reason(
    lease: dict[str, Any], *, run_id: str, now: float, lease_seconds: float
) -> str | None:
    if lease.get("run_id") != run_id:
        return "lease_lost"
    if lease.get("cancel_requested") is True:
        return "cancelled"
    try:
        expires = finite_number(lease.get("expires_at"))
    except ValueError:
        return "lease_lost"
    if expires <= now or expires > now + lease_seconds * 2:
        return "lease_lost"
    return None


def safe_host_env() -> dict[str, str]:
    """仅继承 Docker/系统启动所需字段，不继承宿主全部凭证。"""
    allowed = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def docker_call(command: str, args: list[str], timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [command, *args],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=safe_host_env(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def docker_args(config: dict[str, Any], args: list[str]) -> list[str]:
    """冻结启动时核验的本机 endpoint，不受后续 default context 改动影响。"""
    endpoint = config.get("docker_host")
    return ["--host", endpoint, *args] if endpoint else args


def inspect_owned(config: dict[str, Any]) -> dict[str, Any] | None:
    result = docker_call(config["docker_command"], docker_args(config, ["inspect", "--format", INSPECT_FORMAT, config["container_id"]]))
    if result.returncode:
        # 单凭 Docker inspect 错误不能断言不存在；需成功列举 daemon 资源。
        listing = docker_call(config["docker_command"], docker_args(config, ["ps", "-a", "--no-trunc", "--filter", f"label={OWNER_LABEL}={config['owner_id']}", "--filter", f"label={RUN_LABEL}={config['run_id']}", "--format", "{{.ID}} {{.Names}}"] ))
        if listing.returncode or any(config["container_id"] in line.split() for line in listing.stdout.splitlines()):
            raise RuntimeError("container state unavailable")
        return None
    obj = json.loads(result.stdout)
    if not isinstance(obj, dict):
        raise RuntimeError("invalid container state")
    labels = obj.get("labels", {}) or {}
    if (
        labels.get(SERVICE_LABEL) != "1"
        or labels.get(OWNER_LABEL) != config["owner_id"]
        or labels.get(RUN_LABEL) != config["run_id"]
    ):
        raise RuntimeError("container ownership mismatch")
    return obj


def stop_owned(config: dict[str, Any]) -> bool:
    try:
        obj = inspect_owned(config)
        if obj is None:
            return not config.get("creation_unresolved", False)
        if obj.get("state", {}).get("Running") is False:
            return True
        grace = min(30, max(0, int(config.get("grace_seconds", 3))))
        try:
            docker_call(config["docker_command"], docker_args(config, ["stop", "--time", str(grace), config["container_id"]]), timeout=grace + 5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        obj = inspect_owned(config)
        if obj is not None and obj.get("state", {}).get("Running") is not False:
            docker_call(config["docker_command"], docker_args(config, ["kill", config["container_id"]]))
            obj = inspect_owned(config)
        return obj is None or obj.get("state", {}).get("Running") is False
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return False


def monitor(control: Path) -> int:
    config = read_json(control / "watchdog.json")
    deadline = finite_number(config["deadline_at"])
    lease_seconds = finite_number(config["lease_seconds"])
    if not 3 <= lease_seconds <= 120:
        raise ValueError("invalid lease window")
    monotonic_deadline = time.monotonic() + max(0, deadline - time.time())
    inspect_owned(config)
    write_json(control / "ready.json", {"run_id": config["run_id"], "pid": os.getpid()})
    stop_reason: str | None = None
    while True:
        if time.monotonic() >= monotonic_deadline:
            stop_reason = "timed_out"
        try:
            lease = read_json(control / "lease.json", 4096)
            lease_reason = lease_stop_reason(
                lease, run_id=config["run_id"], now=time.time(), lease_seconds=lease_seconds
            )
            if stop_reason is None:
                stop_reason = lease_reason
        except (OSError, ValueError, TypeError):
            lease = {}
            if stop_reason is None:
                stop_reason = "lease_lost"
        if stop_reason is not None:
            unresolved = lease.get("creation_attempted") is True and lease.get("creation_resolved") is not True
            creation_path = control / "creation.json"
            if creation_path.is_file():
                try:
                    creation = read_json(creation_path, 4096)
                    unresolved = creation.get("run_id") != config["run_id"] or creation.get("resolved") is not True
                except (OSError, ValueError, TypeError):
                    unresolved = True
            if stop_owned({**config, "creation_unresolved": unresolved}):
                write_json(control / "stopped.json", {"run_id": config["run_id"], "reason": stop_reason, "execution_stopped": True})
                return 0
            # Docker 暂时不可用时继续尝试，不假报停止、也不续租任务。
            write_json(control / "stopped.json", {"run_id": config["run_id"], "reason": stop_reason, "execution_stopped": False})
        else:
            try:
                obj = inspect_owned(config)
                if lease.get("container_started") is True and (
                    obj is None or obj.get("state", {}).get("Running") is False
                ):
                    write_json(control / "stopped.json", {"run_id": config["run_id"], "reason": "exited", "execution_stopped": True})
                    return 0
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                # 租约和硬时限仍在生效；失联不是停止成功。
                pass
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        code = monitor(arguments.control)
    except BaseException:
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
