"""子会话契约与脱敏边界：确定性执行器，不调用模型，也不改业务项目。"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness_mcp.config import AppConfig, LimitsConfig, ModelConfig, ProjectConfig
from harness_mcp.conversation import HISTORY_CHARS, HISTORY_TURNS, SessionManager
from harness_mcp.errors import HarnessError
from harness_mcp.job_store import TERMINAL
from harness_mcp.report import ReportError, save_report
from harness_mcp.server import create_server


class NoModelRunner:
    """仅供元数据测试；这些测试不启动调度器或执行器。"""

    _closed = False

    async def recover(self):
        return True

    async def close(self):
        self._closed = True
        return True


@pytest.fixture
def session_config(tmp_path):
    projects = {}
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        (root / "src").mkdir()
        (root / "src" / "sample.py").write_text("value = 1\n", encoding="utf-8")
        projects[name] = ProjectConfig(name, root, allowed_paths=("src",), allow_edits=name == "alpha")
    return AppConfig(
        tmp_path / "state",
        projects,
        LimitsConfig(default_timeout_seconds=15, max_timeout_seconds=30),
        ModelConfig(credential_source="env", credential_env="CONVERSATION_FIXTURE_UNUSED_KEY"),
    )


@contextmanager
def metadata_manager(config):
    # 本上下文只检查 SQLite 接纳和历史，绝不创建后台执行任务。
    manager = SessionManager(config, runner_factory=lambda _job_id: NoModelRunner())
    manager.accepting = True
    try:
        yield manager
    finally:
        manager.accepting = False
        manager.store.close()
        manager.lock.close()


def assert_code(error, code):
    assert isinstance(error, HarnessError)
    assert error.code == code


def completed_turn(manager, session_id, prompt, request_id, summary="fixture reply", status="succeeded"):
    """不伪称执行通过，仅构造受摘要保护的历史记录用于恢复/隔离检查。"""
    receipt = asyncio.run(manager.session_send(session_id, prompt, request_id))
    directory = manager.config.data_dir / "jobs" / receipt["job_id"]
    report = {"job_id": receipt["job_id"], "project_id": manager.store.get(receipt["job_id"])["request"]["project_id"],
              "findings": [], "summary": summary, "raw_text": "", "limitations": []}
    saved = save_report(report, directory)
    manager.store.finish(receipt["job_id"], status, report_id=saved["report_id"])
    return receipt


def test_open_normalizes_scope_and_replays_exact_session(session_config):
    with metadata_manager(session_config) as manager:
        first = asyncio.run(manager.session_open("alpha", "open-alpha", "scope", relative_paths=["src/sample.py", "src\\sample.py"]))
        second = asyncio.run(manager.session_open("alpha", "open-alpha", "scope", relative_paths=["src/sample.py"]))
        assert first["session_id"] == second["session_id"]
        assert first["relative_paths"] == ["src/sample.py"]
        assert not first["idempotent_replay"] and second["idempotent_replay"]
        assert len(manager.session_list()["sessions"]) == 1
        assert manager.session_list()["sessions"][0]["configuration_current"]
        for changed in ({"title": "other"}, {"mode": "edit"}, {"relative_paths": ["src"]}):
            args = {"title": "scope", "relative_paths": ["src/sample.py"], **changed}
            with pytest.raises(HarnessError) as error:
                asyncio.run(manager.session_open("alpha", "open-alpha", **args))
            assert_code(error.value, "IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize("title", ["line\nbreak", "x" * 201, 123])
def test_open_rejects_invalid_titles(session_config, title):
    with metadata_manager(session_config) as manager:
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_open("alpha", "invalid-title", title=title))
        assert_code(error.value, "INVALID_SESSION_TITLE")


def test_open_enforces_project_edit_permission_and_explicit_scope(session_config):
    with metadata_manager(session_config) as manager:
        for params, code in (({"project_id": "missing"}, "PROJECT_NOT_ALLOWED"),
                             ({"project_id": "beta", "mode": "edit"}, "EDIT_NOT_ALLOWED"),
                             ({"project_id": "alpha", "relative_paths": []}, "EMPTY_SCOPE"),
                             ({"project_id": "alpha", "relative_paths": ["../outside"]}, "INVALID_PATH")):
            with pytest.raises(HarnessError) as error:
                asyncio.run(manager.session_open(client_request_id="invalid-binding", **params))
            assert_code(error.value, code)
        assert not manager.session_list()["sessions"]


def test_default_edit_bound_session_and_explicit_review_override(session_config):
    configured = replace(session_config, default_mode="edit")
    with metadata_manager(configured) as manager:
        opened = asyncio.run(manager.session_open("alpha", "default-open", relative_paths=["src/sample.py"]))
        assert opened["mode"] == "edit"
        receipt = asyncio.run(manager.session_send(opened["session_id"], "fix one value", "default-send"))
        request = manager.store.get(receipt["job_id"])["request"]
        assert (request["mode"], request["relative_paths"]) == ("edit", ["src/sample.py"])
        review = asyncio.run(manager.session_open("beta", "review-open", mode="review"))
        assert review["mode"] == "review"
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_open("beta", "edit-not-authorized"))
        assert_code(error.value, "EDIT_NOT_ALLOWED")


def test_shared_proxy_uses_same_trusted_default_and_preserves_review(session_config):
    from harness_mcp.shared_client import SharedClient

    client = SharedClient(replace(session_config, default_mode="edit"), Path("unused-default-config.toml"))
    client._call = AsyncMock(return_value={"job_id": "fixture"})
    asyncio.run(client.submit("alpha", "fix", "default-submit"))
    assert client._call.call_args.args[1]["mode"] == "edit"
    asyncio.run(client.session_open("alpha", "default-open"))
    assert client._call.call_args.args[1]["mode"] == "edit"
    asyncio.run(client.session_open("beta", "review-open", mode="review"))
    assert client._call.call_args.args[1]["mode"] == "review"


def test_same_session_busy_preserves_idempotent_replay_and_binding(session_config):
    with metadata_manager(session_config) as manager:
        session = asyncio.run(manager.session_open("alpha", "open-one", mode="edit", relative_paths=["src/sample.py"]))
        first = asyncio.run(manager.session_send(session["session_id"], "first prompt", "send-one"))
        replay = asyncio.run(manager.session_send(session["session_id"], "first prompt", "send-one"))
        assert first["job_id"] == replay["job_id"] and replay["idempotent_replay"]
        assert replay["session_id"] == session["session_id"]
        request = manager.store.get(first["job_id"])["request"]
        assert (request["project_id"], request["mode"], request["relative_paths"]) == ("alpha", "edit", ["src/sample.py"])
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_send(session["session_id"], "changed prompt", "send-one"))
        assert_code(error.value, "IDEMPOTENCY_CONFLICT")
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_send(session["session_id"], "next prompt", "send-two"))
        assert_code(error.value, "SESSION_BUSY")
        manager.store.finish(first["job_id"], "cancelled")
        second = asyncio.run(manager.session_send(session["session_id"], "next prompt", "send-two"))
        assert second["job_id"] != first["job_id"]
        assert manager.store.get(second["job_id"])["request"]["conversation_context"][0]["status"] == "cancelled"


def test_concurrent_send_cannot_accept_two_rounds_for_one_session(session_config):
    async def check(manager):
        session = await manager.session_open("alpha", "open-race")
        replies = await asyncio.gather(*(manager.session_send(session["session_id"], "round", f"race-{i}") for i in range(2)), return_exceptions=True)
        assert sum(isinstance(reply, dict) for reply in replies) == 1
        assert_code(next(reply for reply in replies if isinstance(reply, Exception)), "SESSION_BUSY")
    with metadata_manager(session_config) as manager:
        asyncio.run(check(manager))


def test_request_namespace_spans_open_send_and_stateless_submit(session_config):
    with metadata_manager(session_config) as manager:
        session = asyncio.run(manager.session_open("alpha", "global-open"))
        for coroutine in (manager.session_send(session["session_id"], "round", "global-open"), manager.submit("alpha", "round", "global-open")):
            with pytest.raises(HarnessError) as error:
                asyncio.run(coroutine)
            assert_code(error.value, "IDEMPOTENCY_CONFLICT")
        submitted = asyncio.run(manager.submit("beta", "independent", "global-job"))
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_open("beta", "global-job"))
        assert_code(error.value, "IDEMPOTENCY_CONFLICT")
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_send(session["session_id"], "independent", "global-job"))
        assert_code(error.value, "IDEMPOTENCY_CONFLICT")
        assert manager.store.get(submitted["job_id"])["request"]["project_id"] == "beta"


def test_idempotent_replay_ignores_later_history_and_avoids_corrupt_history_read(session_config):
    with metadata_manager(session_config) as manager:
        session = asyncio.run(manager.session_open("alpha", "open-replay"))
        first = completed_turn(manager, session["session_id"], "original prompt", "original-send")
        second = completed_turn(manager, session["session_id"], "later prompt", "later-send")
        # 已接纳任务恢复先于历史读取；后来报告损坏也不能重放原任务。
        path = session_config.data_dir / "jobs" / second["job_id"] / "report.json"
        path.write_text("tampered", encoding="utf-8")
        replay = asyncio.run(manager.session_send(session["session_id"], "original prompt", "original-send"))
        assert replay["job_id"] == first["job_id"] and replay["idempotent_replay"]
        with pytest.raises(ReportError) as error:
            asyncio.run(manager.session_send(session["session_id"], "new prompt", "new-send"))
        assert error.value.code == "REPORT_TAMPERED"
        assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_history_is_session_specific_bounded_and_chronological(session_config):
    with metadata_manager(session_config) as manager:
        alpha = asyncio.run(manager.session_open("alpha", "open-history-a"))["session_id"]
        beta = asyncio.run(manager.session_open("beta", "open-history-b"))["session_id"]
        completed_turn(manager, beta, "PRIVATE_BETA_MARKER", "beta-turn", "PRIVATE_BETA_REPLY")
        receipts = [completed_turn(manager, alpha, f"alpha-user-{i}" + "x" * 2200, f"alpha-turn-{i}", f"alpha-reply-{i}" + "y" * 6500) for i in range(6)]
        context = manager._context(alpha)
        assert 1 <= len(context) <= HISTORY_TURNS
        assert sum(len(turn["user"]) + len(turn["assistant"]) for turn in context) <= HISTORY_CHARS
        recent_ids = [receipt["job_id"] for receipt in receipts[-HISTORY_TURNS:]]
        positions = [recent_ids.index(turn["job_id"]) for turn in context]
        assert positions == sorted(positions)
        serialized = json.dumps(context)
        assert "PRIVATE_BETA" not in serialized and "alpha-user-0" not in serialized
        assert manager._context(beta)[0]["user"] == "PRIVATE_BETA_MARKER"


def test_history_timestamp_ties_use_submission_order(session_config, monkeypatch):
    with metadata_manager(session_config) as manager:
        session_id = asyncio.run(manager.session_open("alpha", "open-history-clock-tie"))["session_id"]
        # UUID按逆序生成，避免随机碰巧与真实轮次排序一致而掩盖错误。
        ids = iter(f"{1000 - index:032x}" for index in range(5))
        monkeypatch.setattr("harness_mcp.job_store.uuid.uuid4", lambda: SimpleNamespace(hex=next(ids)))
        for index in range(5):
            completed_turn(manager, session_id, f"turn-{index}", f"tie-turn-{index}", f"answer-{index}")
        with manager.store.transaction() as cursor:
            cursor.execute("UPDATE jobs SET created_at=1")
        assert [turn["user"] for turn in manager._context(session_id)] == ["turn-1", "turn-2", "turn-3", "turn-4"]


def test_open_and_replay_redact_registered_secret_title(session_config):
    with metadata_manager(session_config) as manager:
        secret = "registered-title-value-fixture-0123456789"
        manager._register_secret(secret)
        first = asyncio.run(manager.session_open("alpha", "open-title-redaction", title=secret))
        replay = asyncio.run(manager.session_open("alpha", "open-title-redaction", title=secret))
        assert first["title"] == replay["title"] == "[REDACTED]"
        assert first["session_id"] == replay["session_id"]


def test_secret_is_removed_before_history_user_truncation(session_config):
    with metadata_manager(session_config) as manager:
        secret = "registered-model-credential-fixture-0987654321"
        manager._register_secret(secret)
        session_id = asyncio.run(manager.session_open("alpha", "open-redaction-user"))["session_id"]
        completed_turn(manager, session_id, "x" * 1995 + secret, "secret-user-turn")
        user = manager._context(session_id)[0]["user"]
        assert "regis" not in user
        assert user[1995:] == "[REDA"
        pending = asyncio.run(manager.session_send(session_id, "safe follow-up", "safe-user-follow-up"))
        raw = manager.store.get(pending["job_id"])["request"]["conversation_context"]
        assert "regis" not in raw[0]["user"]


def test_secret_is_removed_before_history_assistant_truncation(session_config):
    with metadata_manager(session_config) as manager:
        secret = "registered-model-credential-fixture-1234567890"
        manager._register_secret(secret)
        session_id = asyncio.run(manager.session_open("alpha", "open-redaction-assistant"))["session_id"]
        # render_markdown 固定头长度包含job_id；填充使实际凭证跨6000字边界。
        prefix_len = len("# Harness 审查报告 " + "0" * 32 + "\n\n")
        completed_turn(manager, session_id, "safe", "secret-assistant-turn", "x" * (5995 - prefix_len) + secret)
        assistant = manager._context(session_id)[0]["assistant"]
        assert "regis" not in assistant
        assert assistant[-5:] == "[REDA"


def test_changed_configuration_blocks_new_round_and_list_marks_stale(session_config):
    with metadata_manager(session_config) as manager:
        opened = asyncio.run(manager.session_open("alpha", "open-freeze", relative_paths=["src/sample.py"]))
        changed = dict(session_config.projects)
        changed["alpha"] = replace(changed["alpha"], allowed_paths=("src/sample.py",))
        manager.config = replace(session_config, projects=changed)
        assert not manager.session_list("alpha")["sessions"][0]["configuration_current"]
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_send(opened["session_id"], "try drift", "send-freeze"))
        assert_code(error.value, "SESSION_CONFIG_CHANGED")
        manager.config = replace(session_config, projects={"beta": session_config.projects["beta"]})
        assert not manager.session_list()["sessions"]
        with pytest.raises(HarnessError) as error:
            asyncio.run(manager.session_send(opened["session_id"], "try removed", "send-removed"))
        assert_code(error.value, "SESSION_CONFIG_CHANGED")


def test_restart_keeps_session_id_and_recovers_only_its_history(session_config):
    with metadata_manager(session_config) as manager:
        first_session = asyncio.run(manager.session_open("alpha", "open-persisted", title="persisted"))
        first_turn = completed_turn(manager, first_session["session_id"], "old alpha prompt", "persisted-first-turn", "old alpha answer")
        other_session = asyncio.run(manager.session_open("beta", "open-other"))
        completed_turn(manager, other_session["session_id"], "foreign prompt", "foreign-turn", "foreign answer")
    with metadata_manager(session_config) as restarted:
        reopened = asyncio.run(restarted.session_open("alpha", "open-persisted", title="persisted"))
        assert reopened["session_id"] == first_session["session_id"] and reopened["idempotent_replay"]
        next_turn = asyncio.run(restarted.session_send(reopened["session_id"], "new alpha prompt", "persisted-next-turn"))
        context = restarted.store.get(next_turn["job_id"])["request"]["conversation_context"]
        assert context[0]["job_id"] == first_turn["job_id"]
        assert "old alpha answer" in context[0]["assistant"] and "foreign" not in json.dumps(context)


@pytest.mark.parametrize("limit", [True, 0, 101, "20"])
def test_list_validates_limit_and_redacts_registered_secret_titles(session_config, limit):
    with metadata_manager(session_config) as manager:
        secret = "registered-title-credential-fixture"
        manager._register_secret(secret)
        asyncio.run(manager.session_open("alpha", "open-title", title=secret))
        assert manager.session_list("alpha")["sessions"][0]["title"] == "[REDACTED]"
        assert manager.session_list("beta")["sessions"] == []
        with pytest.raises(HarnessError) as error:
            manager.session_list(limit=limit)
        assert_code(error.value, "INVALID_PAGE_SIZE")


def test_shared_schema_keeps_four_tools_and_binds_session_parameters():
    async def check():
        proxy = MagicMock(secrets=[])
        proxy.session_open = AsyncMock(return_value={"session_id": "fixture-session"})
        proxy.session_send = AsyncMock(return_value={"job_id": "fixture-job"})
        proxy.session_list = AsyncMock(return_value={"sessions": []})
        server = create_server(AppConfig(Path("unused-by-proxy")), manager=proxy, sessions=True)
        schemas = {tool.name: tool.input_schema for tool in await server.list_tools()}
        assert set(schemas) == {"harness_submit", "harness_status", "harness_result", "harness_cancel", "harness_session_open", "harness_session_send", "harness_session_list"}
        assert set(schemas["harness_session_open"]["required"]) == {"project_id", "client_request_id"}
        assert schemas["harness_session_open"]["properties"]["mode"]["default"] == "review"
        assert set(schemas["harness_session_send"]["properties"]) == {"session_id", "prompt", "client_request_id", "timeout_seconds"}
        assert schemas["harness_session_list"]["properties"]["limit"]["default"] == 20
        assert {"mode", "relative_paths", "timeout_seconds"} <= set(schemas["harness_submit"]["properties"])
        assert LimitsConfig().max_parallel_jobs == 3
    asyncio.run(check())


class ControlledRunner(NoModelRunner):
    def __init__(self, job_id, requests, releases):
        self.job_id, self.requests, self.releases = job_id, requests, releases
        self._closed = False

    async def run(self, request, on_activity):
        self.requests[self.job_id] = request
        on_activity("fixture-running")
        await self.releases[self.job_id].wait()
        return {"raw_text": json.dumps({"summary": f"answer for {request['project_id']}", "findings": []}),
                "finish_reason": "completed", "execution_stopped": True, "error": None, "usage": None}

    async def terminate(self, _job_id):
        if self.job_id in self.releases:
            self.releases[self.job_id].set()
        return True


async def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("controlled conversation did not reach expected state")
        await asyncio.sleep(0.01)


def test_real_dispatch_parallel_rounds_keep_context_and_snapshots_separate(session_config, monkeypatch):
    from harness_mcp.parallel_manager import _ExecutionContext
    from harness_mcp.workspace import create_snapshot

    for project in session_config.projects.values():
        subprocess.run(["git", "init", "-q", str(project.path)], check=True)
        subprocess.run(["git", "-C", str(project.path), "add", "src/sample.py"], check=True)

    async def prepared(_context, job, directory):
        return create_snapshot(session_config.projects[job["request"]["project_id"]], job["request"]["relative_paths"],
                               directory / "snapshot", max_file_bytes=session_config.limits.max_file_bytes,
                               max_total_bytes=session_config.limits.max_snapshot_bytes)

    monkeypatch.setattr(_ExecutionContext, "_snapshot", prepared)

    async def check():
        requests, releases = {}, {}
        def factory(job_id):
            if job_id is not None:
                releases[job_id] = asyncio.Event()
            return ControlledRunner(job_id, requests, releases)
        manager = SessionManager(session_config, runner_factory=factory)
        await manager.start()
        try:
            alpha = await manager.session_open("alpha", "open-dispatch-alpha", relative_paths=["src/sample.py"])
            beta = await manager.session_open("beta", "open-dispatch-beta", relative_paths=["src/sample.py"])
            a1 = await manager.session_send(alpha["session_id"], "ALPHA_ONLY_HISTORY", "dispatch-alpha-first")
            b1 = await manager.session_send(beta["session_id"], "BETA_ONLY_HISTORY", "dispatch-beta-first")
            await wait_until(lambda: len(requests) == 2)
            assert manager.status(a1["job_id"])["status"] == manager.status(b1["job_id"])["status"] == "running"
            assert requests[a1["job_id"]]["snapshot_dir"] != requests[b1["job_id"]]["snapshot_dir"]
            releases[a1["job_id"]].set()
            await wait_until(lambda: manager.status(a1["job_id"])["status"] in TERMINAL)
            assert manager.status(a1["job_id"])["status"] == "succeeded"
            assert manager.status(b1["job_id"])["status"] == "running"
            a2 = await manager.session_send(alpha["session_id"], "ALPHA_FOLLOWUP", "dispatch-alpha-next")
            await wait_until(lambda: a2["job_id"] in requests)
            prompt = requests[a2["job_id"]]["prompt"]
            assert "ALPHA_ONLY_HISTORY" in prompt and "answer for alpha" in prompt
            assert "BETA_ONLY_HISTORY" not in prompt and "answer for beta" not in prompt
            assert requests[a2["job_id"]]["snapshot_dir"] != requests[a1["job_id"]]["snapshot_dir"]
            assert manager.store.get(a2["job_id"])["request"]["relative_paths"] == ["src/sample.py"]
            await manager.cancel(b1["job_id"])
            await wait_until(lambda: manager.status(b1["job_id"])["status"] in TERMINAL)
            assert manager.status(b1["job_id"])["status"] == "cancelled"
            assert manager.status(a2["job_id"])["status"] == "running"
            releases[a2["job_id"]].set()
            await wait_until(lambda: manager.status(a2["job_id"])["status"] in TERMINAL)
            assert manager.status(a2["job_id"])["status"] == "succeeded"
        finally:
            for release in releases.values():
                release.set()
            await manager.close()
    asyncio.run(check())
