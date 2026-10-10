"""SDK schema 隐藏宿主 Context；自动继续只接受可信元数据与本机配置。"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from mcp.server.mcpserver import Context

from harness_mcp.config import AppConfig, CompletionConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.server import create_server
from harness_mcp.task_manager import TaskManager


def proxy():
    manager = MagicMock()
    manager.secrets = ["completion-fixture-private-key"]
    for name in ("submit", "session_send", "session_open", "completion_watch", "completion_list", "completion_ack"):
        setattr(manager, name, AsyncMock())
    manager.submit.return_value = {"job_id": "job-one", "status": "queued"}
    manager.session_send.return_value = {"job_id": "turn-one", "session_id": "session-one", "status": "queued"}
    manager.completion_watch.return_value = {"auto_continue": True, "job_ids": ["job-one"], "events": []}
    manager.completion_list.return_value = {"events": []}
    manager.completion_ack.return_value = {"events": []}
    return manager


def app_config(tmp_path, enabled=True, default_mode="review"):
    return AppConfig(tmp_path / "data", completion=CompletionConfig(enabled), default_mode=default_mode)


def host_context(server, thread_id=None, turn_id=None):
    values = {"thread_id": thread_id or str(uuid.uuid4()), "turn_id": turn_id or str(uuid.uuid4())}
    ctx = Context(request_context=SimpleNamespace(meta={"x-codex-turn-metadata": json.dumps(values)}), mcp_server=server)
    return ctx, {**values, "host_id": "local", "pipe_path": "\\\\.\\pipe\\codex-app-tools-server-fixture"}


@pytest.fixture
def host_pipe(monkeypatch):
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "\\\\.\\pipe\\codex-app-tools-server-fixture")
    monkeypatch.setenv("CODEX_THREAD_ID", str(uuid.uuid4()))
    monkeypatch.setattr("harness_mcp.codex_app_bridge.inspect_origin", AsyncMock(return_value={"kind": "codex", "host_id": "local"}))


def test_failed_host_preflight_retains_submit_without_false_auto_promise(tmp_path, host_pipe, monkeypatch):
    async def scenario():
        from harness_mcp.codex_app_bridge import DeliveryError
        monkeypatch.setattr("harness_mcp.codex_app_bridge.inspect_origin", AsyncMock(side_effect=DeliveryError("COMPLETION_HOST_CAPABILITY_UNAVAILABLE", "no host")))
        manager = proxy()
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, _ = host_context(server)
        result = await server.call_tool("harness_submit", {"project_id":"demo", "prompt":"review", "client_request_id":"request"}, ctx)
        assert result.structured_content["ok"] is True
        assert result.structured_content["auto_continue"] is False
        assert result.structured_content["auto_continue_reason"] == "COMPLETION_HOST_CAPABILITY_UNAVAILABLE"
        manager.submit.assert_awaited_once_with("demo", "review", "request", None, None, "review")
        watch = await server.call_tool("harness_watch", {"job_ids":["job-one"]}, ctx)
        assert watch.structured_content["ok"] is False
        manager.completion_watch.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("sessions,enabled,expected", [(False, False, 6), (True, False, 9), (False, True, 9), (True, True, 12)])
def test_auto_tools_are_exposed_only_when_config_enabled_and_context_never_in_schema(tmp_path, sessions, enabled, expected):
    async def scenario():
        server = create_server(app_config(tmp_path, enabled), manager=proxy(), sessions=sessions)
        tools = await server.list_tools()
        assert len(tools) == expected
        names = {tool.name for tool in tools}
        additions = {"harness_watch", "harness_completion_list", "harness_completion_ack"}
        assert additions <= names if enabled else not additions.intersection(names)
        for tool in tools:
            schema = tool.model_dump(by_alias=True)["inputSchema"]
            assert not {"ctx", "origin", "thread_id", "turn_id", "host_id", "pipe_path", "callback_url", "completion_origin"}.intersection(schema.get("properties", {}))
            assert "ctx" not in schema.get("required", [])
        if enabled:
            assert next(tool for tool in tools if tool.name == "harness_watch").input_schema["properties"].keys() == {"job_ids"}
    asyncio.run(scenario())


def test_submit_uses_hidden_host_context_as_internal_origin(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, origin = host_context(server)
        reply = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "request"}, ctx)
        assert reply.structured_content["auto_continue"] is True
        manager.submit.assert_awaited_once_with("demo", "review", "request", None, None, "review", completion_origin=origin)
        assert "pipe_path" not in json.dumps(reply.structured_content)
    asyncio.run(scenario())


def test_send_uses_hidden_host_context_without_changing_session_parameters(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, origin = host_context(server)
        reply = await server.call_tool("harness_session_send", {"session_id": "session-one", "prompt": "continue", "client_request_id": "request"}, ctx)
        assert reply.structured_content["auto_continue"] is True
        manager.session_send.assert_awaited_once_with("session-one", "continue", "request", None, completion_origin=origin)
    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_missing_context_preserves_old_submit_and_does_not_use_global_thread(tmp_path, host_pipe, enabled):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path, enabled), manager=manager, sessions=True)
        reply = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "request"})
        assert reply.structured_content["ok"] is True and reply.structured_content["auto_continue"] is False
        manager.submit.assert_awaited_once_with("demo", "review", "request", None, None, "review")
    asyncio.run(scenario())


def test_disabled_permission_does_not_bind_even_when_host_context_exists(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path, False), manager=manager, sessions=True)
        ctx, _ = host_context(server)
        reply = await server.call_tool("harness_session_send", {"session_id": "session-one", "prompt": "continue", "client_request_id": "request"}, ctx)
        assert reply.structured_content["auto_continue"] is False
        manager.session_send.assert_awaited_once_with("session-one", "continue", "request", None)
    asyncio.run(scenario())


def test_watch_list_ack_dispatch_only_with_current_host_origin(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, origin = host_context(server)
        await server.call_tool("harness_watch", {"job_ids": ["job-one"]}, ctx)
        manager.completion_watch.assert_awaited_once_with(job_ids=["job-one"], origin=origin)
        await server.call_tool("harness_completion_list", {"state": "uncertain", "limit": 3}, ctx)
        manager.completion_list.assert_awaited_once_with(origin=origin, state="uncertain", limit=3)
        await server.call_tool("harness_completion_ack", {"event_ids": [1, 2]}, ctx)
        manager.completion_ack.assert_awaited_once_with(event_ids=[1, 2], origin=origin)
    asyncio.run(scenario())


def test_completion_error_is_structured_and_redacted(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        manager.completion_watch.side_effect = HarnessError("COMPLETION_ORIGIN_CONFLICT", "completion-fixture-private-key")
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, _ = host_context(server)
        reply = await server.call_tool("harness_watch", {"job_ids": ["job-one"]}, ctx)
        assert reply.structured_content["error"] == {"code": "COMPLETION_ORIGIN_CONFLICT", "message": "[REDACTED]"}
        assert reply.structured_content["ok"] is False
    asyncio.run(scenario())


def test_edit_default_remains_visible_in_schema_and_passed_to_submit(tmp_path):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path, default_mode="edit"), manager=manager, sessions=True)
        tools = await server.list_tools()
        for name in ("harness_submit", "harness_session_open"):
            tool = next(tool for tool in tools if tool.name == name)
            assert tool.input_schema["properties"]["mode"]["default"] == "edit"
        await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "edit", "client_request_id": "request"})
        manager.submit.assert_awaited_once_with("demo", "edit", "request", None, None, "edit")
    asyncio.run(scenario())


class MetadataRunner:
    """仅为构造真实管理器提供依赖；测试不启动调度器或执行器。"""


@contextmanager
def real_metadata_manager(tmp_path, enabled=True):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    config = replace(app_config(tmp_path, enabled), projects={"demo": ProjectConfig("demo", root)},
                     model=ModelConfig(credential_source="env", credential_env="COMPLETION_SERVER_UNUSED_KEY"))
    manager = TaskManager(config, MetadataRunner())
    manager.accepting = True
    try:
        yield manager
    finally:
        manager.store.close()
        manager.lock.close()


def test_real_store_missing_context_does_not_bind_or_execute_model(tmp_path, host_pipe):
    async def scenario():
        with real_metadata_manager(tmp_path) as manager:
            server = create_server(manager.config, manager=manager)
            reply = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "request"})
            assert reply.structured_content["ok"] and not reply.structured_content["auto_continue"]
            assert manager.scheduler is None
            assert manager.store.connection.execute("SELECT COUNT(*) FROM completion_bindings").fetchone()[0] == 0
            assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    asyncio.run(scenario())


def test_real_store_context_binds_current_thread_atomically_and_invalid_origin_accepts_nothing(tmp_path, host_pipe):
    async def scenario():
        with real_metadata_manager(tmp_path) as manager:
            server = create_server(manager.config, manager=manager)
            ctx, origin = host_context(server)
            reply = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "request"}, ctx)
            assert reply.structured_content["ok"] and reply.structured_content["auto_continue"]
            bound = manager.store.connection.execute("SELECT * FROM completion_bindings").fetchone()
            assert bound["thread_id"] == origin["thread_id"] and bound["turn_id"] == origin["turn_id"]
            assert manager.scheduler is None
            invalid, _ = host_context(server, thread_id="invalid-thread")
            failed = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "second-request"}, invalid)
            assert not failed.structured_content["ok"] and failed.structured_content["error"]["code"] == "COMPLETION_ORIGIN_INVALID"
            assert manager.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    asyncio.run(scenario())


def test_model_arguments_cannot_supply_a_notification_target_or_replace_host_context(tmp_path, host_pipe):
    async def scenario():
        manager = proxy()
        server = create_server(app_config(tmp_path), manager=manager, sessions=True)
        ctx, trusted_origin = host_context(server)
        rogue = {**trusted_origin, "thread_id": str(uuid.uuid4()), "pipe_path": "\\\\remote\\pipe\\evil"}
        reply = await server.call_tool("harness_submit", {
            "project_id": "demo", "prompt": "review", "client_request_id": "request",
            "ctx": rogue, "completion_origin": rogue, "thread_id": rogue["thread_id"], "callback_url": "https://example.invalid",
        }, ctx)
        assert reply.structured_content["ok"] is True
        manager.submit.assert_awaited_once_with("demo", "review", "request", None, None, "review", completion_origin=trusted_origin)
    asyncio.run(scenario())


def test_real_tools_without_host_context_cannot_watch_list_or_ack(tmp_path, host_pipe):
    async def scenario():
        with real_metadata_manager(tmp_path) as manager:
            server = create_server(manager.config, manager=manager)
            submit_reply = await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "review", "client_request_id": "request"})
            for name, arguments in (("harness_watch", {"job_ids": [submit_reply.structured_content["job_id"]]}),
                                    ("harness_completion_list", {}), ("harness_completion_ack", {"event_ids": [1]})):
                reply = await server.call_tool(name, arguments)
                assert not reply.structured_content["ok"]
                assert reply.structured_content["error"]["code"] == "AUTO_CONTINUE_UNAVAILABLE"
            assert manager.store.connection.execute("SELECT COUNT(*) FROM completion_bindings").fetchone()[0] == 0
    asyncio.run(scenario())
