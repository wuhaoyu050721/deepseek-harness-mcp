"""已安装 Codex/Claude 的真实 MCP 握手和四工具发现；无模型、无个人注册。

Codex 使用公开 app-server initialize + mcpServerStatus/list；Claude 使用官方
Agent SDK 控制通道 initialize + mcp_status。只连接生产 MCP 服务，不提交任务。
每次生成专用客户端 home、无项目/凭证的 MCP 配置，外层监管整棵进程树。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid


TOOLS = {"harness_submit", "harness_status", "harness_result", "harness_cancel"}
SERVER = "deepseek_harness"
MAX_WIRE_BYTES = 16 * 1024 * 1024


def desktop_execution_records(job_dir: Path) -> list[Path]:
    """同时读取待确认归属与已停止归档；归档记录仍由探针实际查询 Job。"""
    return sorted({*job_dir.glob("desktop_runs/*/execution.json"),
                   *job_dir.glob("desktop_runs/*/stopped/execution.json")})


def _quote(value: str) -> str:
    # JSON 字符串也是有效 TOML basic string；这里只序列化本机路径，不构建 shell。
    return json.dumps(value, ensure_ascii=False)


def _cli(name: str, override: str | None) -> str:
    found = override or shutil.which(name)
    if not found:
        raise FileNotFoundError("ClientCliUnavailable")
    path = Path(found).resolve()
    if name == "claude" and path.suffix.lower() in {".ps1", ".cmd"}:
        native = path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if native.is_file():
            path = native
    return str(path)


class Wire:
    """小型有界控制通道；不保存原始客户端事件/配置/认证信息。"""

    def __init__(self, argv: list[str], environment: dict[str, str], cwd: Path):
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=environment, cwd=cwd,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.messages = queue.Queue(maxsize=100)

        def read():
            total = 0
            try:
                while line := self.process.stdout.readline(MAX_WIRE_BYTES + 1):
                    total += len(line)
                    if total > MAX_WIRE_BYTES or not line.endswith(b"\n"):
                        self.messages.put({"_error": "OutputLimit"})
                        return
                    self.messages.put(json.loads(line))
            except Exception:
                self.messages.put({"_error": "WireReadFailed"})
            finally:
                self.messages.put({"_error": "WireClosed"})

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, message: dict):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self, matches, timeout=30.0):
        deadline = time.monotonic() + timeout
        while remaining := max(0.0, deadline - time.monotonic()):
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("ClientControlTimeout") from None
            if "_error" in message:
                raise RuntimeError(message["_error"])
            if matches(message):
                return message
            # 本探针没有任何用户 turn；遇到权限/工具执行请求失败关闭。
            if message.get("type") == "control_request" or ("method" in message and "id" in message):
                raise RuntimeError("UnexpectedExecutionRequest")
        raise TimeoutError("ClientControlTimeout")

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()


def _tool_names(rows) -> list[str]:
    values = rows.values() if isinstance(rows, dict) else rows if isinstance(rows, list) else []
    names = set()
    for tool in values:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            name = tool["name"]
            prefix = f"mcp__{SERVER}__"
            names.add(name[len(prefix):] if name.startswith(prefix) else name)
    return sorted(names)


def _worker(args: argparse.Namespace) -> dict:
    root = Path(__file__).resolve().parents[1]
    directory = args.probe_dir / args.client
    directory.mkdir()
    home = directory / "client-home"
    home.mkdir()
    config = directory / "mcp-service.toml"
    config.write_text(f'data_dir = {_quote(str(directory / "service-data"))}\n'
                      '[model]\ncredential_source = "env"\ncredential_env = "MCP_DISCOVERY_UNUSED_KEY"\n'
                      'base_url = "http://127.0.0.1:9/anthropic"\n'
                      '[executor]\nbackend = "desktop"\n', encoding="utf-8")
    specification = {"command": sys.executable,
                     "args": ["-m", "harness_mcp.server", "--config", str(config)], "cwd": str(root)}
    environment = dict(os.environ)
    # 外层已允许列表裁剪环境；再将所有客户端缓存定向到专用 home。
    environment.update(CODEX_HOME=str(home), CLAUDE_CONFIG_DIR=str(home),
                       ANTHROPIC_API_KEY="discovery-not-a-real-key", ANTHROPIC_BASE_URL="http://127.0.0.1:9",
                       DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_ENTRYPOINT="sdk-py",
                       MCP_CONNECTION_NONBLOCKING="0", MCP_CONNECT_TIMEOUT_MS="20000")
    binary = _cli(args.client, args.cli)
    version = subprocess.run([binary, "--version"], env=environment, cwd=directory, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8", errors="replace").strip()[:100]
    report = {"client": args.client, "version": version, "handshake_verified": False,
              "four_tools_discovered": False, "tools": []}
    if args.client == "codex":
        client_config = '[analytics]\nenabled = false\n[mcp_servers.deepseek_harness]\n'
        client_config += '\n'.join(f'{key} = {_quote(value) if isinstance(value,str) else json.dumps(value)}'
                                    for key, value in specification.items())
        client_config += '\nstartup_timeout_sec = 20\ntool_timeout_sec = 10\n'
        (home / "config.toml").write_text(client_config, encoding="utf-8")
        argv = [binary, "app-server", "--listen", "stdio://"]
    else:
        mcp_config = directory / "mcp-client.json"
        specification.pop("cwd")  # Claude stdio schema uses current cwd, not a cwd field.
        mcp_config.write_text(json.dumps({"mcpServers": {SERVER: specification}}, ensure_ascii=False), encoding="utf-8")
        argv = [binary, "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--bare", "--strict-mcp-config", "--mcp-config", str(mcp_config), "--setting-sources", "",
                "--no-session-persistence", "--tools", "", "--permission-mode", "dontAsk"]
    wire = Wire(argv, environment, directory)
    try:
        if args.client == "codex":
            wire.send({"id": 1, "method": "initialize", "params": {"clientInfo": {
                "name": "harness_mcp_discovery_probe", "version": "0.2.0"}}})
            initialized = wire.receive(lambda item: item.get("id") == 1)
            if "result" not in initialized:
                raise RuntimeError("CodexInitializeFailed")
            wire.send({"method": "initialized", "params": {}})
            wire.send({"id": 2, "method": "mcpServerStatus/list", "params": {
                "serverName": SERVER, "detail": "toolsAndAuthOnly"}})
            inventory = wire.receive(lambda item: item.get("id") == 2)
            if "result" not in inventory:
                raise RuntimeError("CodexDiscoveryFailed")
            rows = inventory["result"].get("data", [])
        else:
            def control(number, subtype):
                request_id = f"discovery-{number}"
                wire.send({"type": "control_request", "request_id": request_id, "request": {"subtype": subtype}})
                answer = wire.receive(lambda item: item.get("type") == "control_response" and
                                      item.get("response", {}).get("request_id") == request_id)
                response = answer["response"]
                if response.get("subtype") != "success":
                    raise RuntimeError("ClaudeControlFailed")
                return response.get("response", {})

            control(1, "initialize")
            rows = control(2, "mcp_status").get("mcpServers", [])
        server = next((row for row in rows if row.get("name") == SERVER), {})
        info = server.get("serverInfo") or {}
        report["runtime_status"] = server.get("runtimeStatus", server.get("status"))
        report["server_info"] = {key: info[key] for key in ("name", "version") if key in info}
        report["handshake_verified"] = info.get("name") == "deepseek-harness-mcp" and bool(info.get("version"))
        report["tools"] = _tool_names(server.get("tools"))
        report["four_tools_discovered"] = set(report["tools"]) == TOOLS
        report["tool_discovery_error"] = bool(server.get("toolsError") or server.get("error"))
        report["status"] = "passed" if (report["handshake_verified"] and report["four_tools_discovered"] and
                                          not report["tool_discovery_error"]) else "failed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
    finally:
        wire.close()
    return report


async def _supervise(args: argparse.Namespace, client: str) -> dict:
    from harness_mcp.process_control import spawn_owned, stop_process_tree

    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", "--client", client,
            "--probe-dir", str(args.probe_dir)]
    override = getattr(args, f"{client}_cli")
    if override:
        argv += ["--cli", override]
    environment = {name: os.environ[name] for name in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    report = {"client": client, "status": "failed", "model_called": False, "user_turn_submitted": False,
              "personal_client_config_modified": False, "process_tree_stopped": False}
    process = spawn_owned(argv, environment, max_output=1024 * 1024)
    communication = asyncio.create_task(asyncio.to_thread(process.communicate))
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=50)
        if process.returncode == 0:
            report.update(json.loads(stdout))
        else:
            report["error_type"] = "ClientProbeWorkerFailed"
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        report["process_tree_stopped"] = await stop_process_tree(process)
        if report["process_tree_stopped"]:
            await asyncio.gather(communication, return_exceptions=True)
        else:
            report["status"] = "failed"
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=["all", "codex", "claude"], default="all")
    parser.add_argument("--codex-cli")
    parser.add_argument("--claude-cli")
    parser.add_argument("--probe-dir", type=Path)
    parser.add_argument("--output", type=Path, default=root / "artifacts/client-discovery.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cli", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(_worker(args), ensure_ascii=False))
        return
    args.probe_dir = (args.probe_dir or root / "data/phase0" / ("clients-" + uuid.uuid4().hex)).resolve()
    args.probe_dir.mkdir(parents=True, exist_ok=False)
    clients = ["codex", "claude"] if args.client == "all" else [args.client]
    reports = [asyncio.run(_supervise(args, client)) for client in clients]
    payload = {"scope": "installed_client_control_protocol_discovery_only", "clients": reports,
               "status": "passed" if all(item["status"] == "passed" for item in reports) else "failed"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] != "passed":
        sys.exit(1)


if __name__ == "__main__":
    main()
