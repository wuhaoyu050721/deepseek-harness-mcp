"""仅 Docker 执行：受控挂载、资源上限、独立 watchdog 和停止确认。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Callable
import uuid

from .config import AppConfig, resolve_credential
from .errors import HarnessError, error_from_payload, error_payload
from .redaction import redact_value
from .supervisor import JOB_LABEL, OWNER_LABEL, RUN_LABEL, SERVICE_LABEL, read_json, safe_host_env, write_json

INSPECT_FORMAT = '{"id":{{json .Id}},"state":{{json .State}},"labels":{{json .Config.Labels}}}'
MAX_DOCKER_OUTPUT = 256 * 1024


def is_local_docker_endpoint(endpoint) -> bool:
    if not isinstance(endpoint, str):
        return False
    return bool(
        re.fullmatch(r"npipe:/{4}\./pipe/[A-Za-z0-9_.-]+", endpoint)
        or re.fullmatch(r"unix:///[^\x00\r\n?#]+", endpoint)
    )


@dataclass
class RunState:
    job_id: str
    run_id: str
    container_name: str
    control: Path
    executor: dict
    deadline_at: float
    container_id: str = ""
    creation_attempted: bool = False
    creation_resolved: bool = False
    container_started: bool = False
    cancel_requested: bool = False
    watchdog: subprocess.Popen | None = None
    heartbeat: asyncio.Task | None = None
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def reference(self) -> str:
        return self.container_id or self.container_name


class DockerRunner:
    def __init__(self, config: AppConfig, owner_id: str, *, on_credential: Callable[[str], None] | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", owner_id):
            raise HarnessError("INVALID_OWNER", "服务实例标识无效。")
        self.config = config
        self.owner_id = owner_id
        self._on_credential = on_credential
        self._active: dict[str, RunState] = {}
        self._closed = False

    async def _command(
        self, command: str, args: list[str], *, env: dict[str, str] | None = None,
        timeout: float = 8,
    ) -> tuple[int, str]:
        """不使用 shell；stdout 有界、stderr 丢弃，不保存 Docker 原文错误。"""
        try:
            process = await asyncio.create_subprocess_exec(
                command, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env if env is not None else safe_host_env(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            raise HarnessError("EXECUTOR_UNAVAILABLE", "未找到可用 Docker 执行器；任务不会回退到宿主执行。") from None

        async def consume() -> tuple[int, str]:
            assert process.stdout is not None
            chunks: list[bytes] = []
            total = 0
            while chunk := await process.stdout.read(16384):
                total += len(chunk)
                if total > MAX_DOCKER_OUTPUT:
                    process.kill()
                    await process.wait()
                    raise HarnessError("EXECUTOR_PROTOCOL_ERROR", "Docker 返回内容超过安全上限。")
                chunks.append(chunk)
            code = await process.wait()
            return code, b"".join(chunks).decode("utf-8", errors="replace").strip()

        try:
            return await asyncio.wait_for(consume(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise HarnessError("EXECUTOR_UNAVAILABLE", "Docker 命令超时，无法确认执行器状态。") from None

    async def _inspect(self, state: RunState) -> dict | None:
        code, raw = await self._docker(state.executor, ["inspect", "--format", INSPECT_FORMAT, state.reference])
        if code:
            code, names = await self._docker(state.executor, ["ps", "-a", "--no-trunc", "--filter", f"label={OWNER_LABEL}={self.owner_id}", "--filter", f"label={RUN_LABEL}={state.run_id}", "--format", "{{.ID}} {{.Names}}"])
            if code or any(state.reference in line.split() for line in names.splitlines()):
                raise HarnessError("EXECUTOR_UNAVAILABLE", "无法确认该容器是否已停止。")
            return None
        try:
            obj = json.loads(raw)
            labels = obj["labels"] or {}
        except (ValueError, TypeError, KeyError):
            raise HarnessError("EXECUTOR_PROTOCOL_ERROR", "Docker 容器状态格式无效。") from None
        if (
            labels.get(SERVICE_LABEL) != "1"
            or labels.get(OWNER_LABEL) != self.owner_id
            or labels.get(RUN_LABEL) != state.run_id
            or labels.get(JOB_LABEL) != state.job_id
        ):
            raise HarnessError("OWNERSHIP_MISMATCH", "容器标识不属于本次任务，拒绝操作。")
        return obj

    async def _local_endpoint(self, command: str) -> str:
        code, raw = await self._command(command, ["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])
        try:
            endpoint = json.loads(raw) if code == 0 else None
        except ValueError:
            endpoint = None
        if not is_local_docker_endpoint(endpoint):
            # TCP localhost 仍可能被代理到其他 daemon，首版仅接受本机 IPC。
            raise HarnessError("EXECUTOR_ENDPOINT_UNSAFE", "执行器必须使用本机 Unix socket 或 Windows named pipe；已拒绝远程/TCP Docker context。")
        return endpoint

    async def _docker(self, options: dict, args: list[str], **kwargs) -> tuple[int, str]:
        endpoint = options.get("docker_host")
        prefix = ["--host", endpoint] if endpoint else []
        return await self._command(options["docker_command"], [*prefix, *args], **kwargs)

    def _lease(self, state: RunState) -> None:
        write_json(state.control / "lease.json", {
            "run_id": state.run_id,
            "expires_at": time.time() + state.executor["lease_seconds"],
            "cancel_requested": state.cancel_requested,
            "container_started": state.container_started,
            "creation_attempted": state.creation_attempted,
            "creation_resolved": state.creation_resolved,
        })

    async def _heartbeat(self, state: RunState) -> None:
        while True:
            self._lease(state)
            await asyncio.sleep(state.executor["heartbeat_seconds"])

    def _mount(self, source: Path, target: str, readonly: bool) -> str:
        value = str(source.resolve())
        # Docker --mount 的逗号为分隔符；拒绝含逗号的目录，防止附加挂载选项。
        if any(item in value for item in (",", "\n", "\r", "\x00")):
            raise HarnessError("INVALID_EXECUTOR_PATH", "执行目录包含 Docker 挂载不支持的字符。")
        return f"type=bind,source={value},target={target}" + (",readonly" if readonly else "")

    def build_create_args(
        self, state: RunState, *, snapshot: Path, worker_control: Path,
        home: Path, output: Path, credential_env: str,
    ) -> list[str]:
        options = state.executor
        return [
            "create", "--pull", "never", "--name", state.container_name,
            "--label", f"{SERVICE_LABEL}=1", "--label", f"{OWNER_LABEL}={self.owner_id}",
            "--label", f"{RUN_LABEL}={state.run_id}", "--label", f"{JOB_LABEL}={state.job_id}",
            "--user", "10001:10001", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only",
            "--cpus", str(options["cpus"]), "--memory", f"{options['memory_mb']}m",
            "--memory-swap", f"{options['memory_mb']}m", "--pids-limit", str(options["pids_limit"]),
            "--network", "bridge", "--stop-timeout", str(options["termination_grace_seconds"]),
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--mount", self._mount(snapshot, "/source", True),
            "--mount", self._mount(worker_control, "/control", True),
            "--mount", self._mount(home, "/home/harness", False),
            "--mount", self._mount(output, "/output", False),
            "--env", "HOME=/home/harness", "--env", "DSH_HOME=/home/harness",
            "--env", "TMPDIR=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            # 只传变量名；秘密通过 Docker CLI 子进程环境传入，从不出现值 argv。
            "--env", credential_env,
            options["image"],
        ]

    async def _start_watchdog(self, state: RunState) -> None:
        write_json(state.control / "watchdog.json", {
            "owner_id": self.owner_id, "run_id": state.run_id,
            "job_id": state.job_id,
            # 在 create 之前监护已就绪；固定名字与 owner/run 标签可核对
            # Docker CLI 回执丢失之后才迟到的容器，不要求已拿到 ID。
            "container_id": state.container_name,
            "docker_command": state.executor["docker_command"],
            "docker_host": state.executor.get("docker_host"),
            "lease_seconds": state.executor["lease_seconds"],
            "deadline_at": state.deadline_at,
            "grace_seconds": state.executor["termination_grace_seconds"],
        })
        env = safe_host_env()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        # Windows Job 可能将所有子进程随 MCP 一起关闭。明确要求 breakaway，
        # 若宿主拒绝该能力则失败关闭，不把同组进程称为独立崩溃保护。
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
        try:
            state.watchdog = subprocess.Popen(
                [sys.executable, "-m", "harness_mcp.supervisor", "--control", str(state.control)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, creationflags=flags, start_new_session=os.name != "nt",
            )
        except OSError:
            raise HarnessError("WATCHDOG_UNAVAILABLE", "无法启动独立租约监护，已拒绝执行。") from None
        readiness_deadline = time.monotonic() + 8
        while time.monotonic() < readiness_deadline:
            if (state.control / "ready.json").is_file():
                try:
                    value = json.loads((state.control / "ready.json").read_text(encoding="utf-8"))
                    if value.get("run_id") == state.run_id and state.watchdog.poll() is None:
                        return
                except (OSError, ValueError):
                    pass
            if state.watchdog.poll() is not None:
                break
            await asyncio.sleep(0.05)
        raise HarnessError("WATCHDOG_UNAVAILABLE", "独立租约监护未就绪，已拒绝启动模型。")

    async def _stop_state(self, state: RunState) -> bool:
        async with state.command_lock:
            if not state.creation_attempted and not state.container_id:
                return True
            try:
                obj = await self._inspect(state)
                if obj is None:
                    return not (state.creation_attempted and not state.creation_resolved)
                if state.creation_attempted and not state.creation_resolved:
                    # inspect 返回 owned 对象才说明迟到创建已实际落地。
                    state.creation_resolved = True
                    if re.fullmatch(r"[a-f0-9]{64}", obj.get("id", "")):
                        state.container_id = obj["id"]
                    write_json(state.control / "creation.json", {"run_id": state.run_id, "resolved": True})
                    self._lease(state)
                if obj["state"].get("Running") is False:
                    return True
                grace = state.executor["termination_grace_seconds"]
                try:
                    await self._docker(state.executor, ["stop", "--time", str(grace), state.reference], timeout=grace + 5)
                except HarnessError:
                    pass
                obj = await self._inspect(state)
                if obj is not None and obj["state"].get("Running") is not False:
                    await self._docker(state.executor, ["kill", state.reference])
                    obj = await self._inspect(state)
                return obj is None or obj["state"].get("Running") is False
            except (HarnessError, ValueError, TypeError, KeyError):
                return False

    async def terminate(self, job_id: str) -> bool:
        state = self._active.get(job_id)
        if state is None:
            return True
        state.cancel_requested = True
        self._lease(state)
        return await self._stop_state(state)

    async def _release(self, state: RunState, stopped: bool) -> None:
        if state.heartbeat is not None:
            state.heartbeat.cancel()
            await asyncio.gather(state.heartbeat, return_exceptions=True)
        if stopped:
            if state.container_id:
                write_json(state.control / "confirmed_stopped.json", {"run_id": state.run_id, "container_id": state.container_id, "execution_stopped": True})
            if state.watchdog is not None:
                # 容器停止已确认后才能关监护；关闭监护不是容器停止的证据。
                try:
                    await asyncio.to_thread(state.watchdog.wait, 2)
                except subprocess.TimeoutExpired:
                    state.watchdog.terminate()
                    await asyncio.to_thread(state.watchdog.wait, 2)
            if state.container_id:
                try:
                    obj = await self._inspect(state)
                    if obj is not None and obj["state"].get("Running") is False:
                        await self._docker(state.executor, ["rm", state.reference])
                except HarnessError:
                    pass
            self._active.pop(state.job_id, None)
        # 停止失败保留 active + watchdog，停止续租使独立 watchdog 接管重试。

    def _result_file(self, path: Path, maximum: int) -> dict:
        try:
            attributes = path.lstat()
            if (
                not stat.S_ISREG(attributes.st_mode) or path.is_symlink()
                or bool(getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            ):
                raise ValueError()
            with path.open("rb") as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise HarnessError("OUTPUT_LIMIT", "Worker 结果超过允许大小。")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            # 只保留 Worker 固定契约；额外字段不能随报告持久化或覆盖宿主状态。
            raw_text, finish, usage, error = (value.get(key) for key in ("raw_text", "finish_reason", "usage", "error"))
            if not isinstance(raw_text, str):
                raise ValueError()
            if finish is not None and (not isinstance(finish, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", finish)):
                raise ValueError()
            if usage is not None and not isinstance(usage, dict):
                raise ValueError()
            if error is not None and (
                not isinstance(error, dict)
                or not {"code", "message"} <= set(error)
                or not set(error) <= {"code", "message", "diagnostic_id", "stage", "exception_type", "details"}
                or (set(error) != {"code", "message"} and not {"diagnostic_id", "stage", "exception_type"} <= set(error))
                or not isinstance(error.get("code"), str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error["code"])
                or not isinstance(error.get("message"), str)
            ):
                raise ValueError()
            if error is not None:
                restored = error_from_payload(error)
                if restored.code == "PROTOCOL_ERROR" and error["code"] != "PROTOCOL_ERROR":
                    raise ValueError()
                if set(error) != {"code", "message"} and (
                    type(error["diagnostic_id"]) is not str or restored.diagnostic_id != error["diagnostic_id"].lower()
                    or type(error["stage"]) is not str or restored.stage != error["stage"]
                    or type(error["exception_type"]) is not str or restored.exception_type != error["exception_type"]
                    or ("details" in error and type(error["details"]) is not dict)
                ):
                    raise ValueError()
                error = error_payload(restored, stage="model_execute")
            return {"raw_text": raw_text, "finish_reason": finish, "usage": usage, "error": error}
        except HarnessError:
            raise
        except (OSError, ValueError, RecursionError):
            raise HarnessError("WORKER_RESULT_INVALID", "Worker 结果缺失或格式不正确。") from None

    async def run(self, request: dict, on_activity: Callable[[str], None]) -> dict:
        started = time.time()
        job_id = request["job_id"]
        if self._closed or self._active:
            return {"raw_text": "", "finish_reason": None, "usage": None, "execution_stopped": not bool(self._active), "error": {"code": "EXECUTOR_BUSY", "message": "执行器尚未停止或服务已关闭。"}, "timing": {"elapsed_seconds": 0}}
        options = request.get("executor") or asdict(self.config.executor)
        options = {**asdict(self.config.executor), **options}
        root = Path(request["job_dir"]).resolve()
        # job_dir 必须位于本实例数据目录；挂载来自服务准备后的快照。
        try:
            root.relative_to(self.config.data_dir.resolve())
            snapshot = Path(request["snapshot_dir"]).resolve(strict=True)
            snapshot.relative_to(root)
        except (OSError, ValueError):
            raise HarnessError("INVALID_EXECUTOR_PATH", "执行目录不属于本实例任务目录。") from None
        if os.name != "nt":
            root.chmod(0o700)
        run_id = uuid.uuid4().hex
        control = root / "control_host" / run_id
        worker_control = root / "control_worker" / run_id
        home, output = root / "home" / run_id, root / "output" / run_id
        for path in (control, worker_control, home, output):
            path.mkdir(parents=True, exist_ok=False)
            if os.name != "nt":
                path.chmod(0o700 if path == control else 0o755)
        # bind mount 在 Linux 保留宿主 UID。外层 job 根目录私有，专用可写
        # 目录对容器 UID10001 开放；Worker 文件以 umask077 写入。
        if os.name != "nt":
            home.chmod(0o777); output.chmod(0o777)
        state = RunState(job_id, run_id, f"harness-mcp-{self.owner_id[-12:]}-{run_id[-16:]}", control, options, float(request.get("deadline_at", started + request["timeout_seconds"])))
        self._active[job_id] = state
        secret = ""
        stopped = True
        try:
            on_activity("checking_executor")
            options["docker_host"] = await self._local_endpoint(options["docker_command"])
            code, server_os = await self._docker(options, ["version", "--format", "{{.Server.Os}}"])
            if code or server_os != "linux":
                raise HarnessError("EXECUTOR_UNAVAILABLE", "需要可用 Linux Docker daemon，任务不会回退到宿主执行。")
            code, _ = await self._docker(options, ["image", "inspect", options["image"], "--format", "{{.Id}}"])
            if code:
                raise HarnessError("EXECUTOR_IMAGE_MISSING", "Worker 镜像尚未构建；请先完成本地执行器安装。")
            secret = resolve_credential(request["model"])
            # 仅回传可信宿主内存，先登记本作业实际凭证再传给容器环境。
            if self._on_credential is not None:
                self._on_credential(secret)
            credential_env = request["model"]["credential_env"]
            task = {key: request[key] for key in ("job_id", "project_id", "prompt", "timeout_seconds", "model", "limits")}
            task.update({"deadline_at": state.deadline_at, "run_id": run_id})
            write_json(worker_control / "task.json", task)
            if os.name != "nt":
                (worker_control / "task.json").chmod(0o644)
            self._lease(state)
            state.heartbeat = asyncio.create_task(self._heartbeat(state))
            env = safe_host_env(); env[credential_env] = secret
            await self._start_watchdog(state)
            on_activity("creating_container")
            # create 与取消共用锁。不能在 create 尚未返回时用一次 inspect
            # 不存在就宣称清理完成；迟到结果由独立监护按固定名字核对。
            async with state.command_lock:
                if state.cancel_requested:
                    raise HarnessError("CANCELLED", "任务在创建前已请求取消。")
                if time.time() >= state.deadline_at:
                    raise HarnessError("TIMED_OUT", "容器创建前总时限已到。")
                if state.watchdog is None or state.watchdog.poll() is not None:
                    raise HarnessError("WATCHDOG_LOST", "独立监护已退出，拒绝创建执行器。")
                stopped = False
                state.creation_attempted = True
                write_json(control / "creation.json", {"run_id": run_id, "resolved": False})
                self._lease(state)
                code, container_id = await self._docker(options, self.build_create_args(state, snapshot=snapshot, worker_control=worker_control, home=home, output=output, credential_env=credential_env), env=env)
                if code or not re.fullmatch(r"[a-f0-9]{64}", container_id):
                    raise HarnessError("EXECUTOR_START_FAILED", "Docker 创建回执无效，需核对迟到容器。")
                state.container_id = container_id
                if await self._inspect(state) is None:
                    raise HarnessError("EXECUTOR_START_FAILED", "尚未确认已创建容器归属。")
                state.creation_resolved = True
                write_json(control / "creation.json", {"run_id": run_id, "resolved": True})
                self._lease(state)
            async with state.command_lock:
                if state.cancel_requested:
                    raise HarnessError("CANCELLED", "任务在启动前已请求取消。")
                if time.time() >= state.deadline_at:
                    raise HarnessError("TIMED_OUT", "容器启动前总时限已到。")
                if state.watchdog is None or state.watchdog.poll() is not None:
                    raise HarnessError("WATCHDOG_LOST", "独立监护已退出，拒绝启动模型。")
                code, _ = await self._docker(options, ["start", state.reference])
                if code:
                    raise HarnessError("EXECUTOR_START_FAILED", "Docker 无法启动受控 Worker。")
                state.container_started = True
                self._lease(state)
            on_activity("running")
            while True:
                obj = await self._inspect(state)
                if obj is None:
                    stopped = True
                    raise HarnessError("WORKER_RESULT_INVALID", "任务容器提前消失。")
                if obj["state"].get("Running") is False:
                    stopped = True
                    exit_code = obj["state"].get("ExitCode")
                    break
                if state.cancel_requested or time.time() >= state.deadline_at:
                    stopped = await self._stop_state(state)
                    code = "CANCELLED" if state.cancel_requested else "TIMED_OUT"
                    raise HarnessError(code if stopped else "TERMINATION_FAILED", "任务已停止。" if stopped else "停止确认失败，暂停启动后续任务。")
                if state.watchdog is None or state.watchdog.poll() is not None:
                    raise HarnessError("WATCHDOG_LOST", "独立租约监护提前退出，停止当前任务。")
                await asyncio.sleep(0.25)
            if state.cancel_requested:
                raise HarnessError("CANCELLED", "任务已停止。")
            if exit_code == 124:
                raise HarnessError("TIMED_OUT", "Worker 总执行时限已到，容器已停止。")
            result = self._result_file(output / "result.json", request["limits"]["max_output_bytes"] + 64 * 1024)
            if exit_code != 0 and not result.get("error"):
                raise HarnessError("WORKER_FAILED", "Worker 异常退出，容器已停止。")
            result["execution_stopped"] = True
            result["timing"] = {"elapsed_seconds": round(time.time() - started, 3)}
            return redact_value(result, (secret,))
        except asyncio.CancelledError:
            stopped = await asyncio.shield(self._stop_state(state))
            raise
        except Exception as error:
            stopped = stopped or await self._stop_state(state)
            payload = error_payload(error, stage="model_execute")
            if not isinstance(error, HarnessError):
                payload.update(code="EXECUTOR_FAILED", message="执行器处理失败，查看脱敏诊断。")
            if not stopped:
                payload.update(code="TERMINATION_FAILED", message="无法确认整个执行器停止，后续任务已暂停。")
            return redact_value({"raw_text": "", "finish_reason": None, "usage": None, "error": payload, "execution_stopped": stopped, "timing": {"elapsed_seconds": round(time.time() - started, 3)}}, (secret,))
        finally:
            await asyncio.shield(self._release(state, stopped))

    async def recover(self) -> bool:
        options = asdict(self.config.executor)
        try:
            options["docker_host"] = await self._local_endpoint(options["docker_command"])
            targets = {(options["docker_command"], options["docker_host"]): options}
            # 管理员可能切换到另一个本机 context；旧任务必须在原 endpoint
            # 核对。正常退出已经持久确认的记录无需反复访问已卸载旧 daemon。
            for path in self.config.data_dir.glob("jobs/*/control_host/*/watchdog.json"):
                record = read_json(path)
                if record.get("owner_id") != self.owner_id:
                    continue
                already_stopped = False
                for marker in (path.parent / "confirmed_stopped.json", path.parent / "stopped.json"):
                    if marker.is_file():
                        confirmation = read_json(marker, 4096)
                        if confirmation.get("run_id") == record.get("run_id") and confirmation.get("execution_stopped") is True:
                            already_stopped = True
                if already_stopped:
                    continue
                endpoint, command = record.get("docker_host"), record.get("docker_command")
                if not is_local_docker_endpoint(endpoint) or not isinstance(command, str) or not command:
                    return False
                frozen = {**options, "docker_command": command, "docker_host": endpoint}
                creation_path = path.parent / "creation.json"
                if creation_path.is_file():
                    creation = read_json(creation_path, 4096)
                    if creation.get("run_id") != record.get("run_id"):
                        return False
                    if creation.get("resolved") is not True:
                        reference = record.get("container_id")
                        if not isinstance(reference, str) or not reference or not record.get("job_id"):
                            return False
                        pending = RunState(record["job_id"], record["run_id"], reference, path.parent, frozen, time.time(), creation_attempted=True, cancel_requested=True)
                        if not await self._stop_state(pending):
                            return False
                        write_json(path.parent / "confirmed_stopped.json", {"run_id": pending.run_id, "container_id": pending.container_id, "execution_stopped": True})
                targets[(command, endpoint)] = frozen
            for target in targets.values():
                code, identifiers = await self._docker(target, ["ps", "-a", "--no-trunc", "--filter", f"label={SERVICE_LABEL}=1", "--filter", f"label={OWNER_LABEL}={self.owner_id}", "--format", "{{.ID}}"])
                if code:
                    return False
                for container_id in identifiers.splitlines():
                    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
                        return False
                    code, raw = await self._docker(target, ["inspect", "--format", INSPECT_FORMAT, container_id])
                    if code:
                        return False
                    obj = json.loads(raw); labels = obj["labels"] or {}
                    if labels.get(OWNER_LABEL) != self.owner_id or labels.get(SERVICE_LABEL) != "1":
                        return False
                    state = RunState(labels.get(JOB_LABEL, ""), labels.get(RUN_LABEL, ""), "", self.config.data_dir, target, time.time(), container_id)
                    if not state.job_id or not state.run_id or not await self._stop_state(state):
                        return False
                    await self._docker(target, ["rm", container_id])
            # 同一服务内停止失败后明确取消会走 recover；资源确认清理后也
            # 释放内存槽和独立监护，避免数据库已恢复而 Runner 永久 busy。
            for active in list(self._active.values()):
                active.creation_resolved = True
                await self._release(active, True)
            return True
        except (HarnessError, OSError, ValueError, TypeError, KeyError):
            # 初次启动缺 Docker 且没有执行记录时由 manager/doctor 处理；
            # 本方法不能据 daemon 不可用谎报旧执行器已清理。
            return False

    async def close(self) -> bool:
        self._closed = True
        stopped_all = True
        for state in list(self._active.values()):
            state.cancel_requested = True
            self._lease(state)
            stopped = await self._stop_state(state)
            stopped_all &= stopped
            await self._release(state, stopped)
        return stopped_all
