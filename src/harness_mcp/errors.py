"""稳定业务错误与有界诊断；绝不从未知异常文本生成公开内容。"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unicodedata
import uuid


INTERNAL_ERROR_MESSAGE = "本地任务处理失败，请检查脱敏诊断。"
PROTOCOL_ERROR_MESSAGE = "本地进程返回了无效错误信息。"
MAX_DIAGNOSTIC_ITEMS = 64
DIAGNOSTIC_STAGES = frozenset({
    "unknown", "startup", "config_load", "shared_connect", "shared_attach",
    "shared_rpc", "tool_request", "preflight", "job_create", "job_directory",
    "snapshot_prepare", "snapshot_validate", "model_execute",
    "post_model_snapshot_validate", "changes_collect", "report_build",
    "report_save", "finish_commit", "worker_start", "worker_response",
    "session_create", "session_submit", "completion_delivery", "completion_ack",
    "service_info",
})
SOURCE_SCAN_RULES = frozenset({
    "SOURCE_KNOWN_SECRET", "SOURCE_PROVIDER_TOKEN", "SOURCE_PRIVATE_KEY",
    "SOURCE_CREDENTIAL_URL", "SOURCE_HARDCODED_AUTH_LITERAL",
    "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN",
})
SNAPSHOT_EXCLUSION_RULES = frozenset({
    "dependency_or_cache", "sensitive_filename", "private_key_or_credential",
    "binary_or_backup", "configured_exclusion", "tracked_file_deleted",
    "non_regular_file_or_submodule", "binary_content",
})
_KNOWN_EXCEPTION_TYPES = {
    RuntimeError: "RuntimeError", ValueError: "ValueError", TypeError: "TypeError",
    KeyError: "KeyError", IndexError: "IndexError", AttributeError: "AttributeError",
    ImportError: "ImportError", ModuleNotFoundError: "ModuleNotFoundError",
    AssertionError: "AssertionError", MemoryError: "MemoryError",
    RecursionError: "RecursionError", NotImplementedError: "NotImplementedError",
    OSError: "OSError", PermissionError: "PermissionError",
    FileNotFoundError: "FileNotFoundError", FileExistsError: "FileExistsError",
    IsADirectoryError: "IsADirectoryError", NotADirectoryError: "NotADirectoryError",
    TimeoutError: "TimeoutError", ConnectionError: "ConnectionError",
    ConnectionAbortedError: "ConnectionAbortedError",
    ConnectionRefusedError: "ConnectionRefusedError",
    ConnectionResetError: "ConnectionResetError", BrokenPipeError: "BrokenPipeError",
    UnicodeDecodeError: "UnicodeDecodeError", UnicodeEncodeError: "UnicodeEncodeError",
    json.JSONDecodeError: "JSONDecodeError", sqlite3.Error: "SQLiteError",
    sqlite3.OperationalError: "SQLiteOperationalError",
    sqlite3.IntegrityError: "SQLiteIntegrityError",
    sqlite3.DatabaseError: "SQLiteDatabaseError",
    subprocess.TimeoutExpired: "ProcessTimeout",
    subprocess.CalledProcessError: "ProcessFailed",
}
_PUBLIC_EXCEPTION_TYPES = frozenset(_KNOWN_EXCEPTION_TYPES.values()) | {
    "HarnessError", "SnapshotError", "ReportError", "DesktopLaunchError", "UnexpectedException",
}
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_DIAGNOSTIC_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
_WINDOWS_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z")
_CACHE_ATTRIBUTE = "_harness_public_diagnostic"


def _allowed_string(value, allowed):
    return value if type(value) is str and value in allowed else None


def _diagnostic_id(value):
    return value.lower() if type(value) is str and _DIAGNOSTIC_ID.fullmatch(value) else None


def _integer(value, *, positive=False):
    # bool 不能冒充行号和计数；不把字符串、对象转换成公开值。
    return value if type(value) is int and (1 if positive else 0) <= value <= 2**63 - 1 else None


def _plain_fields(value):
    return {key: item for key, item in value.items() if type(key) is str} if type(value) is dict else {}


def _relative_path(value):
    if type(value) is not str or not value or len(value) > 1024:
        return None
    value = value.replace("\\", "/")
    if value.startswith("/") or ":" in value or any(unicodedata.category(char).startswith("C") for char in value):
        return None
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " ")) or _WINDOWS_DEVICE.fullmatch(part) for part in parts):
        return None
    return value


def safe_error_details(value) -> dict:
    """只复制白名单元数据；忽略代码片段、原始异常、任意对象及不安全路径。"""
    if type(value) is not dict:
        return {}
    value = _plain_fields(value)
    result = {}
    path = _relative_path(value.get("path"))
    if path is not None:
        result["path"] = path
    for key in ("line", "limit_bytes", "count", "total_matches", "excluded_count"):
        item = _integer(value.get(key), positive=key in {"line", "limit_bytes"})
        if item is not None:
            result[key] = item
    if type(value.get("truncated")) is bool:
        result["truncated"] = value["truncated"]
    rule = _allowed_string(value.get("rule"), SOURCE_SCAN_RULES | SNAPSHOT_EXCLUSION_RULES)
    if rule is not None:
        result["rule"] = rule
    rules = value.get("rules")
    if type(rules) is list:
        result["rules"] = list(dict.fromkeys(
            item for item in rules[:MAX_DIAGNOSTIC_ITEMS]
            if _allowed_string(item, SOURCE_SCAN_RULES | SNAPSHOT_EXCLUSION_RULES) is not None
        ))
        if len(rules) > MAX_DIAGNOSTIC_ITEMS:
            result["truncated"] = True
    matches = value.get("matches")
    if type(matches) is list:
        safe_matches = []
        for match in matches[:MAX_DIAGNOSTIC_ITEMS]:
            if type(match) is not dict:
                continue
            match = _plain_fields(match)
            path = _relative_path(match.get("path"))
            line = _integer(match.get("line"), positive=True)
            rule = _allowed_string(match.get("rule"), SOURCE_SCAN_RULES)
            if path is not None and line is not None and rule is not None:
                safe_matches.append({"path": path, "line": line, "rule": rule})
        result["matches"] = safe_matches
        result.setdefault("total_matches", len(matches))
        if len(safe_matches) < len(matches):
            result["truncated"] = True
    excluded = value.get("excluded")
    if type(excluded) is list:
        safe_excluded = []
        for entry in excluded[:MAX_DIAGNOSTIC_ITEMS]:
            if type(entry) is not dict:
                continue
            entry = _plain_fields(entry)
            path = _relative_path(entry.get("path"))
            reason = _allowed_string(entry.get("reason"), SNAPSHOT_EXCLUSION_RULES)
            if path is not None and reason is not None:
                safe_excluded.append({"path": path, "reason": reason})
        result["excluded"] = safe_excluded
        result.setdefault("excluded_count", len(excluded))
        if len(safe_excluded) < len(excluded):
            result["truncated"] = True
    return result


class HarnessError(Exception):
    def __init__(self, code: str, message: str, *, stage=None, diagnostic_id=None,
                 exception_type=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = _allowed_string(stage, DIAGNOSTIC_STAGES)
        self.diagnostic_id = _diagnostic_id(diagnostic_id)
        self.exception_type = _allowed_string(exception_type, _PUBLIC_EXCEPTION_TYPES)
        self.details = safe_error_details(details)
        if self.stage or self.diagnostic_id or self.exception_type or self.details:
            self.diagnostic_id = self.diagnostic_id or uuid.uuid4().hex
            self.exception_type = self.exception_type or ("UnexpectedException" if type(code) is str and code == "INTERNAL_ERROR" else "HarnessError")


def _business_values(error):
    # 只信任本项目的业务异常；SDK 异常的同名属性可能包含原始响应或凭证。
    from .desktop_launcher import DesktopLaunchError
    from .report import ReportError
    from .workspace import SnapshotError
    if not isinstance(error, (HarnessError, SnapshotError, ReportError, DesktopLaunchError)):
        return None
    code, message = error.code, error.message
    if type(code) is not str or not _CODE.fullmatch(code) or type(message) is not str:
        return None
    return code, message


def _exception_type(error):
    from .desktop_launcher import DesktopLaunchError
    from .report import ReportError
    from .workspace import SnapshotError
    if isinstance(error, HarnessError):
        return "UnexpectedException" if type(error.code) is str and error.code == "INTERNAL_ERROR" else "HarnessError"
    if isinstance(error, SnapshotError):
        return "SnapshotError"
    if isinstance(error, ReportError):
        return "ReportError"
    if isinstance(error, DesktopLaunchError):
        return "DesktopLaunchError"
    # 仅映射明确已知的类，不公开自定义类名（类名也可能含密钥或本机路径）。
    return next((name for known_type, name in _KNOWN_EXCEPTION_TYPES.items() if type(error) is known_type), "UnexpectedException")


def _stored_diagnostic(error):
    try:
        attributes = BaseException.__getattribute__(error, "__dict__")
    except Exception:
        return {}
    value = attributes.get(_CACHE_ATTRIBUTE) if type(attributes) is dict else None
    if type(value) is not dict:
        return {}
    value = _plain_fields(value)
    result = {}
    for key, validator in (("diagnostic_id", _diagnostic_id),
                           ("stage", lambda item: _allowed_string(item, DIAGNOSTIC_STAGES)),
                           ("exception_type", lambda item: _allowed_string(item, _PUBLIC_EXCEPTION_TYPES))):
        item = validator(value.get(key))
        if item is not None:
            result[key] = item
    return result


def error_payload(error: Exception, *, stage=None) -> dict:
    business = _business_values(error)
    result = {"code": business[0], "message": business[1]} if business else {
        "code": "INTERNAL_ERROR", "message": INTERNAL_ERROR_MESSAGE,
    }
    internal = result["code"] == "INTERNAL_ERROR"
    if internal:
        result["message"] = INTERNAL_ERROR_MESSAGE
    details = safe_error_details(getattr(error, "details", None)) if business else {}
    diagnostic = _stored_diagnostic(error)
    if isinstance(error, HarnessError):
        for key, validator in (("diagnostic_id", _diagnostic_id),
                               ("stage", lambda item: _allowed_string(item, DIAGNOSTIC_STAGES)),
                               ("exception_type", lambda item: _allowed_string(item, _PUBLIC_EXCEPTION_TYPES))):
            item = validator(getattr(error, key, None))
            if item is not None:
                diagnostic.setdefault(key, item)
    safe_stage = _allowed_string(stage, DIAGNOSTIC_STAGES)
    if internal or diagnostic or safe_stage or details:
        if "diagnostic_id" not in diagnostic:
            diagnostic["diagnostic_id"] = uuid.uuid4().hex
        diagnostic.setdefault("stage", safe_stage or "unknown")
        diagnostic.setdefault("exception_type", _exception_type(error))
        try:
            BaseException.__setattr__(error, _CACHE_ATTRIBUTE, diagnostic.copy())
        except Exception:
            pass
        result.update(diagnostic)
    if details:
        result["details"] = details
    return result


def error_from_payload(payload: dict) -> HarnessError:
    """重建可信本机 RPC/worker 错误，保留最早诊断；无效主体成为固定协议错误。"""
    if type(payload) is not dict:
        return HarnessError("PROTOCOL_ERROR", PROTOCOL_ERROR_MESSAGE)
    payload = _plain_fields(payload)
    code, message = payload.get("code"), payload.get("message")
    if (type(code) is not str or not _CODE.fullmatch(code) or type(message) is not str
            or not message or len(message) > 4096 or any(ord(char) < 32 and char not in "\n\t" for char in message)):
        return HarnessError("PROTOCOL_ERROR", PROTOCOL_ERROR_MESSAGE)
    if code == "INTERNAL_ERROR":
        message = INTERNAL_ERROR_MESSAGE
    return HarnessError(code, message, stage=payload.get("stage"),
                        diagnostic_id=payload.get("diagnostic_id"),
                        exception_type=payload.get("exception_type") if code != "INTERNAL_ERROR" else (
                            _allowed_string(payload.get("exception_type"), _PUBLIC_EXCEPTION_TYPES) or "UnexpectedException"),
                        details=payload.get("details"))

