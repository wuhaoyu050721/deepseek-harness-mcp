"""会话权限热更新只用临时配置与 SQLite；不调用模型，也不读取业务源码。"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json

import pytest

from harness_mcp.config import load_config
from harness_mcp.conversation import SessionManager
from harness_mcp.errors import HarnessError


class NoModelRunner:
    async def recover(self):
        return True

    async def close(self):
        return True


def write_config(path, config=None, *, allowed=("src",), allow_edits=True,
                 default_mode="edit", excludes=(), untracked=(), project_path=None,
                 include_project=True):
    root = project_path or path.parent / "project"
    data_dir = config.data_dir if config is not None else path.parent / "state"
    text = [f"data_dir = {json.dumps(str(data_dir))}", f"default_mode = {json.dumps(default_mode)}",
            "[model]", 'credential_source = "env"', 'credential_env = "DYNAMIC_SESSION_FIXTURE_UNUSED"',
            "[limits]", "default_timeout_seconds = 15", "max_timeout_seconds = 30"]
    if include_project:
        text += ["[projects.demo]", f"path = {json.dumps(str(root))}",
                 f"allowed_paths = {json.dumps(list(allowed))}",
                 f"exclude_patterns = {json.dumps(list(excludes))}",
                 f"allow_untracked = {json.dumps(list(untracked))}",
                 f"allow_edits = {'true' if allow_edits else 'false'}"]
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


@contextmanager
def metadata_manager(config, path):
    manager = SessionManager(config, runner_factory=lambda _job: NoModelRunner())
    manager.config_path = path
    manager.accepting = True
    try:
        yield manager
    finally:
        # 测试没有调度器或执行进程；只释放自己创建的元数据锁。
        manager.store.close()
        manager.lock.close()


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "trusted.toml"
    (tmp_path / "project").mkdir()
    write_config(path)
    return path, load_config(path)


def open_session(manager, request="open", **kwargs):
    return asyncio.run(manager.session_open("demo", request, **kwargs))


def send(manager, session, request="turn", prompt="Implement the authorized change", **kwargs):
    return asyncio.run(manager.session_send(session["session_id"], prompt, request, **kwargs))


def assert_error(code, function):
    with pytest.raises(HarnessError) as error:
        function()
    assert error.value.code == code


def test_open_replay_survives_edit_revoke_and_default_change(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, title="original", relative_paths=["src/a.py"])
        write_config(path, config, allow_edits=False, default_mode="review")
        replay = open_session(manager, title="original", relative_paths=["src\\a.py"])
        assert replay["session_id"] == opened["session_id"]
        assert replay["mode"] == "edit" and replay["idempotent_replay"]
        assert_error("CONFIG_RESTART_REQUIRED", lambda: send(manager, opened))
        write_config(path, config, allow_edits=False)
        assert_error("EDIT_NOT_ALLOWED", lambda: send(manager, opened))
        assert open_session(manager, "new-review", mode="review")["mode"] == "review"


@pytest.mark.parametrize("changed", [{"title": "changed"}, {"mode": "review"}, {"relative_paths": ["src/b.py"]}])
def test_open_replay_still_rejects_different_parameters(fixture, changed):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        open_session(manager, title="original", relative_paths=["src/a.py"])
        write_config(path, config, allow_edits=False)
        arguments = {"title": "original", "relative_paths": ["src/a.py"], **changed}
        assert_error("IDEMPOTENCY_CONFLICT", lambda: open_session(manager, **arguments))


@pytest.mark.parametrize("after", ["invalid", "removed", "revoked", "scope"])
def test_existing_round_recovers_before_current_authorization(fixture, after):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, relative_paths=["src/a.py"])
        receipt = send(manager, opened)
        if after == "invalid":
            path.write_text("[invalid", encoding="utf-8")
        elif after == "removed":
            write_config(path, config, include_project=False)
        elif after == "revoked":
            write_config(path, config, allow_edits=False)
        else:
            write_config(path, config, allowed=("tests",))
        replay = send(manager, opened)
        assert replay["job_id"] == receipt["job_id"] and replay["idempotent_replay"]
        assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert_error("IDEMPOTENCY_CONFLICT", lambda: send(manager, opened, prompt="different"))


def test_open_replay_survives_invalid_config_and_service_not_accepting(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        path.write_text("invalid = [", encoding="utf-8")
        manager.accepting = False
        assert open_session(manager)["session_id"] == opened["session_id"]
        assert_error("SERVICE_STOPPING", lambda: open_session(manager, "different-open"))


def test_existing_round_recovers_even_when_service_is_not_accepting(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        receipt = send(manager, opened)
        manager.accepting = False
        assert send(manager, opened)["job_id"] == receipt["job_id"]
        assert_error("SERVICE_STOPPING", lambda: send(manager, opened, "new-turn"))


def test_new_open_rechecks_current_authorized_scope(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        write_config(path, config, allowed=("src/a.py",))
        assert_error("PATH_NOT_AUTHORIZED", lambda: open_session(manager, relative_paths=["src/b.py"]))
        assert_error("PATH_NOT_AUTHORIZED", lambda: open_session(manager, relative_paths=["src"]))
        assert_error("PATH_NOT_AUTHORIZED", lambda: open_session(manager, relative_paths=["src2/a.py"]))
        assert not manager.session_list()["sessions"]
        assert open_session(manager, relative_paths=["src/a.py"])["relative_paths"] == ["src/a.py"]


def test_narrowing_keeps_runtime_current_but_rejects_outside_round(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, relative_paths=["src/b.py"])
        write_config(path, config, allowed=("src/a.py",))
        assert_error("PATH_NOT_AUTHORIZED", lambda: send(manager, opened))
        entry = manager.session_list()["sessions"][0]
        assert entry["configuration_current"] is True
        assert entry["permissions_available"] is False
        assert entry["permissions_error"] == "PATH_NOT_AUTHORIZED"
        assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_narrowing_to_bound_file_allows_next_round_with_new_permissions(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, relative_paths=["src/a.py"])
        write_config(path, config, allowed=("src/a.py",), excludes=("*.tmp",))
        receipt = send(manager, opened)
        execution = manager.store.get(receipt["job_id"])["execution"]
        assert execution["project"]["allowed_paths"] == ["src/a.py"]
        assert execution["project"]["exclude_patterns"] == ["*.tmp"]
        entry = manager.session_list()["sessions"][0]
        assert entry["configuration_current"] and entry["permissions_available"]


def test_scope_expansion_does_not_expand_existing_default_bound_session(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        write_config(path, config, allowed=("src", "tests"))
        receipt = send(manager, opened)
        old_execution = manager.store.get(receipt["job_id"])["execution"]
        assert old_execution["project"]["allowed_paths"] == ["src"]
        new = open_session(manager, "expanded")
        newer_receipt = send(manager, new, "expanded-turn")
        new_execution = manager.store.get(newer_receipt["job_id"])["execution"]
        assert new_execution["project"]["allowed_paths"] == ["src", "tests"]


def test_narrowing_default_bound_session_uses_intersection(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        write_config(path, config, allowed=("src/a.py",))
        receipt = send(manager, opened)
        assert manager.store.get(receipt["job_id"])["execution"]["project"]["allowed_paths"] == ["src/a.py"]


def test_exclusions_accumulate_and_untracked_authority_cannot_expand(fixture):
    path, config = fixture
    write_config(path, config, excludes=("*.private",), untracked=("src/a.py",))
    original = load_config(path)
    with metadata_manager(original, path) as manager:
        opened = open_session(manager)
        write_config(path, original, excludes=("*.tmp",), untracked=("src/a.py", "src/b.py"))
        receipt = send(manager, opened)
        project = manager.store.get(receipt["job_id"])["execution"]["project"]
        assert set(project["exclude_patterns"]) == {"*.private", "*.tmp"}
        assert project["allow_untracked"] == ["src/a.py"]


def test_permission_change_does_not_overwrite_already_accepted_execution(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, relative_paths=["src/a.py"])
        receipt = send(manager, opened)
        before = manager.store.get(receipt["job_id"])["execution"]
        write_config(path, config, allow_edits=False, allowed=("tests",))
        manager.session_list()
        assert manager.store.get(receipt["job_id"])["execution"] == before
        assert before["project"]["allow_edits"] is True
        assert before["project"]["allowed_paths"] == ["src"]


def test_list_preserves_revoked_session_with_reason(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        write_config(path, config, allow_edits=False)
        listed = manager.session_list("demo")["sessions"][0]
        assert listed["session_id"] == opened["session_id"]
        assert listed["configuration_current"]
        assert not listed["permissions_available"]
        assert listed["permissions_error"] == "EDIT_NOT_ALLOWED"


def test_list_preserves_session_when_current_config_invalid(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        path.write_text("[invalid", encoding="utf-8")
        listed = manager.session_list()["sessions"][0]
        assert listed["session_id"] == opened["session_id"]
        assert listed["configuration_current"] is None
        assert not listed["permissions_available"]
        assert listed["permissions_error"]
        with pytest.raises(HarnessError):
            send(manager, opened)
        assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_changed_project_root_rejects_new_round_but_recovers_original_open(fixture):
    path, config = fixture
    with metadata_manager(config, None) as manager:
        opened = open_session(manager)
        # 运行身份校验不可被权限求交掩盖；改同名项目目录必须拒绝沿用会话。
        changed = dict(config.projects)
        changed["demo"] = replace(changed["demo"], path=path.parent / "different-project")
        manager.config = replace(config, projects=changed)
        assert open_session(manager)["session_id"] == opened["session_id"]
        assert_error("SESSION_CONFIG_CHANGED", lambda: send(manager, opened))
        listed = manager.session_list()["sessions"][0]
        assert not listed["configuration_current"] and not listed["permissions_available"]


def test_removed_project_keeps_old_session_visible_and_blocks_new_round(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        write_config(path, config, include_project=False)
        listed = manager.session_list("demo")["sessions"][0]
        assert listed["session_id"] == opened["session_id"]
        assert not listed["permissions_available"]
        assert listed["permissions_error"] == "CONFIG_RESTART_REQUIRED"
        assert_error("CONFIG_RESTART_REQUIRED", lambda: send(manager, opened))


def test_legacy_json_binding_remains_usable_after_permission_only_change(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager, relative_paths=["src/a.py"])
        row = manager._session(opened["session_id"])
        assert isinstance(row["binding"]["execution"]["project"]["allowed_paths"], list)
        write_config(path, config, allowed=("src/a.py",))
        receipt = send(manager, opened)
        assert manager.store.get(receipt["job_id"])["request"]["session_id"] == opened["session_id"]


def test_request_namespace_conflict_survives_permission_revoke(fixture):
    path, config = fixture
    with metadata_manager(config, path) as manager:
        opened = open_session(manager)
        send(manager, opened, "turn")
        write_config(path, config, allow_edits=False)
        assert_error("IDEMPOTENCY_CONFLICT", lambda: open_session(manager, "turn"))
