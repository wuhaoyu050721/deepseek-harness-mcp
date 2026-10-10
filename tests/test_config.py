from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from harness_mcp.config import (
    AppConfig, LimitsConfig, ModelConfig, ProjectConfig, frozen_execution,
    load_config, normalize_relative_path, normalize_request, request_fingerprint,
    resolve_credential,
)
from harness_mcp.errors import HarnessError


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "源 项目"
    project.mkdir()
    return AppConfig(tmp_path / "data", {"demo": ProjectConfig("demo", project)})


def write_config(tmp_path: Path, section: str | None = None, field: str | None = None, value=None) -> Path:
    def toml_value(item):
        if isinstance(item, bool):
            return str(item).lower()
        return json.dumps(item, ensure_ascii=False)
    lines = ['data_dir = "state"']
    if section is None and field:
        lines.append(f"{field} = {toml_value(value)}")
    if section:
        lines.extend([f"[{section}]", f"{field} = {toml_value(value)}"])
    lines.extend(["[projects.demo]", f"path = {toml_value(str(tmp_path))}"])
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_config_relative_state_and_absolute_project_with_defaults(tmp_path):
    loaded = load_config(write_config(tmp_path))
    assert loaded.data_dir == tmp_path / "state"
    assert loaded.projects["demo"].path == tmp_path
    assert loaded.projects["demo"].allowed_paths == (".",)
    assert loaded.limits.max_queue == 5
    assert loaded.limits.max_parallel_jobs == 3
    assert loaded.limits.default_timeout_seconds == 600
    assert loaded.executor.backend == "desktop"


@pytest.mark.parametrize("section,field,value", [
    (None, "unsupported", 1), ("limits", "unknown", 1),
    ("limits", "max_queue", 0), ("limits", "max_queue", 6),
    ("limits", "max_parallel_jobs", 0), ("limits", "max_parallel_jobs", 9), ("limits", "max_parallel_jobs", True),
    ("limits", "max_timeout_seconds", True), ("limits", "default_timeout_seconds", 1801),
    ("limits", "max_output_bytes", "100"), ("limits", "max_file_bytes", 0),
    ("executor", "cpus", False), ("executor", "cpus", 65),
    ("executor", "memory_mb", 127), ("executor", "pids_limit", 15),
    ("executor", "heartbeat_seconds", 6), ("executor", "image", "image;run"),
    ("executor", "backend", "unknown"), ("executor", "desktop_root", "relative/path"),
    ("executor", "backend", ["desktop"]),
    ("executor", "desktop_root", 123),
    ("model", "base_url", "http://external.example"),
    ("model", "base_url", "https://user:pass@example.com"),
    ("model", "base_url", "https://example.com?api_key=secret"),
    ("model", "credential_source", "inline_secret"),
    ("model", "credential_env", "bad-name"),
])
def test_config_known_fields_numeric_limits_and_network_boundary(tmp_path, section, field, value):
    with pytest.raises(HarnessError) as exc:
        load_config(write_config(tmp_path, section, field, value))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("field,value", [
    ("base_url", 123), ("base_url", ["https://api.example.com"]),
    ("model", 123), ("model", True), ("provider", 123),
    ("credential_env", 123), ("base_url", "https://example.com:abc"),
])
def test_model_configuration_type_and_port_errors_are_normalized(tmp_path, field, value):
    with pytest.raises(HarnessError) as exc:
        load_config(write_config(tmp_path, "model", field, value))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("url", ["http://localhost:1234/v1", "http://127.0.0.1:1234", "http://[::1]:1234/v1", "https://api.example.com/v1"])
def test_permitted_model_urls(tmp_path, url):
    assert load_config(write_config(tmp_path, "model", "base_url", url)).model.base_url == url


@pytest.mark.parametrize("bad", ["../escape", "src/../file", "C:\\outside", "C:relative", "\\\\server\\share", "/etc/passwd", "src/file:ads", "src//file", "src/file.", "src/file "])
def test_relative_path_escape_and_windows_aliases_rejected(bad):
    with pytest.raises(HarnessError) as exc:
        normalize_relative_path(bad)
    assert exc.value.code == "INVALID_PATH"


@pytest.mark.parametrize("bad", ["src/NUL.txt", "src/CON", "src/file\nname.py"])
def test_windows_device_and_control_paths_rejected_before_persistence(bad):
    with pytest.raises(HarnessError) as exc:
        normalize_relative_path(bad)
    assert exc.value.code == "INVALID_PATH"


def test_request_normalization_preserves_prompt_and_binds_fingerprint(config):
    left = normalize_request(config, "demo", "审查\r\n代码", "req-1", ["src\\a.py", "src/b.py", "src/a.py"], 1)
    right = normalize_request(config, "demo", "审查\n代码", "req-1", ["src/b.py", "src/a.py"], 1)
    assert left["relative_paths"] == ["src/a.py", "src/b.py"]
    assert request_fingerprint(left) == request_fingerprint(right)
    assert request_fingerprint(left) != request_fingerprint({**right, "prompt": "不同任务"})


@pytest.mark.parametrize("timeout", [0, -1, 1801, True, 1.5, "10"])
def test_request_timeout_only_accepts_bounded_integer(config, timeout):
    with pytest.raises(HarnessError) as exc:
        normalize_request(config, "demo", "read", "req", timeout_seconds=timeout)
    assert exc.value.code == "INVALID_TIMEOUT"


@pytest.mark.parametrize("field,value,code", [
    ("project_id", "other", "PROJECT_NOT_ALLOWED"),
    ("prompt", " ", "INVALID_PROMPT"), ("prompt", "x" * 32001, "INVALID_PROMPT"),
    ("client_request_id", "", "INVALID_REQUEST_ID"), ("client_request_id", "x" * 129, "INVALID_REQUEST_ID"),
    ("relative_paths", "src", "INVALID_PATH"), ("relative_paths", ["src"] * 257, "INVALID_PATH"),
])
def test_request_shape_and_capacity_boundaries(config, field, value, code):
    request = {"project_id": "demo", "prompt": "read", "client_request_id": "req"}
    request[field] = value
    with pytest.raises(HarnessError) as exc:
        normalize_request(config, **request)
    assert exc.value.code == code


def test_empty_scope_is_not_expanded_to_default_authorization(config):
    with pytest.raises(HarnessError) as exc:
        normalize_request(config, "demo", "read", "req", relative_paths=[])
    assert exc.value.code in {"INVALID_PATH", "EMPTY_SCOPE"}


def test_credentials_are_reference_only_in_frozen_execution(config, monkeypatch):
    secret = "test-local-secret-never-persist"
    model = ModelConfig(credential_source="env", credential_env="TEST_HARNESS_SECRET")
    configured = replace(config, model=model)
    monkeypatch.setenv("TEST_HARNESS_SECRET", secret)
    assert resolve_credential(frozen_execution(configured, "demo")["model"]) == secret
    payload = json.dumps(frozen_execution(configured, "demo"))
    assert secret not in payload
    assert "TEST_HARNESS_SECRET" in payload


def test_missing_credential_is_safe_error(config, monkeypatch):
    monkeypatch.delenv("TEST_HARNESS_SECRET", raising=False)
    model = {"credential_source": "env", "credential_env": "TEST_HARNESS_SECRET"}
    with pytest.raises(HarnessError) as exc:
        resolve_credential(model)
    assert exc.value.code == "CREDENTIAL_UNAVAILABLE"


def test_edit_permission_is_trusted_project_setting(config):
    with pytest.raises(HarnessError) as exc:
        normalize_request(config, "demo", "修复", "edit-one", mode="edit")
    assert exc.value.code == "EDIT_NOT_ALLOWED"
    configured = replace(config, projects={"demo": replace(config.projects["demo"], allow_edits=True)})
    request = normalize_request(configured, "demo", "修复", "edit-one", mode="edit")
    assert request["mode"] == "edit"
    with pytest.raises(HarnessError) as exc:
        normalize_request(replace(configured, executor=replace(configured.executor, backend="docker")),
                          "demo", "修复", "edit-one", mode="edit")
    assert exc.value.code == "EDIT_BACKEND_UNSUPPORTED"


@pytest.mark.parametrize("mode", ["write", "EDIT", "", None, True, 1])
def test_unknown_edit_modes_rejected(config, mode):
    with pytest.raises(HarnessError) as exc:
        normalize_request(config, "demo", "read", "one", mode=mode)
    assert exc.value.code == "INVALID_MODE"


def test_review_fingerprint_preserves_legacy_replay(config):
    current = normalize_request(config, "demo", "read", "old-one")
    legacy = {key: value for key, value in current.items() if key != "mode"}
    assert request_fingerprint(current) == request_fingerprint(legacy)
    assert request_fingerprint(current) != request_fingerprint({**current, "mode": "edit"})


@pytest.mark.parametrize("value", ['"true"', "1", "[]"])
def test_edit_permission_requires_bool_in_local_configuration(tmp_path, value):
    path = write_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + '\nallow_edits = ' + value, encoding="utf-8")
    with pytest.raises(HarnessError) as exc:
        load_config(path)
    assert exc.value.code == "INVALID_CONFIG"
