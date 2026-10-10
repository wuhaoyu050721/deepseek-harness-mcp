"""验证阶段 0 探针的真实 stdio 通道、参数错误和 stdin EOF 清理。"""

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys
import time

from mcp import Client, StdioServerParameters


async def sdk_probe(cwd: Path) -> dict:
    started = time.monotonic()
    nonce = "阶段0-真实协议调用-20261010"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness_mcp.probe"],
        cwd=str(cwd),
    )
    async with Client(params) as client:
        listing = await client.list_tools()
        assert [tool.name for tool in listing.tools] == ["phase0_probe"]
        response = await client.call_tool("phase0_probe", {"nonce": nonce})
        result = response.structured_content
        assert not response.is_error, "探针工具返回错误"
        assert result is not None and result.get("nonce") == nonce, result
        assert result["harness_executed"] is False
        assert result["model_called"] is False
    return {
        "client": "official_mcp_python_sdk",
        "status": "passed",
        "tools": [tool.name for tool in listing.tools],
        "result": result,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


async def wire_probe(cwd: Path) -> dict:
    """独立检查每行 stdout 均为 JSON-RPC，并确认 EOF 后子进程退出。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harness_mcp.probe",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    transcript = []

    async def send(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def receive(request_id: int) -> dict:
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            assert raw and len(raw) <= 65536, "stdout 关闭或消息超限"
            message = json.loads(raw)
            assert isinstance(message, dict) and message.get("jsonrpc") == "2.0"
            transcript.append(message)
            if message.get("id") == request_id:
                return message

    try:
        await send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "phase0-wire-check", "version": "0.0.1"},
            },
        })
        initialization = await receive(1)
        assert "result" in initialization, initialization
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = await receive(2)
        assert [tool["name"] for tool in listing["result"]["tools"]] == ["phase0_probe"]
        await send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "phase0_probe", "arguments": {"nonce": "wire-probe"}},
        })
        success = await receive(3)
        assert success["result"]["structuredContent"]["nonce"] == "wire-probe"
        await send({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "phase0_probe", "arguments": {"nonce": ""}},
        })
        invalid = await receive(4)
        assert invalid.get("result", {}).get("isError") is True, invalid
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)
        remainder = await process.stdout.read()
        for raw in remainder.splitlines():
            message = json.loads(raw)
            assert message.get("jsonrpc") == "2.0"
            transcript.append(message)
        assert process.returncode == 0, process.returncode
        # 探针不访问模型，日志只取字节数量；错误堆栈不进入验证报告。
        stderr = await process.stderr.read()
        return {
            "status": "passed", "stdout_jsonrpc_only": True,
            "parameter_error_verified": True, "stdin_eof_exit_code": process.returncode,
            "stderr_bytes": len(stderr), "messages": transcript,
        }
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verify(root: Path) -> dict:
    cwd = root / "data" / "phase0" / "中文 空格目录"
    cwd.mkdir(parents=True, exist_ok=True)
    return {
        "scope": "protocol_probe_only",
        "mcp_version": importlib.metadata.version("mcp"),
        "python_version": sys.version.split()[0],
        "cwd_with_unicode_and_spaces": True,
        "sdk_probe": await sdk_probe(cwd),
        "wire_probe": await wire_probe(cwd),
        "codex_client_verified": False,
        "claude_client_verified": False,
        "real_harness_verified": False,
        "phase0_passed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(asyncio.wait_for(verify(root), timeout=45))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()

