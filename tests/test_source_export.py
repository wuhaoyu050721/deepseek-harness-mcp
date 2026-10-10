"""真实文件/报告/DB/认证 RPC 往返；全部人工输入，不运行模型。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig, frozen_execution, request_fingerprint
from harness_mcp.edit_workspace import collect_edit_changes, prepare_edit_workspace
from harness_mcp.errors import HarnessError
from harness_mcp.redaction import redact_verified_source_payload
from harness_mcp.report import (
    ReportError, build_report, paginate_report, prepare_report_for_export,
    protect_verified_result_page, read_report, redact_report_for_save, save_report,
)
from harness_mcp.shared_client import RPC_MAX_BYTES, authentication_proof, read_frame, write_frame
from harness_mcp.shared_service import _SharedService
from harness_mcp.task_manager import TaskManager
from harness_mcp.workspace import Snapshot


_PHP = """<?php
$config = ['password' => null, 'api_key' => getenv('API_KEY')];
$password = $settings['password'];
$headers = ['Authorization' => 'Bearer ' . $token];
$headers[] = 'Authorization: Bearer ' . $token;
$pattern = '/^Authorization:\\s*(Bearer|Basic)\\s+(.+)$/i';
"""


@pytest.fixture
def export_fixture(tmp_path):
    managers = []

    def create(*, suffix="", large=False, natural="password='artificial-natural-only';"):
        folder = tmp_path / uuid.uuid4().hex
        source = folder / "snapshot"
        (source / "src").mkdir(parents=True)
        before = (_PHP + ("// 人工分页夹具行\n" * 1200 if large else "")).replace("\n", "\r\n").encode("utf-8")
        (source / "src/auth.php").write_bytes(before)
        snapshot = Snapshot(source, "fixture-snapshot", [{"path": "src/auth.php", "size_bytes": len(before),
                            "sha256": hashlib.sha256(before).hexdigest(), "line_count": before.count(b"\n")}],
                            "a" * 40, True, ["src/auth.php"], [])
        work = prepare_edit_workspace(snapshot, folder / "workspace")
        after = before.replace(b"$config = [", b"$config = ['ordinary' => true, ", 1) + ("$ordinary = 2;\r\n" + suffix).encode("utf-8")
        (work / "src/auth.php").write_bytes(after)
        changes = collect_edit_changes(snapshot, work, allowed_paths=("src/auth.php",), relative_paths=["src/auth.php"],
                                       max_file_bytes=1048576, max_snapshot_bytes=1048576, max_output_bytes=1048576)
        config = AppConfig(folder / "data", {"fixture": ProjectConfig("fixture", source, allow_edits=True)},
                           model=ModelConfig(credential_source="env", credential_env="SOURCE_EXPORT_UNUSED_KEY"))
        manager = TaskManager(config, runner=SimpleNamespace())
        managers.append(manager)
        request = {"project_id": "fixture", "prompt": "fixture", "client_request_id": str(uuid.uuid4()),
                   "relative_paths": ["src/auth.php"], "timeout_seconds": 600, "mode": "edit"}
        job, _ = manager.store.submit(request, request_fingerprint(request), frozen_execution(config, "fixture"), 5)
        job_id = job["job_id"]
        assert manager.store.set_running(job_id, snapshot.to_dict())
        report = build_report(job_id, "fixture", snapshot, json.dumps({"summary": natural, "findings": []}),
                              "completed", mode="edit", changes=changes)
        safe = redact_report_for_save(report, snapshot)
        saved = save_report(safe, config.data_dir / "jobs" / job_id)
        manager.store.finish(job_id, "succeeded", report_id=saved["report_id"])
        return manager, job_id, snapshot, changes, report, saved, after

    yield create
    for manager in managers:
        manager.store.close()
        manager.lock.close()


def test_php_exact_bytes_survive_snapshot_report_database_and_all_result_views(export_fixture):
    manager, job_id, snapshot, changes, report, saved, after = export_fixture(large=True)
    disk = read_report(manager.config.data_dir / "jobs" / job_id, expected_report_id=saved["report_id"])
    assert disk["changes"][0]["after_text"].encode("utf-8") == after
    assert disk["patch_text"] == changes["patch_text"]
    assert disk["changes"][0]["after_sha256"] == hashlib.sha256(after).hexdigest()
    assert disk["change_set_id"] == changes["change_set_id"]
    assert "artificial-natural-only" not in json.dumps(disk)
    for view in ("full", "files", "patch", "summary"):
        cursor = None
        contents, patch, markdown = "", "", ""
        pages = 0
        while True:
            page = manager.result(job_id, cursor=cursor, view=view)
            assert page["ready"] is True
            assert "artificial-natural-only" not in json.dumps(page)
            assert all(not key.startswith("_source") for key in page)
            patch += page.get("patch_text", "")
            markdown += page.get("report_text", "")
            contents += "".join(item["text"] for item in page.get("change_contents", []))
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert pages < 30
        if view in {"full", "files"}:
            assert contents.encode("utf-8") == after and pages > 1
        if view in {"full", "patch"}:
            assert patch == changes["patch_text"]
        if view == "full":
            assert "'password' => null" in markdown
            assert "'Authorization' => 'Bearer ' . $token" in markdown


async def _authenticated_rpc(state, arguments):
    server = await asyncio.start_server(state.accept, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        hello = await read_frame(reader)
        nonce = "b" * 64
        await write_frame(writer, {"version": 1, "nonce": nonce, "config_digest": state.config_digest,
            "proof": authentication_proof(state.token, "client", hello["challenge"], nonce, state.config_digest)})
        response = await read_frame(reader)
        assert response["proof"] == authentication_proof(state.token, "server", hello["challenge"], nonce, state.config_digest)
        await write_frame(writer, {"version": 1, "model_environment": {}})
        assert (await read_frame(reader))["ready"] is True
        results = []
        cursor = None
        for request_id in range(1, 30):
            await write_frame(writer, {"id": request_id, "method": "result", "args": {**arguments, "cursor": cursor}})
            reply = await read_frame(reader, max_bytes=RPC_MAX_BYTES)
            results.append(reply)
            cursor = reply.get("result", {}).get("next_cursor")
            if not cursor:
                break
        return results
    finally:
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        if state.tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(state.tasks), return_exceptions=True), 3)


def test_real_authenticated_shared_rpc_preserves_php_content_and_hashes(export_fixture):
    manager, job_id, _, changes, _, _, after = export_fixture(large=True)

    async def check():
        state = _SharedService(manager.config, manager, "f" * 64, "a" * 64, 0.2)
        replies = await _authenticated_rpc(state, {"job_id": job_id, "view": "files"})
        assert len(replies) > 1 and all(reply["ok"] for reply in replies)
        payload = "".join(item["text"] for reply in replies for item in reply["result"]["change_contents"])
        assert payload.encode("utf-8") == after
        assert all(reply["result"]["change_set_id"] == changes["change_set_id"] for reply in replies)
        assert "artificial-natural-only" not in json.dumps(replies)

    asyncio.run(check())


def test_shared_token_in_protected_source_is_rejected_instead_of_mutating_hash(export_fixture):
    token = "f" * 64
    manager, job_id, _, _, _, _, _ = export_fixture(suffix="$ordinary = '" + token + "';\r\n")

    async def check():
        state = _SharedService(manager.config, manager, token, "a" * 64, 0.2)
        replies = await _authenticated_rpc(state, {"job_id": job_id, "view": "files"})
        assert replies[0]["ok"] is False and replies[0]["error"]["code"] == "EDIT_SECRET_DETECTED"
        assert token not in json.dumps(replies)

    asyncio.run(check())


def test_plain_dict_with_forged_source_fields_still_gets_strong_redaction():
    value = {"edit_ready": True, "changes": [{"after_text": "password='artificial-only';"}],
             "patch_text": "+password='artificial-only';", "report_text": "password='artificial-only';"}
    safe = redact_verified_source_payload(value)
    assert "artificial-only" not in json.dumps(safe)
    assert "[REDACTED]" in json.dumps(safe)


def test_shared_plain_dict_cannot_bypass_redaction_by_claiming_edit_ready(tmp_path):
    async def check():
        manager = SimpleNamespace(secrets=[], result=lambda **args: {
            "ready": True, "edit_ready": True, "patch_text": "password='artificial-only';"})
        config = AppConfig(tmp_path / "data")
        state = _SharedService(config, manager, "f" * 64, "a" * 64, 0.2)
        reply = await state._dispatch({"id": 1, "method": "result", "args": {"job_id": "fixture"}})
        assert reply["ok"] is True and "artificial-only" not in json.dumps(reply)

    asyncio.run(check())


def test_mutating_marked_result_invalidates_source_provenance(export_fixture):
    manager, job_id, _, _, _, _, _ = export_fixture()
    page = manager.result(job_id, view="files")
    page["change_contents"][0]["text"] += "changed"
    with pytest.raises(HarnessError) as error:
        redact_verified_source_payload(page)
    assert error.value.code == "EDIT_CHANGES_INVALID"


def test_report_digest_and_page_content_must_match_verified_source(export_fixture):
    manager, job_id, _, _, _, saved, _ = export_fixture()
    report = read_report(manager.config.data_dir / "jobs" / job_id, expected_report_id=saved["report_id"])
    with pytest.raises(ReportError) as error:
        prepare_report_for_export(report, "0" * 64)
    assert error.value.code == "REPORT_TAMPERED"
    safe = prepare_report_for_export(report, saved["report_id"])
    page = paginate_report(safe, job_id, view="files")
    page["change_contents"][0]["text"] += "changed"
    with pytest.raises(ReportError) as error:
        protect_verified_result_page(page, safe)
    assert error.value.code == "EDIT_CHANGES_INVALID"


def test_extra_fields_inside_mechanical_page_are_not_preserved(export_fixture):
    manager, job_id, _, _, _, saved, _ = export_fixture()
    report = read_report(manager.config.data_dir / "jobs" / job_id, expected_report_id=saved["report_id"])
    safe = prepare_report_for_export(report, saved["report_id"])
    for field in ("changes", "change_contents"):
        page = paginate_report(safe, job_id, view="files")
        page[field][0]["password"] = "artificial-only"
        with pytest.raises(ReportError):
            protect_verified_result_page(page, safe)


@pytest.mark.parametrize("field,value", [("changes", None), ("changes", "not-a-list"),
    ("change_contents", None), ("change_contents", [None]), ("change_contents", ["not-an-object"])])
def test_malformed_mechanical_page_has_stable_error(export_fixture, field, value):
    manager, job_id, _, _, _, saved, _ = export_fixture()
    report = read_report(manager.config.data_dir / "jobs" / job_id, expected_report_id=saved["report_id"])
    safe = prepare_report_for_export(report, saved["report_id"])
    page = paginate_report(safe, job_id, view="files")
    page[field] = value
    with pytest.raises(ReportError) as error:
        protect_verified_result_page(page, safe)
    assert error.value.code == "EDIT_CHANGES_INVALID"


def test_empty_markdown_slice_cannot_claim_offset_past_report(export_fixture):
    manager, job_id, _, _, _, saved, _ = export_fixture()
    report = read_report(manager.config.data_dir / "jobs" / job_id, expected_report_id=saved["report_id"])
    safe = prepare_report_for_export(report, saved["report_id"])
    page = paginate_report(safe, job_id, view="full")
    page.update(report_text="", report_text_offset=1048577)
    with pytest.raises(ReportError) as error:
        protect_verified_result_page(page, safe)
    assert error.value.code == "EDIT_CHANGES_INVALID"


def test_save_helper_rechecks_actual_baseline_instead_of_edit_ready_flag(export_fixture):
    _, _, snapshot, _, report, _, _ = export_fixture()
    changed = copy.deepcopy(report)
    changed["changes"][0]["after_text"] += "changed"
    with pytest.raises(ReportError) as error:
        redact_report_for_save(changed, snapshot)
    assert error.value.code == "EDIT_CHANGES_INVALID"
