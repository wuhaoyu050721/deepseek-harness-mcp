"""共享代理的认证、归属与私有文件守卫；所有任务均不调用模型。"""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import struct
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.instance_lock import InstanceLock
from harness_mcp import shared_bootstrap as bootstrap
from harness_mcp.shared_client import AUTH_MAX_BYTES, SharedClient, authentication_proof, open_authenticated, read_frame, write_frame


@pytest.fixture
def private_runtime(tmp_path, monkeypatch):
    # 文件身份与归属单测独立于 ACL 子进程；真实 Windows ACL 由启动验收验证。
    monkeypatch.setattr(bootstrap, "_permissions_safe", lambda *args, **kwargs: True)
    monkeypatch.setattr(bootstrap, "_restrict_permissions", lambda *args, **kwargs: None)
    return bootstrap.prepare_private_runtime(tmp_path / "data" / "shared-service")


def test_digest_covers_project_authorization_model_and_data_dir(tmp_path):
    config = AppConfig(tmp_path / "data", projects={"p": ProjectConfig("p", tmp_path / "project", ("src",))})
    digest = bootstrap.configuration_digest(config)
    assert len(digest) == 64
    assert digest != bootstrap.configuration_digest(replace(config, data_dir=tmp_path / "other"))
    assert digest != bootstrap.configuration_digest(replace(config, projects={"p": replace(config.projects["p"], allow_edits=True)}))
    assert digest != bootstrap.configuration_digest(replace(config, model=replace(config.model, base_url="https://example.org")))
    assert digest == bootstrap.configuration_digest(config)


def test_bootstrap_never_persists_model_environment_even_whitelist_name(tmp_path, monkeypatch):
    config = AppConfig(tmp_path, model=ModelConfig(credential_source="env", credential_env="PATH"))
    monkeypatch.setenv("PATH", "private-model-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "another-model-value")
    monkeypatch.setenv("ARBITRARY_SECRET", "arbitrary-value")
    value = bootstrap._bootstrap_payload(config, tmp_path / "config.toml", "a" * 64)
    wire = json.dumps(value)
    assert "private-model-value" not in wire
    assert "another-model-value" not in wire
    assert "arbitrary-value" not in wire
    assert "PATH" not in value["nonsecret_environment"]
    assert len(value["token"]) == 64


def test_digest_accepts_trusted_wheel_cache_hardlinks_and_detects_changes(tmp_path, monkeypatch):
    module = tmp_path / "module"
    module.mkdir()
    source = module / "__init__.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    os.link(source, tmp_path / "cached-module.py")
    monkeypatch.setattr(bootstrap, "__file__", str(module / "shared_bootstrap.py"))
    config = AppConfig(tmp_path / "data")
    digest = bootstrap.configuration_digest(config)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    assert digest != bootstrap.configuration_digest(config)


def test_digest_rejects_code_read_drift(tmp_path, monkeypatch):
    module = tmp_path / "module"
    module.mkdir()
    source = module / "__init__.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "__file__", str(module / "shared_bootstrap.py"))
    original = os.fstat

    def inspected(descriptor):
        value = original(descriptor)
        source.write_text("VERSION = 2\n", encoding="utf-8")
        return value

    monkeypatch.setattr(os, "fstat", inspected)
    with pytest.raises(HarnessError) as error:
        bootstrap.configuration_digest(AppConfig(tmp_path / "data"))
    assert error.value.code == "SHARED_STORAGE_UNSAFE"


def test_private_json_atomic_round_trip(private_runtime):
    path = private_runtime / "registry.json"
    bootstrap.write_private_json(path, {"version": 1, "token": "first"})
    bootstrap.write_private_json(path, {"version": 1, "token": "second"})
    assert bootstrap.read_private_json(path) == {"version": 1, "token": "second"}
    assert not list(private_runtime.glob(".private-*.tmp"))


def test_private_json_rejects_hard_link_before_read_or_replace(private_runtime):
    target = private_runtime / "sensitive.json"
    target.write_text('{"unchanged":true}', encoding="utf-8")
    linked = private_runtime / "registry.json"
    os.link(target, linked)
    for operation in (lambda: bootstrap.read_private_json(linked), lambda: bootstrap.write_private_json(linked, {"changed": True})):
        with pytest.raises(HarnessError, match="登记目录") as error:
            operation()
        assert error.value.code == "SHARED_STORAGE_UNSAFE"
    assert target.read_text(encoding="utf-8") == '{"unchanged":true}'


def test_private_directory_identity_drift_rejected(private_runtime):
    moved = private_runtime.with_name("previous-runtime")
    private_runtime.rename(moved)
    private_runtime.mkdir()
    with pytest.raises(HarnessError) as error:
        bootstrap.write_private_json(private_runtime / "registry.json", {"x": 1})
    assert error.value.code == "SHARED_STORAGE_UNSAFE"


def test_symlink_ancestor_not_used_for_creation(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account cannot create symlinks")
    with pytest.raises(HarnessError):
        bootstrap.prepare_private_runtime(link / "created" / "runtime")
    assert not (real / "created").exists()


def test_reparse_flag_rejected(tmp_path, monkeypatch):
    original = Path.lstat
    path = tmp_path / "runtime"
    path.mkdir()

    def information(self):
        result = original(self)
        if self == path:
            class Reparse:
                st_mode = stat.S_IFDIR | 0o700
                st_file_attributes = 0x400
            return Reparse()
        return result

    monkeypatch.setattr(Path, "lstat", information)
    with pytest.raises(HarnessError) as error:
        bootstrap._assert_path(path)
    assert error.value.code == "SHARED_STORAGE_UNSAFE"


def test_failed_permission_creation_does_not_claim_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_restrict_permissions", lambda *args, **kwargs: (_ for _ in ()).throw(bootstrap._unsafe()))
    with pytest.raises(HarnessError) as error:
        bootstrap.prepare_private_runtime(tmp_path / "runtime")
    assert error.value.code == "SHARED_STORAGE_UNSAFE"


def test_existing_unsafe_permissions_not_silently_repaired(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(bootstrap, "_permissions_safe", lambda *args, **kwargs: False)
    with patch.object(bootstrap, "_restrict_permissions") as repair:
        with pytest.raises(HarnessError):
            bootstrap.prepare_private_runtime(runtime)
        repair.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows WMI launch contract")
def test_windows_launcher_fixed_hidden_command_and_rejects_wmi_failure(tmp_path):
    config_path, bootstrap_path = tmp_path / "config.toml", tmp_path / "bootstrap.json"
    with patch.object(bootstrap, "_powershell_script", return_value={"pid": 123, "return_value": 0}) as launch:
        assert bootstrap._launch_daemon(config_path, bootstrap_path) == 123
    command = launch.call_args.args[0]
    assert "ShowWindow=[uint16]0" in command
    assert "Win32_Process" in command
    assert "-I -m harness_mcp.shared_service" in command
    assert "--bootstrap" in command
    with patch.object(bootstrap, "_powershell_script", return_value={"pid": 0, "return_value": 2}):
        with pytest.raises(HarnessError) as error:
            bootstrap._launch_daemon(config_path, bootstrap_path)
    assert error.value.code == "SHARED_LAUNCH_FAILED"


def test_launcher_preserves_virtual_environment_python_symlink(tmp_path, monkeypatch):
    environment = tmp_path / "venv" / "Scripts"
    environment.mkdir(parents=True)
    base_python = tmp_path / "base-python.exe"
    base_python.write_bytes(b"artificial interpreter marker")
    python = environment / "python.exe"
    try:
        python.symlink_to(base_python)
    except OSError:
        pytest.skip("account cannot create symlink")
    monkeypatch.setattr(bootstrap.sys, "executable", str(python))
    if os.name == "nt":
        with patch.object(bootstrap, "_powershell_script", return_value={"pid": 123, "return_value": 0}) as launch:
            bootstrap._launch_daemon(tmp_path / "config.toml", tmp_path / "bootstrap.json")
        assert str(python) in launch.call_args.args[0]
        assert str(base_python) not in launch.call_args.args[0]
    else:
        with patch.object(bootstrap.subprocess, "Popen") as launch:
            bootstrap._launch_daemon(tmp_path / "config.toml", tmp_path / "bootstrap.json")
        assert launch.call_args.args[0][0] == str(python)
        assert launch.call_args.kwargs["start_new_session"] is True


class SharedFrameAndConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_json_rejects_duplicate_nonfinite_nonobject_and_oversize(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]', b'{}garbage'):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", len(raw)) + raw)
            with self.assertRaises(HarnessError):
                await read_frame(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", AUTH_MAX_BYTES + 1))
        with self.assertRaises(HarnessError):
            await asyncio.wait_for(read_frame(reader), 0.1)

    async def test_proof_binds_role_challenge_nonce_config(self):
        token, challenge, nonce, digest = "a" * 64, "b" * 64, "c" * 64, "d" * 64
        proof = authentication_proof(token, "client", challenge, nonce, digest)
        self.assertNotEqual(proof, authentication_proof(token, "server", challenge, nonce, digest))
        self.assertNotEqual(proof, authentication_proof(token, "client", "e" * 64, nonce, digest))
        self.assertNotEqual(proof, authentication_proof(token, "client", challenge, "e" * 64, digest))
        self.assertNotEqual(proof, authentication_proof(token, "client", challenge, nonce, "e" * 64))

    async def test_bad_server_proof_never_receives_credentials(self):
        captured = []
        completed = asyncio.Event()

        async def connection(reader, writer):
            await write_frame(writer, {"version": 1, "challenge": "b" * 64, "config_digest": "d" * 64})
            captured.append(await read_frame(reader))
            await write_frame(writer, {"version": 1, "proof": "0" * 64})
            captured.append(await reader.read())
            writer.close()
            await writer.wait_closed()
            completed.set()

        server = await asyncio.start_server(connection, "127.0.0.1", 0)
        try:
            registry = {"port": server.sockets[0].getsockname()[1], "token": "a" * 64, "config_digest": "d" * 64}
            with self.assertRaises(HarnessError) as error:
                await open_authenticated(registry)
            self.assertEqual(error.exception.code, "SHARED_AUTH_FAILED")
            await asyncio.wait_for(completed.wait(), 2)
            self.assertEqual(set(captured[0]), {"version", "nonce", "config_digest", "proof"})
            self.assertEqual(captured[1], b"")
        finally:
            server.close()
            await server.wait_closed()

    async def test_lost_mutation_not_replayed_and_close_only_disconnects(self):
        captured = []
        completed = asyncio.Event()

        async def connection(reader, writer):
            captured.append(await read_frame(reader))
            writer.close()
            await writer.wait_closed()
            completed.set()

        server = await asyncio.start_server(connection, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            client = SharedClient(AppConfig(Path.cwd()), Path.cwd() / "unused.toml")
            client.reader, client.writer = reader, writer
            with self.assertRaises(HarnessError) as error:
                await client.cancel("one")
            self.assertEqual(error.exception.code, "SHARED_CONNECTION_LOST")
            await asyncio.wait_for(completed.wait(), 2)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["method"], "cancel")
            with self.assertRaises(HarnessError) as error:
                await client.cancel("one")
            self.assertEqual(error.exception.code, "SHARED_CONNECTION_CLOSED")
            await client.close()
            self.assertEqual(len(captured), 1)
        finally:
            server.close()
            await server.wait_closed()


def test_live_legacy_data_lock_never_launches_daemon(private_runtime, tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config = AppConfig(private_runtime.parent)
    config_path.write_text(f'data_dir = {json.dumps(str(config.data_dir))}\n', encoding="utf-8")
    lock = InstanceLock(config.data_dir)
    try:
        with patch.object(bootstrap, "_launch_daemon") as launch:
            with pytest.raises(HarnessError) as error:
                asyncio.run(bootstrap.connect_or_start(config, config_path))
            assert error.value.code == "SHARED_BACKEND_UNAVAILABLE"
            launch.assert_not_called()
        assert not (private_runtime / "bootstrap.json").exists()
    finally:
        lock.close()


def test_startup_failure_bound_to_launched_pid_returns_stable_error(private_runtime, tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config = AppConfig(private_runtime.parent)
    config_path.write_text(f'data_dir = {json.dumps(str(config.data_dir))}\n', encoding="utf-8")

    def failed_launch(config_path, bootstrap_path):
        value = bootstrap.read_private_json(bootstrap_path)
        bootstrap.write_private_json(private_runtime / "startup-error.json", {
            "version": 1, "pid": 23456, "parent_pid": 12345, "config_digest": value["config_digest"], "startup_id": value["startup_id"],
            "code": "INVALID_CONFIG", "message": "must-not-echo-original-output",
        })
        return 12345

    monkeypatch.setattr(bootstrap, "_launch_daemon", failed_launch)
    with pytest.raises(HarnessError) as error:
        asyncio.run(bootstrap.connect_or_start(config, config_path))
    assert error.value.code == "INVALID_CONFIG"
    assert "must-not-echo" not in error.value.message
    # 报错不会遗留启动互斥，也不删除后台的历史归属。
    lock = InstanceLock(private_runtime / "start-lock")
    lock.close()


def test_live_different_permission_registry_cannot_be_reused(private_runtime, tmp_path):
    config_path = tmp_path / "config.toml"
    config = AppConfig(private_runtime.parent)
    config_path.write_text(f'data_dir = {json.dumps(str(config.data_dir))}\n', encoding="utf-8")
    bootstrap.write_private_json(private_runtime / "registry.json", {
        "version": 1, "host": "127.0.0.1", "port": 12345, "pid": 12345,
        "token": "a" * 64, "config_digest": "b" * 64,
        "data_dir": str(config.data_dir.resolve()), "service_version": "0.3.2",
    })
    writer = unittest.mock.MagicMock()
    writer.wait_closed = AsyncMock()
    with patch("harness_mcp.shared_client.open_authenticated", AsyncMock(return_value=(object(), writer))), patch.object(bootstrap, "_launch_daemon") as launch:
        with pytest.raises(HarnessError) as error:
            asyncio.run(bootstrap.connect_or_start(config, config_path))
        assert error.value.code == "SHARED_CONFIG_MISMATCH"
        launch.assert_not_called()
    writer.close.assert_called_once()


def test_startup_permission_failure_releases_start_lock(private_runtime, tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config = AppConfig(private_runtime.parent)
    config_path.write_text(f'data_dir = {json.dumps(str(config.data_dir))}\n', encoding="utf-8")
    bootstrap.prepare_private_runtime(private_runtime / "start-lock")
    monkeypatch.setattr(bootstrap, "_restrict_permissions", lambda *args, **kwargs: (_ for _ in ()).throw(bootstrap._unsafe()))
    with pytest.raises(HarnessError):
        asyncio.run(bootstrap.connect_or_start(config, config_path))
    lock = InstanceLock(private_runtime / "start-lock")
    lock.close()
