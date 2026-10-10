from __future__ import annotations

import hashlib
import json
import base64
import copy
import difflib
from pathlib import Path

import pytest

from harness_mcp.report import ReportError, build_report, paginate_report, read_report, render_markdown, save_report
from harness_mcp.workspace import Snapshot


@pytest.fixture
def snapshot(tmp_path: Path) -> Snapshot:
    source = tmp_path / "files"
    source.mkdir()
    content = b"def add(a, b):\n    return a - b\n"
    (source / "main.py").write_bytes(content)
    return Snapshot(source, "snapshot-id", [{"path": "main.py", "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "line_count": 2}], "a" * 40, True, ["main.py"], [])


def model_report(**kwargs):
    raw = {"summary": "发现减法与函数名不符。", "findings": [{"severity": "medium", "title": "返回减法结果", "path": "main.py", "line_start": 2, "line_end": 2, "evidence": "return a - b", "trigger": "调用 add(2, 1)", "recommendation": "确认业务要求后修正算符", "uncertainty": "可能是有意的业务逻辑"}], "limitations": [], "tests": "passed", "changed_files": ["main.py"]}
    raw.update(kwargs)
    return json.dumps(raw, ensure_ascii=False)


@pytest.mark.parametrize("field", ["authorization", "api_key", "api-key", "access_token", "access-token", "password", "secret"])
@pytest.mark.parametrize("quotes", ["double", "single", "none"])
def test_auth_labels_share_full_quoted_value_redaction(field, quotes):
    from harness_mcp.redaction import redact_text as diagnostic
    from harness_mcp.report import redact_text

    if quotes == "double":
        text = json.dumps({field: 'opaque value with "escaped quote" and tail'})
    elif quotes == "single":
        text = "{'" + field + "': 'opaque value with \\'escaped quote\\' and tail'}"
    else:
        text = field + "=opaque-value-tail; ordinary=keep"
    for redact in (redact_text, diagnostic):
        masked = redact(text)
        assert "opaque" not in masked and "tail" not in masked
        assert "[REDACTED]" in masked
        assert redact(masked) == masked


def test_sensitive_keys_and_long_values_are_checked_before_limits(snapshot, tmp_path):
    from harness_mcp.redaction import redact_value
    from harness_mcp.report import _safe_value

    secret = "exact-fake-auth-value"
    key = "prefix-" + "k" * 150 + secret
    value = "v" * 3995 + secret
    nested = {"ordinary": ({secret: 1, "keep": secret},), key: "discard", "password": "discard"}
    masked = redact_value(nested, (secret,))
    assert masked == {"ordinary": ({"keep": "[REDACTED]"},)}
    safe = _safe_value({key: 1, "ordinary": value, "count": 7}, secrets=(secret,))
    assert key[:100] not in safe and safe["count"] == 7
    assert secret[:5] not in safe["ordinary"]
    report = build_report("job-1", "demo", snapshot, model_report(), "stop", secrets=(secret,),
                          usage={"nested": nested}, timing={"nested": nested}, error={"message": value, key: 1})
    saved = save_report(report, tmp_path / "reports")
    assert read_report(tmp_path / "reports", expected_report_id=saved["report_id"]) == report
    for path in (tmp_path / "reports").iterdir():
        content = path.read_text(encoding="utf-8")
        assert secret not in content and key[:100] not in content and secret[:5] not in content


@pytest.mark.parametrize("form", ["valid", "invalid", "truncated"])
def test_wire_escaped_auth_value_is_removed_before_degraded_save(snapshot, tmp_path, form):
    from harness_mcp.redaction import redact_value

    inner = json.dumps({"password": 'fixture opaque with "embedded quote" and tail'})
    wire = model_report(summary=inner)
    if form == "invalid":
        wire = wire[:-1]
    elif form == "truncated":
        wire += "padding" * 100
    outcome = redact_value({"raw_text": wire})
    report = build_report("job-1", "demo", snapshot, outcome["raw_text"], "completed", max_output_bytes=500)
    save_report(report, tmp_path / "reports")
    assert report["report_format"] == ("valid" if form == "valid" else "degraded" if form == "invalid" else "truncated")
    for path in (tmp_path / "reports").iterdir():
        text = path.read_text(encoding="utf-8")
        assert "fixture opaque" not in text and "embedded quote" not in text and "tail" not in text


@pytest.mark.parametrize("quote", ['"', "'"])
def test_dangling_quote_escape_cannot_leave_auth_in_degraded_output(snapshot, tmp_path, quote):
    from harness_mcp.redaction import redact_value
    wire = "{" + quote + "password" + quote + ": " + quote + "fixture opaque tail" + "\\"
    outcome = redact_value({"raw_text": wire})
    report = build_report("job-1", "demo", snapshot, outcome["raw_text"], "completed")
    save_report(report, tmp_path / "reports")
    assert report["report_format"] == "degraded"
    for path in (tmp_path / "reports").iterdir():
        text = path.read_text(encoding="utf-8")
        assert "fixture opaque" not in text and "tail" not in text


@pytest.mark.parametrize("operation", ["add", "modify", "delete"])
def test_quoted_json_secret_never_exports_hash_bound_patch(snapshot, tmp_path, operation):
    from harness_mcp.edit_workspace import EditWorkspaceError, collect_edit_changes, prepare_edit_workspace

    sensitive = json.dumps({"password": 'opaque value with "quote" tail'}) + "\n"
    snapshot.review_scope[:] = ["."]
    path = "new.txt" if operation == "add" else "main.py"
    if operation == "delete":
        data = sensitive.encode("utf-8")
        (snapshot.source_dir / path).write_bytes(data)
        snapshot.manifest[0].update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), line_count=1)
    after = None if operation == "delete" else sensitive
    changes = mechanical_changes(snapshot, {path: after})
    original = copy.deepcopy(changes)
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(findings=[]), "completed", mode="edit", changes=changes)
    assert exc.value.code == "EDIT_SECRET_DETECTED" and changes == original
    assert "opaque" not in exc.value.message
    workspace = prepare_edit_workspace(snapshot, tmp_path / "edit")
    if after is None:
        (workspace / path).unlink()
    else:
        (workspace / path).write_bytes(after.encode("utf-8"))
    with pytest.raises(EditWorkspaceError) as exc:
        collect_edit_changes(snapshot, workspace, allowed_paths=(".",), relative_paths=None,
                             max_file_bytes=1048576, max_snapshot_bytes=1048576, max_output_bytes=1048576)
    assert exc.value.code == "EDIT_SECRET_DETECTED" and "opaque" not in exc.value.message


def test_valid_citation_and_model_test_or_change_claims_not_trusted(snapshot):
    report = build_report("job-1", "demo", snapshot, model_report(), "stop")
    assert report["report_format"] == "valid"
    assert report["findings"][0]["validation"] == {"status": "valid", "errors": []}
    assert report["tests"] == "not_run"
    assert report["changed_files"] == []
    assert report["usage"] is None
    assert report["finish_reason"] == "stop"


@pytest.mark.parametrize("updates, expected", [({"path": "../outside.py"}, "invalid_relative_path"), ({"path": "unread.py"}, "path_not_in_snapshot"), ({"line_start": 99, "line_end": 99}, "line_out_of_range"), ({"evidence": "not present"}, "evidence_not_in_referenced_lines"), ({"line_start": True}, "invalid_line_start")])
def test_finding_validation_uses_only_snapshot(snapshot, updates, expected):
    raw = json.loads(model_report())
    raw["findings"][0].update(updates)
    report = build_report("job-1", "demo", snapshot, json.dumps(raw), "stop")
    finding = report["findings"][0]
    assert finding["validation"]["status"] == "invalid"
    assert expected in finding["validation"]["errors"]


def test_snapshot_tampering_fails_report_instead_of_validating_new_file(snapshot):
    (snapshot.source_dir / "main.py").write_text("totally different\n")
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "stop")
    assert exc.value.code == "SNAPSHOT_CHANGED"


@pytest.mark.parametrize("raw", ["not JSON", "[]", '{"summary":"missing findings"}', ""])
def test_invalid_json_or_schema_degrades_without_fabricating_findings(snapshot, raw):
    report = build_report("job-1", "demo", snapshot, raw, "stop")
    assert report["report_format"] == "degraded"
    assert report["findings"] == []
    assert report["raw_text"] == raw
    assert report["finish_reason"] == "stop"


def test_output_is_bounded_by_utf8_bytes_and_actual_finish_reason_preserved(snapshot):
    report = build_report("job-1", "demo", snapshot, "中文" * 100, "max_tokens", max_output_bytes=100)
    assert report["report_format"] == "truncated"
    assert len(report["raw_text"].encode("utf-8")) <= 100
    assert report["finish_reason"] == "max_tokens"
    assert report["usage"] is None


def test_fenced_json_and_real_usage_supported(snapshot):
    report = build_report("job-1", "demo", snapshot, "```json\n" + model_report() + "\n```", "stop", usage={"input_tokens": 123, "output_tokens": 50}, timing={"duration_seconds": 0.25})
    assert report["report_format"] == "valid"
    assert report["usage"]["input_tokens"] == 123
    assert report["timing"]["duration_seconds"] == 0.25


def test_atomic_report_round_trip_and_plaintext_secret_redaction(snapshot, tmp_path):
    secret = "local-credential-exact-value"
    report = build_report("job-1", "demo", snapshot, "raw " + secret + " sk-" + "x" * 40, "stop", error={"message": "password=abc"}, secrets=(secret,))
    directory = tmp_path / "reports"
    saved = save_report(report, directory)
    assert len(saved["report_id"]) == 64
    assert read_report(directory, expected_report_id=saved["report_id"]) == report
    assert not list(directory.glob(".report-*.tmp"))
    text = (directory / "report.json").read_text(encoding="utf-8")
    assert secret not in text and "x" * 40 not in text and "password=abc" not in text
    assert (directory / "report.md").exists()
    with pytest.raises(ReportError) as exc:
        read_report(directory, max_bytes=10)
    assert exc.value.code == "REPORT_READ_FAILED"


def test_pagination_cursor_is_bound_to_job_digest_and_page_size(snapshot):
    raw = json.loads(model_report())
    raw["findings"] *= 5
    report = build_report("job-1", "demo", snapshot, json.dumps(raw), "stop")
    page = paginate_report(report, "job-1", page_size=2)
    assert len(page["findings"]) == 2
    assert page["next_cursor"]
    second = paginate_report(report, "job-1", page["next_cursor"], 2)
    assert len(second["findings"]) == 2
    with pytest.raises(ReportError):
        paginate_report(report, "job-2", page["next_cursor"], 2)
    with pytest.raises(ReportError):
        paginate_report(report, "job-1", page["next_cursor"], 3)
    report["summary"] += "changed"
    with pytest.raises(ReportError) as exc:
        paginate_report(report, "job-1", page["next_cursor"], 2)
    assert exc.value.code == "INVALID_CURSOR"


def test_degraded_raw_report_can_be_fully_read_with_pages(snapshot):
    raw = "报告正文" * 6000
    report = build_report("job-1", "demo", snapshot, raw, "stop")
    chunks = []
    cursor = None
    while True:
        page = paginate_report(report, "job-1", cursor)
        chunks.append(page["report_text"])
        assert len(page["report_text"]) <= 8000
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert raw in "".join(chunks)


@pytest.mark.parametrize("cursor", ["not-base64!", "W10=", "e30=", "x" * 3000])
def test_invalid_cursors_rejected(snapshot, cursor):
    report = build_report("job-1", "demo", snapshot, model_report(), "stop")
    with pytest.raises(ReportError) as exc:
        paginate_report(report, "job-1", cursor)
    assert exc.value.code == "INVALID_CURSOR"


def test_malformed_severity_is_marked_invalid_instead_of_crashing(snapshot):
    raw = json.loads(model_report())
    raw["findings"][0]["severity"] = ["high"]
    report = build_report("job-1", "demo", snapshot, json.dumps(raw), "stop")
    assert "unknown_severity" in report["findings"][0]["validation"]["errors"]


def test_saved_report_raw_bytes_must_match_database_digest(snapshot, tmp_path):
    report = build_report("job-1", "demo", snapshot, model_report(), "stop")
    directory = tmp_path / "reports"
    saved = save_report(report, directory)
    path = directory / "report.json"
    path.write_bytes(path.read_bytes() + b"\n")
    # 即使解析结果相同，持久化字节被改动也不能通过数据库摘要核验。
    assert read_report(directory) == report
    with pytest.raises(ReportError) as exc:
        read_report(directory, expected_report_id=saved["report_id"])
    assert exc.value.code == "REPORT_TAMPERED"


def test_redaction_expansion_does_not_exceed_raw_output_save_limit(snapshot):
    report = build_report("job-1", "demo", snapshot, "password=x " * 200, "stop", max_output_bytes=200)
    assert len(report["raw_text"].encode("utf-8")) <= 200
    assert "password=x" not in report["raw_text"]


def mechanical_changes(snapshot, updates):
    """由基线和实际新文本生成变更，模型报告不参与这一输入。"""
    baseline = {row["path"]: row for row in snapshot.manifest}
    rows = []
    for path, after in sorted(updates.items()):
        before = (snapshot.source_dir / path).read_text(encoding="utf-8") if path in baseline else None
        before_bytes = before.encode("utf-8") if before is not None else b""
        after_bytes = after.encode("utf-8") if after is not None else b""
        diff_lines = difflib.unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
                                         fromfile="a/" + path if before is not None else "/dev/null",
                                         tofile="b/" + path if after is not None else "/dev/null")
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff_lines)
        rows.append({"path": path, "change_type": "add" if before is None else "delete" if after is None else "modify",
                     "before_sha256": hashlib.sha256(before_bytes).hexdigest() if before is not None else None,
                     "after_sha256": hashlib.sha256(after_bytes).hexdigest() if after is not None else None,
                     "before_size_bytes": len(before_bytes), "after_size_bytes": len(after_bytes),
                     "diff": diff, "after_text": after})
    digest = hashlib.sha256(json.dumps({"snapshot_id": snapshot.snapshot_id, "changes": rows},
                                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": 1, "snapshot_id": snapshot.snapshot_id, "changes": rows,
            "patch_text": "".join(row["diff"] for row in rows), "workspace_dir": "independent/workspace",
            "change_set_id": digest, "source_applied": False}


def test_edit_report_uses_mechanical_changes_and_immutable_citations(snapshot, tmp_path):
    changes = mechanical_changes(snapshot, {"main.py": "def add(a, b):\n    return a + b\n", "new.txt": "新文件\n"})
    report = build_report("job-1", "demo", snapshot, model_report(changed_files=["invented.py"],
                          changes=[{"path": "invented.py"}], patch_text="invented patch", source_applied=True,
                          tests="passed", edit_ready=False), "completed", mode="edit", changes=changes)
    assert report["mode"] == "edit" and report["edit_ready"] is True
    assert report["changed_files"] == ["main.py", "new.txt"]
    assert report["changes"] == changes["changes"]
    assert report["patch_text"] == changes["patch_text"] and "+    return a + b" in report["patch_text"]
    assert report["change_set_id"] == changes["change_set_id"]
    assert report["source_applied"] is False and report["requires_main_agent_review"] is True
    assert report["tests"] == "not_run" and report["findings"][0]["validation"]["status"] == "valid"
    assert (snapshot.source_dir / "main.py").read_text() == "def add(a, b):\n    return a - b\n"
    assert not (snapshot.source_dir / "new.txt").exists()
    saved = save_report(report, tmp_path / "reports")
    assert read_report(tmp_path / "reports", expected_report_id=saved["report_id"]) == report
    markdown = render_markdown(report)
    assert "机械变更" in markdown and changes["patch_text"] in markdown


def test_actual_workspace_collection_round_trips_through_report(snapshot, tmp_path):
    from harness_mcp.edit_workspace import collect_edit_changes, prepare_edit_workspace

    workspace = prepare_edit_workspace(snapshot, tmp_path / "edit")
    actual = b"def add(a, b):\n    return a + b\n"
    (workspace / "main.py").write_bytes(actual)
    changes = collect_edit_changes(snapshot, workspace, allowed_paths=("main.py",), relative_paths=["main.py"],
                                   max_file_bytes=1048576, max_snapshot_bytes=1048576, max_output_bytes=1048576)
    report = build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    row = report["changes"][0]
    assert row["after_text"].encode("utf-8") == actual
    assert row["after_sha256"] == hashlib.sha256(actual).hexdigest()
    assert report["patch_text"] == changes["patch_text"]
    assert report["change_set_id"] == changes["change_set_id"]


@pytest.mark.parametrize("updates", [{"main.py": None}, {"empty.txt": ""}, {"new.txt": "no trailing LF"}])
def test_delete_empty_add_and_no_final_newline_are_preserved(snapshot, updates):
    changes = mechanical_changes(snapshot, updates)
    report = build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    assert report["edit_ready"] is True and report["changes"] == changes["changes"]
    assert report["patch_text"] == changes["patch_text"]
    if "new.txt" in updates:
        assert "\\ No newline at end of file" in report["patch_text"]


@pytest.mark.parametrize("finish,error", [("completed", {"code": "CANCELLED"}), ("max_tokens", None), (None, None), ("error", {"code": "TIMED_OUT"})])
def test_incomplete_edit_does_not_export_partial_changes(snapshot, finish, error):
    changes = mechanical_changes(snapshot, {"main.py": "partly changed\n"})
    report = build_report("job-1", "demo", snapshot, model_report(edit_ready=True), finish,
                          error=error, mode="edit", changes=changes)
    assert report["edit_ready"] is False and report["changed_files"] == []
    assert report["changes"] == [] and report["patch_text"] == "" and report["change_set_id"] is None
    assert report["requires_main_agent_review"] is True and report["source_applied"] is False
    page = paginate_report(report, "job-1")
    assert page["edit_ready"] is False and page["changes"] == [] and page["patch_text"] == ""


def test_edit_without_mechanical_data_ignores_model_claims(snapshot):
    report = build_report("job-1", "demo", snapshot, model_report(edit_ready=True, source_applied=True),
                          "completed", mode="edit")
    assert report["changed_files"] == [] and report["edit_ready"] is False
    assert report["source_applied"] is False and report["changes"] == []


def test_review_does_not_export_changes_argument_or_add_edit_protocol_fields(snapshot):
    changes = mechanical_changes(snapshot, {"main.py": "changed\n"})
    report = build_report("job-1", "demo", snapshot, model_report(), "completed", changes=changes)
    assert report["changed_files"] == [] and "mode" not in report and "changes" not in report
    page = paginate_report(report, "job-1")
    assert "mode" not in page and "patch_text" not in page and "changes" not in page


@pytest.mark.parametrize("field,value", [("after_sha256", "a" * 64), ("after_size_bytes", 1), ("before_sha256", "b" * 64), ("change_type", ["modify"])])
def test_edit_rejects_inconsistent_mechanical_records(snapshot, field, value):
    changes = mechanical_changes(snapshot, {"main.py": "changed\n"})
    changes["changes"][0][field] = value
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    assert exc.value.code == "EDIT_CHANGES_INVALID"


@pytest.mark.parametrize("field", ["patch_text", "change_set_id", "snapshot_id"])
def test_edit_rejects_unbound_patch_or_change_set(snapshot, field):
    changes = mechanical_changes(snapshot, {"main.py": "changed\n"})
    changes[field] += "tampered"
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    assert exc.value.code == "EDIT_CHANGES_INVALID"


def test_edit_rejects_self_consistent_patch_that_disagrees_with_verified_bytes(snapshot):
    changes = mechanical_changes(snapshot, {"main.py": "changed\n"})
    changes["changes"][0]["diff"] = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+invented\n"
    changes["patch_text"] = changes["changes"][0]["diff"]
    changes["change_set_id"] = hashlib.sha256(json.dumps(
        {"snapshot_id": snapshot.snapshot_id, "changes": changes["changes"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    assert exc.value.code == "EDIT_CHANGES_INVALID"


@pytest.mark.parametrize("text,secrets", [("exact fake credential\n", ("fake credential",)), ("api_key=example-value\n", ()), ("Bearer " + "x" * 40 + "\n", ())])
def test_edit_rejects_secret_patch_instead_of_redacting_hash_bound_content(snapshot, text, secrets):
    changes = mechanical_changes(snapshot, {"new.txt": text})
    original = copy.deepcopy(changes)
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes, secrets=secrets)
    assert exc.value.code == "EDIT_SECRET_DETECTED"
    assert changes == original
    assert text.strip() not in exc.value.message


def test_edit_requires_unchanged_baseline_and_bounds_full_content(snapshot):
    changes = mechanical_changes(snapshot, {"main.py": "changed\n"})
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes, max_output_bytes=300)
    assert exc.value.code == "EDIT_OUTPUT_LIMIT"
    (snapshot.source_dir / "main.py").write_text("changed outside report\n")
    with pytest.raises(ReportError) as exc:
        build_report("job-1", "demo", snapshot, model_report(findings=[]), "completed", mode="edit", changes=changes)
    assert exc.value.code == "SNAPSHOT_CHANGED"


def test_findings_about_new_edit_files_still_validate_against_baseline(snapshot):
    changes = mechanical_changes(snapshot, {"new.txt": "new file\n"})
    report = build_report("job-1", "demo", snapshot,
                          model_report(findings=[{"severity": "info", "title": "新增文件", "path": "new.txt", "line": 1, "evidence": "new file"}]),
                          "completed", mode="edit", changes=changes)
    assert "path_not_in_snapshot" in report["findings"][0]["validation"]["errors"]
    assert report["edit_ready"] is True


def test_edit_pagination_reconstructs_complete_diff_and_utf8_content(snapshot):
    changes = mechanical_changes(snapshot, {"main.py": "def add(a, b):\n    return a + b\n", "new.txt": "中文内容\n" * 10000,
                                             "empty.txt": "", "note.txt": "text without LF"})
    report = build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    metadata, patch_chunks, content_chunks, rendered = [], [], {}, []
    cursor = None
    pages = 0
    while True:
        page = paginate_report(report, "job-1", cursor, page_size=2)
        assert page["mode"] == "edit" and page["edit_ready"] is True
        assert page["changed_files"] == [row["path"] for row in page["changes"]]
        assert len(page["changes"]) <= 2 and len(page["patch_text"]) <= 8000
        assert sum(len(row["text"]) for row in page["change_contents"]) <= 8000
        assert len(page["change_contents"]) <= 2 and len(page["report_text"]) <= 8000
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) < 150000
        assert page["patch_text_offset"] == sum(map(len, patch_chunks))
        metadata.extend(page["changes"])
        patch_chunks.append(page["patch_text"])
        rendered.append(page["report_text"])
        for row in page["change_contents"]:
            current = content_chunks.setdefault(row["path"], "")
            assert row["field"] == "after_text" and row["offset"] == len(current)
            content_chunks[row["path"]] += row["text"]
        cursor = page["next_cursor"]
        pages += 1
        assert pages < 100
        if cursor is None:
            break
    patch = "".join(patch_chunks)
    assert patch == changes["patch_text"] and "".join(rendered) == render_markdown(report)
    assert [row["path"] for row in metadata] == report["changed_files"]
    reconstructed = []
    for row in metadata:
        after = None if row["after_text_is_null"] else content_chunks.get(row["path"], "")
        assert len(after or "") == row["after_text_chars"]
        if after is not None:
            assert hashlib.sha256(after.encode("utf-8")).hexdigest() == row["after_sha256"]
        reconstructed.append({**{name: row[name] for name in ("path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes")},
                              "diff": patch[row["diff_offset"]:row["diff_offset"] + row["diff_chars"]], "after_text": after})
    assert reconstructed == changes["changes"] and pages > 2


def test_edit_cursor_rejects_invalid_positions_or_changed_patch(snapshot):
    changes = mechanical_changes(snapshot, {"new.txt": "ordinary text\n" * 2000})
    report = build_report("job-1", "demo", snapshot, model_report(), "completed", mode="edit", changes=changes)
    cursor = paginate_report(report, "job-1", page_size=1)["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor))
    for field, value in (("changes_offset", True), ("patch_offset", 999999), ("content_index", 999), ("content_offset", -1)):
        invalid = {**payload, field: value}
        forged = base64.urlsafe_b64encode(json.dumps(invalid).encode()).decode()
        with pytest.raises(ReportError) as exc:
            paginate_report(report, "job-1", forged, page_size=1)
        assert exc.value.code == "INVALID_CURSOR"
    with pytest.raises(ReportError) as exc:
        paginate_report(report, "job-1", cursor, page_size=2)
    assert exc.value.code == "INVALID_CURSOR"
    report["patch_text"] += "changed"
    with pytest.raises(ReportError) as exc:
        paginate_report(report, "job-1", cursor, page_size=1)
    assert exc.value.code == "INVALID_CURSOR"
