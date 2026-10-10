"""视图经过真实本机 RPC/MCP 的契约；人工已保存报告不调用模型。"""

import asyncio
import hashlib
import json
from pathlib import Path
import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig, frozen_execution, normalize_request, request_fingerprint
from harness_mcp.edit_workspace import _diff
from harness_mcp.errors import HarnessError
from harness_mcp.report import ReportError, build_report, save_report
from harness_mcp.server import create_server
from harness_mcp.shared_client import SharedClient, open_authenticated, read_frame
from harness_mcp.shared_service import serve
from harness_mcp.task_manager import TaskManager
from harness_mcp.workspace import Snapshot


VIEWS = ("full", "summary", "patch", "files")


class NoModelRunner:
    async def run(self, *args, **kwargs):
        pytest.fail("结果传输测试不能调用模型")

    async def close(self):
        return True

    async def recover(self):
        return True


class MetadataManager(TaskManager):
    async def start(self):
        # 本夹具只处理人工已保存报告，没有执行调度器。
        self.accepting = True

    async def preflight(self, project_id, relative_paths=None, mode=None):
        return {"passed": True, "project_id": project_id, "relative_paths": relative_paths, "mode": mode}


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_bytes(b"value = 1\n")
    return AppConfig(tmp_path / "data", {"demo": ProjectConfig("demo", source, allow_edits=True)},
                     model=ModelConfig(credential_source="env", credential_env="RESULT_VIEWS_UNUSED_CREDENTIAL"), default_mode="edit")


def _seed_report(manager, config, status="succeeded", request_id="saved-fixture"):
    request = normalize_request(config, "demo", "人工传输夹具", request_id)
    job, _ = manager.store.submit(request, request_fingerprint(request), frozen_execution(config, "demo"), 5)
    original = b"value = 1\n"
    source = config.projects["demo"].path
    snapshot = Snapshot(source, "saved-fixture-snapshot", [{"path": "main.py", "size_bytes": len(original),
                         "sha256": hashlib.sha256(original).hexdigest(), "line_count": 1}], None, False, ["main.py"], [])
    after = "独立副本内容🙂\r\n" * 3000
    encoded = after.encode("utf-8")
    row = {"path": "new.txt", "change_type": "add", "before_sha256": None, "after_sha256": hashlib.sha256(encoded).hexdigest(),
           "before_size_bytes": 0, "after_size_bytes": len(encoded), "diff": _diff("new.txt", None, encoded), "after_text": after}
    digest = hashlib.sha256(json.dumps({"snapshot_id": snapshot.snapshot_id, "changes": [row]}, ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    changes = {"schema_version": 1, "snapshot_id": snapshot.snapshot_id, "changes": [row], "patch_text": row["diff"],
               "source_applied": False, "change_set_id": digest}
    findings = [{"severity": "info", "title": "人工传输夹具", "path": "main.py", "line_start": 1, "line_end": 1,
                 "evidence": "value = 1"}] * 4
    report = build_report(job["job_id"], "demo", snapshot, json.dumps({"summary": "人工已保存报告", "findings": findings}),
                          "completed", mode="edit", changes=changes)
    saved = save_report(report, config.data_dir / "jobs" / job["job_id"])
    manager.store.finish(job["job_id"], status, report_id=saved["report_id"])
    return job["job_id"], report


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_mcp_result_dispatch_preserves_requested_view_and_legacy_default(config, view, asynchronous):
    async def scenario():
        proxy = MagicMock(secrets=[])
        response = {"ready": True, "view": view, "job_id": "saved"}
        proxy.result = AsyncMock(return_value=response) if asynchronous else MagicMock(return_value=response)
        server = create_server(config, manager=proxy)
        result = await server.call_tool("harness_result", {"job_id": "saved", "cursor": "page", "page_size": 3, "view": view})
        assert result.structured_content["ok"] is True and result.structured_content["view"] == view
        proxy.result.assert_called_once_with("saved", "page", 3, view)
        if asynchronous:
            proxy.result.assert_awaited_once_with("saved", "page", 3, view)
        proxy.result.reset_mock()
        await server.call_tool("harness_result", {"job_id": "saved"})
        proxy.result.assert_called_once_with("saved", None, 20, "full")
    asyncio.run(scenario())


def test_read_only_service_schema_and_preflight_default_dispatch(config):
    async def scenario():
        proxy = MagicMock(secrets=[])
        proxy.service_info = AsyncMock(return_value={"active_connections": 3, "config_state": "current"})
        proxy.preflight = AsyncMock(return_value={"passed": True})
        server = create_server(config, manager=proxy)
        tools = {tool.name: tool for tool in await server.list_tools()}
        view = tools["harness_result"].input_schema["properties"]["view"]
        assert view["default"] == "full" and set(view["enum"]) == set(VIEWS)
        assert not tools["harness_service_info"].input_schema.get("properties")
        assert tools["harness_preflight"].input_schema["properties"]["mode"]["default"] == "edit"
        for name in ("harness_result", "harness_service_info", "harness_preflight"):
            assert tools[name].annotations.read_only_hint is True
        info = await server.call_tool("harness_service_info", {})
        assert info.structured_content["active_connections"] == 3
        proxy.service_info.assert_awaited_once_with()
        await server.call_tool("harness_preflight", {"project_id": "demo"})
        proxy.preflight.assert_awaited_once_with("demo", None, "edit")
        await server.call_tool("harness_preflight", {"project_id": "demo", "relative_paths": ["main.py"], "mode": "review"})
        proxy.preflight.assert_awaited_with("demo", ["main.py"], "review")
    asyncio.run(scenario())


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("status", ["succeeded", "cancelled"])
def test_real_manager_saved_result_quality_and_view_are_preserved_over_mcp(config, view, status):
    async def scenario():
        manager = MetadataManager(config, NoModelRunner())
        try:
            job_id, report = _seed_report(manager, config, status)
            manager.accepting = True
            server = create_server(config, manager=manager)
            result = await server.call_tool("harness_result", {"job_id": job_id, "view": view, "page_size": 1})
            value = result.structured_content
            assert value["ok"] is True and value["ready"] is True and value["view"] == view
            assert value["status"] == status and value["execution_status"] == status
            assert value["quality"]["patch"]["availability"] == ("available_for_review" if status == "succeeded" else "unavailable")
            assert value["report_id"] == manager.store.get(job_id)["report_id"]
            assert value["change_set_id"] == report["change_set_id"]
            info = (await server.call_tool("harness_service_info", {})).structured_content
            assert info["active_connections"] == 1 and info["connection_mode"] == "direct"
            assert info["config_state"] == "unknown" and info["restart_required"] is None
            assert info["queued_tasks"] == 0 and info["active_tasks"] == 0
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("view", ["bad", [], None])
def test_pending_and_saved_manager_results_reject_invalid_view_with_same_error(config, view):
    async def scenario():
        manager = MetadataManager(config, NoModelRunner())
        try:
            saved_id, _ = _seed_report(manager, config)
            request = normalize_request(config, "demo", "人工排队夹具", "pending-fixture")
            pending, _ = manager.store.submit(request, request_fingerprint(request), frozen_execution(config, "demo"), 5)
            for job_id in (saved_id, pending["job_id"]):
                with pytest.raises((HarnessError, ReportError)) as error:
                    manager.result(job_id, view=view)
                assert getattr(error.value, "code", None) == "INVALID_RESULT_VIEW"
            for valid_view in VIEWS:
                pending_result = manager.result(pending["job_id"], view=valid_view)
                assert pending_result["ready"] is False and pending_result["view"] == valid_view
                assert pending_result["execution_status"] == pending["status"]
        finally:
            await manager.close()
    asyncio.run(scenario())


async def _wait_info(client, predicate):
    for _ in range(100):
        result = await client.service_info()
        if predicate(result):
            return result
        await asyncio.sleep(0.01)
    pytest.fail("真实共享连接数没有达到预期")


def test_two_real_shared_frontends_route_views_and_report_actual_connection_counts(config, tmp_path, monkeypatch):
    async def scenario():
        runtime = tmp_path / "runtime" / "shared-service"
        token, config_digest = secrets.token_hex(32), secrets.token_hex(32)
        managers, seeded = [], []

        def factory(value):
            manager = MetadataManager(value, NoModelRunner())
            managers.append(manager)
            seeded.append(_seed_report(manager, value))
            return manager

        service = asyncio.create_task(serve(config, runtime, token, config_digest, idle_seconds=30, manager_factory=factory))
        clients, pending_writer = [], None
        try:
            for _ in range(300):
                if service.done():
                    await service
                registry_path = runtime / "registry.json"
                if registry_path.exists():
                    registry = json.loads(registry_path.read_text(encoding="utf-8"))
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("人工共享后台没有发布连接注册表")

            async def connect_only(_config, _path):
                reader, writer = await open_authenticated(registry)
                return reader, writer, registry

            monkeypatch.setattr("harness_mcp.shared_bootstrap.connect_or_start", connect_only)
            for _ in range(2):
                client = SharedClient(config, tmp_path / "unused-config.toml")
                clients.append(client)
                await client.start()
            first, second = clients
            info = await _wait_info(second, lambda value: value["active_connections"] == 2)
            assert info["pending_connections"] == 0 and info["connection_mode"] == "shared"
            assert info["runtime_version"] == config_digest
            assert info["config_state"] == "unknown" and info["restart_required"] is None
            assert token not in json.dumps(info) and str(config.projects["demo"].path) not in json.dumps(info)
            job_id, report = seeded[0]
            servers = [create_server(config, manager=client, sessions=True) for client in clients]
            for index, view in enumerate(VIEWS):
                response = await servers[index % 2].call_tool("harness_result", {"job_id": job_id, "view": view, "page_size": 1})
                value = response.structured_content
                assert value["ok"] and value["view"] == view and value["execution_status"] == "succeeded"
                assert value["quality"]["patch"]["availability"] == "available_for_review"
                assert value["change_set_id"] == report["change_set_id"]
            summary = await first.result(job_id, page_size=1, view="summary")
            with pytest.raises(HarnessError) as error:
                await second.result(job_id, summary["next_cursor"], 1, "patch")
            assert error.value.code == "INVALID_CURSOR"
            assert (await first.result(job_id))["view"] == "full"
            for view in ("summary", "patch", "files"):
                cursor, pages = None, []
                for index in range(100):
                    page = await clients[index % 2].result(job_id, cursor, 1, view)
                    pages.append(page)
                    assert page["report_id"] == managers[0].store.get(job_id)["report_id"]
                    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 96 * 1024
                    assert page["metadata_included"] is (index == 0)
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                else:
                    pytest.fail("共享结果分页没有结束")
                if view == "summary":
                    assert sum(len(page["findings"]) for page in pages) == 4
                elif view == "patch":
                    assert "".join(page["patch_text"] for page in pages) == report["patch_text"]
                else:
                    assert "".join(item["text"] for page in pages for item in page["change_contents"]) == report["changes"][0]["after_text"]
            assert (config.projects["demo"].path / "main.py").read_bytes() == b"value = 1\n"
            assert not (config.projects["demo"].path / "new.txt").exists()
            assert await second.preflight("demo", ["main.py"]) == {"passed": True, "project_id": "demo", "relative_paths": ["main.py"], "mode": "edit"}
            assert (await second.preflight("demo", mode="review"))["mode"] == "review"

            # 未认证的第三个 TCP 连接只能计入 pending，不能冒充已接入前端。
            pending_reader, pending_writer = await asyncio.open_connection(registry["host"], registry["port"])
            await read_frame(pending_reader)
            info = await _wait_info(second, lambda value: value["pending_connections"] == 1)
            assert info["active_connections"] == 2
            pending_writer.close()
            await pending_writer.wait_closed()
            pending_writer = None
            await _wait_info(second, lambda value: value["pending_connections"] == 0)
            await first.close()
            info = await _wait_info(second, lambda value: value["active_connections"] == 1)
            assert info["pending_connections"] == 0
            assert (await second.result(job_id, view="files"))["ready"] is True
            assert not managers[0].closed
        finally:
            if pending_writer is not None:
                pending_writer.close()
                await pending_writer.wait_closed()
            await asyncio.gather(*(client.close() for client in clients))
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
    asyncio.run(scenario())
