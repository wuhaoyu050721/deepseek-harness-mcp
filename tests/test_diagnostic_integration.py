"""隔离故障注入贯通诊断；只使用本地 fixture，不启动真实后台或调用模型。"""

import asyncio
from io import BytesIO
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness_mcp.config import AppConfig, LimitsConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError, error_from_payload, error_payload
from harness_mcp.job_store import TERMINAL
from harness_mcp.job_store import JobStore
from harness_mcp.parallel_manager import ParallelTaskManager
from harness_mcp.shared_client import SharedClient, read_frame, write_frame, RPC_MAX_BYTES
from harness_mcp.shared_service import _SharedService
from harness_mcp.task_manager import TaskManager
from harness_mcp.workspace import create_snapshot


RAW_DIAGNOSTIC = "C:/Users/private/key.pem secret-fixture-value"


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def result():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "main.py"], check=True)
    return AppConfig(tmp_path / "state", {"demo": ProjectConfig("demo", project, allow_edits=True)},
                     LimitsConfig(default_timeout_seconds=15, max_timeout_seconds=30, max_parallel_jobs=2),
                     ModelConfig(credential_source="env", credential_env="DIAGNOSTIC_FIXTURE_UNUSED_KEY"))


class _Runner:
    def __init__(self):
        self.calls = []

    async def recover(self):
        return True

    async def recover_job(self, _job_id):
        return True

    async def run(self, request, _on_activity):
        self.calls.append(request)
        return {"raw_text": json.dumps({"summary": "local fixture", "findings": []}),
                "finish_reason": "completed", "error": None, "execution_stopped": True}

    async def terminate(self, _job_id):
        return True

    async def close(self):
        return True


def _snapshot(config):
    return create_snapshot(config.projects["demo"], None, config.data_dir / "prepared-fixture",
                           max_file_bytes=config.limits.max_file_bytes,
                           max_total_bytes=config.limits.max_snapshot_bytes)


async def _terminal(manager, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.store.get(job_id)
        if job["status"] in TERMINAL:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("fixture job did not finish")


@pytest.mark.parametrize("stage", [
    "job_directory", "snapshot_prepare", "snapshot_validate", "model_execute",
    "post_model_snapshot_validate", "changes_collect", "report_build", "report_save", "finish_commit",
])
def test_task_failure_records_exact_stage_and_safe_diagnostic(config, monkeypatch, stage):
    async def check():
        snapshot = _snapshot(config)
        runner = _Runner()
        manager = TaskManager(config, runner)
        manager.accepting = True
        manager._snapshot = AsyncMock(return_value=snapshot)
        receipt = await manager.submit("demo", "fixture", stage, mode="edit" if stage == "changes_collect" else "review")
        job = manager.store.next_job()
        error = PermissionError(RAW_DIAGNOSTIC)
        if stage == "job_directory":
            original = Path.mkdir
            destination = config.data_dir / "jobs" / receipt["job_id"]

            def failing_mkdir(path, *args, **kwargs):
                if path == destination:
                    raise error
                return original(path, *args, **kwargs)

            monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        elif stage == "snapshot_prepare":
            manager._snapshot = AsyncMock(side_effect=error)
        elif stage in {"snapshot_validate", "post_model_snapshot_validate"}:
            from harness_mcp import workspace
            monkeypatch.setattr(workspace, "verify_snapshot", MagicMock(side_effect=error if stage == "snapshot_validate" else [None, error]))
        elif stage == "model_execute":
            runner.run = AsyncMock(side_effect=error)
        elif stage == "changes_collect":
            manager._edit_changes = AsyncMock(side_effect=error)
        elif stage in {"report_build", "report_save"}:
            from harness_mcp import report
            monkeypatch.setattr(report, "build_report" if stage == "report_build" else "save_report", MagicMock(side_effect=error))
        elif stage == "finish_commit":
            original = manager.store.finish
            failed_once = False

            def failing_finish(job_id, status, **kwargs):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise error
                return original(job_id, status, **kwargs)

            monkeypatch.setattr(manager.store, "finish", failing_finish)
        try:
            await manager._execute(job)
            failed = manager.store.get(receipt["job_id"])
            assert failed["status"] == "failed"
            assert failed["error"]["stage"] == stage
            assert failed["error"]["exception_type"] == "PermissionError"
            assert len(failed["error"]["diagnostic_id"]) == 32
            assert failed["error"] == error_payload(error)
            assert RAW_DIAGNOSTIC not in json.dumps(failed["error"])
            assert "private/key.pem" not in json.dumps(failed["error"])
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("parallel", [False, True])
def test_mkdir_failure_is_terminal_and_next_job_still_runs(config, monkeypatch, parallel):
    async def check():
        snapshot = _snapshot(config)
        runners = []

        def factory(_job_id):
            runner = _Runner()
            runners.append(runner)
            return runner

        manager = ParallelTaskManager(config, factory) if parallel else TaskManager(config, factory(None))
        monkeypatch.setattr(TaskManager, "_snapshot", AsyncMock(return_value=snapshot))
        original = Path.mkdir
        failed_once = False

        def failing_mkdir(path, *args, **kwargs):
            nonlocal failed_once
            if path.parent.name == "jobs" and not failed_once:
                failed_once = True
                raise PermissionError(RAW_DIAGNOSTIC)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        await manager.start()
        try:
            first = await manager.submit("demo", "fixture", "first")
            second = await manager.submit("demo", "fixture", "second")
            assert (await _terminal(manager, first["job_id"]))["error"]["stage"] == "job_directory"
            assert (await _terminal(manager, second["job_id"]))["status"] == "succeeded"
            assert sum(len(runner.calls) for runner in runners) == 1
            assert manager.scheduler is not None and not manager.scheduler.done()
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("method", ["_snapshot", "_edit_changes"])
def test_worker_failure_reconstruction_preserves_first_diagnostic(config, monkeypatch, method):
    async def check():
        manager = TaskManager(config, _Runner())
        manager.accepting = True
        await manager.submit("demo", "fixture", method)
        job = manager.store.next_job()
        first = error_payload(HarnessError("SOURCE_SECRET_DETECTED", "源码检查失败。", details={
            "matches": [{"path": "main.py", "line": 1, "rule": "SOURCE_PRIVATE_KEY"}],
            "total_matches": 1, "truncated": False,
        }), stage="preflight")

        class _Process:
            returncode = 0

            def communicate(self, _input):
                return json.dumps({"ok": False, "error": first}).encode("utf-8"), b""

        monkeypatch.setattr(manager, "_spawn_preparation", lambda *_args: _Process())
        monkeypatch.setattr(manager, "_stop_preparation", AsyncMock(return_value=True))
        try:
            with pytest.raises(HarnessError) as raised:
                if method == "_snapshot":
                    await manager._snapshot(job, config.data_dir / "job-fixture")
                else:
                    await manager._edit_changes(job, SimpleNamespace(to_dict=lambda: {}), config.data_dir / "workspace-fixture")
            assert error_payload(raised.value, stage="worker_response") == first
            manager.store.finish(job["job_id"], "failed", error=first)
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("module,stage", [("snapshot_worker", "snapshot_prepare"), ("edit_collect_worker", "changes_collect")])
def test_worker_main_generates_bounded_diagnostic_for_bad_input(monkeypatch, module, stage):
    from importlib import import_module
    worker = import_module("harness_mcp." + module)
    stdout = BytesIO()
    monkeypatch.setattr(worker, "sys", SimpleNamespace(
        stdin=SimpleNamespace(buffer=BytesIO(b'{"private": "secret-fixture-value"}')),
        stdout=SimpleNamespace(buffer=stdout),
    ))
    worker.main()
    result = json.loads(stdout.getvalue())
    assert result["ok"] is False
    assert result["error"]["stage"] == stage
    assert len(result["error"]["diagnostic_id"]) == 32
    assert "secret-fixture-value" not in stdout.getvalue().decode("utf-8")


def test_parallel_unhandled_task_error_is_failed_after_scoped_stop(config):
    async def check():
        manager = ParallelTaskManager(config, lambda _job_id: _Runner())
        manager.accepting = True
        await manager.submit("demo", "fixture", "escaped")
        job = manager.store.next_parallel_job(set(), 2)
        error = RuntimeError(RAW_DIAGNOSTIC)

        async def failing():
            raise error

        task = asyncio.create_task(failing())
        await asyncio.wait({task})
        context = SimpleNamespace(execution_task=task, close_owned=AsyncMock(return_value=True), ownership_failed=False)
        manager.contexts[job["job_id"]] = context
        try:
            assert await manager._finish_context(job["job_id"], context)
            failed = manager.store.get(job["job_id"])
            assert failed["status"] == "failed"
            assert failed["error"]["stage"] == "worker_response"
            assert failed["error"]["exception_type"] == "RuntimeError"
            assert manager.contexts == {}
        finally:
            await manager.close()
    asyncio.run(check())


def test_parallel_unconfirmed_stop_and_retry_keep_original_failure(config):
    async def check():
        manager = ParallelTaskManager(config, lambda _job_id: _Runner())
        manager.accepting = True
        await manager.submit("demo", "fixture", "escaped-stop")
        job = manager.store.next_parallel_job(set(), 2)
        error = RuntimeError(RAW_DIAGNOSTIC)

        async def failing():
            raise error

        task = asyncio.create_task(failing())
        await asyncio.wait({task})
        context = SimpleNamespace(execution_task=task, close_owned=AsyncMock(side_effect=[False, True]), ownership_failed=False)
        manager.contexts[job["job_id"]] = context
        try:
            assert not await manager._finish_context(job["job_id"], context)
            blocked = manager.store.get(job["job_id"])
            assert blocked["status"] == "cancelling" and blocked["termination_failed"]
            original = blocked["error"]
            assert original["stage"] == "worker_response"
            assert original["exception_type"] == "RuntimeError"
            assert await manager._finish_context(job["job_id"], context)
            recovered = manager.store.get(job["job_id"])
            assert recovered["status"] == "interrupted" and not recovered["termination_failed"]
            assert recovered["error"] == original
        finally:
            await manager.close()
    asyncio.run(check())


def test_task_stop_exception_does_not_replace_first_failure(config):
    async def check():
        runner = _Runner()
        runner.terminate = AsyncMock(side_effect=RuntimeError("stop failure secret"))
        manager = TaskManager(config, runner)
        manager.accepting = True
        await manager.submit("demo", "fixture", "stop-exception")
        job = manager.store.next_job()
        original_error = PermissionError(RAW_DIAGNOSTIC)
        manager._snapshot = AsyncMock(side_effect=original_error)
        try:
            await manager._execute(job)
            blocked = manager.store.get(job["job_id"])
            assert blocked["status"] == "cancelling" and blocked["termination_failed"]
            assert blocked["error"] == error_payload(original_error, stage="snapshot_prepare")
            assert blocked["error"]["exception_type"] == "PermissionError"
            assert "stop failure secret" not in json.dumps(blocked["error"])
            runner.terminate = AsyncMock(return_value=True)
            manager.store.recover_interrupted(True)
            recovered = manager.store.get(job["job_id"])
            assert recovered["status"] == "interrupted" and not recovered["termination_failed"]
            assert recovered["error"] == blocked["error"]
        finally:
            runner.terminate = AsyncMock(return_value=True)
            await manager.close()
    asyncio.run(check())


def test_job_store_repeated_stop_and_restart_preserve_only_valid_diagnostics(tmp_path):
    directory = tmp_path / "store"
    store = JobStore(directory)

    def submit(request_id):
        request = {"project_id": "demo", "prompt": "fixture", "client_request_id": request_id,
                   "relative_paths": None, "timeout_seconds": 600}
        return store.submit(request, request_id, {}, 5)[0]

    original = error_payload(PermissionError(RAW_DIAGNOSTIC), stage="report_save")
    first = submit("first")
    legacy = submit("legacy")
    malformed = submit("malformed")
    store.termination_failed(first["job_id"], error=original)
    store.termination_failed(first["job_id"])
    assert store.get(first["job_id"])["error"] == original
    store.termination_failed(first["job_id"], error=error_payload(RuntimeError("later error"), stage="shared_rpc"))
    assert store.get(first["job_id"])["error"] == original
    store.termination_failed(legacy["job_id"])
    assert store.get(legacy["job_id"])["error"]["code"] == "TERMINATION_FAILED"
    store.termination_failed(malformed["job_id"], error={"code": "BAD CODE", "message": "secret", "diagnostic_id": "a" * 32})
    assert store.get(malformed["job_id"])["error"]["code"] == "TERMINATION_FAILED"
    store.close()
    restarted = JobStore(directory)
    try:
        restarted.recover_interrupted(False)
        assert restarted.get(first["job_id"])["error"] == original
        restarted.recover_interrupted(True)
        assert restarted.get(first["job_id"])["error"] == original
        assert restarted.get(first["job_id"])["status"] == "interrupted"
        assert restarted.get(legacy["job_id"])["error"] == {
            "code": "SERVICE_RESTARTED", "message": "服务重启，旧任务未自动重放。",
        }
        assert restarted.get(malformed["job_id"])["error"]["code"] == "SERVICE_RESTARTED"
    finally:
        restarted.close()


def test_rpc_frame_client_and_server_keep_first_diagnostic(config):
    async def check():
        original = error_payload(PermissionError(RAW_DIAGNOSTIC), stage="snapshot_prepare")
        manager = SimpleNamespace(secrets=[], status=MagicMock(side_effect=error_from_payload(original)))
        service = _SharedService(config, manager, "a" * 64, "b" * 64, 30)
        completed = asyncio.Event()

        async def handle(reader, writer):
            try:
                request = await read_frame(reader, max_bytes=RPC_MAX_BYTES)
                response = await service._dispatch(request)
                await write_frame(writer, response, max_bytes=RPC_MAX_BYTES)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                completed.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        client = SharedClient(config, config_path=config.data_dir / "unused-fixture.toml")
        try:
            client.reader, client.writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            with pytest.raises(HarnessError) as raised:
                await client.status("fixture-job")
            assert error_payload(raised.value, stage="tool_request") == original
        finally:
            await client.close()
            await asyncio.wait_for(completed.wait(), 2)
            server.close()
            await server.wait_closed()
    asyncio.run(check())


def test_mcp_unknown_error_has_tool_stage_and_forwarded_error_keeps_worker_stage(config):
    async def check():
        from harness_mcp.server import create_server
        manager = MagicMock()
        manager.secrets = []
        manager.status = AsyncMock(side_effect=ValueError(RAW_DIAGNOSTIC))
        server = create_server(config, manager=manager)
        first = await server.call_tool("harness_status", {"job_id": "fixture"})
        assert first.structured_content["error"]["stage"] == "tool_request"
        assert first.structured_content["error"]["exception_type"] == "ValueError"
        forwarded = error_payload(PermissionError(RAW_DIAGNOSTIC), stage="report_save")
        manager.status.side_effect = error_from_payload(forwarded)
        second = await server.call_tool("harness_status", {"job_id": "fixture"})
        assert second.structured_content["error"] == forwarded
    asyncio.run(check())


def test_desktop_worker_encoding_keeps_diagnostic_and_legacy_worker_failed_code(tmp_path, monkeypatch):
    from harness_mcp import desktop_worker
    snapshot, home = tmp_path / "snapshot", tmp_path / "home"
    snapshot.mkdir()
    home.mkdir()
    request = {"deadline_at": time.time() + 60, "executor": {"backend": "desktop"},
               "snapshot_dir": str(snapshot), "_desktop_home": str(home),
               "model": {"credential_env": "DIAGNOSTIC_FIXTURE_UNUSED_KEY"}}
    monkeypatch.setattr(desktop_worker, "owned_by_job", lambda _name: True)
    with patch("harness_mcp.harness_adapter.run_harness", side_effect=RuntimeError(RAW_DIAGNOSTIC)):
        result = desktop_worker.execute(request)
    assert result["error"]["code"] == "WORKER_FAILED"
    assert result["error"]["exception_type"] == "RuntimeError"
    assert result["error"]["stage"] == "model_execute"
    encoded = json.loads(desktop_worker.encode_result(result, request))
    assert encoded["error"] == result["error"]
    assert "secret-fixture-value" not in json.dumps(encoded)


def test_sdk_adapter_to_desktop_and_docker_results_preserve_id_and_controlled_type(config, tmp_path):
    from harness_mcp.desktop_runner import DesktopRunner
    from harness_mcp.desktop_worker import encode_result
    from harness_mcp.harness_adapter import _safe_error
    from harness_mcp.runner import DockerRunner

    class PrivateSDKFailure(Exception):
        code = "RAW_PRIVATE_SDK_CODE"
        message = RAW_DIAGNOSTIC

    first = _safe_error(PrivateSDKFailure(RAW_DIAGNOSTIC))
    assert first["code"] == "HARNESS_RUNTIME_ERROR"
    assert first["exception_type"] == "UnexpectedException"
    assert first["stage"] == "model_execute"
    worker_result = {"raw_text": "", "finish_reason": None, "usage": None, "error": first,
                     "unknown": RAW_DIAGNOSTIC}
    request = {"model": {"credential_env": "DIAGNOSTIC_FIXTURE_UNUSED_KEY"}}
    encoded = encode_result(worker_result, request)
    desktop = DesktopRunner(config, "c" * 32)
    assert desktop._result(encoded, 10000, "", 1)["error"] == first
    file = tmp_path / "worker-result.json"
    file.write_bytes(encoded)
    docker = DockerRunner(config, "c" * 32)
    assert docker._result_file(file, 10000)["error"] == first
    assert RAW_DIAGNOSTIC not in encoded.decode("utf-8")


@pytest.mark.parametrize("update", [
    {"raw_exception": RAW_DIAGNOSTIC}, {"stage": RAW_DIAGNOSTIC},
    {"diagnostic_id": "private-id"}, {"exception_type": "PrivateSDKClass"},
    {"details": RAW_DIAGNOSTIC},
])
def test_docker_result_rejects_unknown_or_invalid_diagnostic_envelope(config, tmp_path, update):
    from harness_mcp.runner import DockerRunner
    first = error_payload(PermissionError(RAW_DIAGNOSTIC), stage="model_execute")
    file = tmp_path / "worker-result.json"
    file.write_text(json.dumps({"raw_text": "", "finish_reason": None, "usage": None,
                                "error": {**first, **update}}), encoding="utf-8")
    with pytest.raises(HarnessError) as raised:
        DockerRunner(config, "c" * 32)._result_file(file, 10000)
    assert raised.value.code == "WORKER_RESULT_INVALID"


def test_shared_startup_record_keeps_controlled_diagnostic_without_original_exception(tmp_path, monkeypatch):
    from harness_mcp import shared_bootstrap, shared_service
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap = {"version": 1, "config_path": str(config_path.resolve()),
                 "startup_id": "a" * 32, "config_digest": "b" * 64,
                 "token": "c" * 64, "nonsecret_environment": {}}
    written = {}
    error = PermissionError(RAW_DIAGNOSTIC)
    monkeypatch.setattr(shared_bootstrap, "prepare_private_runtime", lambda _path: None)
    monkeypatch.setattr(shared_bootstrap, "read_private_json", lambda _path: bootstrap)
    monkeypatch.setattr(shared_bootstrap, "write_private_json", lambda path, value: written.update({path.name: value}))
    monkeypatch.setattr(shared_service, "load_config", MagicMock(side_effect=error))
    with patch("sys.argv", ["fixture", "--config", str(config_path), "--bootstrap", str(bootstrap_path)]):
        with pytest.raises(SystemExit) as raised:
            shared_service.main()
    assert raised.value.code == 1
    record = written["startup-error.json"]
    assert record["diagnostic_id"] == error_payload(error)["diagnostic_id"]
    assert record["stage"] == "startup"
    assert record["exception_type"] == "PermissionError"
    assert record["startup_id"] == bootstrap["startup_id"]
    assert RAW_DIAGNOSTIC not in json.dumps(record)


@pytest.mark.parametrize("error,code,exception_type", [
    (TimeoutError(RAW_DIAGNOSTIC), "SDK_REQUEST_TIMEOUT", "TimeoutError"),
    (ModuleNotFoundError(RAW_DIAGNOSTIC), "SDK_UNAVAILABLE", "ModuleNotFoundError"),
    (HarnessError("INVALID_EXECUTOR_PATH", "执行代码目录不存在。"), "INVALID_EXECUTOR_PATH", "HarnessError"),
])
def test_adapter_diagnostic_keeps_existing_business_codes(error, code, exception_type):
    from harness_mcp.harness_adapter import _safe_error
    result = _safe_error(error)
    assert result["code"] == code
    assert result["exception_type"] == exception_type
    assert result["stage"] == "model_execute"
    assert len(result["diagnostic_id"]) == 32
    assert RAW_DIAGNOSTIC not in json.dumps(result)
