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


def _verified_changes(value: Any, snapshot: Snapshot, max_output_bytes: int, secrets: tuple[str, ...]) -> dict[str, Any]:
    """机械变更必须完整且可核对；禁止截断或脱敏后仍沿用原内容摘要。"""
    from .redaction import redact_text as diagnostic_redact
    from .edit_workspace import EditWorkspaceError, _diff

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
        confirmed.append(dict(raw))
    digest = hashlib.sha256(_json_bytes({"snapshot_id": snapshot.snapshot_id, "changes": confirmed})).hexdigest()
    if value.get("change_set_id") != digest or patch != "".join(row["diff"] for row in confirmed):
        raise invalid
    # 同时覆盖报告脱敏和宿主最后的诊断脱敏。任何改变均拒绝，不能改写可重放补丁。
    for text in (patch, digest, *(item for row in confirmed for item in row.values() if isinstance(item, str))):
        if redact_text(text, secrets) != text or diagnostic_redact(text, secrets, len(text)) != text:
            raise ReportError("EDIT_SECRET_DETECTED", "机械变更含凭证或敏感认证内容，不能导出可合入补丁。")
    return {"changed_files": [row["path"] for row in confirmed], "changes": confirmed,
            "patch_text": patch, "change_set_id": digest}


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


def _edit_page(report: dict[str, Any], positions: tuple[int, int, int, int], page_size: int) -> tuple[dict[str, Any], tuple[int, int, int, int], bool]:
    """变更元数据、补丁、新文本分别分页，所有偏移共用报告游标。"""
    changes = report.get("changes", [])
    patch = report.get("patch_text", "")
    if not isinstance(changes, list) or not isinstance(patch, str):
        raise ReportError("REPORT_CORRUPT", "已保存机械变更结构无效。")
    changes_offset, patch_offset, content_index, content_offset = positions
    if changes_offset > len(changes) or patch_offset > len(patch) or content_index > len(changes):
        raise ReportError("INVALID_CURSOR", "变更读取游标超出报告范围。")
    current_text = changes[content_index].get("after_text") if content_index < len(changes) else None
    if content_offset > len(current_text or ""):
        raise ReportError("INVALID_CURSOR", "变更内容读取游标超出文件范围。")
    metadata = []
    diff_offset = 0
    for row in changes:
        metadata.append({
            **{name: row[name] for name in ("path", "change_type", "before_sha256", "after_sha256", "before_size_bytes", "after_size_bytes")},
            "diff_offset": diff_offset, "diff_chars": len(row["diff"]),
            "after_text_chars": len(row["after_text"] or ""), "after_text_is_null": row["after_text"] is None,
        })
        diff_offset += len(row["diff"])
    page_changes = []
    next_changes_offset = changes_offset
    for row in metadata[changes_offset:changes_offset + page_size]:
        candidate = [*page_changes, row]
        if page_changes and len(_json_bytes({"changes": candidate})) > 65536:
            break
        page_changes = candidate
        next_changes_offset += 1
    next_patch_offset = min(patch_offset + 8000, len(patch))
    # 空新文件由 metadata 的 0 字符、非 null 标记表达；删除由 null 表达。
    content_budget = 8000
    contents = []
    while content_index < len(changes):
        row = changes[content_index]
        after = row["after_text"]
        if after is None or content_offset == len(after):
            content_index += 1
            content_offset = 0
            continue
        if not content_budget or len(contents) >= page_size:
            break
        end = min(content_offset + content_budget, len(after))
        chunk = after[content_offset:end]
        contents.append({"path": row["path"], "field": "after_text", "offset": content_offset,
                         "text": chunk, "total_chars": len(after), "sha256": row["after_sha256"]})
        content_budget -= len(chunk)
        content_offset = end
    next_positions = (next_changes_offset, next_patch_offset, content_index, content_offset)
    more = next_changes_offset < len(changes) or next_patch_offset < len(patch) or content_index < len(changes)
    return {
        "mode": "edit", "edit_ready": report.get("edit_ready") is True,
        "requires_main_agent_review": True, "source_applied": False,
        "change_set_id": report.get("change_set_id"),
        "changed_files": [row["path"] for row in page_changes], "total_changed_files": len(changes),
        "changes": page_changes, "changes_offset": changes_offset, "total_changes": len(changes),
        "patch_text": patch[patch_offset:next_patch_offset], "patch_text_offset": patch_offset, "total_patch_chars": len(patch),
        "change_contents": contents,
    }, next_positions, more


def paginate_report(report: dict[str, Any], job_id: str, cursor: str | None = None, page_size: int = 20) -> dict[str, Any]:
    if report.get("job_id") != job_id:
        raise ReportError("REPORT_JOB_MISMATCH", "报告不属于当前任务。")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ReportError("INVALID_PAGE_SIZE", "每页问题数量必须在 1 到 100 之间。")
    digest = hashlib.sha256(_json_bytes(report)).hexdigest()
    offset, text_offset = 0, 0
    edit = report.get("mode") == "edit"
    positions = (0, 0, 0, 0)
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ReportError("INVALID_CURSOR", "报告读取游标无效。")
        try:
            payload = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        except (ValueError, UnicodeEncodeError, binascii.Error, RecursionError) as exc:
            raise ReportError("INVALID_CURSOR", "报告读取游标无法解析。") from exc
        if not isinstance(payload, dict) or payload.get("job_id") != job_id or payload.get("digest") != digest or payload.get("page_size") != page_size:
            raise ReportError("INVALID_CURSOR", "游标不匹配当前任务、报告内容或分页大小。")
        offset, text_offset = payload.get("offset"), payload.get("text_offset")
        if edit:
            positions = tuple(payload.get(name) for name in ("changes_offset", "patch_offset", "content_index", "content_offset"))
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (offset, text_offset, *positions)):
            raise ReportError("INVALID_CURSOR", "报告读取游标偏移无效。")
    findings = report.get("findings", [])
    text = render_markdown(report)
    if offset > len(findings) or text_offset > len(text):
        raise ReportError("INVALID_CURSOR", "报告读取游标超出报告范围。")
    next_offset = offset
    page_findings: list[dict[str, Any]] = []
    for finding in findings[offset:offset + page_size]:
        candidate = [*page_findings, finding]
        if page_findings and len(_json_bytes({"findings": candidate})) > 65536:
            break
        page_findings = candidate
        next_offset += 1
    next_text_offset = min(text_offset + 8000, len(text))
    edit_values, edit_more = {}, False
    if edit:
        edit_values, positions, edit_more = _edit_page(report, positions, page_size)
        edit_values.update(report_text_offset=text_offset, total_report_text_chars=len(text))
    next_cursor = None
    if next_offset < len(findings) or next_text_offset < len(text) or edit_more:
        payload = {"job_id": job_id, "digest": digest, "page_size": page_size, "offset": next_offset, "text_offset": next_text_offset}
        if edit:
            payload.update(zip(("changes_offset", "patch_offset", "content_index", "content_offset"), positions))
        next_cursor = base64.urlsafe_b64encode(_json_bytes(payload)).decode("ascii")
    # 返回摘要/一页问题/一页文本，避免原文和完整文件清单撑满主 AI 上下文。
    return {
        "job_id": job_id, "project_id": report.get("project_id"), "report_id": digest,
        "snapshot_id": report.get("snapshot_id"), "review_scope": [_bounded(v, 500) for v in report.get("review_scope", [])[:25]],
        "review_scope_count": len(report.get("review_scope", [])),
        "summary": _bounded(report.get("summary"), 2000), "report_format": report.get("report_format"),
        "report_format_error": report.get("report_format_error"), "finish_reason": report.get("finish_reason"),
        "findings": page_findings, "total_findings": len(findings),
        "changed_files": [], "tests": "not_run", "usage": report.get("usage"),
        "timing": report.get("timing"), "error": report.get("error"),
        "limitations": [_bounded(v, 500) for v in report.get("limitations", [])[:10]],
        "limitations_count": len(report.get("limitations", [])),
        "report_text": text[text_offset:next_text_offset], "next_cursor": next_cursor,
        **edit_values,
    }
