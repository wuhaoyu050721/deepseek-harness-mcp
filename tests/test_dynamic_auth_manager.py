"""可信文件权限热更新的管理器边界；隔离短目录，人工执行器不调用模型。"""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from harness_mcp.config import load_config, normalize_request
from harness_mcp.errors import HarnessError
from harness_mcp.parallel_manager import ParallelTaskManager
from harness_mcp.service_state import config_version
from harness_mcp.task_manager import TaskManager
from harness_mcp.workspace import Snapshot


def _toml(value):
    # 夹具只写可信配置中的已知标量/列表；不写凭证值，不依赖生产配置。
    lines = []
    for key in ("data_dir", "default_mode"):
        lines.append(f"{key} = {json.dumps(value[key])}")
    for section in ("limits", "model", "executor", "completion"):
        lines.append(f"[{section}]")
        for key, field in value[section].items():
            lines.append(f"{key} = {json.dumps(field)}")
    for project_id, project in value["projects"].items():
        lines.append(f"[projects.{project_id}]")
        for key, field in project.items():
            lines.append(f"{key} = {json.dumps(field)}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def trusted():
    # 避免 Windows 中 pytest 测试名叠加两个作业 UUID 超过传统路径限制。
    with tempfile.TemporaryDirectory(prefix="da-") as folder:
        root = Path(folder)
        project = root / "p"
        project.mkdir()
        (project / "src").mkdir()
        (project / "tests").mkdir()
        (project / "src/main.py").write_bytes(b"value = 1\n")
        (project / "tests/test_main.py").write_bytes(b"assert True\n")
        source = {
            "data_dir": str(root / "d"),
            "default_mode": "review",
            "limits": {"max_queue": 5, "max_parallel_jobs": 2,
                       "default_timeout_seconds": 30, "max_timeout_seconds": 60},
            "model": {"credential_source": "env", "credential_env": "DYNAMIC_AUTH_UNUSED_KEY"},
            "executor": {"backend": "desktop"},
            "completion": {"auto_continue": False},
            "projects": {"demo": {"path": str(project), "allowed_paths": ["src"],
                                   "exclude_patterns": [], "allow_untracked": [], "allow_edits": True}},
        }
        path = root / "c.toml"
        path.write_text(_toml(source), encoding="utf-8")
        yield SimpleNamespace(root=root, project=project, path=path, source=source)


class ArtificialRunner:
    def __init__(self):
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def recover(self):
        return True

    async def close(self):
        return True

    async def terminate(self, job_id):
        self.release.set()
        return True

    async def recover_job(self, job_id):
        return True

    async def run(self, request, on_activity):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return {"raw_text": '{"summary":"人工执行器报告","findings":[]}',
                "finish_reason": "completed", "execution_stopped": True}


def _manager(trusted, kind):
    config = load_config(trusted.path)
    runner = ArtificialRunner()
    manager = (TaskManager(config, runner) if kind == "single"
               else ParallelTaskManager(config, lambda job_id: runner))
    manager.config_path = trusted.path
    manager.accepting = True
    return manager, runner


def _write(trusted, mutate):
    value = deepcopy(trusted.source)
    mutate(value)
    trusted.path.write_text(_toml(value), encoding="utf-8")
    return load_config(trusted.path)


def _jobs(manager):
    return manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_permission_refresh_applies_four_fields_to_new_jobs_without_restart(trusted, kind):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            original = await manager.submit("demo", "旧范围", "before", ["src/main.py"], mode="edit")
            before = manager.store.get(original["job_id"])["execution"]["project"]
            version = manager.service_info()["permissions_version"]

            def change(value):
                value["projects"]["demo"].update(allowed_paths=["tests"], exclude_patterns=["*private*"],
                                                allow_untracked=["tests/generated.py"], allow_edits=False)

            disk = _write(trusted, change)
            fresh = await manager.submit("demo", "新范围", "after", ["tests/test_main.py"], mode="review")
            execution = manager.store.get(fresh["job_id"])["execution"]["project"]
            assert execution["allowed_paths"] == ["tests"]
            assert execution["exclude_patterns"] == ["*private*"]
            assert execution["allow_untracked"] == ["tests/generated.py"]
            assert execution["allow_edits"] is False
            assert manager.store.get(original["job_id"])["execution"]["project"] == before
            assert before["allow_edits"] is True and before["allowed_paths"] == ["src"]
            info = manager.service_info()
            assert info["permissions_hot_reload"] is True
            assert info["permissions_version"] != version
            assert info["config_version"] == info["disk_config_version"] == config_version(disk)
            assert info["restart_required"] is False and info["new_task_permissions_ready"] is True
            with pytest.raises(HarnessError) as edit:
                await manager.submit("demo", "禁止新修改", "denied-edit", ["tests"], mode="edit")
            assert edit.value.code == "EDIT_NOT_ALLOWED"
            with pytest.raises(HarnessError) as scope:
                await manager.submit("demo", "禁止旧范围", "denied-scope", ["src/main.py"], mode="review")
            assert scope.value.code == "PATH_NOT_AUTHORIZED"
            assert _jobs(manager) == 2 and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_permission_refresh_can_grant_copy_edit_without_restart(trusted, kind):
    trusted.source["projects"]["demo"]["allow_edits"] = False
    trusted.path.write_text(_toml(trusted.source), encoding="utf-8")

    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            with pytest.raises(HarnessError) as denied:
                await manager.submit("demo", "副本修改", "grant", ["src"], mode="edit")
            assert denied.value.code == "EDIT_NOT_ALLOWED" and _jobs(manager) == 0
            _write(trusted, lambda value: value["projects"]["demo"].update(allow_edits=True))
            accepted = await manager.submit("demo", "副本修改", "grant", ["src"], mode="edit")
            job = manager.store.get(accepted["job_id"])
            assert job["request"]["mode"] == "edit" and job["execution"]["project"]["allow_edits"] is True
            assert manager.service_info()["restart_required"] is False and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


IMMUTABLE_CHANGES = (
    ("data_dir", lambda value, root: value.update(data_dir=str(root / "another-state"))),
    ("default_mode", lambda value, root: value.update(default_mode="edit")),
    ("model", lambda value, root: value["model"].update(model="another-model")),
    ("model_reference", lambda value, root: value["model"].update(credential_env="ANOTHER_UNUSED_REFERENCE")),
    ("limits", lambda value, root: value["limits"].update(default_timeout_seconds=31)),
    ("executor", lambda value, root: value["executor"].update(lease_seconds=18)),
    ("completion", lambda value, root: value["completion"].update(auto_continue=True)),
    ("project_root", lambda value, root: value["projects"]["demo"].update(path=str(root / "another-project"))),
    ("project_added", lambda value, root: value["projects"].update(extra=deepcopy(value["projects"]["demo"]))),
    ("project_removed", lambda value, root: value["projects"].clear()),
)


@pytest.mark.parametrize("kind", ["single", "parallel"])
@pytest.mark.parametrize("label,change", IMMUTABLE_CHANGES, ids=[row[0] for row in IMMUTABLE_CHANGES])
def test_non_permission_drift_blocks_new_submit_and_preflight_without_replacing_runtime(trusted, kind, label, change):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            original_config = manager.config
            disk = _write(trusted, lambda value: change(value, trusted.root))
            info = manager.service_info()
            assert info["restart_required"] is True and info["new_task_permissions_ready"] is False
            assert info["config_version"] == config_version(original_config)
            assert info["disk_config_version"] == config_version(disk)
            for operation in (manager.submit("demo", "阻止新任务", "immutable-" + label, ["src"], mode="review"),
                              manager.preflight("demo", ["src"], mode="review")):
                with pytest.raises(HarnessError) as blocked:
                    await operation
                assert blocked.value.code == "CONFIG_RESTART_REQUIRED"
            assert manager.config == original_config and _jobs(manager) == 0 and not runner.requests
            assert not (trusted.root / "another-state").exists()
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
@pytest.mark.parametrize("invalid", ["toml", "missing", "invalid_field", "non_utf8", "symlink", "hardlink"])
def test_invalid_or_linked_trusted_config_fails_closed_for_new_requests(trusted, kind, invalid):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            original_version = config_version(manager.config)
            if invalid == "toml":
                trusted.path.write_text("invalid = [", encoding="utf-8")
            elif invalid == "missing":
                trusted.path.unlink()
            elif invalid == "invalid_field":
                value = deepcopy(trusted.source)
                value["projects"]["demo"]["allow_edits"] = "true"
                trusted.path.write_text(_toml(value), encoding="utf-8")
            elif invalid == "non_utf8":
                trusted.path.write_bytes(b"\xff\xfe\x80")
            else:
                alias_target = trusted.root / "alias-target.toml"
                alias_target.write_bytes(trusted.path.read_bytes())
                trusted.path.unlink()
                if invalid == "symlink":
                    try:
                        trusted.path.symlink_to(alias_target)
                    except OSError as error:
                        pytest.skip(f"本机不能创建 symlink 夹具: {type(error).__name__}")
                else:
                    os.link(alias_target, trusted.path)
            info = manager.service_info()
            assert info["new_task_permissions_ready"] is False
            assert info["config_version"] == original_version
            for operation in (manager.submit("demo", "无效配置不得执行", "invalid-" + invalid, ["src"]),
                              manager.preflight("demo", ["src"])):
                with pytest.raises(HarnessError) as blocked:
                    await operation
                assert blocked.value.code == "PERMISSIONS_CONFIG_INVALID"
            assert _jobs(manager) == 0 and not runner.requests
            # 修复同一受信路径后可以接受新任务，失败尝试未占用请求编号。
            if trusted.path.exists() or trusted.path.is_symlink():
                trusted.path.unlink()
            trusted.path.write_text(_toml(trusted.source), encoding="utf-8")
            recovered = await manager.submit("demo", "无效配置不得执行", "invalid-" + invalid, ["src"])
            assert recovered["idempotent_replay"] is False
            assert manager.service_info()["new_task_permissions_ready"] is True
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_linked_configuration_ancestor_cannot_replace_bound_trusted_file(trusted, kind):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            directory = trusted.root / "cfg"
            directory.mkdir()
            path = directory / "c.toml"
            path.write_bytes(trusted.path.read_bytes())
            manager.config_path = path
            assert manager.service_info()["new_task_permissions_ready"] is True
            target = trusted.root / "real-cfg"
            directory.rename(target)
            try:
                directory.symlink_to(target, target_is_directory=True)
            except OSError as error:
                target.rename(directory)
                pytest.skip(f"本机不能创建目录 symlink 夹具: {type(error).__name__}")
            assert manager.service_info()["new_task_permissions_ready"] is False
            with pytest.raises(HarnessError) as blocked:
                await manager.submit("demo", "不能经目录链接读取授权", "parent-link", ["src"])
            assert blocked.value.code == "PERMISSIONS_CONFIG_INVALID"
            assert _jobs(manager) == 0 and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
@pytest.mark.parametrize("disk_state", ["revoked", "invalid", "restart"])
def test_same_request_recovers_original_job_after_permission_revocation_or_broken_config(trusted, kind, disk_state):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            original = await manager.submit("demo", "原任务", "recover", ["src/main.py"], 42, "edit")
            original_job = manager.store.get(original["job_id"])
            if disk_state == "revoked":
                _write(trusted, lambda value: value["projects"]["demo"].update(allow_edits=False, allowed_paths=["tests"]))
            elif disk_state == "invalid":
                trusted.path.write_text("broken = [", encoding="utf-8")
            else:
                _write(trusted, lambda value: value.update(default_mode="edit"))
            manager.service_info()
            replay = await manager.submit("demo", "原任务", "recover", ["src/main.py"])
            assert replay["job_id"] == original["job_id"] and replay["idempotent_replay"] is True
            job = manager.store.get(replay["job_id"])
            assert job["request"] == original_job["request"]
            assert job["execution"] == original_job["execution"]
            assert job["request"]["mode"] == "edit" and job["request"]["timeout_seconds"] == 42
            assert manager.status(job["job_id"])["job_id"] == job["job_id"]
            assert manager.result(job["job_id"], view="summary")["ready"] is False
            for kwargs in ({"mode": "review"}, {"timeout_seconds": 43}, {"relative_paths": ["tests"]}, {"prompt": "改了任务"}):
                arguments = dict(project_id="demo", prompt="原任务", client_request_id="recover", relative_paths=["src/main.py"])
                arguments.update(kwargs)
                with pytest.raises(HarnessError) as conflict:
                    await manager.submit(**arguments)
                assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
            cancelled = await manager.cancel(job["job_id"])
            assert cancelled["job_id"] == job["job_id"]
            assert manager.status(job["job_id"])["status"] in {"cancelling", "cancelled"}
            assert _jobs(manager) == 1 and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_empty_authorization_blocks_default_and_explicit_ranges(trusted, kind):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            _write(trusted, lambda value: value["projects"]["demo"].update(allowed_paths=[]))
            for paths in (None, ["src"], ["."]):
                with pytest.raises(HarnessError) as blocked:
                    await manager.submit("demo", "范围已撤销", "empty", paths, mode="review")
                assert blocked.value.code == "EMPTY_AUTHORIZATION"
                with pytest.raises(HarnessError) as preflight:
                    await manager.preflight("demo", paths, mode="review")
                assert preflight.value.code == "EMPTY_AUTHORIZATION"
            assert _jobs(manager) == 0 and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("allowed,requested,accepted", [
    (["."], ["tests"], True), (["src"], ["src"], True), (["src"], ["src/main.py"], True),
    (["src"], ["src2/main.py"], False), (["src/main.py"], ["src"], False),
    (["src"], ["."], False), (["src"], ["src/main.py", "tests/test_main.py"], False),
    (["src", "tests"], ["tests/test_main.py", "src/main.py"], True),
])
def test_explicit_request_scope_must_be_a_complete_subset_of_trusted_paths(trusted, allowed, requested, accepted):
    config = _write(trusted, lambda value: value["projects"]["demo"].update(allowed_paths=allowed))
    if accepted:
        normalized = normalize_request(config, "demo", "范围", "scope", requested, mode="review")
        assert normalized["relative_paths"] == sorted(requested)
    else:
        with pytest.raises(HarnessError) as error:
            normalize_request(config, "demo", "范围", "scope", requested, mode="review")
        assert error.value.code == "PATH_NOT_AUTHORIZED"


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_preflight_captures_one_permission_revision_across_an_await(trusted, kind, monkeypatch):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        started, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def snapshot(helper, job, directory, *, preflight_only=False):
            assert preflight_only is True
            captured.append(job["execution"]["project"])
            started.set()
            await release.wait()
            return {"snapshot_id": "artificial", "checked_files": 1, "checked_bytes": 10,
                    "matches": [], "match_count": 0, "matches_truncated": False, "model_called": False}

        monkeypatch.setattr(TaskManager, "_snapshot", snapshot)
        operation = None
        try:
            initial_version = config_version(manager.config)
            operation = asyncio.create_task(manager.preflight("demo", ["src/main.py"], "review"))
            await asyncio.wait_for(started.wait(), 3)
            fresh = _write(trusted, lambda value: value["projects"]["demo"].update(allowed_paths=["tests"], allow_edits=False))
            assert manager.service_info()["config_version"] == config_version(fresh)
            release.set()
            result = await asyncio.wait_for(operation, 3)
            assert result["config_version"] == initial_version
            assert list(captured[0]["allowed_paths"]) == ["src"] and captured[0]["allow_edits"] is True
            assert result["authorization_rechecked_on_submit"] is True
            assert _jobs(manager) == 0 and not runner.requests
        finally:
            release.set()
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["single", "parallel"])
def test_service_info_refreshes_permissions_before_any_new_request(trusted, kind):
    async def scenario():
        manager, runner = _manager(trusted, kind)
        try:
            initial = manager.service_info()
            fresh = _write(trusted, lambda value: value["projects"]["demo"].update(exclude_patterns=["*.internal.py"]))
            updated = manager.service_info()
            assert updated["config_version"] == updated["disk_config_version"] == config_version(fresh)
            assert updated["permissions_version"] != initial["permissions_version"]
            assert updated["permissions_reload_state"] in {"current", "updated"}
            assert updated["permissions_hot_reload"] is True
            assert updated["new_task_permissions_ready"] is True and updated["restart_required"] is False
            assert manager.config.projects["demo"].exclude_patterns == ("*.internal.py",)
            assert str(trusted.project) not in json.dumps(updated)
            assert _jobs(manager) == 0 and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_unconfigured_manager_preserves_direct_call_compatibility(trusted):
    async def scenario():
        manager, runner = _manager(trusted, "single")
        manager.config_path = None
        try:
            info = manager.service_info()
            assert info["permissions_hot_reload"] is False
            assert info["permissions_reload_state"] == "unconfigured"
            assert info["new_task_permissions_ready"] is True
            assert info["config_state"] == "unknown" and info["restart_required"] is None
            receipt = await manager.submit("demo", "未绑定配置的人工接口", "direct", ["src"])
            assert receipt["idempotent_replay"] is False and not runner.requests
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_running_job_keeps_frozen_authorization_when_config_is_revoked(trusted, monkeypatch):
    async def scenario():
        manager, runner = _manager(trusted, "single")
        captured = []

        async def snapshot(job, directory, *, preflight_only=False):
            captured.append(deepcopy(job["execution"]["project"]))
            folder = directory / "snapshot" / "files"
            folder.mkdir(parents=True)
            source = b"value = 1\n"
            (folder / "main.py").write_bytes(source)
            return Snapshot(folder, "artificial", [{"path": "main.py", "size_bytes": len(source),
                            "sha256": hashlib.sha256(source).hexdigest(), "line_count": 1}],
                            None, False, ["main.py"], [])

        monkeypatch.setattr(manager, "_snapshot", snapshot)
        operation = None
        try:
            receipt = await manager.submit("demo", "正在执行的审查", "running", ["src/main.py"], mode="review")
            original = manager.store.get(receipt["job_id"])
            operation = asyncio.create_task(manager._execute(original))
            await asyncio.wait_for(runner.started.wait(), 3)
            _write(trusted, lambda value: value["projects"]["demo"].update(allow_edits=False, allowed_paths=[]))
            manager.service_info()
            assert manager.status(receipt["job_id"])["status"] == "running"
            with pytest.raises(HarnessError) as blocked:
                await manager.submit("demo", "新任务", "new-running", ["src"], mode="review")
            assert blocked.value.code == "EMPTY_AUTHORIZATION"
            runner.release.set()
            await asyncio.wait_for(operation, 5)
            assert manager.status(receipt["job_id"])["status"] == "succeeded"
            assert manager.result(receipt["job_id"], view="summary")["ready"] is True
            assert captured == [original["execution"]["project"]]
            assert runner.requests[0]["relative_paths"] == ["src/main.py"]
            assert len(runner.requests) == 1
        finally:
            runner.release.set()
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())
