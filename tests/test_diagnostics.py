"""诊断跨进程不丢来源，并拒绝原始异常、绝对路径和未登记扫描字段。"""

import json
from pathlib import Path
import re
import sqlite3
import subprocess

import pytest

from harness_mcp.errors import (
    HarnessError,
    INTERNAL_ERROR_MESSAGE,
    MAX_DIAGNOSTIC_ITEMS,
    error_from_payload,
    error_payload,
    safe_error_details,
)
from harness_mcp.workspace import SnapshotError
from harness_mcp.report import ReportError


def test_legacy_business_error_preserves_exact_contract():
    error = HarnessError("JOB_NOT_FOUND", "作业不存在。")
    assert str(error) == "作业不存在。"
    assert error_payload(error) == {"code": "JOB_NOT_FOUND", "message": "作业不存在。"}
    assert error_payload(error_from_payload(error_payload(error))) == error_payload(error)


def test_report_pagination_business_error_keeps_its_known_code():
    error = ReportError("INVALID_CURSOR", "分页游标无效。")
    assert error_payload(error) == {"code": "INVALID_CURSOR", "message": "分页游标无效。"}
    first = error_payload(error, stage="shared_rpc")
    assert first["code"] == "INVALID_CURSOR"
    assert first["exception_type"] == "ReportError"
    assert error_payload(error_from_payload(first), stage="tool_request") == first


def test_earliest_id_stage_and_exception_type_survive_rpc_and_worker_rebuild():
    original = PermissionError("C:/Users/private/key.pem contains sk-sensitive-value")
    first = error_payload(original, stage="snapshot_prepare")
    assert re.fullmatch(r"[0-9a-f]{32}", first["diagnostic_id"])
    assert first["exception_type"] == "PermissionError"
    assert first["stage"] == "snapshot_prepare"
    assert first["code"] == "INTERNAL_ERROR"
    assert first["message"] == INTERNAL_ERROR_MESSAGE
    restored = error_from_payload(json.loads(json.dumps(first)))
    for later_stage in ("worker_response", "shared_rpc", "tool_request"):
        assert error_payload(restored, stage=later_stage) == first
        restored = error_from_payload(error_payload(restored, stage=later_stage))
    assert error_payload(original, stage="report_save") == first


def test_metadata_on_constructor_is_sanitized_and_has_stable_id():
    error = HarnessError("SNAPSHOT_FAILED", "快照失败。", stage="snapshot_prepare",
                         diagnostic_id="ABCDEF0123456789ABCDEF0123456789",
                         exception_type="FileNotFoundError")
    first = error_payload(error, stage="shared_rpc")
    assert first["diagnostic_id"] == "abcdef0123456789abcdef0123456789"
    assert first["stage"] == "snapshot_prepare"
    assert first["exception_type"] == "FileNotFoundError"


def test_details_constructor_does_not_preempt_first_actual_stage():
    error = HarnessError("SOURCE_SECRET_DETECTED", "源码检查失败。", details={"count": 2})
    assert error.diagnostic_id
    assert error_payload(error, stage="preflight")["stage"] == "preflight"
    assert error_payload(error, stage="shared_rpc")["stage"] == "preflight"


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("secret"), "RuntimeError"),
    (TimeoutError("Bearer secret"), "TimeoutError"),
    (FileNotFoundError("C:/private/path"), "FileNotFoundError"),
    (json.JSONDecodeError("secret", "secret source", 0), "JSONDecodeError"),
    (sqlite3.OperationalError("SELECT secret FROM private"), "SQLiteOperationalError"),
    (subprocess.TimeoutExpired("C:/private/command --token secret", 10), "ProcessTimeout"),
    (subprocess.CalledProcessError(1, "private command", output="secret"), "ProcessFailed"),
])
def test_known_exception_types_never_emit_raw_message(error, expected):
    payload = error_payload(error)
    assert payload["message"] == INTERNAL_ERROR_MESSAGE
    assert payload["stage"] == "unknown"
    assert payload["exception_type"] == expected
    assert set(payload) == {"code", "message", "stage", "exception_type", "diagnostic_id"}


def test_arbitrary_class_name_and_duck_typed_business_fields_are_not_public():
    custom = type("sk-private-classname", (RuntimeError,), {})
    error = custom("secret traceback C:/private/path")
    error.code = "EXTERNAL_ERROR"
    error.message = "external SDK raw private response"
    payload = error_payload(error, stage="model_execute")
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["exception_type"] == "UnexpectedException"
    assert "private" not in json.dumps(payload)


def test_unknown_exception_and_field_objects_are_not_stringified():
    class NoString:
        def __str__(self):
            raise AssertionError("must not inspect arbitrary objects")

    class Unknown(Exception):
        def __str__(self):
            raise AssertionError("must not inspect exception text")

    payload = error_payload(Unknown(NoString()), stage=NoString())
    assert payload["stage"] == "unknown"
    assert payload["exception_type"] == "UnexpectedException"
    assert safe_error_details({"path": NoString(), "line": NoString(), "rules": [NoString()]}) == {"rules": []}
    assert error_from_payload({"code": NoString(), "message": NoString()}).code == "PROTOCOL_ERROR"


def test_unregistered_metadata_does_not_escape_or_change_safe_business_contract():
    error = HarnessError("JOB_NOT_FOUND", "作业不存在。", stage="C:/private/token",
                         diagnostic_id="sk-private-id", exception_type="PrivateSDKClass")
    assert error_payload(error) == {"code": "JOB_NOT_FOUND", "message": "作业不存在。"}
    payload = error_payload(ValueError("secret"), stage="prompt with secret")
    assert payload["stage"] == "unknown"


@pytest.mark.parametrize("payload", [
    None, [], "raw secret", {}, {"code": "JOB_NOT_FOUND"},
    {"code": "BAD-CODE secret", "message": "raw"},
    {"code": "A" * 81, "message": "raw"},
    {"code": "JOB_NOT_FOUND", "message": {"secret": "raw"}},
    {"code": "JOB_NOT_FOUND", "message": "x" * 4097},
    {"code": "JOB_NOT_FOUND", "message": "raw\x00secret"},
])
def test_malformed_payload_becomes_fixed_protocol_error(payload):
    restored = error_from_payload(payload)
    assert error_payload(restored) == {
        "code": "PROTOCOL_ERROR", "message": "本地进程返回了无效错误信息。",
    }


def test_internal_payload_message_is_replaced_even_for_legacy_or_untrusted_sender():
    restored = error_from_payload({"code": "INTERNAL_ERROR", "message": "C:/secret sk-private"})
    payload = error_payload(restored, stage="shared_rpc")
    assert payload["message"] == INTERNAL_ERROR_MESSAGE
    assert payload["exception_type"] == "UnexpectedException"
    assert payload["stage"] == "shared_rpc"
    assert re.fullmatch(r"[0-9a-f]{32}", payload["diagnostic_id"])


def test_direct_internal_business_error_cannot_reintroduce_raw_exception_message():
    payload = error_payload(HarnessError("INTERNAL_ERROR", "C:/secret private exception"))
    assert payload["message"] == INTERNAL_ERROR_MESSAGE
    assert payload["exception_type"] == "UnexpectedException"
    assert payload["stage"] == "unknown"
    assert re.fullmatch(r"[0-9a-f]{32}", payload["diagnostic_id"])


@pytest.mark.parametrize("path", [
    "C:/private/secret", "C:\\private\\secret", "C:relative.txt", "/etc/private",
    "\\\\server\\private", "//server/private", "../private", "src/../private",
    "src/./file.py", "src//file.py", "src/file.py:secret", "src/\x00secret",
    "src/\u202esecret", "src/secret\nfile.py", "src/NUL.txt", "src/file. ",
    "src/file.", "", "x" * 1025, Path("src/file.py"),
])
def test_details_reject_unsafe_and_non_string_paths(path):
    assert "path" not in safe_error_details({"path": path, "unknown": "secret"})
    details = safe_error_details({"matches": [{"path": path, "line": 1, "rule": "SOURCE_PRIVATE_KEY"}]})
    assert details["matches"] == []
    assert details["truncated"] is True


def test_scan_details_only_keep_relative_location_and_fixed_rule():
    raw = {
        "matches": [
            {"path": "src\\设置.py", "line": 7, "rule": "SOURCE_HARDCODED_AUTH_LITERAL",
             "snippet": "password='secret'", "absolute_path": "C:/private/path"},
            {"path": "src/file.py", "line": 8, "rule": "sk-private-rule"},
            {"path": "src/file.py", "line": True, "rule": "SOURCE_KNOWN_SECRET"},
            {"path": "src/file.py", "line": 0, "rule": "SOURCE_KNOWN_SECRET"},
        ],
        "total_matches": 4, "truncated": False, "raw_exception": "secret", "traceback": "secret",
    }
    safe = safe_error_details(raw)
    assert safe == {
        "matches": [{"path": "src/设置.py", "line": 7, "rule": "SOURCE_HARDCODED_AUTH_LITERAL"}],
        "total_matches": 4, "truncated": True,
    }
    assert raw["matches"][0]["snippet"] == "password='secret'"
    assert "secret" not in json.dumps(safe)


def test_details_lists_are_bounded_and_retains_total():
    match = {"path": "src/main.py", "line": 3, "rule": "SOURCE_KNOWN_SECRET"}
    details = safe_error_details({"matches": [match] * (MAX_DIAGNOSTIC_ITEMS + 20), "truncated": False})
    assert len(details["matches"]) == MAX_DIAGNOSTIC_ITEMS
    assert details["total_matches"] == MAX_DIAGNOSTIC_ITEMS + 20
    assert details["truncated"] is True
    assert all(set(item) == {"path", "line", "rule"} for item in details["matches"])


def test_snapshot_error_details_are_filtered_without_mutating_exception():
    raw = {"path": "src/main.py", "limit_bytes": 2048, "excluded": [
        {"path": ".env", "reason": "sensitive_filename", "contents": "private"},
        {"path": "C:/private/secret", "reason": "configured_exclusion"},
        {"path": "src/file.py", "reason": "private token from an SDK"},
    ], "raw": "secret"}
    error = SnapshotError("SNAPSHOT_SIZE_LIMIT", "授权文件总大小超限。", raw)
    payload = error_payload(error, stage="snapshot_prepare")
    assert payload["code"] == "SNAPSHOT_SIZE_LIMIT"
    assert payload["exception_type"] == "SnapshotError"
    assert payload["details"] == {
        "path": "src/main.py", "limit_bytes": 2048,
        "excluded": [{"path": ".env", "reason": "sensitive_filename"}],
        "excluded_count": 3, "truncated": True,
    }
    assert error.details is raw
    assert raw["excluded"][0]["contents"] == "private"
    assert error_payload(error_from_payload(payload), stage="worker_response") == payload


def test_details_roundtrip_has_no_mutable_alias_or_unregistered_object():
    raw = {"matches": [{"path": "src/main.py", "line": 1, "rule": "SOURCE_PRIVATE_KEY"}],
           "total_matches": 1, "truncated": False, "credential": object()}
    error = HarnessError("SOURCE_SECRET_DETECTED", "源码检查失败。", details=raw)
    raw["matches"][0]["path"] = "C:/private/secret"
    first = error_payload(error, stage="preflight")
    first["details"]["matches"][0]["path"] = "C:/another/secret"
    second = error_payload(error)
    assert second["details"]["matches"][0]["path"] == "src/main.py"
    restored = error_from_payload(second)
    second["details"]["matches"].clear()
    assert error_payload(restored)["details"]["matches"]


@pytest.mark.parametrize("value", [True, -1, "2", 2.5, float("nan"), 2**64, object()])
def test_counts_lines_and_bool_flags_do_not_coerce_unknown_values(value):
    safe = safe_error_details({"line": value, "count": value, "limit_bytes": value,
                               "total_matches": value, "truncated": value})
    assert set(safe) <= {"truncated"}
    if type(value) is bool:
        assert safe == {"truncated": True}
    else:
        assert safe == {}


def test_unknown_detail_keys_and_dict_subclasses_are_not_accepted():
    class EvilDict(dict):
        def get(self, *_args):
            raise AssertionError("must not invoke arbitrary mapping methods")

    assert safe_error_details(EvilDict(path="src/main.py")) == {}
    assert safe_error_details({"traceback": "secret", "argv": "secret", "config_version": "secret", "restart_required": True}) == {}
    assert error_from_payload(EvilDict(code="JOB_NOT_FOUND", message="secret")).code == "PROTOCOL_ERROR"
