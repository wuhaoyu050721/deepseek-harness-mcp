"""人工示例仓库的真实桌面 Harness 模型验收；不会自动重试新的付费任务。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from mcp import Client, StdioServerParameters

from harness_mcp.config import load_config
from harness_mcp.process_control import named_job_stopped
from verify_clients import desktop_execution_records


async def verify(args) -> dict:
    config = load_config(args.config.resolve())
    assert config.executor.backend == "desktop", "验收必须使用 desktop backend"
    project = config.projects["sample"].path
    source = project / "calculator.py"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    environment = {name: os.environ[name] for name in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if name in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    metrics = []
    params = StdioServerParameters(command=sys.executable, args=["-m", "harness_mcp.server", "--config", str(args.config.resolve())], env=environment)
    async with Client(params) as client:
        tools = await client.list_tools()
        names = {item.name for item in tools.tools}
        assert names == {"harness_submit", "harness_status", "harness_result", "harness_cancel"}

        async def call(name, arguments):
            started = time.monotonic()
            reply = await client.call_tool(name, arguments)
            metrics.append({"tool": name, "seconds": round(time.monotonic() - started, 4)})
            assert not reply.is_error
            value = reply.structured_content
            assert value and value["ok"], name
            return value

        receipt = await call("harness_submit", {
            "project_id": "sample", "prompt": "审查 calculator.py 的空列表平均值和负数提款边界，只读代码并给出准确行号与原始证据，不执行测试。",
            "relative_paths": ["calculator.py"], "client_request_id": args.client_request_id,
            "timeout_seconds": args.timeout_seconds,
        })
        job_id = receipt["job_id"]
        last_phase = None
        deadline = time.monotonic() + args.timeout_seconds + 20
        while time.monotonic() < deadline:
            status = await call("harness_status", {"job_id": job_id})
            if status["phase"] != last_phase:
                print(json.dumps({"job_id": job_id, "phase": status["phase"]}, ensure_ascii=False), flush=True)
                last_phase = status["phase"]
            if status["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                break
            await asyncio.sleep(1)
        else:
            await call("harness_cancel", {"job_id": job_id})
            raise AssertionError("真实任务未按预期进入终态；已发起取消，不自动重试")
        result = await call("harness_result", {"job_id": job_id})
        replay = await call("harness_submit", {
            "project_id": "sample", "prompt": "审查 calculator.py 的空列表平均值和负数提款边界，只读代码并给出准确行号与原始证据，不执行测试。",
            "relative_paths": ["calculator.py"], "client_request_id": args.client_request_id,
            "timeout_seconds": args.timeout_seconds,
        })
        assert replay["job_id"] == job_id and replay["idempotent_replay"]
    report_path = config.data_dir / "jobs" / job_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    findings = report.get("findings", [])
    # 报告校验契约由宿主负责；验收另独立核对引用是否确实出自人工源文件。
    lines = source.read_text(encoding="utf-8").splitlines()
    citations_correct = bool(findings) and all(
        finding.get("path") == "calculator.py" and
        isinstance(finding.get("line_start"), int) and isinstance(finding.get("line_end"), int) and
        1 <= finding["line_start"] <= finding["line_end"] <= len(lines) and
        finding.get("evidence", "").strip() in "\n".join(lines[finding["line_start"]-1:finding["line_end"]])
        for finding in findings
    )
    executions = [json.loads(path.read_text(encoding="utf-8")) for path in desktop_execution_records(report_path.parent)]
    stopped = bool(executions) and all([await named_job_stopped(item["job_name"]) for item in executions])
    homes_cleaned = bool(executions) and all(not path.exists() for path in report_path.parent.glob("desktop_runs/*/home"))
    read_only_report = report.get("tests") == "not_run" and report.get("changed_files") == []
    mechanical_valid = bool(findings) and all(item.get("validation", {}).get("status") == "valid" for item in findings)
    unchanged = before == hashlib.sha256(source.read_bytes()).hexdigest()
    passed = status["status"] == "succeeded" and result["ready"] and unchanged and citations_correct and mechanical_valid and read_only_report and stopped and homes_cleaned and not status["termination_failed"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "service_version": "0.2.0",
        "scope": "official_mcp_sdk_to_installed_desktop_runtime_real_model_artificial_repository",
        "status": "passed" if passed else "failed", "backend": "desktop", "job_id": job_id,
        "client_request_id": args.client_request_id, "job_status": status["status"],
        "error": status.get("error"), "report_path": str(report_path),
        "findings_count": len(findings), "citations_independently_verified": citations_correct,
        "source_hash_unchanged": unchanged, "idempotent_replay_verified": replay["idempotent_replay"],
        "mechanical_citations_valid": mechanical_valid, "read_only_report_verified": read_only_report,
        "owned_process_tree_stopped": stopped, "dedicated_homes_cleaned": homes_cleaned,
        "termination_failed": status["termination_failed"], "metadata_max_seconds": max(item["seconds"] for item in metrics),
        "host_filesystem_sandbox": False, "gui_existing_session_takeover": False,
        "real_codex_client_acceptance": "not_run", "real_claude_client_acceptance": "not_run",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/codex.local.toml"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/desktop-mvp.json"))
    parser.add_argument("--client-request-id", default="desktop-acceptance-v1")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    record = asyncio.run(verify(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    raise SystemExit(0 if record["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
