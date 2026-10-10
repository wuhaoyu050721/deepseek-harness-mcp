"""真实本机连接验证共享队列、相互认证、凭证隔离和前端生命周期。"""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import struct

import pytest

from harness_mcp.config import AppConfig, ModelConfig
from harness_mcp.errors import HarnessError
from harness_mcp.shared_client import authentication_proof, read_frame, write_frame
from harness_mcp.shared_service import serve


class _Store:
    def __init__(self):
        self.jobs = {}

    def has_pending(self):
        return any(value["status"] == "queued" for value in self.jobs.values())


class _Manager:
    def __init__(self, config):
        self.config = config
        self.store = _Store()
        self.secrets = []
        self.closed = False
        self.close_calls = 0
        self.close_confirmed = True
        self.executions = 0
        self.requests = {}
        self.reject_secret = None

    def _register_secret(self, value):
        if value not in self.secrets:
            self.secrets.append(value)

    async def start(self):
        return None

    async def close(self):
        self.close_calls += 1
        self.closed = self.close_confirmed

    async def submit(self, project_id, prompt, client_request_id, **other):
        if client_request_id in self.requests:
            old, job_id = self.requests[client_request_id]
            if old != (project_id, prompt, other):
                raise HarnessError("IDEMPOTENCY_CONFLICT", "编号参数不同。")
            return {**self.store.jobs[job_id], "idempotent_replay": True}
        self.executions += 1
        job_id = f"fixture-{self.executions}"
        job = {"job_id": job_id, "status": "queued"}
        self.requests[client_request_id] = ((project_id, prompt, other), job_id)
        self.store.jobs[job_id] = job
        return {**job, "idempotent_replay": False}

    def status(self, job_id):
        if self.reject_secret is not None:
            raise HarnessError("FIXTURE_REJECTED", self.reject_secret)
        return dict(self.store.jobs[job_id])

    def result(self, job_id, **other):
        return {"ready": True, "job_id": job_id}

    def session_open(self, project_id, client_request_id, title="", **other):
        return {"session_id": "fixture-session", "title": title}

    async def cancel(self, job_id):
        self.store.jobs[job_id]["status"] = "cancelled"
        return self.status(job_id)


async def _launch(tmp_path, *, model=None, idle=0.12):
    runtime = tmp_path / "service" / "shared-service"
    config = AppConfig(tmp_path / "service", model=model or ModelConfig(credential_source="env", credential_env="SHARED_TEST_UNUSED_KEY"))
    managers = []

    def factory(config):
        value = _Manager(config)
        managers.append(value)
        return value

    token, digest = secrets.token_hex(32), secrets.token_hex(32)
    task = asyncio.create_task(serve(config, runtime, token, digest, idle_seconds=idle, manager_factory=factory))
    try:
        for _ in range(300):
            if task.done():
                await task
            path = runtime / "registry.json"
            if path.exists():
                registry = json.loads(path.read_text(encoding="utf-8"))
                return task, registry, managers, config
            await asyncio.sleep(0.01)
        raise AssertionError("人工共享服务未发布注册表。")
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _connect(registry, environment=None, *, token=None, digest=None):
    reader, writer = await asyncio.open_connection(registry["host"], registry["port"])
    hello = await read_frame(reader)
    nonce = secrets.token_hex(32)
    proof = authentication_proof(token or registry["token"], "client", hello["challenge"], nonce, digest or registry["config_digest"])
    await write_frame(writer, {"version": 1, "nonce": nonce, "config_digest": digest or registry["config_digest"], "proof": proof})
    response = await read_frame(reader)
    if "proof" not in response:
        return reader, writer, response
    expected = authentication_proof(registry["token"], "server", hello["challenge"], nonce, registry["config_digest"])
    assert response["proof"] == expected
    assert registry["token"] not in json.dumps(hello) + json.dumps(response)
    await write_frame(writer, {"version": 1, "model_environment": environment or {}})
    return reader, writer, await read_frame(reader)


async def _rpc(reader, writer, method, arguments, request_id=1):
    await write_frame(writer, {"id": request_id, "method": method, "args": arguments})
    return await read_frame(reader)


async def _close(*writers):
    for writer in writers:
        writer.close()
    await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)


async def _stop(task, *writers):
    await _close(*writers)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_two_real_connections_share_idempotency_and_disconnect_keeps_other_alive(tmp_path):
    async def check():
        task, registry, managers, _ = await _launch(tmp_path)
        writers = []
        try:
            first, one, ready = await _connect(registry)
            writers.append(one)
            assert ready["ready"] is True
            second, two, ready = await _connect(registry)
            writers.append(two)
            assert ready["ready"] is True
            arguments = {"project_id": "fixture", "prompt": "人工检查", "client_request_id": "same-request"}
            original = await _rpc(first, one, "submit", arguments)
            replay = await _rpc(second, two, "submit", arguments)
            assert original["result"]["job_id"] == replay["result"]["job_id"]
            assert replay["result"]["idempotent_replay"] is True
            conflict = await _rpc(second, two, "submit", {**arguments, "prompt": "changed"}, 2)
            assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            assert len(managers) == 1 and managers[0].executions == 1
            await _close(one)
            await asyncio.sleep(0.05)
            status = await _rpc(second, two, "status", {"job_id": original["result"]["job_id"]}, 3)
            assert status["ok"] is True and status["result"]["status"] == "queued"
            assert managers[0].close_calls == 0
            cancelled = await _rpc(second, two, "cancel", {"job_id": original["result"]["job_id"]}, 4)
            assert cancelled["result"]["status"] == "cancelled"
            await _close(two)
            await asyncio.wait_for(task, 3)
            assert managers[0].closed
        finally:
            await _stop(task, *writers)
    asyncio.run(check())


def test_zero_frontends_keeps_pending_job_until_terminal_then_idle_exit(tmp_path):
    async def check():
        task, registry, managers, _ = await _launch(tmp_path)
        _, writer, ready = await _connect(registry)
        try:
            assert ready["ready"]
            managers[0].store.jobs["pending"] = {"job_id": "pending", "status": "queued"}
            await _close(writer)
            await asyncio.sleep(0.4)
            assert not task.done() and managers[0].close_calls == 0
            managers[0].store.jobs["pending"]["status"] = "succeeded"
            await asyncio.wait_for(task, 3)
            assert managers[0].closed
        finally:
            await _stop(task, writer)
    asyncio.run(check())


@pytest.mark.parametrize("wrong", ["token", "config"])
def test_auth_rejects_wrong_cookie_or_configuration_without_closing_backend(tmp_path, wrong):
    async def check():
        task, registry, managers, _ = await _launch(tmp_path, idle=1)
        writers = []
        try:
            arguments = {wrong if wrong == "token" else "digest": secrets.token_hex(32)}
            _, writer, error = await _connect(registry, **arguments)
            writers.append(writer)
            assert error["ready"] is False
            assert error["error"]["code"] == ("SHARED_AUTH_FAILED" if wrong == "token" else "SHARED_CONFIG_MISMATCH")
            reader, valid, ready = await _connect(registry)
            writers.append(valid)
            assert ready["ready"] and not managers[0].closed
            assert registry["token"] not in json.dumps(error)
        finally:
            await _stop(task, *writers)
    asyncio.run(check())


def test_challenge_replay_is_rejected_on_a_fresh_connection(tmp_path):
    async def check():
        task, registry, _, _ = await _launch(tmp_path, idle=1)
        writers = []
        try:
            first, writer = await asyncio.open_connection(registry["host"], registry["port"])
            writers.append(writer)
            hello = await read_frame(first)
            nonce = secrets.token_hex(32)
            request = {"version": 1, "nonce": nonce, "config_digest": registry["config_digest"],
                       "proof": authentication_proof(registry["token"], "client", hello["challenge"], nonce, registry["config_digest"])}
            await write_frame(writer, request)
            assert "proof" in await read_frame(first)
            second, replay = await asyncio.open_connection(registry["host"], registry["port"])
            writers.append(replay)
            assert (await read_frame(second))["challenge"] != hello["challenge"]
            await write_frame(replay, request)
            error = await read_frame(second)
            assert error["error"]["code"] == "SHARED_AUTH_FAILED"
        finally:
            await _stop(task, *writers)
    asyncio.run(check())


def test_credentials_only_after_auth_are_pinned_and_errors_are_redacted(tmp_path):
    async def check():
        task, registry, managers, config = await _launch(tmp_path, idle=1)
        writers = []
        try:
            reader, writer, ready = await _connect(registry, {config.model.credential_env: "fixture-key-one"})
            writers.append(writer)
            assert ready["ready"]
            assert os.environ[config.model.credential_env] == "fixture-key-one"
            assert managers[0].secrets == ["fixture-key-one"]
            _, changed, rejected = await _connect(registry, {config.model.credential_env: "fixture-key-two"})
            writers.append(changed)
            assert rejected["error"]["code"] == "SHARED_CREDENTIAL_MISMATCH"
            assert "fixture-key" not in json.dumps(rejected)
            managers[0].reject_secret = registry["token"] + " fixture-key-one"
            response = await _rpc(reader, writer, "status", {"job_id": "missing"})
            assert response["ok"] is False and response["error"]["code"] == "FIXTURE_REJECTED"
            assert registry["token"] not in json.dumps(response)
            assert "fixture-key-one" not in json.dumps(response)
            success = await _rpc(reader, writer, "session_open", {"project_id": "fixture", "client_request_id": "open",
                                 "title": registry["token"] + " fixture-key-one"}, 2)
            assert success["ok"] is True
            assert registry["token"] not in json.dumps(success)
            assert "fixture-key-one" not in json.dumps(success)
            _, extra, invalid = await _connect(registry, {"PYTHONPATH": "malicious"})
            writers.append(extra)
            assert invalid["error"]["code"] == "SHARED_ENV_NOT_ALLOWED"
        finally:
            await _stop(task, *writers)
        assert config.model.credential_env not in os.environ
    asyncio.run(check())


def test_oversized_preauth_frame_and_non_whitelisted_method_are_rejected(tmp_path):
    async def check():
        task, registry, _, _ = await _launch(tmp_path, idle=1)
        writers = []
        try:
            reader, writer = await asyncio.open_connection(registry["host"], registry["port"])
            writers.append(writer)
            await read_frame(reader)
            writer.write(struct.pack("!I", 8193))
            await writer.drain()
            response = await read_frame(reader)
            assert response["ready"] is False
            reader, writer, _ = await _connect(registry)
            writers.append(writer)
            response = await _rpc(reader, writer, "close", {})
            assert response["ready"] is False and response["error"]["code"] == "SHARED_PROTOCOL_ERROR"
        finally:
            await _stop(task, *writers)
    asyncio.run(check())


def test_unconfirmed_idle_close_retains_owner_and_retries_instead_of_exiting(tmp_path):
    async def check():
        task, registry, managers, _ = await _launch(tmp_path, idle=0.1)
        manager = managers[0]
        manager.close_confirmed = False
        try:
            await asyncio.sleep(0.3)
            assert manager.close_calls >= 1 and not task.done() and not manager.closed
            # 停止未确认之后即使出现非终态，仍必须继续停止确认，不能永远跳过。
            manager.store.jobs["unconfirmed"] = {"job_id": "unconfirmed", "status": "queued"}
            manager.close_confirmed = True
            await asyncio.wait_for(task, 3)
            assert manager.closed and manager.close_calls >= 2
        finally:
            await _stop(task)
    asyncio.run(check())


def test_bootstrap_environment_cannot_persist_configured_credential(tmp_path):
    async def check():
        config = AppConfig(tmp_path, model=replace(ModelConfig(), credential_source="env", credential_env="PATH"))
        with pytest.raises(HarnessError) as error:
            await serve(config, tmp_path / "runtime", secrets.token_hex(32), secrets.token_hex(32),
                        nonsecret_environment={"PATH": "not-a-real-key"}, manager_factory=_Manager)
        assert error.value.code == "SHARED_PROTOCOL_ERROR"
        assert not (tmp_path / "runtime" / "registry.json").exists()
    asyncio.run(check())
