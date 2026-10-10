"""保存有界报告，并对引用做机械校验；模型自述不作为测试证据。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .workspace import Snapshot, SnapshotError, _check_path_chain, _read_verified, normalize_relative_path
from .redaction import redact_auth_fields, sensitive_key


class ReportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _bounded(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """凭证脱敏作为保存前的最后一道检查，不宣称能识别所有硬编码秘密。"""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", "[REDACTED]", text)
    text = redact_auth_fields(text)
    text = re.sub(r"(?s)-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?-----END (?:[A-Z ]*PRIVATE KEY)-----", "[REDACTED PRIVATE KEY]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return text


def _safe_value(value: Any, depth: int = 0, *, secrets: tuple[str, ...] = ()) -> Any:
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_text(value, secrets)[:4000]
    if isinstance(value, dict):
        return {str(k)[:100]: _safe_value(v, depth + 1, secrets=secrets)
                for k, v in list(value.items())[:100]
                if not sensitive_key(k, secrets) and redact_text(str(k), secrets) == str(k)}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, depth + 1, secrets=secrets) for v in value[:100]]
    return None


def _validate_finding(raw: Any, snapshot: Snapshot) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"severity": "unknown", "title": "非对象问题条目", "path": None, "line_start": None, "line_end": None, "evidence": "", "trigger": "", "recommendation": "", "uncertainty": "模型问题条目格式无效。", "validation": {"status": "invalid", "errors": ["finding_not_object"]}}
    errors: list[str] = []
    path = raw.get("path", raw.get("file"))
    normalized: str | None = None
    try:
        normalized = normalize_relative_path(path)
    except SnapshotError:
        errors.append("invalid_relative_path")
    start = raw.get("line_start", raw.get("line"))
    end = raw.get("line_end", start)
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        errors.append("invalid_line_start")
        start = None
    if isinstance(end, bool) or not isinstance(end, int) or end < 1 or (start is not None and end < start):
        errors.append("invalid_line_end")
        end = None
    evidence = _bounded(raw.get("evidence"))
    row = next((r for r in snapshot.manifest if r["path"] == normalized), None)
    if normalized is not None and row is None:
        errors.append("path_not_in_snapshot")
    if row is not None:
        try:
            data = _read_verified(snapshot.source_dir / normalized, row["size_bytes"])
        except (SnapshotError, OSError) as exc:
            raise ReportError("SNAPSHOT_CHANGED", "报告引用的快照文件不可读取或已变化。") from exc
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ReportError("SNAPSHOT_CHANGED", "报告引用的快照文件哈希已变化。")
        lines = data.decode("utf-8", errors="replace").splitlines()
        if start is not None and end is not None:
            if start > len(lines) or end > len(lines):
                errors.append("line_out_of_range")
            elif evidence and evidence.replace("\r\n", "\n").strip() not in "\n".join(lines[start - 1:end]):
                errors.append("evidence_not_in_referenced_lines")
    if not evidence:
        errors.append("missing_evidence")
    severity = raw.get("severity", "unknown")
    if not isinstance(severity, str) or severity not in {"critical", "high", "medium", "low", "info", "P0", "P1", "P2", "P3"}:
        severity = "unknown"
        errors.append("unknown_severity")
    if not _bounded(raw.get("title"), 300):
        errors.append("missing_title")
    return {
        "severity": severity, "title": _bounded(raw.get("title"), 300), "path": normalized,
        "line_start": start, "line_end": end, "evidence": evidence,
        "trigger": _bounded(raw.get("trigger", raw.get("trigger_conditions"))),
        "recommendation": _bounded(raw.get("recommendation", raw.get("fix"))),
        "uncertainty": _bounded(raw.get("uncertainty")),
        "validation": {"status": "valid" if not errors else "invalid", "errors": errors},
    }


def _parse_output(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S | re.I)
        if match:
            text = match.group(1)
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ReportError("REPORT_JSON_INVALID", "模型输出不是有效的结构化 JSON。") from exc
    if not isinstance(parsed, dict):
        raise ReportError("REPORT_SCHEMA_INVALID", "模型报告的根节点必须是对象。")
    if not isinstance(parsed.get("summary"), str) or not isinstance(parsed.get("findings"), list):
        raise ReportError("REPORT_SCHEMA_INVALID", "模型报告缺少字符串 summary 或 findings 数组。")
    return parsed


def _source_secret_error(matches: list[dict]) -> ReportError:
    error = ReportError("EDIT_SECRET_DETECTED", "实际文件或机械数据命中凭证规则，拒绝导出可合入补丁。")
    error.details = {"matches": matches[:64], "total_matches": len(matches), "truncated": len(matches) > 64}
    return error


def _verified_changes(value: Any, snapshot: Snapshot, max_output_bytes: int, secrets: tuple[str, ...]) -> dict[str, Any]:
    """机械变更必须完整且可核对；禁止截断或脱敏后仍沿用原内容摘要。"""
    from .edit_workspace import EditWorkspaceError, _diff
    from .source_secrets import scan_deterministic, scan_source

    invalid = ReportError("EDIT_CHANGES_INVALID", "机械变更清单结构或内容摘要未通过校验。")
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("snapshot_id") != snapshot.snapshot_id:
        raise invalid
    rows, patch = value.get("changes"), value.get("patch_text")
    if not isinstance(rows, list) or len(rows) > 10000 or not isinstance(patch, str) or value.get("source_applied") is not False:
        raise invalid
    try:
        if len(_json_bytes(value)) > min(max_output_bytes, 1048576):
            raise ReportError("EDIT_OUTPUT_LIMIT", "完整机械变更超过报告上限，请缩小修改范围。")
    except (ReportError, UnicodeError) as exc:
        if isinstance(exc, ReportError) and exc.code == "EDIT_OUTPUT_LIMIT":
            raise
        raise invalid from None
    fields = {"path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes", "diff", "after_text"}
    baseline = {row["path"]: row for row in snapshot.manifest}
    baseline_aliases = {path.casefold(): path for path in baseline}
    paths: set[str] = set()
    confirmed = []
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise invalid
        try:
            path = normalize_relative_path(raw["path"])
        except SnapshotError:
            raise invalid from None
        if path != raw["path"] or path.casefold() in paths or not isinstance(raw["change_type"], str) or raw["change_type"] not in {"add", "modify", "delete"}:
            raise invalid
        if path.casefold() in baseline_aliases and baseline_aliases[path.casefold()] != path:
            raise invalid
        paths.add(path.casefold())
        if not isinstance(raw["diff"], str) or any(
            isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 0
            for name in ("before_size_bytes", "after_size_bytes")
        ):
            raise invalid
        before = baseline.get(path)
        original = None
        if raw["change_type"] == "add":
            if before is not None or raw["before_sha256"] is not None or raw["before_size_bytes"] != 0:
                raise invalid
        else:
            if before is None or raw["before_sha256"] != before["sha256"] or raw["before_size_bytes"] != before["size_bytes"]:
                raise invalid
            try:
                original = _read_verified(snapshot.source_dir / path, before["size_bytes"])
            except (SnapshotError, OSError) as exc:
                raise ReportError("SNAPSHOT_CHANGED", "变更引用的原始快照不可读取或已变化。") from exc
            if hashlib.sha256(original).hexdigest() != before["sha256"]:
                raise ReportError("SNAPSHOT_CHANGED", "变更引用的原始快照摘要已变化。")
        after = raw["after_text"]
        if raw["change_type"] == "delete":
            encoded = None
            if after is not None or raw["after_sha256"] is not None or raw["after_size_bytes"] != 0:
                raise invalid
        else:
            if not isinstance(after, str) or "\x00" in after:
                raise invalid
            try:
                encoded = after.encode("utf-8", errors="strict")
            except UnicodeError:
                raise invalid from None
            if len(encoded) != raw["after_size_bytes"] or hashlib.sha256(encoded).hexdigest() != raw["after_sha256"]:
                raise invalid
            if raw["change_type"] == "modify" and raw["before_sha256"] == raw["after_sha256"]:
                raise invalid
        # 再由已核对的原始/新字节生成补丁，不能只相信一份自洽的摘要及拼接。
        try:
            if raw["diff"] != _diff(path, original, encoded):
                raise invalid
        except EditWorkspaceError:
            raise invalid from None
        # 对已核对的完整 before/after 做源码检查；diff 本身不能作为完整语法解析。
        matches = [match for content in (original, encoded) if content is not None
                   for match in scan_source(path, content, secrets)]
        if matches:
            raise _source_secret_error(matches)
        confirmed.append(dict(raw))
    digest = hashlib.sha256(_json_bytes({"snapshot_id": snapshot.snapshot_id, "changes": confirmed})).hexdigest()
    if value.get("change_set_id") != digest or patch != "".join(row["diff"] for row in confirmed):
        raise invalid
    # 补丁、路径、摘要只检查确定凭证；保持原始源码与哈希，不用自然语言脱敏作判定。
    for text in (patch, digest, *(item for row in confirmed for item in row.values() if isinstance(item, str))):
        matches = scan_deterministic(text, secrets)
        if matches:
            raise _source_secret_error(matches)
    return {"changed_files": [row["path"] for row in confirmed], "changes": confirmed,
            "patch_text": patch, "change_set_id": digest}


_MECHANICAL_EXPORT_FIELDS = frozenset({"changed_files", "changes", "patch_text", "change_set_id"})


def _scan_saved_mechanical(report: dict, secrets) -> None:
    from .source_secrets import scan_source, scan_deterministic
    for row in report["changes"]:
        path = row["path"]
        matches = scan_deterministic(path, secrets, path) + scan_deterministic(row["diff"], secrets, path)
        if row["after_text"] is not None:
            matches += scan_source(path, row["after_text"], secrets)
        if matches:
            raise _source_secret_error(matches)


def redact_report_for_save(report: dict, snapshot: Snapshot, secrets=(), *, max_output_bytes=1048576) -> dict:
    """以实际 Snapshot 重验来源后保护机械字段，不能依据 edit_ready 自述跳过检查。"""
    from .redaction import _mark_verified_source_payload, redact_verified_source_payload, redact_value
    if report.get("mode") != "edit" or report.get("edit_ready") is not True:
        return redact_value(report, secrets)
    raw = {"schema_version": 1, "snapshot_id": report.get("snapshot_id"), "changes": report.get("changes"),
           "patch_text": report.get("patch_text"), "change_set_id": report.get("change_set_id"), "source_applied": False}
    mechanical = _verified_changes(raw, snapshot, max_output_bytes, tuple(secrets))
    if any(report.get(key) != mechanical[key] for key in _MECHANICAL_EXPORT_FIELDS):
        raise ReportError("EDIT_CHANGES_INVALID", "报告机械数据与实际来源不一致。")
    return redact_verified_source_payload(_mark_verified_source_payload(report, _MECHANICAL_EXPORT_FIELDS), secrets)


def prepare_report_for_export(report: dict, expected_report_id: str, secrets=()) -> dict:
    """仅在 read_report 已按 DB 摘要校验后调用，并再次绑定规范持久化字节。"""
    from .redaction import _mark_verified_source_payload, redact_verified_source_payload, redact_value
    if (not isinstance(expected_report_id, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_report_id)
            or not hmac.compare_digest(hashlib.sha256(_json_bytes(report)).hexdigest(), expected_report_id)):
        raise ReportError("REPORT_TAMPERED", "报告不匹配已校验的数据库摘要，不能保护机械数据。")
    if report.get("mode") != "edit" or report.get("edit_ready") is not True:
        return redact_value(report, secrets)
    if not _complete_mechanical_structure(report):
        raise ReportError("EDIT_CHANGES_INVALID", "已保存机械数据结构与摘要不一致。")
    _scan_saved_mechanical(report, secrets)
    # 先脱敏自然字段，后续 Markdown 必须由这份安全报告渲染。
    return redact_verified_source_payload(_mark_verified_source_payload(report, _MECHANICAL_EXPORT_FIELDS), secrets)


def protect_verified_result_page(page: dict, report: dict, secrets=()) -> dict:
    """核对分页切片与安全报告，再授予本进程来源标记；普通 JSON 字段名没有权限。"""
    from .redaction import _mark_verified_source_payload, _VerifiedSourcePayload, redact_verified_source_payload, redact_value
    if not isinstance(report, _VerifiedSourcePayload):
        return redact_value(page, secrets)
    report = redact_verified_source_payload(report, secrets)
    invalid = ReportError("EDIT_CHANGES_INVALID", "分页机械数据不匹配已验证报告。")
    if not isinstance(page, dict) or ("changes" in page and not isinstance(page["changes"], list)):
        raise invalid
    rows = {row["path"]: row for row in report["changes"]}
    _, verified_metadata, _ = _change_metadata(report)
    metadata = {row["path"]: row for row in verified_metadata}
    fields = set(_MECHANICAL_EXPORT_FIELDS) & set(page)
    if "patch_text" in page:
        offset = page.get("patch_text_offset")
        text = page["patch_text"]
        if (type(offset) is not int or not 0 <= offset <= len(report["patch_text"]) or not isinstance(text, str)
                or text != report["patch_text"][offset:offset + len(text)]):
            raise invalid
    for row in page.get("changes", []):
        original = rows.get(row.get("path")) if isinstance(row, dict) else None
        if original is None or row != metadata[row["path"]]:
            raise invalid
    if "changed_files" in page and page["changed_files"] != [row["path"] for row in page.get("changes", [])]:
        raise invalid
    if "change_set_id" in page and page["change_set_id"] != report["change_set_id"]:
        raise invalid
    if "change_contents" in page:
        if not isinstance(page["change_contents"], list):
            raise invalid
        fields.add("change_contents")
        for item in page["change_contents"]:
            if not isinstance(item, dict):
                raise invalid
            original = rows.get(item.get("path"))
            offset, text = item.get("offset"), item.get("text")
            if (original is None or set(item) != {"path", "field", "offset", "text", "total_chars", "sha256"}
                    or original["after_text"] is None or item.get("field") != "after_text"
                    or type(offset) is not int or not 0 <= offset <= len(original["after_text"]) or not isinstance(text, str)
                    or item.get("total_chars") != len(original["after_text"])
                    or item.get("sha256") != original["after_sha256"]
                    or text != original["after_text"][offset:offset + len(text)]):
                raise invalid
    if "report_text" in page:
        fields.add("report_text")
        offset, text = page.get("report_text_offset"), page["report_text"]
        markdown = render_markdown(report)
        if (type(offset) is not int or not 0 <= offset <= len(markdown) or not isinstance(text, str)
                or text != markdown[offset:offset + len(text)]):
            raise invalid
    return redact_verified_source_payload(_mark_verified_source_payload(page, fields), secrets)


def build_report(
    job_id: str, project_id: str, snapshot: Snapshot, raw_text: str,
    finish_reason: str | None, usage: dict[str, Any] | None = None,
    timing: dict[str, Any] | None = None, error: Any = None,
    max_output_bytes: int = 1048576, *, secrets: tuple[str, ...] = (),
    mode: str = "review", changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw_text, str) or isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ReportError("INVALID_REPORT_INPUT", "报告输出必须是文本，大小上限必须为正整数。")
    if not isinstance(mode, str) or mode not in {"review", "edit"}:
        raise ReportError("INVALID_REPORT_INPUT", "报告模式必须是 review 或 edit。")
    encoded = raw_text.encode("utf-8")
    truncated = len(encoded) > max_output_bytes
    bounded_raw = encoded[:max_output_bytes].decode("utf-8", errors="ignore")
    limitations = [
        "未运行项目测试或构建；tests 固定为 not_run。",
        "路径、行号和证据仅经过机械校验，问题结论仍需主 AI 复核。",
        "默认排除规则不能保证源代码中不存在硬编码秘密。",
    ]
    if snapshot.excluded:
        limitations.append(f"本次快照明确排除了 {len(snapshot.excluded)} 项；详见 excluded。")
    report: dict[str, Any] = {
        "schema_version": 1, "job_id": job_id, "project_id": project_id,
        "snapshot_id": snapshot.snapshot_id, "review_scope": snapshot.review_scope,
        "git_commit": snapshot.git_commit, "git_dirty": snapshot.git_dirty,
        "summary": "", "findings": [], "limitations": limitations,
        "excluded": snapshot.excluded, "changed_files": [], "tests": "not_run",
        "usage": _safe_value(usage, secrets=secrets) if usage is not None else None,
        "timing": _safe_value(timing, secrets=secrets) if timing is not None else None,
        "error": _safe_value(error, secrets=secrets), "finish_reason": _bounded(finish_reason, 200) or None,
        "report_format": "valid", "report_format_error": None,
        "output_truncated": truncated, "raw_text": None,
    }
    mechanical = None
    if mode == "edit":
        report.update(mode="edit", changed_files=[], changes=[], patch_text="", change_set_id=None,
                      edit_ready=False, requires_main_agent_review=True, source_applied=False)
        if changes is not None and error is None and finish_reason == "completed":
            mechanical = _verified_changes(changes, snapshot, max_output_bytes, secrets)
        report["limitations"].append("修改仅在独立工作副本发生；原项目未应用任何补丁，须由主 AI 复核后合入。")
        if mechanical is None:
            report["limitations"].append("任务未提供可合入的完整机械变更；取消、失败和超时的部分修改不进入报告。")
    try:
        if truncated:
            raise ReportError("REPORT_OUTPUT_LIMIT", "模型输出超过报告保存上限，保留有界原文。")
        if not bounded_raw.strip():
            raise ReportError("REPORT_EMPTY", "模型未返回可用报告文本。")
        parsed = _parse_output(bounded_raw)
        report["summary"] = _bounded(parsed["summary"], 8000)
        report["findings"] = [_validate_finding(row, snapshot) for row in parsed["findings"][:200]]
        if len(parsed["findings"]) > 200:
            report["limitations"].append("模型问题条目超过 200 条，报告只保留前 200 条。")
            report["report_format"] = "degraded"
            report["report_format_error"] = {"code": "FINDING_COUNT_LIMIT", "message": "问题条目超过保存上限。"}
        if isinstance(parsed.get("limitations"), list):
            report["limitations"].extend(_bounded(item, 2000) for item in parsed["limitations"][:100] if isinstance(item, str))
        if parsed.get("changed_files"):
            report["limitations"].append("模型自述的文件修改未被采信；变更仅以机械清单为准。" if mode == "edit" else "模型自述的文件修改未被采信；首版只读任务 changed_files 固定为空。")
        if any(f["validation"]["status"] != "valid" for f in report["findings"]):
            report["limitations"].append("部分问题的引用未通过机械校验；详见每条问题的 validation。")
    except ReportError as exc:
        if exc.code == "SNAPSHOT_CHANGED":
            raise
        report["report_format"] = "truncated" if truncated else "degraded"
        report["report_format_error"] = {"code": exc.code, "message": exc.message}
        report["summary"] = "Harness 已返回输出，但结构化报告未通过格式校验；请复核有界原文。"
        report["raw_text"] = bounded_raw
        report["limitations"].append(exc.message)
    # 不保存未知的模型字段；所有被保留文本最后统一脱敏。
    def redact_value(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value, secrets)
        if isinstance(value, list):
            return [redact_value(v) for v in value]
        if isinstance(value, dict):
            return {k: redact_value(v) for k, v in value.items()
                    if not sensitive_key(k, secrets) and redact_text(str(k), secrets) == str(k)}
        return value
    report = redact_value(report)
    # 机械数据在所有自然语言脱敏之后加入，验证过的字节和摘要保持完全一致。
    if mechanical is not None:
        report.update(mechanical, edit_ready=True)
    if isinstance(report.get("raw_text"), str):
        masked_raw = report["raw_text"].encode("utf-8")
        if len(masked_raw) > max_output_bytes:
            report["raw_text"] = masked_raw[:max_output_bytes].decode("utf-8", errors="ignore")
            report["output_truncated"] = True
    return report


def _json_bytes(report: dict[str, Any]) -> bytes:
    try:
        return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportError("REPORT_NOT_SERIALIZABLE", "报告包含不可保存的数据。") from exc


def render_markdown(report: dict[str, Any]) -> str:
    edit = report.get("mode") == "edit"
    label = "修改" if edit else "审查"
    lines = [f"# Harness {label}报告 {report['job_id']}", "", report.get("summary", ""), "", f"- 项目：{report.get('project_id')}", f"- 快照：{report.get('snapshot_id')}", f"- 审查范围：{', '.join(report.get('review_scope', []))}", f"- 提交：{report.get('git_commit') or '无提交'}", f"- 工作区有修改：{report.get('git_dirty')}", f"- 报告格式：{report.get('report_format')}", f"- 完成原因：{report.get('finish_reason')}", "- 测试：not_run", f"- 修改文件：{json.dumps(report.get('changed_files', []) if edit else [], ensure_ascii=False)}"]
    if edit:
        lines.extend([f"- 可供复核合入：{report.get('edit_ready') is True}", f"- 变更摘要：{report.get('change_set_id') or '无完整变更'}", "- 原项目已应用：False", "- 需主 AI 复核：True"])
    lines.extend(["", "## 问题", ""])
    for i, finding in enumerate(report.get("findings", []), 1):
        lines.extend([f"### {i}. [{finding['severity']}] {finding['title']}", "", f"位置：{finding['path']}:{finding['line_start']}-{finding['line_end']}", f"引用校验：{finding['validation']['status']} ({', '.join(finding['validation']['errors'])})", "", "依据："])
        evidence = finding.get("evidence", "")
        longest = max((len(m.group()) for m in re.finditer(r"`+", evidence)), default=0)
        fence = "`" * max(3, longest + 1)
        lines.extend([fence, evidence, fence, "", f"触发条件：{finding.get('trigger', '')}", f"建议：{finding.get('recommendation', '')}", f"不确定性：{finding.get('uncertainty', '')}", ""])
    lines.extend(["## 限制", "", *[f"- {item}" for item in report.get("limitations", [])], "", "## 明确排除", "", *[f"- {e['path']}：{e['reason']}" for e in report.get("excluded", [])]])
    if edit:
        lines.extend(["", "## 机械变更", ""])
        for row in report.get("changes", []):
            lines.append(f"- {row['change_type']} {row['path']} ({row['before_size_bytes']} → {row['after_size_bytes']} bytes)")
        patch = report.get("patch_text", "")
        if patch:
            longest = max((len(m.group()) for m in re.finditer(r"`+", patch)), default=0)
            fence = "`" * max(3, longest + 1)
            lines.extend(["", fence + "diff", patch, fence])
    if report.get("raw_text"):
        raw = report["raw_text"]
        longest = max((len(m.group()) for m in re.finditer(r"`+", raw)), default=0)
        fence = "`" * max(3, longest + 1)
        lines.extend(["", "## 有界原文", "", fence, raw, fence])
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_report(report: dict[str, Any], directory: Path) -> dict[str, str]:
    directory = Path(directory)
    try:
        _check_path_chain(directory)
    except SnapshotError as exc:
        raise ReportError("UNSAFE_REPORT_PATH", "报告目录包含不安全链接。") from exc
    directory.mkdir(parents=True, exist_ok=True)
    content = _json_bytes(report)
    if len(content) > 2097152:
        raise ReportError("REPORT_SAVE_LIMIT", "报告超过本地持久化大小上限。")
    # JSON 最后替换，作为完整报告可读取的提交点。
    _atomic_write(directory / "report.md", render_markdown(report).encode("utf-8"))
    _atomic_write(directory / "report.json", content)
    return {"report_id": hashlib.sha256(content).hexdigest(), "json_path": str(directory / "report.json"), "markdown_path": str(directory / "report.md")}


def read_report(directory: Path, max_bytes: int = 2097152, expected_report_id: str | None = None) -> dict[str, Any]:
    if not isinstance(max_bytes, int) or max_bytes < 1:
        raise ReportError("INVALID_REPORT_LIMIT", "报告读取上限必须为正整数。")
    path = Path(directory) / "report.json"
    try:
        content = _read_verified(Path(os.path.abspath(path)), max_bytes)
    except (SnapshotError, OSError) as exc:
        raise ReportError("REPORT_READ_FAILED", "报告不存在、不安全或超过读取上限。") from exc
    # 数据库中保存的摘要绑定实际持久化字节，不能仅信任报告内的自述字段。
    if expected_report_id is not None:
        actual = hashlib.sha256(content).hexdigest()
        if not isinstance(expected_report_id, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_report_id) or not hmac.compare_digest(actual, expected_report_id):
            raise ReportError("REPORT_TAMPERED", "报告内容与任务数据库记录的摘要不一致。")
    try:
        report = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ReportError("REPORT_CORRUPT", "已保存报告不能解析。") from exc
    if not isinstance(report, dict) or not isinstance(report.get("findings"), list) or not isinstance(report.get("job_id"), str):
        raise ReportError("REPORT_CORRUPT", "已保存报告结构无效。")
    return report


_CITATION_ERRORS = frozenset({
    "invalid_relative_path", "invalid_line_start", "invalid_line_end", "path_not_in_snapshot",
    "line_out_of_range", "evidence_not_in_referenced_lines", "missing_evidence",
})
_FINDING_FORMAT_ERRORS = frozenset({"finding_not_object", "unknown_severity", "missing_title"})
_PAGE_LIMIT_BYTES = 96 * 1024
_CURSOR_RESERVE_BYTES = 2048
_CHANGE_FIELDS = ("path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes")
_POSITION_FIELDS = ("offset", "text_offset", "changes_offset", "patch_offset", "content_index", "content_offset")


def _complete_mechanical_structure(report: dict[str, Any]) -> bool:
    """复用已保存的机械证据核对结构/摘要；这不检查当前原项目或批准合入。"""
    rows, patch = report.get("changes"), report.get("patch_text")
    if (report.get("edit_ready") is not True or report.get("source_applied") is not False
            or report.get("requires_main_agent_review") is not True or report.get("error") is not None
            or report.get("finish_reason") != "completed" or not isinstance(rows, list)
            or len(rows) > 10000 or not isinstance(patch, str) or not isinstance(report.get("snapshot_id"), str)):
        return False
    fields = {*_CHANGE_FIELDS, "diff", "after_text"}
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            return False
        try:
            path = normalize_relative_path(row["path"])
        except SnapshotError:
            return False
        if (path != row["path"] or path.casefold() in paths or not isinstance(row["change_type"], str)
                or row["change_type"] not in {"add", "modify", "delete"}):
            return False
        paths.add(path.casefold())
        if not isinstance(row["diff"], str) or any(type(row[name]) is not int or row[name] < 0 for name in ("before_size_bytes", "after_size_bytes")):
            return False
        before, after = row["before_sha256"], row["after_sha256"]
        if row["change_type"] == "add":
            if before is not None or row["before_size_bytes"] != 0:
                return False
        elif not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before):
            return False
        text = row["after_text"]
        if row["change_type"] == "delete":
            if text is not None or after is not None or row["after_size_bytes"] != 0:
                return False
        else:
            if not isinstance(text, str) or "\x00" in text or not isinstance(after, str):
                return False
            try:
                encoded = text.encode("utf-8", errors="strict")
            except UnicodeError:
                return False
            if len(encoded) != row["after_size_bytes"] or hashlib.sha256(encoded).hexdigest() != after:
                return False
            if row["change_type"] == "modify" and before == after:
                return False
    try:
        digest = hashlib.sha256(_json_bytes({"snapshot_id": report["snapshot_id"], "changes": rows})).hexdigest()
    except (ReportError, UnicodeError):
        return False
    return (report.get("changed_files") == [row["path"] for row in rows]
            and report.get("change_set_id") == digest and patch == "".join(row["diff"] for row in rows))


def _validation_counts(report: dict[str, Any], family: frozenset[str]) -> dict[str, Any]:
    counts = {"valid": 0, "invalid": 0, "unknown": 0}
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return {"state": "unavailable", **counts, "total": 0, "scope": "retained_findings"}
    known = _CITATION_ERRORS | _FINDING_FORMAT_ERRORS
    for finding in findings:
        validation = finding.get("validation") if isinstance(finding, dict) else None
        errors = validation.get("errors") if isinstance(validation, dict) else None
        status = validation.get("status") if isinstance(validation, dict) else None
        # 旧记录没有可识别的校验明细时，不把缺失证据推断为 valid。
        if (not isinstance(errors, list) or any(not isinstance(error, str) for error in errors)
                or not isinstance(status, str) or status not in {"valid", "invalid"} or (status == "valid" and errors)
                or (status == "invalid" and not errors)):
            counts["unknown"] += 1
        elif any(error in family for error in errors):
            counts["invalid"] += 1
        elif any(error not in known for error in errors) or (family == _CITATION_ERRORS and "finding_not_object" in errors):
            counts["unknown"] += 1
        else:
            counts["valid"] += 1
    active = [name for name, count in counts.items() if count]
    if not findings:
        parsed = report.get("report_format") == "valid" or (
            isinstance(report.get("report_format_error"), dict)
            and report["report_format_error"].get("code") == "FINDING_COUNT_LIMIT")
        state = "no_findings" if parsed else "unavailable"
    else:
        state = active[0] if len(active) == 1 else "mixed"
    return {"state": state, **counts, "total": len(findings), "scope": "retained_findings"}


def report_quality(report: dict[str, Any], execution_status: str | None = None) -> dict[str, Any]:
    """派生读取质量，不改已保存报告，也不把模型/引用校验当作业务验收。"""
    edit = report.get("mode") == "edit"
    rows = report.get("changes")
    availability = "not_applicable" if not edit else "unavailable"
    if edit and execution_status == "succeeded" and _complete_mechanical_structure(report):
        availability = "available_for_review"
    return {
        "report_format": report.get("report_format", "unknown"),
        "citations": _validation_counts(report, _CITATION_ERRORS),
        "finding_format": _validation_counts(report, _FINDING_FORMAT_ERRORS),
        "patch": {"availability": availability, "changed_files_count": len(rows) if isinstance(rows, list) else 0,
                  "source_applied": False, "requires_main_agent_review": True, "current_source_match": "not_checked"},
        "tests": "not_run",
    }


def _page_fits(page: dict[str, Any]) -> bool:
    # 用含默认空白的 UTF-8 JSON 计量，比持久化的紧凑编码保守；预留完整游标。
    try:
        size = len(json.dumps(page, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReportError("REPORT_NOT_SERIALIZABLE", "报告包含不可分页的数据。") from exc
    return size <= _PAGE_LIMIT_BYTES - _CURSOR_RESERVE_BYTES


def _append_page_rows(page: dict[str, Any], field: str, rows: list[Any], offset: int, page_size: int) -> int:
    start = offset
    while offset < min(start + page_size, len(rows)):
        page[field].append(rows[offset])
        if not _page_fits(page) or (offset > start and len(_json_bytes({field: page[field]})) > 65536):
            page[field].pop()
            break
        offset += 1
    return offset


def _page_text(page: dict[str, Any], field: str, text: str, offset: int, limit: int = 8000) -> int:
    """按整体字节预算取完整 Unicode 字符，所有偏移仍是 Python 字符数。"""
    lower, upper = 0, min(limit, len(text) - offset)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        page[field] = text[offset:offset + middle]
        if _page_fits(page):
            lower = middle
        else:
            upper = middle - 1
    page[field] = text[offset:offset + lower]
    return offset + lower


def _change_metadata(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    rows, patch = report.get("changes", []), report.get("patch_text", "")
    if not isinstance(rows, list) or not isinstance(patch, str):
        raise ReportError("REPORT_CORRUPT", "已保存机械变更结构无效。")
    metadata, diff_offset = [], 0
    for row in rows:
        if (not isinstance(row, dict) or any(name not in row for name in (*_CHANGE_FIELDS, "diff", "after_text"))
                or not isinstance(row["path"], str) or not isinstance(row["diff"], str)
                or not (row["after_text"] is None or isinstance(row["after_text"], str))):
            raise ReportError("REPORT_CORRUPT", "已保存机械变更条目结构无效。")
        metadata.append({**{name: row[name] for name in _CHANGE_FIELDS}, "diff_offset": diff_offset,
                         "diff_chars": len(row["diff"]), "after_text_chars": len(row["after_text"] or ""),
                         "after_text_is_null": row["after_text"] is None})
        diff_offset += len(row["diff"])
    return rows, metadata, patch


def _page_contents(page: dict[str, Any], rows: list[dict[str, Any]], index: int, offset: int, page_size: int) -> tuple[int, int]:
    budget = 8000
    while index < len(rows):
        row, text = rows[index], rows[index]["after_text"]
        # 空文件与删除只使用原元数据标记；不合成内容或改变其摘要。
        if text is None or offset == len(text):
            index, offset = index + 1, 0
            continue
        if not budget or len(page["change_contents"]) >= page_size:
            break
        item = {"path": row["path"], "field": "after_text", "offset": offset, "text": "",
                "total_chars": len(text), "sha256": row["after_sha256"]}
        page["change_contents"].append(item)
        lower, upper = 0, min(budget, len(text) - offset)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            item["text"] = text[offset:offset + middle]
            if _page_fits(page):
                lower = middle
            else:
                upper = middle - 1
        if not lower:
            page["change_contents"].pop()
            break
        item["text"] = text[offset:offset + lower]
        budget, offset = budget - lower, offset + lower
    return index, offset


def paginate_report(
    report: dict[str, Any], job_id: str, cursor: str | None = None, page_size: int = 20,
    view: str = "full", *, execution_status: str | None = None,
) -> dict[str, Any]:
    if report.get("job_id") != job_id:
        raise ReportError("REPORT_JOB_MISMATCH", "报告不属于当前任务。")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ReportError("INVALID_PAGE_SIZE", "每页问题数量必须在 1 到 100 之间。")
    if not isinstance(view, str) or view not in {"summary", "patch", "files", "full"}:
        raise ReportError("INVALID_RESULT_VIEW", "结果视图必须是 summary、patch、files 或 full。")
    digest = hashlib.sha256(_json_bytes(report)).hexdigest()
    edit = report.get("mode") == "edit"
    fields = {
        "summary": ("offset",), "patch": ("changes_offset", "patch_offset"),
        "files": ("changes_offset", "content_index", "content_offset"),
        "full": _POSITION_FIELDS if edit else ("offset", "text_offset"),
    }[view]
    positions = dict.fromkeys(_POSITION_FIELDS, 0)
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ReportError("INVALID_CURSOR", "报告读取游标无效。")
        try:
            payload = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        except (ValueError, UnicodeEncodeError, binascii.Error, RecursionError) as exc:
            raise ReportError("INVALID_CURSOR", "报告读取游标无法解析。") from exc
        if (not isinstance(payload, dict) or payload.get("job_id") != job_id or payload.get("digest") != digest
                or type(payload.get("page_size")) is not int or payload.get("page_size") != page_size):
            raise ReportError("INVALID_CURSOR", "游标不匹配当前任务、报告内容或分页大小。")
        legacy = "version" not in payload and "view" not in payload
        if ((legacy and view != "full") or (not legacy and (
                type(payload.get("version")) is not int or payload.get("version") != 2 or payload.get("view") != view))):
            raise ReportError("INVALID_CURSOR", "游标不匹配当前结果视图或游标版本。")
        positions.update({name: payload.get(name) for name in fields})
        if any(type(positions[name]) is not int or positions[name] < 0 for name in fields):
            raise ReportError("INVALID_CURSOR", "报告读取游标偏移无效。")
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        raise ReportError("REPORT_CORRUPT", "已保存问题清单结构无效。")
    # 新视图不生成 Markdown，避免把整份补丁再次装进另一文本流。
    text = render_markdown(report) if view == "full" else ""
    rows, metadata, patch = _change_metadata(report) if edit and view != "summary" else ([], [], "")
    if (positions["offset"] > len(findings) or positions["text_offset"] > len(text)
            or positions["changes_offset"] > len(metadata) or positions["patch_offset"] > len(patch)
            or positions["content_index"] > len(rows)):
        raise ReportError("INVALID_CURSOR", "报告读取游标超出报告范围。")
    current_text = rows[positions["content_index"]]["after_text"] if positions["content_index"] < len(rows) else None
    if positions["content_offset"] > len(current_text or ""):
        raise ReportError("INVALID_CURSOR", "变更内容读取游标超出文件范围。")
    original_positions = dict(positions)
    metadata_included = view == "full" or cursor is None
    page: dict[str, Any] = {
        "job_id": job_id, "project_id": report.get("project_id"), "report_id": digest,
        "snapshot_id": report.get("snapshot_id"), "view": view,
        "execution_status": execution_status if execution_status is not None else "unknown",
        "quality": report_quality(report, execution_status), "metadata_included": metadata_included,
        "next_cursor": None,
    }
    if metadata_included:
        page.update({
            "review_scope": [_bounded(v, 500) for v in report.get("review_scope", [])[:25]],
            "review_scope_count": len(report.get("review_scope", [])),
            "summary": _bounded(report.get("summary"), 2000), "report_format": report.get("report_format"),
            "report_format_error": report.get("report_format_error"), "finish_reason": report.get("finish_reason"),
            "tests": "not_run", "usage": report.get("usage"), "timing": report.get("timing"), "error": report.get("error"),
            "limitations": [_bounded(v, 500) for v in report.get("limitations", [])[:10]],
            "limitations_count": len(report.get("limitations", [])),
        })
    if view != "full":
        page["mode"] = "edit" if edit else "review"
    if edit:
        page.update(mode="edit", edit_ready=report.get("edit_ready") is True, requires_main_agent_review=True,
                    source_applied=False, change_set_id=report.get("change_set_id"),
                    total_changed_files=len(report.get("changes", [])) if isinstance(report.get("changes"), list) else 0)
    if view in {"summary", "full"}:
        page.update(findings=[], total_findings=len(findings))
        if view == "summary":
            page["findings_offset"] = positions["offset"]
    if view == "full":
        page.update(changed_files=[], report_text="")
        if edit:
            page.update(report_text_offset=positions["text_offset"], total_report_text_chars=len(text))
    if (edit and view == "full") or view in {"patch", "files"}:
        page.update(changed_files=[], changes=[], changes_offset=positions["changes_offset"],
                    total_changes=len(metadata), total_changed_files=len(metadata))
    if (edit and view == "full") or view == "patch":
        page.update(patch_text="", patch_text_offset=positions["patch_offset"], total_patch_chars=len(patch))
    if (edit and view == "full") or view == "files":
        page["change_contents"] = []
    if not _page_fits(page):
        raise ReportError("REPORT_PAGE_LIMIT", "结果元数据超过单页大小上限，请缩小任务范围。")
    if view in {"summary", "full"}:
        positions["offset"] = _append_page_rows(page, "findings", findings, positions["offset"], page_size)
    if view == "full":
        positions["text_offset"] = _page_text(page, "report_text", text, positions["text_offset"])
    if "changes" in page:
        positions["changes_offset"] = _append_page_rows(page, "changes", metadata, positions["changes_offset"], page_size)
        page["changed_files"] = [row["path"] for row in page["changes"]]
        # 路径清单和元数据是同一页；将它计入预算后再决定后续文本流。
        while page["changes"] and not _page_fits(page):
            page["changes"].pop()
            page["changed_files"].pop()
            positions["changes_offset"] -= 1
    if "patch_text" in page:
        positions["patch_offset"] = _page_text(page, "patch_text", patch, positions["patch_offset"])
    if "change_contents" in page:
        positions["content_index"], positions["content_offset"] = _page_contents(
            page, rows, positions["content_index"], positions["content_offset"], page_size)
    limits = {"offset": len(findings), "text_offset": len(text), "changes_offset": len(metadata), "patch_offset": len(patch), "content_index": len(rows)}
    more = any(positions[name] < limits[name] for name in fields if name in limits)
    if more:
        if cursor is not None and all(positions[name] == original_positions[name] for name in fields):
            raise ReportError("REPORT_PAGE_LIMIT", "单条结果超过分页大小上限，请缩小任务范围。")
        payload = {"version": 2, "view": view, "job_id": job_id, "digest": digest, "page_size": page_size,
                   **{name: positions[name] for name in fields}}
        page["next_cursor"] = base64.urlsafe_b64encode(_json_bytes(payload)).decode("ascii")
        if len(page["next_cursor"]) > _CURSOR_RESERVE_BYTES:
            raise ReportError("REPORT_PAGE_LIMIT", "结果游标超过大小上限。")
    return page
