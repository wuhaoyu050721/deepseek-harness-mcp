"""动态授权的共享归属回归：人工后台、不启动执行器或模型。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import secrets

import pytest

from harness_mcp.config import AppConfig, ProjectConfig, load_config
from harness_mcp.errors import HarnessError
from harness_mcp import doctor
from harness_mcp import service_diagnostics as diagnostics
from harness_mcp import shared_bootstrap as bootstrap
from harness_mcp.shared_client import SharedClient
from harness_mcp.shared_service import serve


def _write_config(path, data_dir, project_path, *, allow_edits=False,
                  allowed_paths=("src",), exclude_patterns=(), allow_untracked=()):
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    path.write_text(
        f"data_dir = {quote(data_dir)}\n"
        "[projects.p]\n"
        f"path = {quote(project_path)}\n"
        f"allow_edits = {str(allow_edits).lower()}\n"
        f"allowed_paths = {json.dumps(list(allowed_paths))}\n"
        f"exclude_patterns = {json.dumps(list(exclude_patterns))}\n"
        f"allow_untracked = {json.dumps(list(allow_untracked))}\n",
        encoding="utf-8",
    )
    return load_config(path)


@pytest.fixture
def isolated_transport(tmp_path, monkeypatch):
    # 主团队可能同时编写其他源文件；本用例固定人工源码身份以专测配置归属。
    source_dir = tmp_path / "module"
    source_dir.mkdir()
    (source_dir / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "__file__", str(source_dir / "shared_bootstrap.py"))
    # ACL 实测已有单独用例；人工目录只绕过 ACL 子进程，不放宽路径/身份检查。
    monkeypatch.setattr(bootstrap, "_permissions_safe", lambda *args, **kwargs: True)
    monkeypatch.setattr(bootstrap, "_restrict_permissions", lambda *args, **kwargs: None)
    monkeypatch.setattr(bootstrap, "_launch_daemon", lambda *args: pytest.fail("不得启动另一后台"))
    return tmp_path, source_dir


@pytest.mark.parametrize("permission, value", [
    ("allow_edits", True),
    ("allowed_paths", ("src", "tests")),
    ("exclude_patterns", ("**/private.*",)),
    ("allow_untracked", ("src/new.py",)),
])
def test_runtime_digest_excludes_only_project_authorizations(isolated_transport, permission, value):
    tmp_path, _ = isolated_transport
    config = AppConfig(tmp_path / "data", {"p": ProjectConfig("p", tmp_path / "p", ("src",))})
    changed = replace(config, projects={"p": replace(config.projects["p"], **{permission: value})})
    assert bootstrap.configuration_digest(config) == bootstrap.configuration_digest(changed)
    assert bootstrap.configuration_digest(config, tmp_path / "one.toml") == bootstrap.configuration_digest(changed, tmp_path / "one.toml")


@pytest.mark.parametrize("change", [
    "root", "project_added", "project_removed", "project_id", "model", "model_provider",
    "model_url", "credential_source", "credential_env", "data_dir", "limits", "executor",
    "default_mode", "completion",
])
def test_runtime_changes_still_change_digest(isolated_transport, change):
    tmp_path, _ = isolated_transport
    config = AppConfig(tmp_path / "data", {"p": ProjectConfig("p", tmp_path / "p")})
    if change == "root":
        changed = replace(config, projects={"p": replace(config.projects["p"], path=tmp_path / "other")})
    elif change == "project_added":
        changed = replace(config, projects={**config.projects, "q": ProjectConfig("q", tmp_path / "q")})
    elif change == "project_removed":
        changed = replace(config, projects={})
    elif change == "project_id":
        changed = replace(config, projects={"q": replace(config.projects["p"], id="q")})
    elif change in {"model", "model_provider", "model_url", "credential_source", "credential_env"}:
        field, value = {
            "model": ("model", "another-model"), "model_provider": ("provider", "another-provider"),
            "model_url": ("base_url", "https://example.org"),
            "credential_source": ("credential_source", "env"),
            "credential_env": ("credential_env", "ANOTHER_KEY"),
        }[change]
        changed = replace(config, model=replace(config.model, **{field: value}))
    elif change == "data_dir":
        changed = replace(config, data_dir=tmp_path / "another-data")
    elif change == "limits":
        changed = replace(config, limits=replace(config.limits, max_queue=4))
    elif change == "executor":
        changed = replace(config, executor=replace(config.executor, desktop_root=str(tmp_path / "desktop")))
    elif change == "default_mode":
        changed = replace(config, default_mode="edit")
    else:
        changed = replace(config, completion=replace(config.completion, auto_continue=True))
    assert bootstrap.configuration_digest(config, tmp_path / "one.toml") != bootstrap.configuration_digest(changed, tmp_path / "one.toml")


def test_config_source_path_is_pinned_and_normalized(isolated_transport):
    tmp_path, _ = isolated_transport
    config = AppConfig(tmp_path / "data")
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    assert bootstrap.configuration_digest(config, one) != bootstrap.configuration_digest(config, two)
    assert bootstrap.configuration_digest(config, one) == bootstrap.configuration_digest(config, tmp_path / "unused" / ".." / "one.toml")
    assert bootstrap.configuration_digest(config) != bootstrap.configuration_digest(config, one)


def test_source_and_python_identity_still_change_runtime_digest(isolated_transport, monkeypatch):
    tmp_path, source_dir = isolated_transport
    config = AppConfig(tmp_path / "data")
    baseline = bootstrap.configuration_digest(config, tmp_path / "one.toml")
    (source_dir / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")
    changed_source = bootstrap.configuration_digest(config, tmp_path / "one.toml")
    assert changed_source != baseline
    monkeypatch.setattr(bootstrap.sys, "executable", str(tmp_path / "another-python.exe"))
    assert bootstrap.configuration_digest(config, tmp_path / "one.toml") != changed_source


class _Store:
    def has_pending(self):
        return False


class _Manager:
    def __init__(self, config):
        self.config = config
        self.closed = False
        self.secrets = []
        self.store = _Store()
        self.executions = 0

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    def service_info(self):
        return {"active_tasks": 0, "queued_tasks": 0, "config_state": "current"}

    def submit(self, **kwargs):
        self.executions += 1
        pytest.fail("连接/诊断测试不能提交任务")


@asynccontextmanager
async def _running_service(tmp_path):
    config_path = tmp_path / "trusted.toml"
    config = _write_config(config_path, tmp_path / "state", tmp_path / "project")
    managers = []

    def factory(value):
        manager = _Manager(value)
        managers.append(manager)
        return manager

    runtime = config.data_dir / "shared-service"
    task = asyncio.create_task(serve(config, runtime, secrets.token_hex(32),
        bootstrap.configuration_digest(config, config_path), idle_seconds=0.1,
        manager_factory=factory, config_path=config_path))
    try:
        for _ in range(200):
            if task.done():
                await task
            if (runtime / "registry.json").exists():
                registry = bootstrap.read_private_json(runtime / "registry.json")
                yield config, config_path, managers, registry
                return
            await asyncio.sleep(0.01)
        raise AssertionError("人工后台未登记")
    finally:
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 3)
            except asyncio.TimeoutError:
                task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_existing_and_new_clients_reuse_backend_after_all_authorization_changes(isolated_transport):
    tmp_path, _ = isolated_transport

    async def scenario():
        async with _running_service(tmp_path) as (config, path, managers, registry):
            old = SharedClient(config, path)
            new = None
            try:
                await old.start()
                changed = _write_config(path, config.data_dir, config.projects["p"].path,
                    allow_edits=True, allowed_paths=("src", "tests"),
                    exclude_patterns=("**/private.*",), allow_untracked=("src/new.py",))
                new = SharedClient(changed, path)
                await new.start()
                old_info, new_info = await asyncio.gather(old.service_info(), new.service_info())
                assert old_info["runtime_version"] == new_info["runtime_version"] == registry["config_digest"]
                assert old_info["active_connections"] == new_info["active_connections"] == 2
                assert len(managers) == 1 and managers[0].executions == 0
                assert managers[0].config_path == path.resolve()
            finally:
                await old.close()
                if new is not None:
                    await new.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["another_file", "another_root"])
def test_other_config_source_or_root_cannot_attach_to_existing_backend(isolated_transport, change):
    tmp_path, _ = isolated_transport

    async def scenario():
        async with _running_service(tmp_path) as (config, path, managers, _):
            original = SharedClient(config, path)
            rejected = None
            try:
                await original.start()
                other = tmp_path / "another.toml" if change == "another_file" else path
                modified = _write_config(other, config.data_dir,
                    tmp_path / "different-project" if change == "another_root" else config.projects["p"].path,
                    allow_edits=True)
                rejected = SharedClient(modified, other)
                with pytest.raises(HarnessError) as error:
                    await rejected.start()
                assert error.value.code == "SHARED_CONFIG_MISMATCH"
                assert (await original.service_info())["active_connections"] == 1
                assert len(managers) == 1 and managers[0].executions == 0
            finally:
                await original.close()
                if rejected is not None:
                    await rejected.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("forbidden", ["allow_edits", "allowed_paths", "exclude_patterns", "allow_untracked", "config_path"])
def test_rpc_cannot_supply_authorizations_or_other_source(isolated_transport, forbidden):
    tmp_path, _ = isolated_transport

    async def scenario():
        async with _running_service(tmp_path) as (config, path, managers, _):
            client = SharedClient(config, path)
            try:
                await client.start()
                with pytest.raises(HarnessError) as error:
                    await client._call("submit", {"project_id": "p", "prompt": "p", "client_request_id": "fixture", forbidden: True})
                assert error.value.code in {"SHARED_PROTOCOL_ERROR", "SHARED_PROTOCOL_INVALID", "SHARED_CONNECTION_LOST"}
                assert managers[0].executions == 0
            finally:
                await client.close()

    asyncio.run(scenario())


def test_diagnostics_pure_permission_change_is_readonly_and_no_restart_required(isolated_transport):
    tmp_path, _ = isolated_transport

    async def scenario():
        async with _running_service(tmp_path) as (config, path, managers, _):
            changed = _write_config(path, config.data_dir, config.projects["p"].path, allow_edits=True)
            result = await diagnostics.inspect_existing_service(changed, path)
            assert result["state"] == "connected" and result["restart_required"] is False
            assert result["active_connections"] == 0 and result["active_tasks"] == 0
            assert result["queued_tasks"] == 0 and result["counts_available"] is True
            assert managers[0].executions == 0 and not managers[0].closed

    asyncio.run(scenario())


def test_serve_rejects_config_digest_from_other_source_before_manager_creation(isolated_transport):
    tmp_path, _ = isolated_transport
    config = _write_config(tmp_path / "one.toml", tmp_path / "data", tmp_path / "p")
    constructed = []
    with pytest.raises(HarnessError) as error:
        asyncio.run(serve(config, config.data_dir / "shared-service", secrets.token_hex(32),
            bootstrap.configuration_digest(config, tmp_path / "another.toml"),
            manager_factory=lambda value: constructed.append(value), config_path=tmp_path / "one.toml"))
    assert error.value.code == "SHARED_CONFIG_MISMATCH" and not constructed


def test_doctor_passes_canonical_config_source_to_readonly_diagnostics(isolated_transport, monkeypatch, capsys):
    tmp_path, _ = isolated_transport
    path = tmp_path / "one.toml"
    _write_config(path, tmp_path / "data", tmp_path / "p")
    observed = []
    monkeypatch.setattr(doctor, "collect_checks", lambda config: {
        "blocking": [], "execution_ready": False,
        "credential_presence": {"configured_reference_available": False}})
    monkeypatch.setattr(diagnostics, "service_diagnostics", lambda config, config_path: observed.append(config_path) or {"restart_required": False})
    assert doctor.main(["--config", str(path)]) == 0
    assert observed == [path.resolve()]
    assert json.loads(capsys.readouterr().out)["service_state"]["restart_required"] is False
