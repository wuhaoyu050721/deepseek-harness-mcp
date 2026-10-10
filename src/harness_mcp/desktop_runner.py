"""直接调用已安装的 Windows Harness，负责进程归属和停止确认。

每次执行先持久化命名 JobObject 的归属，再同步建立整棵进程树的监管。
专用 home 与快照用于避免复用个人会话；它们不是本机文件访问沙箱。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from typing import Callable
import uuid

from .config import AppConfig, resolve_credential
from .errors import HarnessError, error_from_payload, error_payload
from .process_control import OwnedProcess, named_job_stopped, spawn_owned, stop_process_tree
from .redaction import redact_text, redact_value


MAX_RECORD_BYTES = 16 * 1024
PROTOCOL_OVERHEAD = 64 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _write_record(path: Path, value: dict) -> None:
    """创建进程前提交归属记录；原子替换且刷新文件，缩小崩溃窗口。"""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_record(path: Path, limit: int = MAX_RECORD_BYTES) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("record too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("record requires an object")
    return value


def _archive_record(path: Path) -> None:
    """归档必须晚于实际停止及 home 清理；仅保留审计，不提供停止证明。"""
    archive = path.parent / "stopped"
    if path.is_symlink() or path.resolve() != path.absolute():
        raise OSError("invalid desktop archive source")
    if archive.is_symlink() or getattr(archive, "is_junction", lambda: False)():
        raise OSError("invalid desktop archive directory")
    archive.mkdir(exist_ok=True)
    if archive.resolve() != archive.absolute():
        raise OSError("invalid desktop archive path")
    target = archive / path.name
    if target.exists() or target.is_symlink():
        raise OSError("desktop archive already exists")
    os.rename(path, target)  # 同卷原子移动；Windows 不覆盖既有审计记录。


def worker_environment(home: Path, credential_env: str, secret: str) -> dict[str, str]:
    """只传必要的 Windows 启动字段与一个凭证引用，不继承全部宿主环境。"""
    names = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "LOCALAPPDATA")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    temporary = home / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    application = home / "appdata"
    application.mkdir(parents=True, exist_ok=True)
    environment.update({
        "HOME": str(home), "USERPROFILE": str(home), "DSH_HOME": str(home),
        "APPDATA": str(application), "TEMP": str(temporary), "TMP": str(temporary),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        credential_env: secret,
    })
    return environment


@dataclass
class DesktopRunState:
    job_id: str
    run_id: str
    job_name: str
    record: Path
    home: Path
    deadline_at: float
    owned: OwnedProcess | None = None
    communication: asyncio.Task | None = None
    cancel_requested: bool = False
    confirmed_stopped: bool = False
    cleanup_failed: bool = False


class DesktopRunner:
    def __init__(self, config: AppConfig, owner_id: str, *, on_credential: Callable[[str], None] | None = None):
        if not isinstance(owner_id, str) or not re.fullmatch(r"[0-9a-f]{32}", owner_id):
            raise HarnessError("INVALID_OWNER", "桌面执行器需要有效的持久实例标识。")
        self.config, self.owner_id = config, owner_id
        self._on_credential = on_credential
        self._active: dict[str, DesktopRunState] = {}
        self._closed = False
        self._recovery_confirmed = True

    def _record(self, state: DesktopRunState) -> dict:
        return {
            "version": 1, "backend": "desktop", "owner_id": self.owner_id,
            "job_id": state.job_id, "run_id": state.run_id, "job_name": state.job_name,
            "deadline_at": state.deadline_at,
            "confirmed_stopped": state.confirmed_stopped,
        }

    def _save(self, state: DesktopRunState) -> None:
        _write_record(state.record, self._record(state))

    def _check_record(self, path: Path, value: dict) -> str:
        job_id, run_id = value.get("job_id"), value.get("run_id")
        if (
            value.get("version") != 1 or value.get("backend") != "desktop"
            or value.get("owner_id") != self.owner_id
            or not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
            or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
        ):
            raise ValueError("invalid ownership record")
        expected = self.config.data_dir.resolve() / "jobs" / job_id / "desktop_runs" / run_id / "execution.json"
        if path.resolve() != expected or path.parent.is_symlink() or getattr(path.parent, "is_junction", lambda: False)():
            raise ValueError("invalid ownership path")
        name = f"Local\\deepseek-harness-{self.owner_id}-{run_id}"
        if value.get("job_name") != name:
            raise ValueError("invalid ownership name")
        return name

    async def recover(self) -> bool:
        """恢复只查询本实例预先命名的 Job，不重新打开旧 PID 或杀其他会话。"""
        confirmed = True
        unconfirmed = 0
        for job_id in tuple(self._active):
            if not await self.terminate(job_id):
                confirmed = False
        try:
            for path in self.config.data_dir.glob("jobs/*/desktop_runs/*/execution.json"):
                value = _read_record(path)
                name = self._check_record(path, value)
                # confirmed_stopped 是日志提示，不能代替对实际归属对象的查询。
                if not await named_job_stopped(name):
                    confirmed = False
                    unconfirmed += 1
                    if unconfirmed >= 10000:
                        break
                else:
                    if not self._clean_home(path.parent / "home", value["job_id"], value["run_id"]):
                        confirmed = False
                    else:
                        _archive_record(path)
        except (OSError, ValueError, TypeError):
            self._recovery_confirmed = False
            return False
        self._recovery_confirmed = confirmed
        return confirmed

    async def recover_job(self, job_id: str) -> bool:
        """并发上下文只收敛本作业；不扫描、停止或证明其他作业。"""
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return False
        if not await self.terminate(job_id):
            return False
        directory = self.config.data_dir.resolve() / "jobs" / job_id / "desktop_runs"
        confirmed = True
        unconfirmed = 0
        directories = {}
        try:
            for part in (directory.parent.parent, directory.parent, directory):
                if not os.path.lexists(part):
                    return confirmed
                info = part.lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or part.resolve() != part.absolute():
                    return False
                directories[part] = (info.st_dev, info.st_ino)
            for path in directory.glob("*/execution.json"):
                parent = path.parent.lstat()
                info = path.lstat()
                if (not stat.S_ISDIR(parent.st_mode) or getattr(parent, "st_file_attributes", 0) & 0x400
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400
                    or path.parent.resolve() != path.parent.absolute()):
                    return False
                value = _read_record(path)
                after = path.lstat()
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
                    return False
                for part, identity in directories.items():
                    observed = part.lstat()
                    if not stat.S_ISDIR(observed.st_mode) or getattr(observed, "st_file_attributes", 0) & 0x400 or identity != (observed.st_dev, observed.st_ino):
                        return False
                if value.get("job_id") != job_id:
                    return False
                name = self._check_record(path, value)
                if not await named_job_stopped(name):
                    confirmed = False
                    unconfirmed += 1
                    if unconfirmed >= 10000:
                        break
                elif not self._clean_home(path.parent / "home", job_id, value["run_id"]):
                    confirmed = False
                else:
                    _archive_record(path)
        except (OSError, ValueError, TypeError):
            return False
        # 此返回值仅证明当前 job；不能据此重写全实例 _recovery_confirmed。
        return confirmed

    def _clean_home(self, home: Path, job_id: str, run_id: str) -> bool:
        """仅在全树停止后清理本作业会话，不触碰报告、快照或个人 Harness home。"""
        expected = self.config.data_dir.resolve() / "jobs" / job_id / "desktop_runs" / run_id / "home"
        try:
            if home.is_symlink() or getattr(home, "is_junction", lambda: False)() or home.resolve() != expected:
                return False
            if not home.exists():
                return True
            if not home.is_dir():
                return False
            # Python rmtree 不跟随子目录符号链接/Windows junction；顶层仍独立验证。
            deletion_path = str(home)
            if os.name == "nt":
                # Harness 将绝对 cwd 编进 session 目录名，普通路径可能超 MAX_PATH。
                # 只在校验本作业绝对路径之后内部转换，不改变调用端路径校验规则。
                deletion_path = ("\\\\?\\UNC\\" + deletion_path[2:]) if deletion_path.startswith("\\\\") else ("\\\\?\\" + deletion_path)
            shutil.rmtree(deletion_path)
            return not home.exists()
        except OSError:
            return False

    def _prepare(self, request: dict) -> tuple[DesktopRunState, dict, bytes, str]:
        if os.name != "nt":
            raise HarnessError("DESKTOP_PLATFORM_UNSUPPORTED", "桌面执行器需要 Windows。")
        if self._closed or self._active or not self._recovery_confirmed:
            raise HarnessError("EXECUTION_BLOCKED", "桌面执行器已关闭或上次执行尚未确认停止。")
        mode = request.get("mode", "review")
        if not isinstance(mode, str) or mode not in {"review", "edit"}:
            raise HarnessError("INVALID_MODE", "执行模式必须为 review 或 edit。")
        job_id = request["job_id"]
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise HarnessError("INVALID_EXECUTOR_PATH", "执行任务编号不正确。")
        job_dir = Path(request["job_dir"]).resolve()
        expected = self.config.data_dir.resolve() / "jobs" / job_id
        snapshot = Path(request["snapshot_dir"]).resolve()
        if job_dir != expected or not snapshot.is_relative_to(job_dir) or not snapshot.is_dir():
            raise HarnessError("INVALID_EXECUTOR_PATH", "桌面执行目录不属于本实例作业。")
        workspace = None
        if mode == "edit":
            workspace_value = request.get("workspace_dir")
            if not isinstance(workspace_value, str) or not Path(workspace_value).is_absolute():
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改模式需要独立工作副本的绝对路径。")
            workspace = Path(workspace_value).resolve()
            if (
                not workspace.is_dir() or not workspace.is_relative_to(job_dir) or workspace == job_dir
                or workspace == snapshot or workspace.is_relative_to(snapshot) or snapshot.is_relative_to(workspace)
            ):
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改工作副本不属于本作业或与基线快照重叠。")
        execution = dict(request.get("executor") or asdict(self.config.executor))
        if execution.get("backend") != "desktop":
            raise HarnessError("EXECUTOR_BACKEND_INVALID", "桌面执行器不能处理其他执行类型。")
        from .desktop_launcher import DesktopLaunchError, resolve_desktop_root
        try:
            execution["desktop_root"] = str(resolve_desktop_root(execution.get("desktop_root")))
        except DesktopLaunchError as error:
            raise HarnessError(error.code, error.message) from None
        deadline = request.get("deadline_at", time.time() + request["timeout_seconds"])
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise HarnessError("INVALID_TIMEOUT", "桌面执行期限不正确。")
        if deadline <= time.time():
            raise HarnessError("TIMED_OUT", "任务已达到总时限。")
        limit = request["limits"]["max_output_bytes"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 16 * 1024 * 1024:
            raise HarnessError("INVALID_PROCESS_LIMIT", "桌面输出上限不正确。")
        credential_env = request["model"]["credential_env"]
        if not isinstance(credential_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", credential_env):
            raise HarnessError("CREDENTIAL_UNAVAILABLE", "模型凭证引用不正确。")
        secret = resolve_credential(request["model"])
        # 解析后、启动前同步登记这次实际凭证，引用轮换也不会绕过宿主导出检查。
        if self._on_credential is not None:
            self._on_credential(secret)
        run_id = uuid.uuid4().hex
        directory = job_dir / "desktop_runs" / run_id
        directory.mkdir(parents=True, exist_ok=False)
        home = directory / "home"
        if any(home == source or home.is_relative_to(source) or source.is_relative_to(home) for source in (snapshot, workspace) if source is not None):
            raise HarnessError("INVALID_EXECUTOR_PATH", "专用 home 必须与代码目录分开。")
        home.mkdir()
        state = DesktopRunState(job_id, run_id, f"Local\\deepseek-harness-{self.owner_id}-{run_id}", directory / "execution.json", home, float(deadline))
        worker_request = dict(request)
        worker_request.update(mode=mode, executor=execution, snapshot_dir=str(snapshot), job_dir=str(job_dir),
                              _desktop_home=str(home), _desktop_job_name=state.job_name, deadline_at=float(deadline))
        if workspace is not None:
            worker_request["workspace_dir"] = str(workspace)
        else:
            worker_request.pop("workspace_dir", None)
        encoded = json.dumps(worker_request, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise HarnessError("EXECUTOR_INPUT_LIMIT", "桌面任务输入超过安全上限。")
        environment = worker_environment(home, credential_env, secret)
        # 在进程创建之前落盘，崩溃恢复查询命名对象时不会遗漏首次启动窗口。
        self._save(state)
        return state, environment, encoded, secret

    def _activity(self, state: DesktopRunState, callback: Callable[[str], None], previous: float) -> float:
        try:
            value = _read_record(state.home / "mcp-activity.json", 4096)
            at, stage = value.get("at"), value.get("stage")
            if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and at > previous and stage in {"initializing", "reviewing", "editing", "finishing"}:
                callback(stage)
                return at
        except (OSError, ValueError, TypeError):
            pass
        return previous

    def _result(self, output: bytes, limit: int, secret: str, returncode: int | None) -> dict:
        try:
            value = json.loads(output.decode("utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("raw_text"), str):
                raise ValueError()
            raw = value["raw_text"]
            reason = value.get("finish_reason")
            if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", reason)):
                raise ValueError()
            error = value.get("error")
            if error is not None and (
                not isinstance(error, dict) or not isinstance(error.get("code"), str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error["code"])
                or not isinstance(error.get("message"), str)
            ):
                raise ValueError()
        except (UnicodeError, ValueError, TypeError):
            raise HarnessError("EXECUTOR_PROTOCOL_ERROR", "桌面 Worker 未返回有效的有界结果。") from None
        raw = redact_text(raw, (secret,), len(raw))
        if len(raw.encode("utf-8")) > limit:
            raw = raw.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            error = {"code": "OUTPUT_LIMIT", "message": "模型回复超过配置的报告大小上限。"}
        elif error is not None:
            restored = error_from_payload(error)
            if restored.code == "PROTOCOL_ERROR" and error["code"] != "PROTOCOL_ERROR":
                raise HarnessError("EXECUTOR_PROTOCOL_ERROR", "桌面 Worker 未返回有效的有界结果。")
            error = redact_value(error_payload(restored, stage="model_execute"), (secret,))
        elif returncode != 0:
            error = {"code": "WORKER_FAILED", "message": "桌面 Worker 未正常退出。"}
        timing = {}
        for name in ("initialization_seconds", "elapsed_seconds"):
            number = (value.get("timing") or {}).get(name) if isinstance(value.get("timing"), dict) else None
            if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
                timing[name] = number
        # 官方 SDK 没有可核实的 usage 汇总接口，不接受 Worker 任意扩展字段。
        return {"raw_text": raw, "finish_reason": reason, "usage": None, "error": error, "timing": timing}

    async def run(self, request: dict, on_activity: Callable[[str], None]) -> dict:
        started = time.monotonic()
        state = None
        secret = ""
        result = {"raw_text": "", "finish_reason": None, "usage": None, "error": None, "timing": {}}
        try:
            on_activity("checking_executor")
            state, environment, encoded, secret = self._prepare(request)
            # 这里保持同步：归属建立、active 登记、通信 task 登记之前绝不能 await。
            self._active[state.job_id] = state
            on_activity("starting_desktop")
            state.owned = spawn_owned(
                [sys.executable, "-m", "harness_mcp.desktop_worker"], environment,
                max_output=request["limits"]["max_output_bytes"] + PROTOCOL_OVERHEAD,
                job_name=state.job_name,
            )
            state.communication = asyncio.create_task(asyncio.to_thread(state.owned.communicate, encoded))
            on_activity("running")
            activity_at = 0.0
            monotonic_deadline = time.monotonic() + max(0, state.deadline_at - time.time())
            while not state.communication.done():
                if state.cancel_requested:
                    raise HarnessError("CANCELLED", "任务已请求停止。")
                remaining = monotonic_deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError("TIMED_OUT", "任务已达到总时限。")
                await asyncio.wait({state.communication}, timeout=min(0.2, remaining))
                activity_at = self._activity(state, on_activity, activity_at)
            output, _ = state.communication.result()
            if state.cancel_requested:
                raise HarnessError("CANCELLED", "任务已请求停止。")
            if time.monotonic() >= monotonic_deadline:
                raise HarnessError("TIMED_OUT", "任务已达到总时限。")
            if state.owned.returncode == 124:
                raise HarnessError("TIMED_OUT", "桌面 Worker 已达到任务硬时限。")
            result = self._result(output, request["limits"]["max_output_bytes"], secret, state.owned.returncode)
        except asyncio.CancelledError:
            if state is not None:
                await asyncio.shield(self.terminate(state.job_id))
            raise
        except HarnessError as error:
            payload = error_payload(error, stage="model_execute")
            code = payload["code"]
            if code == "SNAPSHOT_OUTPUT_LIMIT":
                code = "OUTPUT_LIMIT"
            elif code == "PREPARATION_PROCESS_UNAVAILABLE":
                code = "EXECUTOR_UNAVAILABLE"
            payload["code"] = code
            result["error"] = redact_value(payload, (secret,))
        except Exception as error:
            payload = error_payload(error, stage="model_execute")
            payload.update(code="WORKER_FAILED", message="桌面执行未正常完成；原始诊断未进入报告。")
            result["error"] = redact_value(payload, (secret,))
        finally:
            stopped = True if state is None else await self.terminate(state.job_id)
            result["execution_stopped"] = stopped
            if state and state.cleanup_failed:
                if result.get("error") is None:
                    result["error"] = {"code": "DESKTOP_HOME_CLEANUP_FAILED", "message": "桌面进程已停止，但专用会话目录清理未完成。"}
                else:
                    result["error"]["message"] += " 专用会话目录清理未完成。"
            result.setdefault("timing", {})["runner_elapsed_seconds"] = round(time.monotonic() - started, 3)
            if state and state.communication:
                if state.communication.done():
                    # 取回取消/超限后通信线程的异常，避免后台 task 泄露原始诊断。
                    if not state.communication.cancelled():
                        state.communication.exception()
                elif stopped:
                    try:
                        await asyncio.wait_for(asyncio.shield(state.communication), 6)
                    except (Exception, asyncio.CancelledError):
                        pass
        return result

    async def terminate(self, job_id: str) -> bool:
        state = self._active.get(job_id)
        if state is None:
            return True
        state.cancel_requested = True
        stopped = state.owned is None or await stop_process_tree(state.owned)
        if not stopped:
            return False
        if self._active.get(job_id) is not state:
            return True  # 另一个停止调用已完成归档，不能重新创建活跃归属记录。
        state.confirmed_stopped = True
        try:
            self._save(state)
        except OSError:
            # 实际停止证据仍由 JobObject 给出；恢复也会查询该对象而非相信文件标志。
            pass
        state.cleanup_failed = not self._clean_home(state.home, state.job_id, state.run_id)
        if not state.cleanup_failed:
            try:
                _archive_record(state.record)
            except OSError:
                # 实际停止已确认，但归属未安全归档；保留 active 阻止启动新执行。
                return False
        self._active.pop(job_id, None)
        return True

    async def close(self) -> bool:
        self._closed = True
        return await self.recover()
