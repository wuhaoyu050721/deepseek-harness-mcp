"""真实 loopback 连接验证 Claude channel 路由；不连接 Claude/Codex 或调用模型。"""
import asyncio
from contextlib import asynccontextmanager
import secrets
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest

from harness_mcp.config import AppConfig, CompletionConfig, ModelConfig
from harness_mcp.errors import HarnessError
from harness_mcp.shared_client import SharedClient, open_authenticated
from harness_mcp.shared_service import _SharedService
import harness_mcp.shared_service as shared_service


def origin():
    return {"client": "claude_code", "connection_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4())}


@asynccontextmanager
async def service(tmp_path, enabled=True):
    config = AppConfig(tmp_path, model=ModelConfig(credential_source="env", credential_env="CHANNEL_FIXTURE_UNUSED_KEY"), completion=CompletionConfig(enabled))
    calls = []
    class Manager:
        secrets = []
        def status(self, job_id):
            calls.append(job_id)
            return {"job_id": job_id, "status": "succeeded"}
        def completion_list(self, origin, **kwargs):
            calls.append(origin)
            return {"events": []}
    state = _SharedService(config, Manager(), secrets.token_hex(32), secrets.token_hex(32), 30)
    server = await asyncio.start_server(state.accept, "127.0.0.1", 0)
    registry = {"port": server.sockets[0].getsockname()[1], "token": state.token, "config_digest": state.config_digest}
    async def connect(*_args):
        reader, writer = await open_authenticated(registry)
        return reader, writer, registry
    with patch("harness_mcp.shared_bootstrap.connect_or_start", connect):
        try:
            yield state, config, calls
        finally:
            server.close()
            await server.wait_closed()
            for writer in tuple(state.writers):
                writer.close()
            for task in tuple(state.tasks):
                task.cancel()
            await asyncio.gather(*state.tasks, return_exceptions=True)


def test_rpc_and_completion_are_demultiplexed_and_only_channel_write_is_confirmed(tmp_path):
    async def scenario():
        async with service(tmp_path) as (state, config, calls):
            client = SharedClient(config, tmp_path / "unused.toml")
            target, notifications = origin(), []
            async def callback(event):
                notifications.append(event)
                # 回调可以进行 RPC，不会阻塞独立 reader 或等待自身回执。
                assert (await client.status("callback-result"))["status"] == "succeeded"
            client.enable_channel(target, callback)
            await client.start()
            try:
                receipt, status = await asyncio.gather(state.send_completion(target, "fixed completion prompt", "event-1"), client.status("normal-result"))
                assert receipt["status"] == "channel_written" and status["job_id"] == "normal-result"
                assert len(notifications) == 1 and notifications[0]["origin"] == target
                assert notifications[0]["prompt"] == "fixed completion prompt"
                assert set(calls) == {"normal-result", "callback-result"}
                assert state._completion_receipts == {} and client._pending == {}
            finally:
                await client.close()
    asyncio.run(scenario())


def test_two_clients_receive_only_their_own_completion_and_cannot_forge_origin(tmp_path):
    async def scenario():
        async with service(tmp_path) as (state, config, calls):
            clients, targets, notifications = [], [origin(), origin()], [[], []]
            for index, target in enumerate(targets):
                client = SharedClient(config, tmp_path / "unused.toml")
                async def callback(event, index=index):
                    notifications[index].append(event)
                client.enable_channel(target, callback)
                await client.start()
                clients.append(client)
            try:
                await asyncio.gather(*(state.send_completion(target, f"event {index}", f"call-{index}") for index, target in enumerate(targets)))
                assert [messages[0]["prompt"] for messages in notifications] == ["event 0", "event 1"]
                assert all(len(messages) == 1 for messages in notifications)
                with pytest.raises(HarnessError) as error:
                    await clients[0].completion_list(targets[1])
                assert error.value.code == "CHANNEL_TARGET_MISMATCH" and calls == []
                assert await clients[0].completion_list(targets[0]) == {"events": []}
            finally:
                await asyncio.gather(*(client.close() for client in clients))
    asyncio.run(scenario())


def test_callback_failure_is_uncertain_and_never_claims_claude_receipt(tmp_path):
    async def scenario():
        async with service(tmp_path) as (state, config, _calls):
            client, target = SharedClient(config, tmp_path / "unused.toml"), origin()
            async def callback(_event):
                raise RuntimeError("fixture write failed")
            client.enable_channel(target, callback)
            await client.start()
            try:
                with pytest.raises(HarnessError) as error:
                    await state.send_completion(target, "fixed event", "call-1")
                assert error.value.code == "CHANNEL_WRITE_UNCONFIRMED" and error.value.uncertain is True
                assert (await client.status("still-alive"))["status"] == "succeeded"
            finally:
                await client.close()
    asyncio.run(scenario())


def test_new_connection_cannot_take_over_disconnected_original_session(tmp_path):
    async def scenario():
        async with service(tmp_path) as (state, config, _calls):
            target = origin()
            async def callback(_event):
                pass
            first = SharedClient(config, tmp_path / "unused.toml")
            first.enable_channel(target, callback)
            await first.start()
            await first.close()
            await asyncio.sleep(0.01)
            second = SharedClient(config, tmp_path / "unused.toml")
            second.enable_channel({**target, "connection_id": str(uuid.uuid4())}, callback)
            await second.start()
            try:
                with pytest.raises(HarnessError) as error:
                    await state.send_completion(target, "event", "call-1")
                assert error.value.code == "CHANNEL_DISCONNECTED" and error.value.uncertain is False
            finally:
                await second.close()
    asyncio.run(scenario())


def test_channel_registration_requires_trusted_permission_and_unique_connection(tmp_path):
    async def scenario():
        async def callback(_event):
            pass
        async with service(tmp_path, enabled=False) as (_state, config, _calls):
            client = SharedClient(config, tmp_path / "unused.toml")
            client.enable_channel(origin(), callback)
            with pytest.raises(HarnessError):
                await client.start()
            assert client.writer is None
        async with service(tmp_path) as (_state, config, _calls):
            first, second = SharedClient(config, tmp_path / "unused.toml"), SharedClient(config, tmp_path / "unused.toml")
            target = origin()
            first.enable_channel(target, callback)
            second.enable_channel(target, callback)
            await first.start()
            try:
                with pytest.raises(HarnessError):
                    await second.start()
                assert (await first.status("one"))["status"] == "succeeded"
                with pytest.raises(HarnessError):
                    first.enable_channel(origin(), callback)
            finally:
                await first.close()
    asyncio.run(scenario())


def test_non_channel_connection_cannot_supply_claude_origin(tmp_path):
    async def scenario():
        async with service(tmp_path) as (_state, config, calls):
            client = SharedClient(config, tmp_path / "unused.toml")
            await client.start()
            try:
                with pytest.raises(HarnessError) as error:
                    await client.completion_list(origin())
                assert error.value.code == "CHANNEL_TARGET_MISMATCH" and calls == []
            finally:
                await client.close()
    asyncio.run(scenario())


def test_waiting_for_writer_lock_is_bounded_and_safe_to_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_service, "CHANNEL_DELIVERY_TIMEOUT_SECONDS", 0.02)
    async def scenario():
        async with service(tmp_path) as (state, config, _calls):
            client, target = SharedClient(config, tmp_path / "unused.toml"), origin()
            received = []
            async def callback(event):
                received.append(event)
            client.enable_channel(target, callback)
            await client.start()
            writer = state.channels[target["connection_id"]][1]
            lock = state._writer_locks.setdefault(writer, asyncio.Lock())
            await lock.acquire()
            try:
                with pytest.raises(HarnessError) as error:
                    await state.send_completion(target, "fixed event", "call-1")
                assert error.value.uncertain is False and received == []
                assert state._completion_receipts == {}
            finally:
                lock.release()
                await client.close()
    asyncio.run(scenario())


def test_channel_write_timeout_is_uncertain_and_does_not_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_service, "CHANNEL_DELIVERY_TIMEOUT_SECONDS", 0.02)
    async def scenario():
        async with service(tmp_path) as (state, config, _calls):
            client, target = SharedClient(config, tmp_path / "unused.toml"), origin()
            entered, release = asyncio.Event(), asyncio.Event()
            received = []
            async def callback(event):
                received.append(event)
                entered.set()
                await release.wait()
            client.enable_channel(target, callback)
            await client.start()
            try:
                with pytest.raises(HarnessError) as error:
                    await state.send_completion(target, "fixed event", "call-1")
                assert error.value.uncertain is True and entered.is_set() and len(received) == 1
                release.set()
                await asyncio.sleep(0.01)
                assert (await client.status("still-live"))["status"] == "succeeded"
                assert state._completion_receipts == {} and len(received) == 1
            finally:
                release.set()
                await client.close()
    asyncio.run(scenario())
