"""预检、配置漂移与自适应查询的无模型验证。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig, load_config
from harness_mcp.errors import HarnessError, error_payload
from harness_mcp.service_state import configuration_state, poll_interval
from harness_mcp.task_manager import TaskManager
from harness_mcp.workspace import create_snapshot


class NoModel:
    def __init__(self):
        self.calls = 0

    async def recover(self):
        return True

    async def close(self):
        return True

    async def run(self, *args):
        self.calls += 1
        raise AssertionError("model must not be called")

    async def terminate(self, *args):
        return True


def project_config(tmp_path, text):
    project = tmp_path / "project"
    project.mkdir()
    (project / "auth.php").write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "auth.php"], check=True)
    return AppConfig(tmp_path / "state", {"demo": ProjectConfig("demo", project, allow_edits=True)},
                     model=ModelConfig(credential_source="env", credential_env="PREFLIGHT_TEST_KEY"))


def test_preflight_accepts_dynamic_php_and_creates_no_source_copy(tmp_path):
    config = project_config(tmp_path, "<?php\n$c = ['api_key' => null, 'password' => $password];\n")
    async def check():
        runner = NoModel()
        manager = TaskManager(config, runner)
        try:
            value = await manager.preflight("demo", ["auth.php"], "edit")
            assert value["passed"] and value["checked_files"] == 1 and value["model_called"] is False
            assert value["authorization_rechecked_on_submit"] is True
            assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert not list(config.data_dir.rglob("snapshot"))
            assert not list(config.data_dir.rglob("workspace"))
            assert runner.calls == 0
        finally:
            await manager.close()
        assert manager.closed
    asyncio.run(check())


def test_preflight_returns_only_rule_relative_path_and_line(tmp_path):
    secret = "artificial-short-password"
    config = project_config(tmp_path, "<?php\n$c = ['password' => '" + secret + "'];\n")
    async def check():
        manager = TaskManager(config, NoModel())
        try:
            with pytest.raises(HarnessError) as caught:
                await manager.preflight("demo", ["auth.php"], "edit")
            error = error_payload(caught.value)
            assert error["code"] == "SOURCE_SECRET_DETECTED"
            assert error["details"]["matches"] == [{"path": "auth.php", "line": 2, "rule": "SOURCE_HARDCODED_AUTH_LITERAL"}]
            assert secret not in json.dumps(error) and str(config.projects["demo"].path) not in json.dumps(error)
        finally:
            await manager.close()
    asyncio.run(check())


def test_actual_snapshot_rechecks_bytes_after_passed_preflight(tmp_path):
    config = project_config(tmp_path, "<?php\n$c = ['password' => null];\n")
    project = config.projects["demo"]
    create_snapshot(project, None, tmp_path / "dry", preflight_only=True)
    (project.path / "auth.php").write_text("<?php\n$c = ['password' => 'changed-literal'];\n", encoding="utf-8")
    with pytest.raises(Exception) as caught:
        create_snapshot(project, None, tmp_path / "snapshot", scan_sources=True)
    assert caught.value.code == "SOURCE_SECRET_DETECTED"
    assert not (tmp_path / "snapshot").exists()


def test_preparation_refreshes_rotated_frozen_credential_before_copy(tmp_path, monkeypatch):
    # 不具有 provider 前缀、没有凭证字段名的普通字符串，只能由真实引用的精确匹配发现。
    value = "artificial-rotated-exact-value"
    config = project_config(tmp_path, "<?php\n$ordinary = '" + value + "';\n")
    async def check():
        manager = TaskManager(config, NoModel())
        from harness_mcp.config import frozen_execution
        request = {"relative_paths": ["auth.php"], "mode": "review"}
        job = {"job_id": "credential-refresh", "request": request, "execution": frozen_execution(config, "demo")}
        monkeypatch.setenv("PREFLIGHT_TEST_KEY", value)
        try:
            with pytest.raises(HarnessError) as caught:
                await manager._snapshot(job, config.data_dir / "jobs/credential-refresh")
            assert caught.value.code == "SOURCE_SECRET_DETECTED"
            assert value in manager.secrets
            assert error_payload(caught.value)["details"]["matches"][0]["rule"] == "SOURCE_KNOWN_SECRET"
            assert not list(config.data_dir.rglob("snapshot"))
        finally:
            await manager.close()
    asyncio.run(check())


def test_automatic_prepare_rejects_credentials_before_model(tmp_path):
    config = project_config(tmp_path, "<?php\n$c = ['password' => 'fixed-literal'];\n")
    async def check():
        runner = NoModel()
        manager = TaskManager(config, runner)
        await manager.start()
        try:
            receipt = await manager.submit("demo", "check", "automatic-recheck")
            deadline = time.monotonic() + 5
            while manager.status(receipt["job_id"])["status"] not in {"failed", "succeeded"}:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            state = manager.status(receipt["job_id"])
            assert state["status"] == "failed" and state["error"]["code"] == "SOURCE_SECRET_DETECTED"
            assert runner.calls == 0
            assert not list(config.data_dir.rglob("snapshot"))
        finally:
            await manager.close()
    asyncio.run(check())


def test_config_fingerprint_separates_loaded_and_disk(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('data_dir="state"\ndefault_mode="review"\n', encoding="utf-8")
    loaded = load_config(path)
    state = configuration_state(loaded, path)
    assert state["config_state"] == "current" and state["restart_required"] is False
    assert len(state["config_version"]) == 64
    path.write_text('data_dir="state"\ndefault_mode="edit"\n', encoding="utf-8")
    changed = configuration_state(loaded, path)
    assert changed["config_state"] == "changed" and changed["restart_required"] is True
    assert changed["config_version"] == state["config_version"]
    assert changed["disk_config_version"] != state["config_version"]
    path.write_text('bad = [', encoding="utf-8")
    assert configuration_state(loaded, path)["config_state"] == "invalid"
    assert configuration_state(loaded, path)["restart_required"] is None
    assert configuration_state(loaded)["config_state"] == "unknown"


@pytest.mark.parametrize("age,expected", [(0,2), (9,2), (10,5), (29,5), (30,10), (119,10), (120,20), (600,20)])
def test_poll_intervals_backoff_without_sleep(age, expected):
    assert poll_interval({"status": "running", "phase": "reviewing", "started_at": 100}, 100 + age) == expected


def test_terminal_and_finish_polling():
    assert poll_interval({"status": "succeeded"}, 500) == 0
    assert poll_interval({"status": "cancelling", "created_at": 1}, 500) == 2
    assert poll_interval({"status": "running", "phase": "collecting_changes", "created_at": 1}, 500) == 2
