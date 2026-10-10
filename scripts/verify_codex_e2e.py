"""通过已安装 Codex 的公开接口验收个人注册的 Harness MCP 四个业务工具。

只创建 ephemeral 控制上下文，不发送 Codex 模型 turn；真实模型审查由桌面
Harness 执行。固定请求编号支持失败后核查/幂等重放，不自动创建新的付费重试。
原始客户端通知、认证信息、模型推理过程均不写入验收记录。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

from verify_clients import Wire, _cli, _tool_names, SERVER, TOOLS, desktop_execution_records

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
PROMPT = "审查 calculator.py 的空列表平均值和负数提款边界，只读代码并给出准确行号与原始证据，不执行测试。"


class CheckFailed(RuntimeError):
    """仅保存探针自己定义的错误标签，避免泄露第三方原始诊断。"""


def require(condition, label):
    if not condition:
        raise CheckFailed(label)


def worker(args):
    from harness_mcp.config import load_config
    from harness_mcp.process_control import named_job_stopped

    config = load_config(args.config.resolve())
    require(config.executor.backend == "desktop", "DesktopBackendRequired")
    client_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    personal_config = client_home / "config.toml"
    config_hash = hashlib.sha256(personal_config.read_bytes()).hexdigest()
    registration = tomllib.loads(personal_config.read_text(encoding="utf-8"))["mcp_servers"][SERVER]
    expected_args = ["-m", "harness_mcp.server", "--config", str(args.config.resolve())]
    require(Path(registration["command"]).resolve() == Path(sys.executable).resolve()
            and registration["args"] == expected_args and registration.get("enabled", True),
            "PersonalRegistrationMismatch")
    project = config.projects["sample"].path
    source = project / "calculator.py"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    environment = dict(os.environ)
    binary = _cli("codex", args.cli)
    version = subprocess.run([binary, "--version"], env=environment, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8").strip()[:100]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "status": "failed",
        "scope": "installed_codex_public_mcp_tool_call_personal_registration_real_desktop_model",
        "codex_version": version, "backend": "desktop", "personal_registration_verified": True,
        "codex_model_turn_sent": False, "ephemeral_context": True,
        "current_gui_conversation_hot_reload": "not_verified",
        "client_request_id": args.client_request_id, "calls": [],
        "cancel_scope": "queued_job_before_desktop_execution",
    }
    # 只为本探针关闭无关的 node_repl 启动，不修改个人配置文件或 Harness 注册。
    wire = Wire([binary, "app-server", "--listen", "stdio://", "-c", "mcp_servers.node_repl.enabled=false"],
                environment, ROOT)
    request_id = 0
    thread_id = None

    def rpc(method, params=None):
        nonlocal request_id
        request_id += 1
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        wire.send(message)
        answer = wire.receive(lambda value: value.get("id") == request_id, timeout=45)
        require("result" in answer, "CodexRpcFailed_" + method.replace("/", "_"))
        return answer["result"]

    def call(name, arguments):
        started = time.monotonic()
        response = rpc("mcpServer/tool/call", {
            "threadId": thread_id, "server": SERVER, "tool": name, "arguments": arguments,
        })
        value = response.get("structuredContent")
        if value is None:
            contents = response.get("content", [])
            text = next((item.get("text") for item in contents if item.get("type") == "text"), None)
            value = json.loads(text) if text else None
        require(not response.get("isError") and isinstance(value, dict) and value.get("ok"),
                "HarnessToolFailed_" + name)
        record = {"method": "mcpServer/tool/call", "tool": name, "ok": True,
                  "seconds": round(time.monotonic() - started, 4)}
        for key in ("job_id", "status", "phase", "ready", "idempotent_replay", "termination_failed"):
            if key in value:
                record[key] = value[key]
        report["calls"].append(record)
        return value

    try:
        rpc("initialize", {"clientInfo": {"name": "harness_codex_e2e", "version": "0.2.0"}})
        wire.send({"method": "initialized", "params": {}})
        started = rpc("thread/start", {"ephemeral": True, "cwd": str(project),
                                       "approvalPolicy": "never", "sandbox": "read-only"})
        thread_id = started["thread"]["id"]
        inventory = rpc("mcpServerStatus/list", {"threadId": thread_id, "serverName": SERVER,
                                                "detail": "toolsAndAuthOnly"})
        server = next((item for item in inventory.get("data", []) if item.get("name") == SERVER), {})
        report["tools"] = _tool_names(server.get("tools"))
        require(set(report["tools"]) == TOOLS and not server.get("toolsError"), "FourToolsUnavailable")
        info = server.get("serverInfo") or {}
        report["server_info"] = {key: info[key] for key in ("name", "version") if key in info}
        submission = {"project_id": "sample", "prompt": PROMPT, "relative_paths": ["calculator.py"],
                      "client_request_id": args.client_request_id, "timeout_seconds": args.timeout_seconds}
        receipt = call("harness_submit", submission)
        report["first_submit_idempotent_replay"] = receipt["idempotent_replay"]
        job_id = receipt["job_id"]
        report["job_id"] = job_id
        # 新鲜首轮才创建排队取消项；首项回放若已终态，不能再建可能执行模型的第二项。
        # 重放仅检查原作业的幂等取消，保留其原来的成功/失败终态。
        queued_cancel = not receipt["idempotent_replay"] and receipt["status"] not in TERMINAL
        if queued_cancel:
            cancelled = call("harness_submit", {**submission,
                "client_request_id": args.client_request_id + "-cancel",
                "prompt": "排队取消验收；取消后不得执行此任务。"})
            cancel_id = cancelled["job_id"]
            if cancelled["status"] != "queued":
                call("harness_cancel", {"job_id": cancel_id})
                raise CheckFailed("SecondJobNotQueued")
        else:
            cancel_id = job_id
            report["cancel_scope"] = "terminal_idempotent_only"
        report["cancel_job_id"] = cancel_id
        if queued_cancel:
            call("harness_cancel", {"job_id": cancel_id})
        deadline = time.monotonic() + args.timeout_seconds + 15
        while time.monotonic() < deadline:
            status = call("harness_status", {"job_id": job_id})
            if status["status"] in TERMINAL:
                break
            time.sleep(max(1, min(3, status.get("poll_after_seconds", 1))))
        else:
            call("harness_cancel", {"job_id": job_id})
            raise CheckFailed("RealDesktopJobDeadlineExceeded")
        result = call("harness_result", {"job_id": job_id})
        replay = call("harness_submit", submission)
        cancel_status = call("harness_status", {"job_id": cancel_id})
        cancel_replay = call("harness_cancel", {"job_id": cancel_id})
        require(replay["job_id"] == job_id and replay["idempotent_replay"], "IdempotentSubmitFailed")
        if queued_cancel:
            require(cancel_status["status"] == "cancelled" and cancel_replay["status"] == "cancelled",
                    "QueuedCancelFailed")
            require(not desktop_execution_records(config.data_dir / "jobs" / cancel_id),
                    "CancelledJobStartedDesktop")
        else:
            require(cancel_replay["status"] == status["status"], "TerminalCancelChangedStatus")
        report_path = config.data_dir / "jobs" / job_id / "report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        findings = payload.get("findings", [])
        lines = source.read_text(encoding="utf-8").splitlines()
        citations = bool(findings) and all(
            item.get("path") == "calculator.py" and isinstance(item.get("line_start"), int)
            and isinstance(item.get("line_end"), int)
            and 1 <= item["line_start"] <= item["line_end"] <= len(lines)
            and item.get("evidence", "").strip()
            and item["evidence"].strip() in "\n".join(lines[item["line_start"]-1:item["line_end"]])
            and item.get("validation", {}).get("status") == "valid" for item in findings)
        executions = [json.loads(path.read_text(encoding="utf-8"))
                      for path in desktop_execution_records(report_path.parent)]
        stopped = bool(executions) and all(asyncio.run(named_job_stopped(item["job_name"])) for item in executions)
        homes_cleaned = bool(executions) and not list(report_path.parent.glob("desktop_runs/*/home"))
        unchanged = before == hashlib.sha256(source.read_bytes()).hexdigest()
        read_only = payload.get("tests") == "not_run" and payload.get("changed_files") == []
        report.update(job_status=status["status"], report_path=str(report_path), findings_count=len(findings),
                      desktop_model_execution_started_this_run=not receipt["idempotent_replay"] and bool(executions),
                      citations_independently_verified=bool(citations), source_hash_unchanged=unchanged,
                      read_only_report_verified=read_only, owned_desktop_tree_stopped=stopped,
                      dedicated_homes_cleaned=homes_cleaned, idempotent_replay_verified=True,
                      queued_cancel_verified=True if queued_cancel else None,
                      cancelled_job_desktop_execution_count=0 if queued_cancel else None,
                      four_business_tools_called=set(item["tool"] for item in report["calls"]) == TOOLS)
        require(status["status"] == "succeeded" and result.get("ready") and not status["termination_failed"]
                and citations and unchanged and read_only and stopped and homes_cleaned, "DesktopResultCheckFailed")
        report["status"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, CheckFailed):
            report["check_failed"] = str(error)
    finally:
        if thread_id is not None:
            try:
                report["ephemeral_context_unsubscribed"] = rpc("thread/unsubscribe", {"threadId": thread_id})["status"] == "unsubscribed"
            except Exception:
                report["ephemeral_context_unsubscribed"] = False
        try:
            wire.close()
            report["client_control_closed"] = True
        except Exception:
            # 关闭故障不覆盖先前诊断；外层所有权仍必须核对整个 Codex 进程树停止。
            report["client_control_closed"] = False
            report["status"] = "failed"
        report["personal_config_hash_unchanged"] = config_hash == hashlib.sha256(personal_config.read_bytes()).hexdigest()
        if not report["personal_config_hash_unchanged"]:
            report["status"] = "failed"
    return report


async def supervise(args):
    from harness_mcp.process_control import spawn_owned, stop_process_tree

    environment = {name: os.environ[name] for name in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME",
    ) if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", "--config", str(args.config.resolve()),
            "--client-request-id", args.client_request_id, "--timeout-seconds", str(args.timeout_seconds)]
    if args.cli:
        argv += ["--cli", args.cli]
    process = spawn_owned(argv, environment, max_output=2 * 1024 * 1024)
    communication = asyncio.create_task(asyncio.to_thread(process.communicate))
    report = {"status": "failed"}
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=args.timeout_seconds + 120)
        require(process.returncode == 0, "ProbeWorkerFailed")
        report.update(json.loads(stdout))
    except Exception as error:
        report["supervisor_error_type"] = type(error).__name__
    finally:
        report["owned_codex_process_tree_stopped"] = await stop_process_tree(process)
        if report["owned_codex_process_tree_stopped"]:
            await asyncio.gather(communication, return_exceptions=True)
        else:
            report["status"] = "failed"
    return report


def main():
    sys.path.insert(0, str(ROOT / "src"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/codex.local.toml")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--client-request-id", default="codex-acceptance-v1")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--cli")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False))
        return
    print("正在通过本机 Codex 调用桌面 Harness；仅保存验收结果。", flush=True)
    report = asyncio.run(supervise(args))
    # 默认将重放写到单独记录，保留首轮真实提交和排队取消的原始证据。
    output = args.output or ROOT / "artifacts" / (
        "codex-e2e-replay.json" if report.get("first_submit_idempotent_replay") else "codex-e2e.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
