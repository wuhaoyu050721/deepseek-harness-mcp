"""通过 Codex 公开 MCP 接口验收桌面修改模式；原项目不自动合入。

仅建立 ephemeral 控制上下文，不发送 Codex 模型 turn。固定编号提交一次
Harness 修改任务；再次运行只核查同一作业，不创建新的付费重试。模型生成的
代码只做文本、摘要和 AST 静态检查，不执行。产物不保存模型全文、代码或秘密。
幂等重放验收须在原样例仍未合入时进行；原项目发生修改会使基线核对失败。
固定编号的幂等性仅限相同配置和 data_dir；更换存储实例会创建新的模型任务。
默认首轮与重放分别保存，避免用重放记录覆盖首次真实执行的验收证据。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from verify_clients import SERVER, TOOLS, Wire, _cli, _quote, _tool_names, desktop_execution_records


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "edit_sample"
REQUEST_ID = "desktop-edit-acceptance-v1"
TIMEOUT = 180
PAGE_SIZE = 2
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
PROMPT = ("修改 calculator.py：average([]) 明确抛 ValueError，withdraw 中 amount<0 或 "
          "amount>balance 明确抛 ValueError；正常平均和提款保持原结果。只修改 calculator.py，"
          "不添加或删除其他文件，不执行任何测试、构建或安装。")


class CheckFailed(RuntimeError):
    """只允许探针自己的固定标签进入记录，不复制客户端/模型的诊断。"""


def require(condition, label):
    if not condition:
        raise CheckFailed(label)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _read_json(path, limit=2 * 1024 * 1024):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, "LocalEvidenceSizeLimit")
    return json.loads(data)


def _personal_config_hash():
    # 仅摘要读取，不解析、复制或输出个人配置内容。
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "config.toml"
    return _sha(path.read_bytes()) if path.is_file() else None


def _source_inventory(project):
    values = {}
    total = 0
    for directory, folders, files in os.walk(project, followlinks=False):
        folders[:] = [name for name in folders if name != ".git"]
        require(not any((Path(directory) / name).is_symlink() or getattr(Path(directory) / name, "is_junction", lambda: False)()
                        for name in folders), "SourceLinkUnsupported")
        for name in files:
            path = Path(directory) / name
            require(not path.is_symlink() and path.is_file(), "SourceLinkUnsupported")
            require(path.stat().st_size <= 2 * 1024 * 1024 and len(values) < 2000, "SourceInventorySizeLimit")
            data = path.read_bytes()
            total += len(data)
            require(total <= 8 * 1024 * 1024, "SourceInventorySizeLimit")
            values[path.relative_to(project).as_posix()] = _sha(data)
    return values


def _condition(node, names):
    """只解释固定条件 AST 的语法；不 eval/compile/调用生成的代码。"""
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool, type(None))):
        return node.value
    if isinstance(node, ast.List) and not node.elts:
        return []
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1 and not node.keywords:
        return len(_condition(node.args[0], names))
    if isinstance(node, ast.UnaryOp):
        value = _condition(node.operand, names)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub) and type(value) is int:
            return -value
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [bool(_condition(value, names)) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _condition(node.left, names)
        for operator, right_node in zip(node.ops, node.comparators):
            right = _condition(right_node, names)
            if isinstance(operator, ast.Lt):
                matched = left < right
            elif isinstance(operator, ast.LtE):
                matched = left <= right
            elif isinstance(operator, ast.Gt):
                matched = left > right
            elif isinstance(operator, ast.GtE):
                matched = left >= right
            elif isinstance(operator, ast.Eq):
                matched = left == right
            elif isinstance(operator, ast.NotEq):
                matched = left != right
            elif isinstance(operator, ast.Is):
                matched = left is right
            elif isinstance(operator, ast.IsNot):
                matched = left is not right
            else:
                raise CheckFailed("StaticConditionUnsupported")
            if not matched:
                return False
            left = right
        return True
    raise CheckFailed("StaticConditionUnsupported")


def _static_behavior(before, after):
    old, new = ast.parse(before), ast.parse(after)
    originals = {node.name: node for node in old.body if isinstance(node, ast.FunctionDef)}
    functions = {node.name: node for node in new.body if isinstance(node, ast.FunctionDef)}
    require(set(originals) == set(functions) and {"average", "withdraw"} <= set(functions), "FunctionInventoryChanged")

    def guarded(name, rejected, accepted):
        function, original = functions[name], originals[name]
        require(ast.dump(function.args) == ast.dump(original.args), "FunctionSignatureChanged")
        old_returns = [ast.dump(node.value) for node in ast.walk(original) if isinstance(node, ast.Return)]
        new_returns = [ast.dump(node.value) for node in ast.walk(function) if isinstance(node, ast.Return)]
        same_returns = bool(old_returns) and old_returns == new_returns
        guards = []
        for node in function.body:
            if isinstance(node, ast.Return):
                break
            if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1 or not isinstance(node.body[0], ast.Raise):
                continue
            exception = node.body[0].exc
            target = exception.func if isinstance(exception, ast.Call) else exception
            if isinstance(target, ast.Name) and target.id == "ValueError":
                guards.append(node.test)
        rejects = bool(guards) and all(any(bool(_condition(condition, values)) for condition in guards) for values in rejected)
        accepts = all(not any(bool(_condition(condition, values)) for condition in guards) for values in accepted)
        return same_returns, rejects and accepts

    average_same, empty_guard = guarded("average", [{"values": []}], [{"values": [2, 4]}, {"values": [0]}])
    withdraw_same, amount_guard = guarded("withdraw", [{"balance": 10, "amount": -1}, {"balance": 10, "amount": 11}],
                                          [{"balance": 10, "amount": 2}, {"balance": 10, "amount": 0}, {"balance": 10, "amount": 10}])
    return {"scope": "static_ast_only_no_generated_code_execution", "python_ast_valid": True,
            "average_empty_value_error_guard": empty_guard, "withdraw_amount_value_error_guards": amount_guard,
            "normal_return_expressions_preserved": average_same and withdraw_same}


def _reconstruct(call, job_id):
    """完整重建 result 的机械 rows；模型原文/报告正文立即丢弃，不写 artifact。"""
    cursor = None
    seen = set()
    first = None
    metadata, patch_parts, contents = [], [], {}
    patch_chars = 0
    report_chars = 0
    for pages in range(1, 1001):
        page = call("harness_result", {"job_id": job_id, "cursor": cursor, "page_size": PAGE_SIZE})
        require(page.get("ready") is True and page.get("status") == "succeeded" and page.get("mode") == "edit"
                and page.get("edit_ready") is True and page.get("source_applied") is False
                and page.get("requires_main_agent_review") is True and page.get("tests") == "not_run", "EditResultNotReady")
        if first is None:
            first = {name: page.get(name) for name in ("report_id", "snapshot_id", "change_set_id", "total_changes", "total_patch_chars")}
        require(all(page.get(name) == value for name, value in first.items()), "ResultPaginationIdentityChanged")
        require(page["changes_offset"] == len(metadata) and len(page["changes"]) <= PAGE_SIZE
                and page["changed_files"] == [row["path"] for row in page["changes"]], "ChangeMetadataOffsetInvalid")
        metadata.extend(page["changes"])
        require(page["patch_text_offset"] == patch_chars and len(page["patch_text"]) <= 8000, "PatchOffsetInvalid")
        patch_parts.append(page["patch_text"])
        patch_chars += len(page["patch_text"])
        require(page["report_text_offset"] == report_chars and len(page["report_text"]) <= 8000, "ReportTextOffsetInvalid")
        report_chars += len(page["report_text"])
        require(sum(len(row["text"]) for row in page["change_contents"]) <= 8000 and len(page["change_contents"]) <= PAGE_SIZE,
                "ContentPageLimitExceeded")
        for row in page["change_contents"]:
            require(row["path"] == "calculator.py" and row["field"] == "after_text", "UnexpectedChangedFile")
            current = contents.get(row["path"], "")
            require(row["offset"] == len(current), "ContentOffsetInvalid")
            current += row["text"]
            require(len(current.encode("utf-8")) <= 1048576, "ReconstructedContentLimitExceeded")
            contents[row["path"]] = current
        require(patch_chars <= 1048576 and len(metadata) <= 10000, "ReconstructedPatchLimitExceeded")
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        require(isinstance(cursor, str) and cursor not in seen, "RepeatedResultCursor")
        seen.add(cursor)
    else:
        raise CheckFailed("ResultPageLimitExceeded")
    patch = "".join(patch_parts)
    require(len(metadata) == first["total_changes"] and len(patch) == first["total_patch_chars"], "IncompleteChangePagination")
    rows = []
    expected_offset = 0
    for row in metadata:
        require(row["path"] == "calculator.py" and row["change_type"] == "modify"
                and row["after_text_is_null"] is False and row["diff_offset"] == expected_offset, "UnexpectedChangeTypeOrPath")
        after = contents.get(row["path"], "")
        require(len(after) == row["after_text_chars"] and len(after.encode("utf-8")) == row["after_size_bytes"]
                and _sha(after.encode("utf-8")) == row["after_sha256"], "ReconstructedAfterHashMismatch")
        diff = patch[row["diff_offset"]:row["diff_offset"] + row["diff_chars"]]
        expected_offset += row["diff_chars"]
        rows.append({**{name: row[name] for name in ("path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes")},
                     "diff": diff, "after_text": after})
    require(len(rows) == 1 and expected_offset == len(patch), "ExpectedOneModifiedFile")
    require(_sha(_canonical({"snapshot_id": first["snapshot_id"], "changes": rows})) == first["change_set_id"], "ChangeSetHashMismatch")
    return first, rows, patch, pages


def worker(args):
    from harness_mcp.config import load_config
    from harness_mcp.process_control import named_job_stopped
    from harness_mcp.report import read_report

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "status": "failed",
              "scope": "installed_codex_public_mcp_tool_call_independent_config_real_desktop_edit",
              "project_id": PROJECT, "client_request_id": REQUEST_ID, "timeout_seconds": TIMEOUT,
              "codex_model_turn_sent": False, "ephemeral_context": True,
              "fresh_independent_app_server": True, "current_gui_conversation_hot_reload": "not_verified",
              "personal_client_config_modified": False, "source_applied": False,
              "generated_code_executed_by_probe": False, "probe_project_tests_run": False,
              "raw_model_output_saved_to_artifact": False, "calls": []}
    personal_hash = _personal_config_hash()
    report["personal_config_sha256_before"] = personal_hash
    wire = None
    thread_id = job_id = None
    terminal = False
    request_id = 0
    try:
        config = load_config(args.config.resolve())
        require(config.executor.backend == "desktop" and PROJECT in config.projects
                and config.projects[PROJECT].allow_edits, "DesktopEditProjectRequired")
        project = config.projects[PROJECT].path
        source = project / "calculator.py"
        before_bytes = source.read_bytes()
        before_files = _source_inventory(project)
        args.probe_dir.mkdir(parents=True, exist_ok=False)
        home = args.probe_dir / "client-home"
        home.mkdir()
        (home / "config.toml").write_text(
            '[analytics]\nenabled = false\n[mcp_servers.deepseek_harness]\n'
            f'command = {_quote(sys.executable)}\n'
            f'args = {json.dumps(["-m", "harness_mcp.server", "--config", str(args.config.resolve())], ensure_ascii=False)}\n'
            f'cwd = {_quote(str(ROOT))}\nstartup_timeout_sec = 20\ntool_timeout_sec = 30\n', encoding="utf-8")
        environment = dict(os.environ)
        environment.update(CODEX_HOME=str(home), PYTHONPATH=str(ROOT / "src"), DISABLE_TELEMETRY="1")
        binary = _cli("codex", args.cli)
        version = subprocess.run([binary, "--version"], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8", errors="replace").strip()[:100]
        require(bool(re.fullmatch(r"codex(?:-cli)? [0-9][A-Za-z0-9.+-]*", version)), "UnexpectedCodexVersionFormat")
        report.update(codex_version=version, backend="desktop", independent_codex_home=True)
        wire = Wire([binary, "app-server", "--listen", "stdio://"], environment, ROOT)

        def rpc(method, params=None):
            nonlocal request_id
            request_id += 1
            wire.send({"id": request_id, "method": method, **({"params": params} if params is not None else {})})
            answer = wire.receive(lambda value: value.get("id") == request_id, timeout=30)
            require("result" in answer, "CodexRpcFailed_" + method.replace("/", "_"))
            return answer["result"]

        def call(name, arguments):
            started = time.monotonic()
            response = rpc("mcpServer/tool/call", {"threadId": thread_id, "server": SERVER, "tool": name, "arguments": arguments})
            value = response.get("structuredContent")
            if value is None:
                text = next((item.get("text") for item in response.get("content", []) if item.get("type") == "text"), None)
                value = json.loads(text) if text else None
            require(not response.get("isError") and isinstance(value, dict) and value.get("ok") is True,
                    "HarnessToolFailed_" + name)
            record = {"tool": name, "ok": True, "seconds": round(time.monotonic() - started, 4)}
            for key in ("ready", "idempotent_replay", "termination_failed", "edit_ready"):
                if type(value.get(key)) is bool:
                    record[key] = value[key]
            if value.get("status") in TERMINAL | {"queued", "preparing", "running", "cancelling"}:
                record["status"] = value["status"]
            report["calls"].append(record)
            return value

        rpc("initialize", {"clientInfo": {"name": "harness_codex_edit_acceptance", "version": __import__("harness_mcp").__version__}})
        wire.send({"method": "initialized", "params": {}})
        thread_id = rpc("thread/start", {"ephemeral": True, "cwd": str(project), "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
        inventory = rpc("mcpServerStatus/list", {"threadId": thread_id, "serverName": SERVER, "detail": "toolsAndAuthOnly"})
        server = next((item for item in inventory.get("data", []) if item.get("name") == SERVER), {})
        report["tools"] = _tool_names(server.get("tools"))
        require(set(report["tools"]) == TOOLS and not server.get("toolsError"), "FourToolsUnavailable")
        info = server.get("serverInfo") or {}
        require(info.get("name") == "deepseek-harness-mcp" and isinstance(info.get("version"), str)
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9.-]+)?", info["version"]), "UnexpectedMcpServer")
        report["mcp_handshake_verified"] = True
        report["server_info"] = {"name": info["name"], "version": info["version"]}
        tools = server.get("tools", {})
        rows = tools.values() if isinstance(tools, dict) else tools
        submit = next((item for item in rows if item.get("name", "").endswith("harness_submit")), {})
        mode = submit.get("inputSchema", {}).get("properties", {}).get("mode", {})
        require(mode.get("enum") == ["review", "edit"] and mode.get("default") == "review", "LiveEditModeSchemaMissing")
        report["live_submit_mode_schema"] = {"enum": mode["enum"], "default": mode["default"]}
        submission = {"project_id": PROJECT, "prompt": PROMPT, "client_request_id": REQUEST_ID,
                      "relative_paths": ["calculator.py"], "timeout_seconds": TIMEOUT, "mode": "edit"}
        receipt = call("harness_submit", submission)
        job_id = receipt["job_id"]
        require(isinstance(job_id, str) and bool(re.fullmatch(r"[0-9a-f]{32}", job_id)), "InvalidJobId")
        report.update(job_id=job_id, first_submit_idempotent_replay=receipt.get("idempotent_replay") is True)
        deadline = time.monotonic() + TIMEOUT + 15
        while time.monotonic() < deadline:
            status = call("harness_status", {"job_id": job_id})
            if status["status"] in TERMINAL:
                terminal = True
                break
            time.sleep(1)
        else:
            raise CheckFailed("DesktopEditDeadlineExceeded")
        report["job_status"] = status["status"]
        require(status["status"] == "succeeded" and status.get("termination_failed") is False, "DesktopEditDidNotSucceed")
        identity, changes, patch, pages = _reconstruct(call, job_id)
        job_dir = config.data_dir / "jobs" / job_id
        payload = read_report(job_dir, expected_report_id=identity["report_id"])
        require(payload["changes"] == changes and payload["patch_text"] == patch and payload["source_applied"] is False,
                "ClientAndSavedChangesMismatch")
        manifest_bytes = (job_dir / "snapshot/manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        require(_sha(manifest_bytes) == identity["snapshot_id"] and len(manifest["manifest"]) == 1, "BaselineManifestHashMismatch")
        row = changes[0]
        baseline = (job_dir / "snapshot/files/calculator.py").read_bytes()
        require(baseline == before_bytes and row["before_sha256"] == _sha(baseline)
                and row["before_size_bytes"] == len(baseline)
                and manifest["manifest"][0]["sha256"] == _sha(baseline), "ImmutableBaselineMismatch")
        require((job_dir / "workspace/calculator.py").read_bytes() == row["after_text"].encode("utf-8"), "ActualWorkspaceAfterMismatch")
        behavior = _static_behavior(baseline.decode("utf-8"), row["after_text"])
        report["behavior_checks"] = behavior
        require(all(value is True for name, value in behavior.items() if name != "scope"), "StaticBehaviorCheckFailed")
        executions_before_replay = set(desktop_execution_records(job_dir))
        replay = call("harness_submit", submission)
        cancel = call("harness_cancel", {"job_id": job_id})
        final_status = call("harness_status", {"job_id": job_id})
        require(replay["job_id"] == job_id and replay["idempotent_replay"] is True, "IdempotentReplayFailed")
        require(cancel["status"] == final_status["status"] == "succeeded", "TerminalCancelChangedSucceededJob")
        execution_paths = set(desktop_execution_records(job_dir))
        require(execution_paths == executions_before_replay and len(execution_paths) == 1, "ReplayStartedAdditionalExecution")
        executions = [_read_json(path, 16384) for path in execution_paths]
        stopped = all(item.get("confirmed_stopped") is True and asyncio.run(named_job_stopped(item["job_name"])) for item in executions)
        homes_cleaned = not list(job_dir.glob("desktop_runs/*/home"))
        unchanged = before_files == _source_inventory(project)
        require(stopped and homes_cleaned and unchanged, "DesktopCleanupOrOriginalSourceCheckFailed")
        report.update(report_id=identity["report_id"], snapshot_id=identity["snapshot_id"], change_set_id=identity["change_set_id"],
                      changed_files=["calculator.py"], change_type="modify", before_sha256=row["before_sha256"], after_sha256=row["after_sha256"],
                      before_size_bytes=row["before_size_bytes"], after_size_bytes=row["after_size_bytes"], diff_chars=len(patch), result_pages=pages,
                      complete_diff_and_after_content_reconstructed=True, client_saved_report_digest_verified=True,
                      immutable_baseline_hash_verified=True, actual_workspace_hash_verified=True,
                      original_source_inventory_hash_unchanged=unchanged, source_hash_unchanged=before_files["calculator.py"] == _sha(source.read_bytes()),
                      report_tests="not_run", model_test_command_execution="not_independently_observable",
                      no_tests_evidence_scope="fixed_no_test_instruction_and_report_not_run_only",
                      desktop_execution_count=len(executions), desktop_model_execution_started_this_run=not receipt["idempotent_replay"],
                      owned_desktop_tree_stopped=stopped, dedicated_homes_cleaned=homes_cleaned,
                      idempotent_replay_verified=True, replay_additional_execution_count=0,
                      terminal_cancel_preserved_success=True, four_business_tools_called=set(item["tool"] for item in report["calls"]) == TOOLS,
                      status="passed")
    except Exception as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, CheckFailed):
            report["check_failed"] = str(error)
    finally:
        if wire is not None:
            if job_id is not None and not terminal:
                try:
                    call("harness_cancel", {"job_id": job_id})
                    report["failure_cancel_requested"] = True
                except Exception:
                    report["failure_cancel_requested"] = False
            if thread_id is not None:
                try:
                    report["ephemeral_context_unsubscribed"] = rpc("thread/unsubscribe", {"threadId": thread_id})["status"] == "unsubscribed"
                except Exception:
                    report["ephemeral_context_unsubscribed"] = False
                if not report["ephemeral_context_unsubscribed"]:
                    report["status"] = "failed"
            try:
                wire.close()
                report["client_control_closed"] = True
            except Exception:
                report.update(client_control_closed=False, status="failed")
        try:
            report["personal_config_hash_unchanged"] = personal_hash == _personal_config_hash()
        except Exception:
            report["personal_config_hash_unchanged"] = False
        if not report["personal_config_hash_unchanged"]:
            report["status"] = "failed"
    return report


async def supervise(args):
    from harness_mcp.config import load_config
    from harness_mcp.process_control import spawn_owned, stop_process_tree

    environment = {name: os.environ[name] for name in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME",
    ) if name in os.environ}
    # 如独立配置选择 env，只传递其固定引用；从不输出该值。
    config = load_config(args.config.resolve())
    if config.model.credential_source == "env" and config.model.credential_env in os.environ:
        environment[config.model.credential_env] = os.environ[config.model.credential_env]
    environment["PYTHONIOENCODING"] = "utf-8"
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", "--config", str(args.config.resolve()), "--probe-dir", str(args.probe_dir)]
    if args.cli:
        argv += ["--cli", args.cli]
    process = spawn_owned(argv, environment, max_output=2 * 1024 * 1024)
    communication = asyncio.create_task(asyncio.to_thread(process.communicate))
    report = {"status": "failed", "codex_model_turn_sent": False, "source_applied": False}
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=TIMEOUT + 150)
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="包含 edit_sample 的独立可信服务配置")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cli")
    parser.add_argument("--probe-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False))
        return
    args.probe_dir = (args.probe_dir or ROOT / "data/phase0" / ("edit-client-" + uuid.uuid4().hex)).resolve()
    print("将通过独立 Codex 连接验收桌面副本修改；固定编号，不自动合入或执行生成代码。", flush=True)
    try:
        report = asyncio.run(supervise(args))
    except Exception as error:
        report = {"status": "failed", "error_type": type(error).__name__, "source_applied": False}
    output = args.output or ROOT / "artifacts" / (
        "codex-edit-e2e-replay.json" if report.get("first_submit_idempotent_replay") else "codex-edit-e2e.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
