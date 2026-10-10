"""只保留有界诊断；凭证与常见认证字段从报告及错误中移除。"""

import copy
import hashlib
import json
import re


_AUTH_FIELD = re.compile(
    r'''(?i)(?P<label>(?P<quote>["']?)\b(?:api[_-]?key|access[_-]?token|authorization|password|secret)\b(?P=quote)\s*[=:]\s*)'''
    r'''(?:Bearer\s+)?(?:\[REDACTED\]|"(?:\\[\s\S]|[^"\\])*(?:"|\\?$)|'(?:\\[\s\S]|[^'\\])*(?:'|\\?$)|[^\s,;}"']+)'''
)
_ESCAPED_AUTH_FIELD = re.compile(
    r'''(?i)(?P<label>\\(?P<quote>["'])\b(?:api[_-]?key|access[_-]?token|authorization|password|secret)\b\\(?P=quote)\s*[=:]\s*)'''
    r'''(?:Bearer\s+)?(?:\[REDACTED\]|\\(?P=quote)(?:\\\\[\s\S]|[^\\]|\\(?!(?P=quote))[\s\S])*(?:\\(?P=quote)|$)|[^\s,;}"']+)'''
)
_SENSITIVE_KEY = re.compile(r"(?i)api.?key|credential|password|secret|token_value|authorization|access.?token")


def redact_auth_fields(text: str) -> str:
    """两条保存链共用认证字段规则；支持 JSON 键及带空格、转义引号的值。"""
    # 无效/截断的模型 JSON 可能包含仍未解码的 summary JSON 字符串。
    text = _ESCAPED_AUTH_FIELD.sub(lambda match: match["label"] + "[REDACTED]", text)
    return _AUTH_FIELD.sub(lambda match: match["label"] + "[REDACTED]", text)


def sensitive_key(key, secrets=()) -> bool:
    text = str(key)
    # 先检查完整键，不能截断后才找凭证，也不能改名后与正常键碰撞。
    return bool(_SENSITIVE_KEY.search(text)) or redact_text(text, secrets, max(len(text) * 2 + 1024, 4096)) != text


def redact_text(value: str, secrets=(), max_chars: int = 4096) -> str:
    text = str(value)
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    text = redact_auth_fields(text)
    return text[:max_chars]


def redact_value(value, secrets=()):
    if isinstance(value, str):
        return redact_text(value, secrets, len(value))
    if isinstance(value, dict):
        return {key: redact_value(item, secrets) for key, item in value.items() if not sensitive_key(key, secrets)}
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    return value


_SOURCE_EXPORT_FIELDS = frozenset({
    "changes", "patch_text", "change_contents", "changed_files", "change_set_id", "report_text",
})


def _source_projection_digest(value, fields):
    return hashlib.sha256(json.dumps(
        {key: value[key] for key in fields}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


class _VerifiedSourcePayload(dict):
    """仅本进程保存的来源能力；普通 JSON 同名字段不会产生此标记。"""

    def __init__(self, value, fields):
        super().__init__(copy.deepcopy(dict(value)))
        self._source_fields = frozenset(fields)
        self._source_digest = _source_projection_digest(self, self._source_fields)


def _mark_verified_source_payload(value, fields):
    """调用者须已核对 Snapshot 或 DB 报告摘要及实际分页；不是公共输入解析器。"""
    fields = frozenset(fields)
    if not fields <= _SOURCE_EXPORT_FIELDS or not fields <= set(value):
        raise ValueError("Invalid verified source projection")
    return _VerifiedSourcePayload(value, fields)


def redact_verified_source_payload(value, secrets=()):
    """强脱敏自然字段，保持已验证机械字节；确定凭证命中则拒绝而非改写。"""
    if not isinstance(value, _VerifiedSourcePayload):
        return redact_value(value, secrets)
    from .errors import HarnessError
    from .source_secrets import scan_deterministic
    try:
        fields = value._source_fields
        valid = (fields <= _SOURCE_EXPORT_FIELDS and fields <= set(value)
                 and _source_projection_digest(value, fields) == value._source_digest)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise HarnessError("EDIT_CHANGES_INVALID", "机械数据已变化，不能使用既有来源标记导出。")
    protected = {key: value[key] for key in fields}
    stack = list(protected.values())
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if scan_deterministic(item, secrets):
                raise HarnessError("EDIT_SECRET_DETECTED", "已验证机械数据命中确定凭证规则，拒绝导出。")
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    natural = redact_value({key: item for key, item in value.items() if key not in fields}, secrets)
    return _mark_verified_source_payload({**natural, **protected}, fields)

