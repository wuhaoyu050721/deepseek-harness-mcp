"""只保留有界诊断；凭证与常见认证字段从报告及错误中移除。"""

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

