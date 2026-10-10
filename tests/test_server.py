"""实际 SDK 工具 schema/错误转换；真实 stdio 的独立脚本只运行一轮。"""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from harness_mcp.config import AppConfig
from harness_mcp.errors import HarnessError
from harness_mcp.server import create_server

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_mvp.py"
SPEC = importlib.util.spec_from_file_location("verify_mvp_contract", SCRIPT)
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


class ServerContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = MagicMock()
        self.manager.secrets = ["test-sensitive-value"]
        self.manager.submit = AsyncMock()
        self.manager.cancel = AsyncMock()
        with patch("harness_mcp.server.TaskManager", return_value=self.manager):
            self.server = create_server(AppConfig(Path("unused-by-mocked-manager")))

    async def test_exact_four_tools_and_required_request_id(self):
        tools = [tool.model_dump(by_alias=True) for tool in await self.server.list_tools()]
        verification.validate_tools_contract(tools)
        submit = next(tool for tool in tools if tool["name"] == "harness_submit")
        self.assertEqual(set(submit["inputSchema"]["properties"]), {"project_id", "prompt", "client_request_id", "relative_paths", "timeout_seconds", "mode"})
        self.assertEqual(submit["inputSchema"]["properties"]["mode"]["default"], "review")

    async def test_business_error_returned_structured_and_redacted(self):
        self.manager.status.side_effect = HarnessError("JOB_NOT_FOUND", "test-sensitive-value")
        response = await self.server.call_tool("harness_status", {"job_id": "missing"})
        self.assertFalse(response.is_error)
        self.assertFalse(response.structured_content["ok"])
        self.assertEqual(response.structured_content["error"]["code"], "JOB_NOT_FOUND")
        self.assertEqual(response.structured_content["error"]["message"], "[REDACTED]")

    async def test_receipt_does_not_wait_for_backend_run(self):
        self.manager.submit.return_value = {"job_id": "one", "status": "queued", "poll_after_seconds": 1}
        response = await self.server.call_tool("harness_submit", {"project_id": "fixture", "prompt": "审查", "client_request_id": "request-one"})
        self.assertTrue(response.structured_content["ok"])
        self.assertEqual(response.structured_content["status"], "queued")
        self.manager.submit.assert_awaited_once_with("fixture", "审查", "request-one", None, None, "review")

    async def test_edit_mode_passed_to_manager_without_applying_source(self):
        self.manager.submit.return_value = {"job_id": "edit-one", "status": "queued", "poll_after_seconds": 1}
        response = await self.server.call_tool("harness_submit", {"project_id": "fixture", "prompt": "修复", "client_request_id": "edit-one", "mode": "edit"})
        self.assertTrue(response.structured_content["ok"])
        self.manager.submit.assert_awaited_once_with("fixture", "修复", "edit-one", None, None, "edit")

    async def test_shared_proxy_queries_await_and_preserve_business_errors(self):
        proxy = MagicMock()
        proxy.secrets = ["test-sensitive-value"]
        proxy.status = AsyncMock(return_value={"job_id": "shared-one", "status": "running"})
        proxy.result = AsyncMock(side_effect=HarnessError("JOB_NOT_FOUND", "test-sensitive-value"))
        with patch("harness_mcp.server.TaskManager") as local_manager:
            server = create_server(AppConfig(Path("unused-proxy")), manager=proxy)
            local_manager.assert_not_called()
        response = await server.call_tool("harness_status", {"job_id": "shared-one"})
        self.assertEqual(response.structured_content["status"], "running")
        proxy.status.assert_awaited_once_with("shared-one")
        response = await server.call_tool("harness_result", {"job_id": "missing"})
        self.assertEqual(response.structured_content["error"], {"code": "JOB_NOT_FOUND", "message": "[REDACTED]"})
        proxy.result.assert_awaited_once_with("missing", None, 20)

    async def test_shared_session_schema_and_followup_dispatch(self):
        proxy = MagicMock()
        proxy.secrets = []
        proxy.session_open = AsyncMock(return_value={"session_id": "agent-one"})
        proxy.session_send = AsyncMock(return_value={"job_id": "turn-one", "session_id": "agent-one"})
        proxy.session_list = AsyncMock(return_value={"sessions": []})
        server = create_server(AppConfig(Path("unused-sessions")), manager=proxy, sessions=True)
        tools = await server.list_tools()
        self.assertEqual(len(tools), 7)
        self.assertTrue({"harness_session_open", "harness_session_send", "harness_session_list"} <= {tool.name for tool in tools})
        response = await server.call_tool("harness_session_open", {"project_id": "demo", "client_request_id": "open-one"})
        self.assertEqual(response.structured_content["session_id"], "agent-one")
        proxy.session_open.assert_awaited_once_with("demo", "open-one", "", "review", None)
        await server.call_tool("harness_session_send", {"session_id": "agent-one", "prompt": "继续", "client_request_id": "send-one"})
        proxy.session_send.assert_awaited_once_with("agent-one", "继续", "send-one", None)
        await server.call_tool("harness_session_list", {"project_id": "demo"})
        proxy.session_list.assert_awaited_once_with("demo", 20)

    async def test_desktop_default_edit_schema_dispatch_and_explicit_review(self):
        proxy = MagicMock()
        proxy.secrets = []
        proxy.submit = AsyncMock(return_value={"job_id": "default-edit"})
        proxy.session_open = AsyncMock(return_value={"session_id": "default-agent"})
        server = create_server(AppConfig(Path("unused-default-edit"), default_mode="edit"), manager=proxy, sessions=True)
        tools = {tool.name: tool for tool in await server.list_tools()}
        for name in ("harness_submit", "harness_session_open"):
            self.assertEqual(tools[name].input_schema["properties"]["mode"]["default"], "edit")
            self.assertEqual(tools[name].input_schema["properties"]["mode"]["enum"], ["review", "edit"])
        await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "fix", "client_request_id": "default-one"})
        proxy.submit.assert_awaited_once_with("demo", "fix", "default-one", None, None, "edit")
        await server.call_tool("harness_session_open", {"project_id": "demo", "client_request_id": "default-open"})
        proxy.session_open.assert_awaited_once_with("demo", "default-open", "", "edit", None)
        await server.call_tool("harness_submit", {"project_id": "demo", "prompt": "read", "client_request_id": "review-one", "mode": "review"})
        proxy.submit.assert_awaited_with("demo", "read", "review-one", None, None, "review")


if __name__ == "__main__":
    unittest.main()
