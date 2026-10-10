"""聚合本地检查和单独记录的真实桌面验收；不自动提交付费任务。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], name: str, timeout=90) -> int:
    logs = ROOT / "artifacts/local"
    logs.mkdir(parents=True,exist_ok=True)
    with (logs / f"{name}.txt").open("wb") as stream:
        try:
            process = subprocess.run(arguments,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127
    return process.returncode


def read_record(name: str) -> dict:
    path = ROOT / "artifacts" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def security_regressions(suites) -> dict:
    required = {
        "SEC-01": ["test_snapshot_does_not_execute_real_clean_filter", "test_outside_scope_filter_and_source_are_not_read", "test_missing_promisor_tree_fails_without_remote_helper"],
        "SEC-02": ["test_auth_labels_share_full_quoted_value_redaction", "test_quoted_json_secret_never_exports_hash_bound_patch", "test_wire_escaped_auth_value_is_removed_before_degraded_save", "test_dangling_quote_escape_cannot_leave_auth_in_degraded_output"],
        "SEC-03": ["test_sensitive_keys_and_long_values_are_checked_before_limits", "test_worker_contract_rejects_untrusted_error_keys_and_field_types"],
        "SEC-04": ["test_owner_crash_after_atomic_creation_before_resume_has_no_orphan", "test_attribute_cleanup_interrupt_closes_untransferred_native_handles", "test_fresh_host_runs_snapshot_and_edit_collection_with_real_manager_commands"],
        "SEC-05": ["test_more_than_10000_confirmed_history_records_cannot_block_recovery", "test_confirmed_history_above_10000_does_not_block_desktop_recovery", "test_no_pending_database_cannot_ignore_unconfirmed_desktop_history"],
        "source_hardlink_hardening": ["test_source_hardlink_is_refused", "test_source_link_count_change_while_reading_is_refused"],
        "mechanical_patch_integrity_hardening": ["test_edit_rejects_self_consistent_patch_that_disagrees_with_verified_bytes"],
    }
    passed = {case.attrib.get("name", "").split("[", 1)[0]
              for suite in suites for case in suite.findall("testcase")
              if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))}
    return {finding: {"status": "passed" if all(name in passed for name in names) else "unverified",
                      "required_regressions": names, "missing_or_failed": [name for name in names if name not in passed]}
            for finding, names in required.items()}


def scan_deliverables() -> dict:
    """只扫版本控制候选文件；个人 store 仅在内存提取固定 API key/token 供匹配。"""
    import yaml
    secrets = []
    store = Path.home() / ".dsh/.credentials.yaml"
    if store.is_file() and store.stat().st_size <= 1024*1024:
        try:
            values = yaml.safe_load(store.read_text(encoding="utf-8")) or {}
            secrets = [values.get("refs",{}).get("DEEPSEEK_API_KEY"),values.get("records",{}).get("deepseek-account-platform/default",{}).get("payload",{}).get("token")]
        except Exception:
            pass
    patterns = [value.encode() for value in secrets if isinstance(value,str) and len(value)>8]
    listing = subprocess.run(["git","ls-files","-co","--exclude-standard","-z"],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True).stdout
    checked, leaks = 0, []
    for raw in listing.split(b"\x00"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        path = ROOT / relative
        if not path.is_file():
            continue
        content = path.read_bytes()
        checked += 1
        found = any(pattern in content for pattern in patterns)
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as wheel:
                found = found or any(any(pattern in wheel.read(name) for pattern in patterns) for name in wheel.namelist())
        if found:
            leaks.append(relative)
    return {"scope":"publishable_git_candidates_only","fixed_personal_credential_patterns_loaded":len(patterns),"files_checked":checked,"known_credential_values_found":bool(leaks),"affected_files":leaks,"does_not_prove_all_possible_secrets_absent":True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT / "artifacts/mvp-validation.json")
    parser.add_argument("--offline-build", action="store_true", help="使用已缓存的固定构建依赖，不访问包索引")
    parser.add_argument("--original-plan", type=Path, help="可选：与本地保留的原始计划核对字节，不发布计划内容")
    args = parser.parse_args()
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    (ROOT / "artifacts").mkdir(exist_ok=True)
    checks = {}
    checks["compile"] = run([sys.executable,"-m","compileall","-q","src","scripts","tests"],"compile")
    checks["pytest"] = run([sys.executable,"-m","pytest","-q","--junitxml=artifacts/test-results.xml"],"pytest",180)
    checks["mcp_protocol"] = run([sys.executable,"scripts/verify_mvp.py","--output","artifacts/mvp-protocol.json"],"mcp-protocol",75)
    checks["client_discovery"] = run([sys.executable,"scripts/verify_clients.py","--output","artifacts/client-discovery.json"],"client-discovery",75)
    checks["doctor"] = run([sys.executable,"-m","harness_mcp.doctor","--config","config/codex.local.toml","--output","artifacts/mvp-environment.json"],"doctor")
    checks["wheel"] = run(["uv","build","--wheel", *(["--offline"] if args.offline_build else []), "--out-dir","artifacts/dist"],"wheel")
    results_path = ROOT / "artifacts/test-results.xml"
    suites = list(ET.parse(results_path).getroot().iter("testsuite")) if results_path.is_file() else []
    test_counts = {key:sum(int(suite.attrib.get(key,0)) for suite in suites) for key in ("tests","failures","errors","skipped")}
    protocol, environment, handshake = read_record("mvp-protocol.json"),read_record("mvp-environment.json"),read_record("desktop-sdk-handshake.json")
    desktop = read_record("desktop-mvp.json")
    codex = read_record("codex-e2e.json")
    codex_edit = read_record("codex-edit-e2e.json")
    security_edit = read_record("security-fix-codex-edit.json")
    release = __import__("harness_mcp").__version__
    current_edit_verified = security_edit.get("status") == "passed" and security_edit.get("server_info", {}).get("version") == release
    edit_apply = read_record("edit-apply.json")
    clients = read_record("client-discovery.json")
    native_results = ROOT / "artifacts/desktop-lifecycle-test-results.xml"
    native_suites = list(ET.parse(native_results).getroot().iter("testsuite")) if native_results.is_file() else []
    native_counts = {key:sum(int(suite.attrib.get(key,0)) for suite in native_suites) for key in ("tests","failures","errors","skipped")}
    leak_scan = scan_deliverables()
    original = args.original_plan
    retained = ROOT / "docs/original-mvp-plan.md"
    original_unchanged = original.read_bytes() == retained.read_bytes() if original is not None and original.is_file() and retained.is_file() else None
    wheels = [{"path":path.relative_to(ROOT).as_posix(),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted((ROOT / "artifacts/dist").glob("*.whl"))]
    source_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((ROOT / "src/harness_mcp").glob("*.py"))}
    current_wheel = ROOT / "artifacts/dist" / f"deepseek_harness_mcp-{release}-py3-none-any.whl"
    wheel_matches_source = False
    if checks["wheel"] == 0 and current_wheel.is_file():
        with zipfile.ZipFile(current_wheel) as archive:
            wheel_matches_source = all(archive.read(path.removeprefix("src/")) == (ROOT / path).read_bytes()
                                       for path in source_hashes)
    regressions = security_regressions(suites)
    passed = all(value==0 for value in checks.values()) and wheel_matches_source and not leak_scan["known_credential_values_found"] and protocol.get("status")=="passed" and not test_counts["failures"] and not test_counts["errors"]
    if release == "0.3.1":
        passed = passed and all(item["status"] == "passed" for item in regressions.values())
    record = {
        "schema_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"service_version":__import__("harness_mcp").__version__,
        "scope":"windows_local_implementation_and_mcp_protocol","status":"passed" if passed else "failed",
        "check_exit_codes":checks,"pytest":test_counts,"mcp_protocol_status":protocol.get("status"),
        "offline_wheel_build": args.offline_build,
        "source_sha256": source_hashes, "current_wheel_source_byte_match": wheel_matches_source,
        "mcp_metadata_max_seconds":max((item["seconds"] for item in protocol.get("sdk",{}).get("metadata_calls",[])),default=None),
        "stdout_jsonrpc_only":protocol.get("wire",{}).get("stdout_jsonrpc_only"),
        "stdin_eof_exit_code":protocol.get("wire",{}).get("stdin_eof_exit_code"),
        "installed_desktop_sdk_initialize_verified":handshake.get("sdk_handshake_verified"),
        "desktop_probe_process_tree_stopped":handshake.get("process_tree_stopped"),
        "configuration_valid":environment.get("configuration",{}).get("valid"),
        "credential_reference_available":environment.get("configuration",{}).get("credential_reference_available"),
        "credential_value_scan":leak_scan,"original_plan_byte_identical":original_unchanged,"wheels":wheels,
        "real_docker_build":"not_run","real_container_isolation":"not_run",
        "execution_backend":environment.get("execution_backend"),
        "real_harness_model_call":security_edit.get("status", "not_run") if release == "0.3.1" else desktop.get("status","not_run"),
        "security_regressions": regressions,
        "current_release_codex_edit_acceptance": security_edit,
        "current_release_codex_edit_verified": current_edit_verified,
        "historical_model_evidence": {"scope": "prior_releases_not_current_source_acceptance", "review": desktop, "codex_review": codex, "codex_edit": codex_edit, "main_agent_apply": edit_apply},
        "evidence_scope_notes": "旧版审查、GUI重启及主AI合入记录保留为历史；本版独立Codex新连接与桌面真实修改另由security-fix-codex-edit.json记录，不证明GUI热加载。",
        "desktop_acceptance_scope":desktop.get("scope"),
        "desktop_acceptance_job_id":desktop.get("job_id"),
        "desktop_citations_verified":desktop.get("citations_independently_verified"),
        "desktop_source_hash_unchanged":desktop.get("source_hash_unchanged"),
        "desktop_process_tree_stopped":desktop.get("owned_process_tree_stopped"),
        "desktop_dedicated_homes_cleaned":desktop.get("dedicated_homes_cleaned"),
        "native_lifecycle_fixture": {"scope":"real_windows_owned_python_worker_and_process_families_without_model", "pytest":native_counts},
        "installed_client_discovery":clients,
        "personal_client_registration":read_record("client-registration.json"),
        "authorized_mcp_project_snapshot":read_record("mcp-project-scope.json"),
        "authorized_mcp_edit_project_snapshot":read_record("mcp-edit-project-scope.json"),
        "real_codex_client_acceptance":codex.get("status","not_run"),
        "codex_acceptance":codex,"real_claude_client_acceptance":"not_run",
        "codex_current_conversation_acceptance":read_record("codex-conversation.json"),
        "codex_edit_acceptance":codex_edit,
        "main_agent_edit_apply_acceptance":edit_apply,
        "edit_mode_real_client_verified": current_edit_verified,
        "edit_mode_main_agent_apply_verified": edit_apply.get("status")=="passed" and release == "0.3.0",
        "edit_mode_usable_after_client_reload":passed and current_edit_verified,
        "edit_current_conversation_state":read_record("codex-edit-conversation-state.json"),
        "linux_or_macos_runtime":"not_run","execution_ready":environment.get("execution_ready",False),
        "desktop_model_acceptance_passed":current_edit_verified, "full_mvp_acceptance_passed":False,
        "desktop_switch_usable":passed and environment.get("execution_ready",False) and current_edit_verified and clients.get("status")=="passed",
        "remaining_blocker":"可选 Linux 容器及 Claude 完整模型流程未验收；Codex 完整业务调用及重启后当前会话连接按独立证据记录。",
    }
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":record["status"],"pytest":test_counts,"execution_ready":record["execution_ready"],"desktop_model_acceptance_passed":record["desktop_model_acceptance_passed"],"output":str(args.output)},ensure_ascii=False))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
