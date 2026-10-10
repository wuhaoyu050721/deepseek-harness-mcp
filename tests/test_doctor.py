from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct

import pytest

from harness_mcp import doctor
from harness_mcp.config import AppConfig, ExecutorConfig, ModelConfig, ProjectConfig


def make_desktop(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "DeepSeek Harness.exe").write_bytes(b"MZfixture")
    command = root / "resources/runtime/cli/bin/dsh.cmd"
    command.parent.mkdir(parents=True)
    command.write_text("fixture")
    package = json.dumps({"version": "0.2.0-rc.2"}).encode()
    tree = {"files": {"package.json": {"size": len(package), "offset": "1"}}}
    node = tree
    for part in doctor.CLI_ASAR_PATH.split("/")[:-1]:
        node = node.setdefault("files", {}).setdefault(part, {})
    node.setdefault("files", {})["cli.js"] = {"size": 1, "offset": "0"}
    header = json.dumps(tree, separators=(",", ":")).encode()
    padding = b"\x00" * (-len(header) % 4)
    header_size = len(header) + len(padding) + 8
    archive = root / "resources/app.asar"
    archive.write_bytes(struct.pack("<4I", 4, header_size, header_size - 4, len(header)) + header + padding + b"x" + package)
    return command


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "桌面 安装"
    command = make_desktop(root)
    config = AppConfig(tmp_path / "data", {"sample": ProjectConfig("sample", tmp_path)}, model=ModelConfig(credential_source="env", credential_env="DOCTOR_TEST_CREDENTIAL"), executor=ExecutorConfig(backend="desktop", desktop_root=str(root)))
    monkeypatch.setenv("DOCTOR_TEST_CREDENTIAL", "test-key-must-never-be-in-doctor-output")
    monkeypatch.setattr(doctor, "_windows_candidates", lambda: {"harness": [command]})
    monkeypatch.setattr(doctor, "_find_command", lambda name, _: "git" if name == "git" else None)
    monkeypatch.setattr(doctor, "_cli_version", lambda name, executable: {"installed": bool(executable), "executable": executable, "version": "1.2.3" if executable else None, "status": "ok" if executable else "missing"})
    monkeypatch.setattr(doctor, "_package_version", lambda name: {"installed": True, "version": "0.1.5rc1" if name == "deepseek-harness-sdk" else "2.3.0"})
    monkeypatch.setattr(doctor, "_docker_server", lambda _: {"available": False, "linux_server": False, "status": "missing"})
    monkeypatch.setattr(doctor, "_wsl_status", lambda _: {"installed": False, "status": "missing"})
    monkeypatch.setattr(doctor, "_desktop_credential_store_nonempty", lambda: False)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    return config


def test_desktop_ready_checks_do_not_require_docker_or_packaged_runtime(environment):
    report = doctor.collect_checks(environment)
    assert report["execution_backend"] == "desktop"
    assert report["execution_ready"]
    assert report["execution_ready_scope"] == "prerequisites_only"
    assert not report["acceptance_verified"]
    assert report["desktop_harness"]["version"] == "0.2.0-rc.2"
    assert report["desktop_harness"]["cli_entry_verified"]
    assert report["packages"]["deepseek-harness-sdk"]["required_scope"] == "host"
    assert not report["container"]["available"]
    assert not report["blocking"]
    encoded = json.dumps(report)
    assert "test-key-must-never-be-in-doctor-output" not in encoded
    assert "deepseek-harness-runtime-bin" not in report["packages"]


def test_asar_archive_index_is_read_without_launching_runtime(tmp_path, monkeypatch):
    root = tmp_path / "desktop"
    make_desktop(root)
    monkeypatch.setattr(doctor, "_bounded_command", lambda _: pytest.fail("doctor must not start desktop runtime"))
    report = doctor._desktop_harness({}, str(root))
    assert report["installed"] and report["cli_entry_verified"]
    assert not Path(report["cli_entry"]).is_file()


@pytest.mark.parametrize("missing", ["DeepSeek Harness.exe", "resources/app.asar"])
def test_desktop_incomplete_install_is_not_ready(environment, missing):
    (Path(environment.executor.desktop_root) / missing).unlink()
    report = doctor.collect_checks(environment)
    assert not report["execution_ready"]
    assert "DESKTOP_RUNTIME_UNAVAILABLE" in {item["code"] for item in report["blocking"]}


def test_desktop_compatibility_launcher_does_not_require_cmd_wrapper(environment):
    (Path(environment.executor.desktop_root) / "resources/runtime/cli/bin/dsh.cmd").unlink()
    report = doctor.collect_checks(environment)
    assert report["execution_ready"]
    assert not report["desktop_harness"]["public_cli_available"]


def test_unrecognized_sdk_version_is_not_ready(environment, monkeypatch):
    monkeypatch.setattr(doctor, "_package_version", lambda name: {"installed": True, "version": "0.2.0" if name == "deepseek-harness-sdk" else "2.3.0"})
    report = doctor.collect_checks(environment)
    assert not report["execution_ready"]
    assert "SDK_VERSION_UNSUPPORTED" in {item["code"] for item in report["blocking"]}


def test_missing_sdk_is_not_ready(environment, monkeypatch):
    monkeypatch.setattr(doctor, "_package_version", lambda name: {"installed": name != "deepseek-harness-sdk", "version": "2.3.0" if name == "mcp" else None})
    report = doctor.collect_checks(environment)
    assert not report["execution_ready"]
    assert any(item["code"] == "PACKAGE_MISSING" and item.get("package") == "deepseek-harness-sdk" for item in report["blocking"])


def test_missing_reference_and_non_windows_are_independent_blockers(environment, monkeypatch):
    monkeypatch.delenv("DOCTOR_TEST_CREDENTIAL")
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    report = doctor.collect_checks(environment)
    assert not report["execution_ready"]
    assert {"MODEL_CREDENTIAL_UNAVAILABLE", "DESKTOP_OS_UNSUPPORTED"} <= {item["code"] for item in report["blocking"]}


def test_docker_optional_backend_retains_docker_blockers(environment):
    config = replace(environment, executor=replace(environment.executor, backend="docker"))
    report = doctor.collect_checks(config)
    assert not report["execution_ready"]
    assert report["packages"]["deepseek-harness-sdk"]["required_scope"] == "worker_container"
    assert "CONTAINER_RUNTIME_UNAVAILABLE" in {item["code"] for item in report["blocking"]}
    assert "DESKTOP_RUNTIME_UNAVAILABLE" not in {item["code"] for item in report["blocking"]}


def test_asar_oversized_or_truncated_headers_are_rejected(tmp_path):
    archive = tmp_path / "bad.asar"
    huge = doctor.ASAR_HEADER_LIMIT_BYTES + 4
    archive.write_bytes(struct.pack("<4I", 4, huge + 8, huge + 4, huge))
    assert doctor._asar_entries(archive) == (False, None)
    archive.write_bytes(b"short")
    assert doctor._asar_entries(archive) == (False, None)


def test_doctor_cli_configuration_and_ready_exit_code(environment, tmp_path, capsys):
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    config_path = tmp_path / "doctor.local.toml"
    config_path.write_text(
        f"data_dir = {quote(environment.data_dir)}\n"
        f"[model]\ncredential_source = \"env\"\ncredential_env = \"DOCTOR_TEST_CREDENTIAL\"\n"
        f"[executor]\nbackend = \"desktop\"\ndesktop_root = {quote(environment.executor.desktop_root)}\n"
        f"[projects.sample]\npath = {quote(tmp_path)}\n", encoding="utf-8",
    )
    output = tmp_path / "doctor.json"
    assert doctor.main(["--config", str(config_path), "--output", str(output), "--require-execution"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["execution_ready"] and report["configuration"]["credential_validity"] == "not_verified"
    assert "test-key-must-never-be-in-doctor-output" not in capsys.readouterr().out


def test_invalid_config_cannot_keep_ready_true(environment, tmp_path, monkeypatch, capsys):
    original = doctor.collect_checks
    monkeypatch.setattr(doctor, "collect_checks", lambda _: original(environment))
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('data_dir = "state"\nsecret_value = "do-not-output-this"\n')
    assert doctor.main(["--config", str(invalid), "--require-execution"]) == 1
    printed = capsys.readouterr().out
    assert "do-not-output-this" not in printed
    assert json.loads(printed)["configuration"]["valid"] is False
