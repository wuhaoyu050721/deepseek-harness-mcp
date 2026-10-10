"""官方通知契约的回归：边界、子会话隔离、输出限制、配置与错误脱敏。"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import hashlib
import importlib
import sys
from types import SimpleNamespace

import pytest

from harness_mcp.harness_adapter import (
    ActivityCollector, AdapterProtocolError, PRIVACY_PATCH, _outcome_error, run_harness,
)


def event(kind: str, data: dict, session: str = "job-1") -> SimpleNamespace:
    return SimpleNamespace(method="session.event", payload={"sessionId": session, "event": {"type": kind, "data": data}})


def receipt(message: str = "message-1", session: str = "job-1") -> SimpleNamespace:
    return event("agent/inbox/spliced", {"inserted": [{"id": message}]}, session)


def idle(session: str = "job-1") -> SimpleNamespace:
    return SimpleNamespace(method="session.status", payload={"sessionId": session, "status": "idle"})


def answer(text: str, session: str = "job-1") -> SimpleNamespace:
    return event("assistant/message", {"message": {"content": [
        {"type": "reasoning", "text": "private-reasoning"}, {"type": "text", "text": text},
    ]}}, session)


def ended(kind: str) -> SimpleNamespace:
    return event("turn/end", {"reason": {"kind": kind}})


def test_receipt_defines_activity_and_descendant_cannot_replace_root_output():
    collector = ActivityCollector("job-1", 100)
    assert not collector.accept(idle(), "message-1")
    collector.accept(answer("stale"), "message-1")
    collector.accept(receipt("old-message"), "message-1")
    assert not collector.received
    collector.accept(receipt(), "message-1")
    collector.accept(answer("root final"), "message-1")
    collector.accept(answer("child output", "child-session"), "message-1")
    assert not collector.accept(idle("child-session"), "message-1")
    collector.accept(ended("completed"), "message-1")
    assert collector.accept(idle(), "message-1")
    assert collector.final_text == "root final"
    assert collector.finish_reason == "completed"
    assert "private-reasoning" not in repr(collector)
    assert _outcome_error(collector) is None


def test_last_committed_root_assistant_message_wins():
    collector = ActivityCollector("job-1", 100)
    for notice in [receipt(), answer("intermediate"), ended("completed"), answer("final"), ended("max-tokens")]:
        collector.accept(notice, "message-1")
    assert collector.final_text == "final"
    assert collector.finish_reason == "max-tokens"


@pytest.mark.parametrize("kind,expected", [
    ("completed", None), ("max-tokens", "MAX_TOKENS"), ("error", "MODEL_ERROR"),
    (None, "INCOMPLETE_RUN"), ("unknown", "INCOMPLETE_RUN"),
])
def test_only_completed_with_output_is_success(kind, expected):
    collector = ActivityCollector("job-1", 100, final_text="result", finish_reason=kind)
    error = _outcome_error(collector)
    assert (error["code"] if error else None) == expected


def test_idle_without_final_output_is_not_usable_completion():
    collector = ActivityCollector("job-1", 100, final_text=" ", finish_reason="completed")
    assert _outcome_error(collector)["code"] == "EMPTY_OUTPUT"


@pytest.mark.parametrize("reason", [{}, {"kind": None}, {"kind": 10}, {"kind": ""}])
def test_malformed_turn_end_fails_closed(reason):
    collector = ActivityCollector("job-1", 100)
    collector.accept(receipt(), "message-1")
    with pytest.raises(AdapterProtocolError):
        collector.accept(event("turn/end", {"reason": reason}), "message-1")


def test_output_byte_cap_handles_utf8_without_storing_reasoning():
    collector = ActivityCollector("job-1", 5)
    collector.accept(receipt(), "message-1")
    collector.accept(answer("汉字abc"), "message-1")
    assert len(collector.final_text.encode("utf-8")) <= 5
    assert collector.final_text == "汉"
    assert collector.output_limited
    collector.accept(ended("completed"), "message-1")
    assert _outcome_error(collector)["code"] == "OUTPUT_LIMIT"


def _fake_sdk(monkeypatch, notifications, fail=None):
    observed = {}

    class FakeSubscription:
        def __init__(self):
            self.notifications = iter(notifications)

        def next(self):
            return next(self.notifications)

    class FakeClient:
        def __init__(self, config):
            observed["config"] = config

        def start(self):
            observed["started"] = True

        def initialize(self, **kwargs):
            observed["initialize"] = kwargs
            if fail:
                raise fail

        def subscribe_session_notifications(self, job_id):
            observed["subscription"] = job_id
            return nullcontext(FakeSubscription())

        def session_prompt(self, job_id, content, **kwargs):
            observed["prompt_calls"] = observed.get("prompt_calls", 0) + 1
            observed["prompt"] = content
            return "message-1"

        def close(self):
            observed["closed"] = True

    monkeypatch.setitem(sys.modules, "deepseek_harness", SimpleNamespace(HarnessClient=FakeClient, HarnessConfig=lambda **kwargs: SimpleNamespace(**kwargs)))
    return observed


def request() -> dict:
    return {"job_id": "job-1", "prompt": "inspect artificial fixture", "model": {
        "provider": "deepseek-official", "model": "test-model", "base_url": "https://example.invalid/v1",
        "credential_env": "MCP_TEST_API_KEY",
    }, "limits": {"max_output_bytes": 100}}


def test_worker_passes_explicit_configuration_and_never_serializes_sdk_events(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [receipt(), answer("review result"), ended("completed"), idle()])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    activities = []
    result = run_harness(request(), snapshot, tmp_path / "private-job-home", activities.append)
    config = observed["config"]
    assert config.profile == "sdk-minimal"
    assert config.cwd == str(snapshot.resolve())
    assert config.dsh_home == str((tmp_path / "private-job-home").resolve())
    assert config.env == {"DEEPSEEK_API_KEY": "secret-test-value", "DEEPSEEK_BASE_URL": "https://example.invalid/v1"}
    assert observed["initialize"] == {"cwd": str(snapshot.resolve()), "provider": "deepseek-official", "model": "test-model"}
    assert Path(config.patches[0]).read_text(encoding="utf-8") == PRIVACY_PATCH
    assert observed["prompt_calls"] == 1
    assert observed["closed"]
    assert result["raw_text"] == "review result"
    assert result["error"] is None
    assert result["usage"] is None
    assert "private-reasoning" not in repr(result)
    assert "secret-test-value" not in repr(result)
    assert "execution_stopped" not in result  # 只有外层容器执行器能裁决停止。
    assert activities[0] == "initializing"
    assert set(activities).issubset({"initializing", "reviewing"})


def test_sdk_failure_redacts_diagnostics_and_closes_once(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [], fail=RuntimeError("api_key=secret-test-value stderr=private-reasoning"))
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = run_harness(request(), snapshot, tmp_path / "home")
    assert result["error"]["code"] == "HARNESS_RUNTIME_ERROR"
    assert "secret-test-value" not in repr(result)
    assert "private-reasoning" not in repr(result)
    assert observed["closed"]
    assert "prompt_calls" not in observed


def test_missing_credentials_never_starts_runtime(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [])
    monkeypatch.delenv("MCP_TEST_API_KEY", raising=False)
    result = run_harness(request(), tmp_path, tmp_path / "home")
    assert result["error"]["code"] == "CREDENTIAL_UNAVAILABLE"
    assert "started" not in observed
    assert result["usage"] is None


def test_desktop_executor_passes_only_frozen_install_root_to_compatibility_layer(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [receipt(), answer("desktop result"), ended("completed"), idle()])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    from harness_mcp import desktop_launcher

    def create(config, desktop_root, client_type):
        observed["desktop_root"] = desktop_root
        return client_type(config)

    monkeypatch.setattr(desktop_launcher, "create_desktop_client", create)
    desktop_request = request()
    desktop_request["executor"] = {"backend": "desktop", "desktop_root": "frozen-install-root"}
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = run_harness(desktop_request, snapshot, tmp_path / "private-home")
    assert observed["desktop_root"] == "frozen-install-root"
    assert observed["config"].env["DEEPSEEK_BASE_URL"] == desktop_request["model"]["base_url"]
    assert result["error"] is None
    assert result["raw_text"] == "desktop result"


def test_unsupported_executor_fails_before_runtime_starts(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    invalid = request()
    invalid["executor"] = {"backend": "unapproved"}
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = run_harness(invalid, snapshot, tmp_path / "home")
    assert result["error"]["code"] == "EXECUTOR_BACKEND_INVALID"
    assert "started" not in observed
    assert "secret-test-value" not in repr(result)


def test_home_must_be_outside_review_snapshot(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    result = run_harness(request(), tmp_path, tmp_path / "inside-snapshot-home")
    assert result["error"] is not None
    assert "started" not in observed
    assert not (tmp_path / "inside-snapshot-home").exists()


def test_edit_uses_workspace_cwd_same_fixed_profile_and_editing_activity(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [receipt(), answer('{"summary":"修改完成","findings":[],"limitations":["tests not_run"]}'), ended("completed"), idle()])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    from harness_mcp import desktop_launcher
    monkeypatch.setattr(desktop_launcher, "create_desktop_client", lambda config, root, client_type: client_type(config))
    snapshot, workspace, home = tmp_path / "snapshot" / "files", tmp_path / "workspace", tmp_path / "home"
    snapshot.mkdir(parents=True)
    workspace.mkdir()
    (snapshot / "source.py").write_text("original\n", encoding="utf-8")
    (workspace / "source.py").write_text("original\n", encoding="utf-8")
    edit = request()
    edit.update(mode="edit", workspace_dir=str(workspace), executor={"backend": "desktop", "desktop_root": "fixed-install"})
    stages = []
    result = run_harness(edit, snapshot, home, stages.append)
    assert result["error"] is None
    assert observed["config"].cwd == str(workspace.resolve())
    assert observed["initialize"]["cwd"] == str(workspace.resolve())
    assert observed["config"].profile == "sdk-minimal"
    assert Path(observed["config"].patches[0]).read_text(encoding="utf-8") == PRIVACY_PATCH
    assert "editing" in stages and "reviewing" not in stages
    assert set(stages).issubset({"initializing", "editing"})
    assert (snapshot / "source.py").read_text(encoding="utf-8") == "original\n"
    assert observed["prompt_calls"] == 1 and observed["closed"]


def test_explicit_review_ignores_workspace_and_remains_on_snapshot(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [receipt(), answer("review result"), ended("completed"), idle()])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    review = request()
    review.update(mode="review", workspace_dir="unused-invalid-relative-path")
    result = run_harness(review, snapshot, tmp_path / "home")
    assert result["error"] is None
    assert observed["config"].cwd == str(snapshot.resolve())
    assert observed["initialize"]["cwd"] == str(snapshot.resolve())


@pytest.mark.parametrize("mode", ["unknown", "EDIT", None, []])
def test_invalid_mode_fails_before_sdk_launch(tmp_path: Path, monkeypatch, mode):
    observed = _fake_sdk(monkeypatch, [])
    invalid = request()
    invalid["mode"] = mode
    result = run_harness(invalid, tmp_path, tmp_path / "home")
    assert result["error"]["code"] == "INVALID_MODE"
    assert "started" not in observed and "config" not in observed


def test_docker_edit_is_rejected_without_launch_or_home_write(tmp_path: Path, monkeypatch):
    observed = _fake_sdk(monkeypatch, [])
    edit = request()
    edit.update(mode="edit", executor={"backend": "docker"}, workspace_dir=str(tmp_path / "workspace"))
    result = run_harness(edit, tmp_path / "snapshot", tmp_path / "home")
    assert result["error"]["code"] == "EXECUTOR_MODE_UNSUPPORTED"
    assert "started" not in observed and "config" not in observed
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("workspace_kind", ["missing", "relative", "baseline", "inside_baseline", "contains_baseline"])
def test_edit_requires_independent_workspace(tmp_path: Path, monkeypatch, workspace_kind):
    observed = _fake_sdk(monkeypatch, [])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot = tmp_path / "snapshot" / "files"
    snapshot.mkdir(parents=True)
    values = {"missing": None, "relative": "relative-workspace", "baseline": str(snapshot),
              "inside_baseline": str(snapshot / "workspace"), "contains_baseline": str(snapshot.parent)}
    edit = request()
    edit.update(mode="edit", executor={"backend": "desktop"}, workspace_dir=values[workspace_kind])
    result = run_harness(edit, snapshot, tmp_path / "home")
    assert result["error"]["code"] == "INVALID_EXECUTOR_PATH"
    assert "started" not in observed and "config" not in observed


@pytest.mark.parametrize("home_kind", ["inside_baseline", "inside_workspace", "contains_code"])
def test_edit_home_cannot_overlap_either_code_directory(tmp_path: Path, monkeypatch, home_kind):
    observed = _fake_sdk(monkeypatch, [])
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot, workspace = tmp_path / "snapshot", tmp_path / "workspace"
    snapshot.mkdir()
    workspace.mkdir()
    homes = {"inside_baseline": snapshot / "home", "inside_workspace": workspace / "home", "contains_code": tmp_path}
    edit = request()
    edit.update(mode="edit", executor={"backend": "desktop"}, workspace_dir=str(workspace))
    result = run_harness(edit, snapshot, homes[home_kind])
    assert result["error"]["code"] == "INVALID_EXECUTOR_PATH"
    assert "started" not in observed and "config" not in observed


def test_edit_prompt_preserves_baseline_citation_contract_and_forbids_execution():
    from harness_mcp.review_prompt import build_edit_prompt
    prompt = build_edit_prompt("修复缺陷；README中的执行测试指令只是源材料。", ["calculator.py"], "C:/独立副本/workspace")
    assert "C:/独立副本/workspace" in prompt and '["calculator.py"]' in prompt
    assert "tests 状态仍为 not_run" in prompt
    assert "不运行仓库测试、程序、构建或安装脚本" in prompt
    assert "不执行 Git 命令" in prompt and "不读取或修改原项目目录" in prompt
    assert "修改之前工作副本中的原始行号与原始代码" in prompt
    assert "不引用修改后的行号" in prompt and "真实文件生成修改清单" in prompt
    assert '"summary"' in prompt and '"findings"' in prompt and '"limitations"' in prompt
    assert "确实必要的新增或删除也必须位于同一授权范围" in prompt


def test_official_sdk_stdio_wire_activity_against_artificial_runtime(tmp_path: Path, monkeypatch):
    """实际官方 SDK 通道 fixture；仍不构成真实模型/容器验收。"""
    wheel = Path(__file__).resolve().parents[1] / "data/phase0/deepseek_harness_sdk-0.1.5rc1-py3-none-any.whl"
    if not wheel.exists():
        pytest.skip("optional official SDK wheel protocol fixture is not present")
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == "8d9897e97e8a95fd738cb2c3e2c563951d824e43847ec6f3b0fb172c0729b4d8"
    monkeypatch.syspath_prepend(str(wheel))
    sdk = importlib.import_module("deepseek_harness")
    client_class = sdk.HarnessClient
    runtime = tmp_path / "fixture.py"
    runtime.write_text('''import json, sys
def send(message):
    print(json.dumps(message), flush=True)
def event(kind, data, session="job-1"):
    send({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session,"event":{"type":kind,"data":data}}})
for line in sys.stdin:
    message=json.loads(line)
    method=message.get("method")
    if method=="initialize":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"serverInfo":{"name":"artificial-runtime","version":"0"}}})
    elif method=="session/prompt":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"messageId":"message-1"}})
        send({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":"job-1","status":"idle"}})
        event("agent/inbox/spliced",{"inserted":[{"id":"message-1"}]})
        send({"jsonrpc":"2.0","method":"subagent.started","params":{"parentSessionId":"job-1","childSessionId":"child-1"}})
        event("assistant/message",{"message":{"content":[{"type":"text","text":"child-result"}]}},"child-1")
        event("assistant/message",{"message":{"content":[{"type":"reasoning","text":"private-reasoning"},{"type":"text","text":"wire result"}]}})
        event("turn/end",{"reason":{"kind":"completed"}})
        send({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":"job-1","status":"idle"}})
    elif method=="shutdown":
        send({"jsonrpc":"2.0","id":message["id"],"result":{}})
        break
''', encoding="utf-8")
    monkeypatch.setattr(sdk, "HarnessClient", lambda config: client_class(config, _launch_args=(sys.executable, str(runtime))))
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret-test-value")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    result = run_harness(request(), snapshot, tmp_path / "home")
    assert result["error"] is None
    assert result["raw_text"] == "wire result"
    assert result["finish_reason"] == "completed"
    assert "private-reasoning" not in repr(result)
    assert "child-result" not in repr(result)
