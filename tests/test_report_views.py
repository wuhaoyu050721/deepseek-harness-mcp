from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from harness_mcp.edit_workspace import _diff
from harness_mcp.report import ReportError, build_report, paginate_report, report_quality
from harness_mcp.workspace import Snapshot


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def snapshot(tmp_path: Path) -> Snapshot:
    source = tmp_path / "files"
    source.mkdir()
    contents = {"main.py": b"def add(a, b):\n    return a - b\n", "delete.txt": b"remove me\n"}
    manifest = []
    for path, content in contents.items():
        (source / path).write_bytes(content)
        manifest.append({"path": path, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "line_count": 2})
    return Snapshot(source, "view-snapshot", manifest, "a" * 40, True, list(contents), [])


def _finding(**updates):
    return {"severity": "medium", "title": "返回值与约定不符", "path": "main.py", "line_start": 2,
            "line_end": 2, "evidence": "return a - b", "trigger": "调用加法", "recommendation": "核对实现",
            "uncertainty": "须主 AI 复核", **updates}


def _report(snapshot, updates=None, findings=None, raw=None, **kwargs):
    changes = None
    if updates is not None:
        rows = []
        for path, after in sorted(updates.items()):
            original_path = snapshot.source_dir / path
            before = original_path.read_bytes() if original_path.exists() else None
            encoded = after.encode("utf-8") if after is not None else None
            rows.append({"path": path, "change_type": "add" if before is None else "delete" if encoded is None else "modify",
                         "before_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
                         "after_sha256": hashlib.sha256(encoded).hexdigest() if encoded is not None else None,
                         "before_size_bytes": len(before or b""), "after_size_bytes": len(encoded or b""),
                         "diff": _diff(path, before, encoded), "after_text": after})
        changes = {"schema_version": 1, "snapshot_id": snapshot.snapshot_id, "changes": rows,
                   "change_set_id": hashlib.sha256(_canonical({"snapshot_id": snapshot.snapshot_id, "changes": rows})).hexdigest(),
                   "patch_text": "".join(row["diff"] for row in rows), "source_applied": False}
    raw = raw if raw is not None else json.dumps({"summary": "需要复核实现。", "findings": findings if findings is not None else [_finding()]}, ensure_ascii=False)
    return build_report("job-views", "demo", snapshot, raw, "completed", mode="edit" if updates is not None else "review", changes=changes, **kwargs)


def _pages(report, view="full", page_size=2, execution_status="succeeded"):
    cursor = None
    for _ in range(1000):
        page = paginate_report(report, report["job_id"], cursor, page_size, view, execution_status=execution_status)
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 96 * 1024
        yield page
        cursor = page["next_cursor"]
        if cursor is None:
            return
    pytest.fail("分页没有结束")


@pytest.mark.parametrize("view", ["summary", "patch", "files"])
def test_new_views_do_not_render_markdown_and_include_natural_metadata_once(snapshot, monkeypatch, view):
    report = _report(snapshot, {"new.txt": "新文件唯一标记🙂\n" * 3000}, findings=[_finding()] * 5)
    monkeypatch.setattr("harness_mcp.report.render_markdown", lambda report: pytest.fail("新视图不应渲染 Markdown"))
    pages = list(_pages(report, view))
    assert len(pages) > 1
    assert pages[0]["metadata_included"] and pages[0]["summary"] == report["summary"]
    assert all(not page["metadata_included"] and "summary" not in page and "limitations" not in page for page in pages[1:])
    assert all("report_text" not in page for page in pages)
    if view == "summary":
        assert sum(len(page["findings"]) for page in pages) == 5
        assert all("patch_text" not in page and "changes" not in page and "change_contents" not in page for page in pages)
        assert "新文件唯一标记" not in json.dumps(pages, ensure_ascii=False)
    elif view == "patch":
        assert all("findings" not in page and "change_contents" not in page for page in pages)
        assert "".join(page["patch_text"] for page in pages) == report["patch_text"]
    else:
        assert all("findings" not in page and "patch_text" not in page for page in pages)
        assert "".join(item["text"] for page in pages for item in page["change_contents"]) == report["changes"][0]["after_text"]


def test_patch_and_files_reconstruct_original_mechanical_hashes_and_unicode_offsets(snapshot):
    report = _report(snapshot, {"main.py": "def add(a, b):\r\n    return a + b\r\n", "delete.txt": None,
                                "empty.txt": "", "emoji.txt": "🙂中文\r\n" * 4000 + "尾行无换行"})
    patch_pages = list(_pages(report, "patch", 1))
    file_pages = list(_pages(report, "files", 1))
    patch = "".join(page["patch_text"] for page in patch_pages)
    assert patch == report["patch_text"]
    metadata = [row for page in file_pages for row in page["changes"]]
    assert metadata == [row for page in patch_pages for row in page["changes"]]
    contents = {}
    for page in file_pages:
        assert page["change_set_id"] == report["change_set_id"]
        for item in page["change_contents"]:
            text = contents.setdefault(item["path"], "")
            assert item["offset"] == len(text)
            contents[item["path"]] += item["text"]
    reconstructed = []
    for row in metadata:
        after = None if row["after_text_is_null"] else contents.get(row["path"], "")
        if after is not None:
            assert len(after) == row["after_text_chars"]
            assert hashlib.sha256(after.encode("utf-8")).hexdigest() == row["after_sha256"]
        reconstructed.append({**{key: row[key] for key in ("path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes")},
                              "diff": patch[row["diff_offset"]:row["diff_offset"] + row["diff_chars"]], "after_text": after})
    assert reconstructed == report["changes"]
    assert hashlib.sha256(_canonical({"snapshot_id": report["snapshot_id"], "changes": reconstructed})).hexdigest() == report["change_set_id"]
    assert next(row for row in metadata if row["path"] == "empty.txt")["after_text_is_null"] is False
    assert next(row for row in metadata if row["path"] == "delete.txt")["after_text_is_null"] is True


@pytest.mark.parametrize("view", ["summary", "patch", "files", "full"])
def test_new_cursors_bind_view_job_digest_and_page_size(snapshot, view):
    report = _report(snapshot, {"new.txt": "ordinary text\n" * 3000}, findings=[_finding()] * 5)
    cursor = paginate_report(report, "job-views", page_size=1, view=view)["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor))
    assert payload["version"] == 2 and payload["view"] == view
    for other_view in {"summary", "patch", "files", "full"} - {view}:
        with pytest.raises(ReportError, match="视图") as exc:
            paginate_report(report, "job-views", cursor, 1, other_view)
        assert exc.value.code == "INVALID_CURSOR"
    for field, value in (("job_id", "another-job"), ("digest", "0" * 64), ("page_size", 2), ("version", True)):
        forged = base64.urlsafe_b64encode(_canonical({**payload, field: value})).decode("ascii")
        with pytest.raises(ReportError) as exc:
            paginate_report(report, "job-views", forged, 1, view)
        assert exc.value.code == "INVALID_CURSOR"
    for field in (key for key in payload if key.endswith("offset") or key == "content_index"):
        forged = base64.urlsafe_b64encode(_canonical({**payload, field: -1})).decode("ascii")
        with pytest.raises(ReportError) as exc:
            paginate_report(report, "job-views", forged, 1, view)
        assert exc.value.code == "INVALID_CURSOR"
    changed = copy.deepcopy(report)
    changed["summary"] += "变化"
    with pytest.raises(ReportError) as exc:
        paginate_report(changed, "job-views", cursor, 1, view)
    assert exc.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize("edit", [False, True])
def test_legacy_full_cursor_and_default_result_shape_remain_compatible(snapshot, edit):
    report = _report(snapshot, {"new.txt": "ordinary text\n" * 2000} if edit else None, findings=[_finding()] * 5)
    first = paginate_report(report, "job-views", page_size=1)
    assert first["view"] == "full" and "report_text" in first and "findings" in first
    assert ("patch_text" in first) == edit
    payload = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    legacy = {key: value for key, value in payload.items() if key not in {"version", "view"}}
    cursor = base64.urlsafe_b64encode(_canonical(legacy)).decode("ascii")
    assert paginate_report(report, "job-views", cursor, 1) == paginate_report(report, "job-views", first["next_cursor"], 1)
    for view in ("summary", "patch", "files"):
        with pytest.raises(ReportError) as exc:
            paginate_report(report, "job-views", cursor, 1, view)
        assert exc.value.code == "INVALID_CURSOR"


def test_aggregate_page_limit_preserves_all_full_streams(snapshot):
    text = "🙂中文\r\n" * 12000
    findings = [_finding(trigger="🙂" * 4000, recommendation="中文" * 2000, uncertainty="🙂" * 4000)] * 8
    report = _report(snapshot, {"new.txt": text}, findings=findings)
    pages = list(_pages(report, "full", 100))
    assert sum(len(page["findings"]) for page in pages) == 8
    assert "".join(page["patch_text"] for page in pages) == report["patch_text"]
    assert "".join(item["text"] for page in pages for item in page["change_contents"]) == text
    from harness_mcp.report import render_markdown
    assert "".join(page["report_text"] for page in pages) == render_markdown(report)


def test_quality_separates_execution_report_format_citations_and_patch(snapshot):
    report = _report(snapshot, {"new.txt": "content\n"}, findings=[_finding(), _finding(path="missing.py"), _finding(title="", severity="bad")])
    report["report_format"] = "degraded"
    report["report_format_error"] = {"code": "FINDING_COUNT_LIMIT", "message": "只保留部分条目"}
    before = copy.deepcopy(report)
    quality = report_quality(report, "succeeded")
    assert quality["report_format"] == "degraded"
    assert quality["citations"] == {"state": "mixed", "valid": 2, "invalid": 1, "unknown": 0, "total": 3, "scope": "retained_findings"}
    assert quality["finding_format"] == {"state": "mixed", "valid": 2, "invalid": 1, "unknown": 0, "total": 3, "scope": "retained_findings"}
    assert quality["patch"]["availability"] == "available_for_review"
    assert quality["patch"]["source_applied"] is False and quality["patch"]["current_source_match"] == "not_checked"
    assert quality["tests"] == "not_run" and report == before
    page = paginate_report(report, "job-views", view="summary", execution_status="succeeded")
    assert page["execution_status"] == "succeeded" and page["quality"] == quality


@pytest.mark.parametrize("status", [None, "failed", "cancelled", "timed_out", "interrupted", "running"])
def test_patch_quality_requires_confirmed_execution_success(snapshot, status):
    report = _report(snapshot, {"new.txt": "content\n"})
    assert report_quality(report, status)["patch"]["availability"] == "unavailable"


@pytest.mark.parametrize("field,value", [("error", {}), ("edit_ready", False), ("source_applied", True),
                                        ("requires_main_agent_review", False), ("finish_reason", "cancelled"),
                                        ("change_set_id", "0" * 64), ("patch_text", "partial"), ("changed_files", [])])
def test_patch_quality_rejects_incomplete_or_inconsistent_mechanical_structure(snapshot, field, value):
    report = _report(snapshot, {"new.txt": "content\n"})
    report[field] = value
    assert report_quality(report, "succeeded")["patch"]["availability"] == "unavailable"


@pytest.mark.parametrize("field,value", [("after_sha256", "0" * 64), ("after_size_bytes", True),
                                        ("change_type", []), ("path", "../escape.txt"), ("after_text", None)])
def test_patch_quality_rejects_malformed_change_rows_without_crashing(snapshot, field, value):
    report = _report(snapshot, {"new.txt": "content\n"})
    report["changes"][0][field] = value
    assert report_quality(report, "succeeded")["patch"]["availability"] == "unavailable"


def test_review_and_empty_edits_have_explicit_patch_quality(snapshot):
    assert report_quality(_report(snapshot), "succeeded")["patch"]["availability"] == "not_applicable"
    empty = report_quality(_report(snapshot, {}), "succeeded")["patch"]
    assert empty["availability"] == "available_for_review" and empty["changed_files_count"] == 0


def test_no_findings_and_unusable_json_are_not_reported_as_clean_citations(snapshot):
    assert report_quality(_report(snapshot, findings=[]), "succeeded")["citations"]["state"] == "no_findings"
    quality = report_quality(_report(snapshot, raw="not JSON"), "succeeded")
    assert quality["citations"]["state"] == "unavailable" and quality["citations"]["valid"] == 0
    assert quality["finding_format"]["state"] == "unavailable"


def test_degraded_json_does_not_invalidate_complete_mechanical_patch(snapshot):
    quality = report_quality(_report(snapshot, {"new.txt": "content\n"}, raw="not JSON"), "succeeded")
    assert quality["report_format"] == "degraded" and quality["citations"]["state"] == "unavailable"
    assert quality["patch"]["availability"] == "available_for_review"


def test_non_object_finding_has_format_failure_without_a_verified_citation(snapshot):
    quality = report_quality(_report(snapshot, findings=[None]), "succeeded")
    assert quality["citations"]["unknown"] == 1 and quality["citations"]["valid"] == 0
    assert quality["finding_format"]["invalid"] == 1


def test_missing_and_unknown_validation_are_counted_conservatively(snapshot):
    report = _report(snapshot, findings=[_finding()] * 5)
    report["findings"][0].pop("validation")
    report["findings"][1]["validation"] = {"status": "invalid", "errors": ["future_check"]}
    report["findings"][2]["validation"] = {"status": [], "errors": []}
    report["findings"][3]["validation"] = {"status": "invalid", "errors": ["finding_not_object"]}
    quality = report_quality(report, "succeeded")
    assert quality["citations"] == {"state": "mixed", "valid": 1, "invalid": 0, "unknown": 4, "total": 5, "scope": "retained_findings"}
    assert quality["finding_format"] == {"state": "mixed", "valid": 1, "invalid": 1, "unknown": 3, "total": 5, "scope": "retained_findings"}


@pytest.mark.parametrize("view", [None, "code", True, []])
def test_invalid_view_is_rejected(snapshot, view):
    with pytest.raises(ReportError) as exc:
        paginate_report(_report(snapshot), "job-views", view=view)
    assert exc.value.code == "INVALID_RESULT_VIEW"


def test_oversized_unpageable_metadata_has_explicit_error(snapshot):
    report = _report(snapshot)
    report["usage"] = {"unpageable": "🙂" * 30000}
    with pytest.raises(ReportError) as exc:
        paginate_report(report, "job-views", view="summary")
    assert exc.value.code == "REPORT_PAGE_LIMIT"
