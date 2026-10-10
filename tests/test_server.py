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


if __name__ == "__main__":
    unittest.main()
