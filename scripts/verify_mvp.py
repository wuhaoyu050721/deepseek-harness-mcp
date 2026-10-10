"""基础六工具真实 stdio 验证：缺失桌面安装失败报告、缺失 Docker 恢复阻断。

此记录仅验证官方 MCP SDK 客户端和独立 JSON-RPC 通道，不等同于 Codex、
Claude Code、真实 Harness、真实容器或付款模型的验收。总时限60秒。
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

from mcp import Client, StdioServerParameters

TOOLS = {"harness_submit", "harness_status", "harness_result", "harness_cancel", "harness_service_info", "harness_preflight"}
REQUIRED = {
    "harness_submit": {"project_id", "prompt", "client_request_id"},
    "harness_status": {"job_id"}, "harness_result": {"job_id"}, "harness_cancel": {"job_id"},
    "harness_service_info": set(), "harness_preflight": {"project_id"},
}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
MISSING_DESKTOP_ERROR = "DESKTOP_NOT_FOUND" if os.name == "nt" else "DESKTOP_PLATFORM_UNSUPPORTED"


def validate_tools_contract(tools: list[dict[str, Any]]) -> None:
    assert {tool["name"] for tool in tools} == TOOLS, "基础六工具发现集合不符合契约"
    assert len(tools) == len(TOOLS), "存在重复或额外工具"
    for tool in tools:
        schema = tool["inputSchema"]
        assert set(schema.get("required", [])) == REQUIRED[tool["name"]], tool["name"]
        assert "api_key" not in schema.get("properties", {}), "工具不得接收明文凭证"
        assert "provider" not in schema.get("properties", {}), "工具不得更换模型路由"
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    view = schemas["harness_result"]["properties"]["view"]
    assert view["default"] == "full" and set(view["enum"]) == {"full", "summary", "patch", "files"}, "结果视图契约不一致"
    assert not schemas["harness_service_info"].get("properties"), "服务信息不得接收配置路径或凭证"
    assert set(schemas["harness_preflight"]["properties"]) == {"project_id", "relative_paths", "mode"}, "预检只能使用已授权项目/相对范围/模式"


def fixture_env(root: Path) -> dict[str, str]:
    # 不继承桌面 credentials 或个人模型环境；值是无效、人工测试标记。
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({
        "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "HARNESS_TEST_FIXTURE_KEY": "not-a-real-model-credential",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return environment


def prepare_fixture(root: Path, directory: Path) -> tuple[Path, dict[str, Path], dict[str, str]]:
    environment = fixture_env(root)
    repository = directory / "人工 Git 项目"
    repository.mkdir()
    repository.joinpath("sample.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    for command in (
        ["git", "init", "--quiet"], ["git", "add", "--", "sample.py"],
        ["git", "-c", "user.name=MCP Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=" + str(directory / "no-hooks"), "commit", "--quiet", "-m", "人工协议验证"],
    ):
        result = subprocess.run(command, cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        assert result.returncode == 0, "人工 Git 仓库准备失败"
    missing_docker = directory / "Docker明确不存在"
    missing_desktop = directory / "Harness桌面安装明确不存在"
    assert not missing_docker.exists() and not missing_desktop.exists()
    configs = {}
    for name in ("sdk-a", "sdk-b", "wire", "wire-docker-blocked"):
        configuration = directory / f"{name}.toml"
        quote = lambda path: json.dumps(path.as_posix(), ensure_ascii=False)
        executor = (
            f"[executor]\nbackend = \"docker\"\ndocker_command = {quote(missing_docker)}\n"
            if name == "wire-docker-blocked"
            else f"[executor]\nbackend = \"desktop\"\ndesktop_root = {quote(missing_desktop)}\n"
        )
        configuration.write_text(
            f"data_dir = {quote(directory / name)}\n"
            + executor +
            "[model]\ncredential_source = \"env\"\ncredential_env = \"HARNESS_TEST_FIXTURE_KEY\"\n"
            f"[projects.fixture]\npath = {quote(repository)}\n",
            encoding="utf-8",
        )
        configs[name] = configuration
    return repository, configs, environment


def parameters(config: Path, directory: Path, environment: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "harness_mcp.server", "--config", str(config)], cwd=str(directory), env=environment)


def fixture_job_count(data_dir: Path) -> int:
    """只读核对本次人工实例，不根据工具错误自行宣称没有创建作业。"""
    uri = data_dir.joinpath("jobs.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


async def sdk_call(client: Client, name: str, arguments: dict, metrics: list[dict]) -> dict:
    started = time.monotonic()
    response = await client.call_tool(name, arguments)
    metrics.append({"tool": name, "seconds": round(time.monotonic() - started, 4)})
    assert not response.is_error, f"{name} SDK/协议调用异常"
    result = response.structured_content
    assert isinstance(result, dict), "工具没有结构化结果"
    return result


async def poll_terminal(call, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = await call("harness_status", {"job_id": job_id})
        assert status["ok"] is True
        if status["status"] in TERMINAL:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("人工任务未在预期时间内进入终态")


async def check_duplicate(config: Path, directory: Path, environment: dict[str, str]) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness_mcp.server", "--config", str(config),
        cwd=directory, env=environment, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        assert process.returncode == 1, "共享目录第二实例未被拒绝"
        assert not stdout, "启动锁错误混入 stdout"
        assert b"INSTANCE_LOCKED" in stderr, "第二实例未报告实例锁错误"
        return {"status": "passed", "exit_code": 1, "stdout_bytes": 0, "stderr_bytes": len(stderr)}
    finally:
        if process.returncode is None:
            process.kill(); await process.wait()


async def sdk_verify(configs: dict[str, Path], directory: Path, environment: dict[str, str]) -> dict:
    metrics = []
    started = time.monotonic()
    async with Client(parameters(configs["sdk-a"], directory, environment)) as client:
        initialization_seconds = round(time.monotonic() - started, 4)
        tools = await client.list_tools()
        validate_tools_contract([tool.model_dump(by_alias=True) for tool in tools.tools])
        call = lambda name, arguments: sdk_call(client, name, arguments, metrics)
        request = {"project_id": "fixture", "prompt": "只读查看 sample.py 并说明边界", "client_request_id": "sdk-one", "relative_paths": ["sample.py"], "timeout_seconds": 10}
        submitted = await call("harness_submit", request)
        assert submitted["ok"] is True and submitted["idempotent_replay"] is False
        replay = await call("harness_submit", request)
        assert replay["job_id"] == submitted["job_id"] and replay["idempotent_replay"] is True
        conflict = await call("harness_submit", {**request, "prompt": "不同审查描述"})
        assert conflict["ok"] is False and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        for tool in ("harness_status", "harness_result"):
            missing = await call(tool, {"job_id": "missing-job"})
            assert missing["ok"] is False and missing["error"]["code"] == "JOB_NOT_FOUND"
        first_result = await call("harness_result", {"job_id": submitted["job_id"]})
        assert first_result["ok"] is True and isinstance(first_result["ready"], bool)
        terminal = await poll_terminal(call, submitted["job_id"])
        assert terminal["status"] == "failed" and terminal["error"]["code"] == MISSING_DESKTOP_ERROR, terminal
        final_result = await call("harness_result", {"job_id": submitted["job_id"]})
        assert final_result["ready"] is True, final_result
        cancelled = await call("harness_cancel", {"job_id": submitted["job_id"]})
        again = await call("harness_cancel", {"job_id": submitted["job_id"]})
        assert cancelled["status"] == again["status"] == "failed"
        assert cancelled["job_id"] == again["job_id"] == submitted["job_id"]
        duplicate = await check_duplicate(configs["sdk-a"], directory, environment)
        async with Client(parameters(configs["sdk-b"], directory, environment)) as separate:
            listing = await separate.list_tools()
            validate_tools_contract([tool.model_dump(by_alias=True) for tool in listing.tools])
            isolated = await sdk_call(separate, "harness_status", {"job_id": submitted["job_id"]}, metrics)
            assert isolated["error"]["code"] == "JOB_NOT_FOUND"
            separate_submit = await sdk_call(separate, "harness_submit", {**request, "client_request_id": "separate-one"}, metrics)
            separate_call = lambda name, arguments: sdk_call(separate, name, arguments, metrics)
            separate_terminal = await poll_terminal(separate_call, separate_submit["job_id"])
            assert separate_terminal["status"] == "failed" and separate_terminal["error"]["code"] == MISSING_DESKTOP_ERROR
        assert fixture_job_count(directory / "sdk-a") == fixture_job_count(directory / "sdk-b") == 1
        assert not list(directory.glob("sdk-*/jobs/*/desktop_runs")), "缺失安装仍创建了桌面执行目录"
        return {
            "status": "passed", "client": "official_mcp_python_sdk", "tools": sorted(TOOLS),
            "initialization_seconds": initialization_seconds, "submit_job_id": submitted["job_id"],
            "idempotency_verified": True, "conflict_verified": True, "unknown_job_verified": True,
            "missing_executor_terminal": terminal["status"], "missing_executor_error": terminal["error"]["code"],
            "failure_report_persisted": final_result["ready"], "terminal_cancel_idempotent": True,
            "executor_backend": "desktop", "desktop_installation_explicitly_missing": True,
            "desktop_worker_started": False, "single_job_per_instance_verified": True,
            "same_directory_second_instance": duplicate, "two_separate_instances_verified": True,
            "metadata_calls": metrics,
            "prewarmed_metadata_2_second_target_met": all(item["seconds"] <= 2 for item in metrics),
        }


class WireSession:
    def __init__(self, process):
        self.process = process
        self.request_id = 0
        self.messages = []
        self.metrics = []

    async def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    def parse(self, raw: bytes) -> dict:
        assert raw and len(raw) <= 65536, "stdout 消息关闭或超出上限"
        message = json.loads(raw)
        assert isinstance(message, dict) and message.get("jsonrpc") == "2.0", "stdout 包含非协议内容"
        self.messages.append(message)
        return message

    async def rpc(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request_id = self.request_id
        started = time.monotonic()
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self.parse(await asyncio.wait_for(self.process.stdout.readline(), 10))
            if message.get("id") == request_id:
                self.metrics.append({"method": method, "seconds": round(time.monotonic() - started, 4)})
                assert "result" in message, message
                return message["result"]

    async def tool(self, name: str, arguments: dict) -> dict:
        response = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert response.get("isError") is not True, response
        result = response.get("structuredContent")
        assert isinstance(result, dict), result
        return result


async def count_stderr(stream) -> int:
    total = 0
    while raw := await stream.read(8192):
        total += len(raw)
    return total


async def wire_verify(config: Path, directory: Path, environment: dict[str, str], *, recovery_blocked: bool = False) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "harness_mcp.server", "--config", str(config),
        cwd=directory, env=environment, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    stderr_task = asyncio.create_task(count_stderr(process.stderr))
    wire = WireSession(process)
    try:
        initialization = await wire.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mvp-wire-verification", "version": "0.1.0"}})
        await wire.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = await wire.rpc("tools/list", {})
        validate_tools_contract(listing["tools"])
        arguments = {"project_id": "fixture", "prompt": "人工协议测试，不运行项目命令", "client_request_id": "wire-cancel", "timeout_seconds": 10}
        submitted = await wire.tool("harness_submit", arguments)
        terminal = None
        if recovery_blocked:
            assert submitted["ok"] is False and submitted["error"]["code"] == "SERVICE_STOPPING", submitted
            assert "job_id" not in submitted, "恢复未确认仍返回了新作业"
            repeated = await wire.tool("harness_submit", arguments)
            assert repeated["ok"] is False and repeated["error"]["code"] == "SERVICE_STOPPING", repeated
            assert "job_id" not in repeated
            for name in ("harness_status", "harness_result", "harness_cancel"):
                unknown = await wire.tool(name, {"job_id": "never-created"})
                assert unknown["ok"] is False and unknown["error"]["code"] == "JOB_NOT_FOUND", unknown
            assert fixture_job_count(directory / config.stem) == 0, "恢复阻断仍持久化了作业"
            assert not (directory / config.stem / "jobs").exists(), "恢复阻断仍启动了准备或模型执行"
        else:
            assert submitted["ok"] is True
            cancellation = await wire.tool("harness_cancel", {"job_id": submitted["job_id"]})
            assert cancellation["ok"] is True
            terminal = await poll_terminal(wire.tool, submitted["job_id"])
            assert terminal["status"] in {"cancelled", "failed"}, terminal
            if terminal["status"] == "failed":
                assert terminal["error"]["code"] == MISSING_DESKTOP_ERROR, terminal
            result = await wire.tool("harness_result", {"job_id": submitted["job_id"]})
            assert result["ok"] is True
            assert not list((directory / config.stem).glob("jobs/*/desktop_runs"))
        eof_started = time.monotonic()
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        while raw := await process.stdout.readline():
            wire.parse(raw)
        assert process.returncode == 0, "stdin EOF 后服务未正常退出"
        stderr_bytes = await stderr_task
        if recovery_blocked:
            assert fixture_job_count(directory / config.stem) == 0
            assert not (directory / config.stem / "jobs").exists()
        return {
            "status": "passed", "negotiated_protocol": initialization["protocolVersion"],
            "stdout_jsonrpc_only": True, "stdout_protocol_messages": len(wire.messages),
            "stdin_eof_exit_code": process.returncode, "stdin_eof_exit_seconds": round(time.monotonic() - eof_started, 4),
            "executor_backend": "docker" if recovery_blocked else "desktop",
            "startup_recovery_blocked": recovery_blocked,
            "submit_error": submitted["error"]["code"] if recovery_blocked else None,
            "no_jobs_persisted_verified": recovery_blocked, "no_execution_started_verified": recovery_blocked,
            "cancellation_terminal": terminal["status"] if terminal else None, "stderr_bytes": stderr_bytes,
            "metadata_calls": wire.metrics, "messages": wire.messages,
        }
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)


async def verify(root: Path) -> dict:
    parent = root / "data" / "verification"
    parent.mkdir(parents=True, exist_ok=True)
    # 只清理由本脚本创建并验证归属的一次性目录，不触碰授权业务项目。
    with tempfile.TemporaryDirectory(prefix="MCP 中文 空格 ", dir=parent) as temporary:
        directory = Path(temporary).resolve()
        directory.relative_to(parent.resolve())
        _, configurations, environment = await asyncio.to_thread(prepare_fixture, root, directory)
        sdk = await sdk_verify(configurations, directory, environment)
        wire = await wire_verify(configurations["wire"], directory, environment)
        docker_blocked = await wire_verify(configurations["wire-docker-blocked"], directory, environment, recovery_blocked=True)
        return {
            "scope": "mcp_sdk_and_raw_stdio_fixtures_only", "status": "passed",
            "mcp_version": importlib.metadata.version("mcp"), "python_version": sys.version.split()[0],
            "cwd_with_unicode_and_spaces": True, "fixture_repository_only": True,
            "real_codex_claude_clients": False, "real_harness": False, "real_model": False, "real_docker": False,
            "production_executor_fake_mode": False,
            "executor_scenarios": {
                "desktop": "explicitly missing installation produces a terminal failure report without starting Harness",
                "docker": "explicitly missing CLI blocks startup recovery and rejects submit without creating jobs",
            },
            "sdk": sdk, "wire": wire, "docker_startup_recovery": docker_blocked,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="基础六工具 MCP stdio 验证（人工仓库、无模型调用）")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(asyncio.wait_for(verify(root), timeout=60))
    output = args.output or root / "artifacts" / "mvp-protocol.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "scope": report["scope"], "output": str(output), "real_model": False, "real_docker": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
