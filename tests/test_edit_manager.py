"""修改模式的真实准备/收集进程与受控模型；不调用真实模型或写原项目。"""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from harness_mcp.config import AppConfig, LimitsConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.process_control import spawn_owned
from harness_mcp.task_manager import TaskManager


BEFORE = "def divide(a, b):\n    return a / b\n"
AFTER = "def divide(a, b):\n    if b == 0:\n        raise ValueError('zero divisor')\n    return a / b\n"


ROTATED_CREDENTIAL_WORKER = r'''
import json, os
from pathlib import Path
from harness_mcp import harness_adapter
def controlled_run(request, snapshot, home, on_activity):
    on_activity('editing')
    # 人工凭证故意写进副本，用真实宿主收集链路验证精确拒绝；不调用 SDK/模型。
    value = os.environ[request['model']['credential_env']]
    (Path(request['workspace_dir']) / 'sample.py').write_bytes(("value = '" + value + "'\n").encode('utf-8'))
    return {'raw_text': json.dumps({'summary': 'controlled fixture', 'findings': [], 'limitations': []}),
            'finish_reason': 'completed', 'usage': None, 'error': None}
harness_adapter.run_harness = controlled_run
from harness_mcp.desktop_worker import main
main()
'''


@pytest.fixture
def editable_config(tmp_path):
    project = tmp_path / "源 项目"
    project.mkdir()
    (project / "sample.py").write_bytes(BEFORE.encode("utf-8"))
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "sample.py"], check=True)
    return AppConfig(tmp_path / "state", {"demo": ProjectConfig("demo", project, allow_edits=True)},
                     LimitsConfig(default_timeout_seconds=15, max_timeout_seconds=30),
                     ModelConfig(credential_source="env", credential_env="EDIT_FIXTURE_UNUSED_KEY"))


class EditFixtureRunner:
    def __init__(self, *, unsafe=False, delay=0, error=False, baseline_mutation=False):
        self.unsafe, self.delay, self.error, self.baseline_mutation = unsafe, delay, error, baseline_mutation
        self.started = asyncio.Event()
        self.calls = []

    async def recover(self):
        return True

    async def run(self, request, on_activity):
        self.calls.append(request)
        workspace = Path(request["workspace_dir"])
        (workspace / "sample.py").write_bytes(AFTER.encode("utf-8"))
        if self.unsafe:
            (workspace / ".env").write_text("SHOULD_NOT_EXPORT=yes", encoding="utf-8")
        if self.baseline_mutation:
            baseline = Path(request["snapshot_dir"]) / "sample.py"
            baseline.chmod(0o600)
            baseline.write_text("tampered", encoding="utf-8")
        on_activity("editing")
        self.started.set()
        await asyncio.sleep(self.delay)
        return {"raw_text": json.dumps({"summary": "修复零除数", "findings": [],
                                         "changed_files": ["fake.py"], "tests": "passed"}),
                "finish_reason": "completed", "usage": None,
                "error": {"code": "MODEL_ERROR", "message": "人工错误"} if self.error else None,
                "execution_stopped": True}

    async def terminate(self, _job_id):
        return True

    async def close(self):
        return True


async def finished(manager, job_id):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if status["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(manager.status(job_id))


def test_edit_returns_actual_diff_and_keeps_original_and_baseline(editable_config):
    async def check():
        runner = EditFixtureRunner()
        manager = TaskManager(editable_config, runner)
        await manager.start()
        try:
            receipt = await manager.submit("demo", "修复零除数", "edit-one", ["sample.py"], mode="edit")
            assert receipt["mode"] == "edit"
            assert (await finished(manager, receipt["job_id"]))["status"] == "succeeded"
            report = manager.result(receipt["job_id"])
            assert report["mode"] == "edit" and report["edit_ready"]
            assert report["changed_files"] == ["sample.py"] and report["tests"] == "not_run"
            assert not report["source_applied"] and report["requires_main_agent_review"]
            assert "zero divisor" in report["patch_text"]
            request = runner.calls[0]
            assert Path(request["snapshot_dir"]).joinpath("sample.py").read_text(encoding="utf-8") == BEFORE
            assert editable_config.projects["demo"].path.joinpath("sample.py").read_text(encoding="utf-8") == BEFORE
            payload = json.loads((editable_config.data_dir / "jobs" / receipt["job_id"] / "report.json").read_text(encoding="utf-8"))
            change = payload["changes"][0]
            assert change["after_text"] == AFTER
            assert change["after_sha256"] == hashlib.sha256(AFTER.encode("utf-8")).hexdigest()
            replay = await manager.submit("demo", "修复零除数", "edit-one", ["sample.py"], mode="edit")
            assert replay["idempotent_replay"] and len(runner.calls) == 1
            with pytest.raises(HarnessError) as exc:
                await manager.submit("demo", "修复零除数", "edit-one", ["sample.py"], mode="review")
            assert exc.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("unsafe,baseline_mutation", [(True, False), (False, True)])
def test_edit_rejects_forbidden_file_or_baseline_mutation(editable_config, unsafe, baseline_mutation):
    async def check():
        manager = TaskManager(editable_config, EditFixtureRunner(unsafe=unsafe, baseline_mutation=baseline_mutation))
        await manager.start()
        try:
            receipt = await manager.submit("demo", "修改", "unsafe", mode="edit")
            assert (await finished(manager, receipt["job_id"]))["status"] == "failed"
            assert not manager.result(receipt["job_id"])["ready"]
            assert editable_config.projects["demo"].path.joinpath("sample.py").read_text(encoding="utf-8") == BEFORE
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("cancel,error", [(True, False), (False, True)])
def test_stopped_or_failed_edits_not_exported_as_applicable(editable_config, cancel, error):
    async def check():
        runner = EditFixtureRunner(delay=100 if cancel else 0, error=error)
        manager = TaskManager(editable_config, runner)
        await manager.start()
        try:
            receipt = await manager.submit("demo", "修改", "stop-or-error", mode="edit")
            await asyncio.wait_for(runner.started.wait(), 5)
            if cancel:
                await manager.cancel(receipt["job_id"])
            assert (await finished(manager, receipt["job_id"]))["status"] == ("cancelled" if cancel else "failed")
            result = manager.result(receipt["job_id"])
            assert result["ready"] and not result["edit_ready"]
            assert result["changed_files"] == [] and result["patch_text"] == ""
            assert not result["source_applied"]
            assert editable_config.projects["demo"].path.joinpath("sample.py").read_text(encoding="utf-8") == BEFORE
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.skipif(os.name != "nt", reason="actual Windows named JobObject + production desktop runner")
def test_rotated_credential_rejected_by_real_manager_runner_collection(editable_config, tmp_path, monkeypatch):
    fixture = tmp_path / "controlled_worker.py"
    fixture.write_text(ROTATED_CREDENTIAL_WORKER, encoding="utf-8")
    old_value = "initial-opaque-artificial-credential-value"
    actual_value = "rotated-opaque-artificial-credential-value"
    monkeypatch.setenv(editable_config.model.credential_env, old_value)
    monkeypatch.setattr("harness_mcp.desktop_launcher.resolve_desktop_root", lambda _value: tmp_path / "人工 桌面安装")
    created = []

    def controlled_spawn(argv, environment, **kwargs):
        # 只替换 SDK 实现；实际执行器、Worker、进程归属、快照、收集与报告链路保留。
        assert actual_value in manager.secrets
        assert environment[editable_config.model.credential_env] == actual_value
        assert old_value not in json.dumps(argv) and actual_value not in json.dumps(argv)
        owned = spawn_owned([sys.executable, str(fixture)], environment, **kwargs)
        created.append((owned, environment))
        return owned

    monkeypatch.setattr("harness_mcp.desktop_runner.spawn_owned", controlled_spawn)

    async def check():
        nonlocal manager
        manager = TaskManager(editable_config)
        await manager.start()
        try:
            assert old_value in manager.secrets and actual_value not in manager.secrets
            monkeypatch.setenv(editable_config.model.credential_env, actual_value)
            receipt = await manager.submit("demo", "受控修改", "rotated-credential", ["sample.py"], mode="edit")
            status = await finished(manager, receipt["job_id"])
            assert status["status"] == "failed" and status["error"]["code"] == "EDIT_SECRET_DETECTED"
            result = manager.result(receipt["job_id"])
            assert not result["ready"]
            assert actual_value not in json.dumps(status) + json.dumps(result)
            directory = editable_config.data_dir / "jobs" / receipt["job_id"]
            assert not list(directory.glob("report*"))
            assert old_value in manager.secrets and actual_value in manager.secrets
            assert created and all(owned.tree_stopped for owned, _ in created)
            assert all(not Path(environment["HOME"]).exists() for _, environment in created)
            assert (directory / "snapshot" / "files" / "sample.py").read_bytes() == BEFORE.encode("utf-8")
            assert editable_config.projects["demo"].path.joinpath("sample.py").read_bytes() == BEFORE.encode("utf-8")
            for path in directory.rglob("*.json"):
                assert old_value not in path.read_text(encoding="utf-8") and actual_value not in path.read_text(encoding="utf-8")
        finally:
            await manager.close()

    manager = None
    asyncio.run(check())


@pytest.mark.parametrize("stop_reason", ["cancelled", "timed_out"])
def test_stop_during_change_collection_never_publishes_applicable_patch(editable_config, monkeypatch, stop_reason):
    async def check():
        manager = TaskManager(editable_config, EditFixtureRunner())
        entered = asyncio.Event()
        collecting_cancelled = asyncio.Event()
        original_collect = manager._edit_changes

        async def delayed_collect(job, snapshot, workspace):
            # 先真实收集得到合法差异，再让回执晚于停止请求，覆盖编辑结果发布边界。
            changes = await original_collect(job, snapshot, workspace)
            assert changes and "zero divisor" in json.dumps(changes)
            entered.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                collecting_cancelled.set()
                return changes

        monkeypatch.setattr(manager, "_edit_changes", delayed_collect)
        await manager.start()
        try:
            receipt = await manager.submit("demo", "受控修改", "stop-collection", ["sample.py"],
                                           timeout_seconds=2 if stop_reason == "timed_out" else 15, mode="edit")
            await asyncio.wait_for(entered.wait(), 5)
            assert manager.status(receipt["job_id"])["phase"] == "collecting_changes"
            if stop_reason == "cancelled":
                await manager.cancel(receipt["job_id"])
            status = await finished(manager, receipt["job_id"])
            assert status["status"] == stop_reason and collecting_cancelled.is_set()
            result = manager.result(receipt["job_id"])
            assert result["ready"] and not result["edit_ready"]
            assert result["changed_files"] == [] and result["patch_text"] == ""
            assert not result["source_applied"]
            directory = editable_config.data_dir / "jobs" / receipt["job_id"]
            persisted = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            assert not persisted["edit_ready"] and persisted["changes"] == [] and persisted["patch_text"] == ""
            assert not persisted["source_applied"]
            assert (directory / "snapshot" / "files" / "sample.py").read_bytes() == BEFORE.encode("utf-8")
            assert editable_config.projects["demo"].path.joinpath("sample.py").read_bytes() == BEFORE.encode("utf-8")
        finally:
            await manager.close()
    asyncio.run(check())
