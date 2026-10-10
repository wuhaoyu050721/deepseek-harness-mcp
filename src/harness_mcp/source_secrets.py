"""只识别源码中的凭证候选，不脱敏或执行源码，也不回传命中值。

字段检查使用有界语言词法信息；它不提供跨变量的数据流或完整语言语义分析。
补丁只能使用 scan_deterministic，不能作为带 +/- 前缀的完整源码解析。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Iterable


_FIELDS = frozenset({"apikey", "accesstoken", "authorization", "password", "secret",
                     "clientsecret", "authtoken", "tokenvalue"})
_NULLS = frozenset({"null", "none", "undefined", "true", "false"})
_PROVIDERS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----\s+[A-Za-z0-9+/=\r\n]{32,}")
_CREDENTIAL_URL = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
_HEADER = re.compile(r"(?i)^authorization\s*:\s*(.*)$", re.S)
_SCHEME_ONLY = re.compile(r"(?i)\s*(?:(?:authorization\s*:\s*)?(?:bearer|basic)\s*|authorization\s*:\s*)")
_LOOKUPS = frozenset({"getenv", "env", "config", "os.getenv", "os.environ.get", "get",
                      "getenvrequired", "getenvoptional"})
_REGEX_CALLS = frozenset({"re.compile", "re.match", "re.search", "re.fullmatch", "regexp",
                          "preg_match", "preg_match_all", "preg_replace", "preg_split"})
_UNCERTAIN = "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN"
_HARDCODED = "SOURCE_HARDCODED_AUTH_LITERAL"
_MAX_TOKENS = 100_000


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int
    closed: bool = True
    quote: str = ""
    prefix: str = ""


def _known(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True))


def _path(relative_path: str, secrets: tuple[str, ...]) -> str:
    if (not isinstance(relative_path, str) or not relative_path or PureWindowsPath(relative_path).drive
            or relative_path.startswith(("/", "\\")) or ":" in relative_path
            or any(ord(char) < 32 or ord(char) == 127 for char in relative_path)):
        raise ValueError("Source scan requires a safe relative path")
    normalized = relative_path.replace("\\", "/")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("Source scan requires a safe relative path")
    if any(secret in normalized for secret in secrets) or _PROVIDERS.search(normalized):
        return "redacted-source"
    return normalized


def _text(content: str | bytes) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, "\x00" not in content
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
            return text, "\x00" not in text
        except UnicodeDecodeError:
            return content.decode("utf-8", errors="replace"), False
    raise TypeError("Source scan requires text or bytes")


def _finding(path: str, text: str, start: int, rule: str) -> dict:
    return {"path": path, "line": text.count("\n", 0, start) + 1, "rule": rule}


def _deduplicate(findings: list[dict]) -> list[dict]:
    return [dict(path=path, line=line, rule=rule) for path, line, rule in
            sorted({(item["path"], item["line"], item["rule"]) for item in findings})]


def scan_deterministic(text: str | bytes, known_secrets=(), relative_path: str = "source") -> list[dict]:
    """精确已知凭证与确定性特征检查；不使用认证字段或通用 Bearer 值正则。"""
    secrets = _known(known_secrets)
    path = _path(relative_path, secrets)
    value, _ = _text(text)
    findings = []
    for secret in secrets:
        offset = 0
        while (start := value.find(secret, offset)) >= 0:
            findings.append(_finding(path, value, start, "SOURCE_KNOWN_SECRET"))
            offset = start + len(secret)
    for rule, pattern in (("SOURCE_PROVIDER_TOKEN", _PROVIDERS),
                          ("SOURCE_PRIVATE_KEY", _PRIVATE_KEY),
                          ("SOURCE_CREDENTIAL_URL", _CREDENTIAL_URL)):
        findings.extend(_finding(path, value, match.start(), rule) for match in pattern.finditer(value))
    return _deduplicate(findings)


def _language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".php", ".phtml", ".inc"}:
        return "php"
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue"}:
        return "javascript"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".conf", ".txt", ".env", ""}:
        return "data"
    return "unsupported"


def _lex(text: str, language: str, *, offset: int = 0) -> list[_Token]:
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        if len(tokens) >= _MAX_TOKENS:
            tokens.append(_Token("limit", "", offset + index, offset + index, False))
            break
        start = index
        char = text[index]
        if char in " \t\r\f":
            index += 1
            continue
        if char == "\\" and text.startswith(("\\\n", "\\\r\n"), index):
            index += 3 if text.startswith("\\\r\n", index) else 2
            continue
        if char == "\n":
            tokens.append(_Token("newline", "\n", offset + index, offset + index + 1))
            index += 1
            continue
        if text.startswith(("<?php", "<?", "?>"), index) and language == "php":
            index += 5 if text.startswith("<?php", index) else 2
            continue
        comment = ((char == "#" and language in {"php", "python", "data"})
                   or (text.startswith("//", index) and language in {"php", "javascript"}))
        if comment:
            end = text.find("\n", index)
            index = length if end < 0 else end
            continue
        if text.startswith("/*", index) and language in {"php", "javascript"}:
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if language == "php" and text.startswith("<<<", index):
            opening = re.match(r"<<<[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[ \t]*\r?\n", text[index:])
            if opening:
                body = index + opening.end()
                closing = re.search(r"(?m)^[ \t]*" + re.escape(opening[2]) + r"(?=[;,\r\n]|$)", text[body:])
                end = length if closing is None else body + closing.start()
                index = length if closing is None else body + closing.end()
                tokens.append(_Token("string", text[body:end], offset + start, offset + index,
                                     closing is not None, "nowdoc" if opening[1] == "'" else "heredoc"))
                continue
            tokens.append(_Token("unknown", "<<<", offset + index, offset + index + 3, False))
            index += 3
            continue
        prefix = ""
        if language == "python":
            match = re.match(r"(?i)([rubf]{1,3})(?=['\"])", text[index:])
            if match:
                prefix = match[1]
                index += len(prefix)
                char = text[index]
        if char in "'\"" or (char == "`" and language == "javascript"):
            quote = char * 3 if language == "python" and text.startswith(char * 3, index) else char
            index += len(quote)
            body = index
            while index < length:
                if text[index] == "\\":
                    index += 2
                elif text.startswith(quote, index):
                    break
                else:
                    index += 1
            closed = index < length
            end = min(index, length)
            index = min(length, index + len(quote)) if closed else length
            tokens.append(_Token("string", text[body:end], offset + start, offset + index,
                                 closed, quote, prefix))
            continue
        # JS 正则是代码模式，不能把模式内部的 Authorization: 当作真实认证头。
        previous = next((token for token in reversed(tokens) if token.kind != "newline"), None)
        if (language == "javascript" and char == "/" and (previous is None
                or previous.value in {"=", "(", "[", "{", ":", ",", "=>", "!", "return"})):
            index += 1
            in_class = False
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == "[":
                    in_class = True
                elif text[index] == "]":
                    in_class = False
                elif text[index] == "/" and not in_class:
                    break
                elif text[index] == "\n":
                    break
                index += 1
            closed = index < length and text[index] == "/"
            if closed:
                index += 1
                while index < length and text[index].isalpha():
                    index += 1
            tokens.append(_Token("regex", "", offset + start, offset + index, closed))
            continue
        match = re.match(r"\$?[A-Za-z_][A-Za-z0-9_]*", text[index:])
        if match:
            index += match.end()
            tokens.append(_Token("name", match[0], offset + start, offset + index))
            continue
        match = re.match(r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)", text[index:])
        if match:
            index += match.end()
            tokens.append(_Token("number", match[0], offset + start, offset + index))
            continue
        operator = next((value for value in ("**=", "||=", "&&=", "??=", ".=", "+=", "-=", "*=", "/=", "%=",
                                                "?->", "===", "!==", "=>", "->", "::", "??", "?:",
                                                "==", "!=", "<=", ">=", "&&", "||", ":=", "**")
                         if text.startswith(value, index)), char)
        index += len(operator)
        kind = "operator" if operator[0] in "=:+-*/%!?.,;()[]{}<>@|&\\" else "unknown"
        tokens.append(_Token(kind, operator, offset + start, offset + index))
    return tokens


def _decode(value: str, quote: str, prefix: str, language: str) -> str:
    if language == "python" and "f" not in prefix.lower() and quote not in {"heredoc", "nowdoc"}:
        try:
            decoded = ast.literal_eval(prefix + quote + value + quote)
            if isinstance(decoded, bytes):
                return decoded.decode("utf-8", errors="replace")
            if isinstance(decoded, str):
                return decoded
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            return value
    if quote in {"nowdoc", "heredoc"} or "r" in prefix.lower():
        return value
    if language == "php" and quote == "'":
        return re.sub(r"\\(['\\])", r"\1", value)
    def replace(match):
        escaped = match[1]
        if escaped.startswith(("u", "x")):
            try:
                return chr(int(escaped[1:], 16))
            except ValueError:
                return match[0]
        return {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"', "$": "$", "`": "`"}.get(escaped, match[0])
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", replace, value, flags=re.S)


def _string_parts(token: _Token, language: str) -> tuple[str, list[str], bool]:
    """保留常量部分，另检查插值表达式内的固定回退，避免动态外观绕过。"""
    value = token.value
    expressions = []
    uncertain = not token.closed
    if language == "php" and token.quote in {'"', "heredoc"}:
        variable = re.compile(r"(?<!\\)(?:\{\$[A-Za-z_]\w*(?:->\w+|\[[^\]\n]+\])*\}|\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*(?:->\w+|\[[^\]\n]+\])*)")
        value = variable.sub(lambda match: expressions.append(match[0]) or "", value)
        if re.search(r"(?<!\\)\$", value):
            uncertain = True
    elif ((language == "javascript" and token.quote == "`")
          or (language == "python" and "f" in token.prefix.lower())):
        opening = "${" if language == "javascript" else "{"
        chunks = []
        index = 0
        while index < len(value):
            if value[index] == "\\" and index + 1 < len(value):
                chunks.append(value[index:index + 2])
                index += 2
                continue
            if language == "python" and value[index:index + 2] in {"{{", "}}"}:
                chunks.append(value[index])
                index += 2
                continue
            if not value.startswith(opening, index):
                chunks.append(value[index])
                index += 1
                continue
            begin = index + len(opening)
            cursor, depth = begin, 1
            while cursor < len(value) and depth:
                if value[cursor] in "'\"`":
                    quote = value[cursor]
                    cursor += 1
                    while cursor < len(value) and value[cursor] != quote:
                        cursor += 2 if value[cursor] == "\\" else 1
                elif value[cursor] == "{":
                    depth += 1
                elif value[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth:
                uncertain = True
                chunks.append(value[index:])
                break
            expressions.append(value[begin:cursor - 1])
            index = cursor
        value = "".join(chunks)
    return _decode(value, token.quote, token.prefix, language), expressions, uncertain


def _field_name(value: str) -> str | None:
    name = re.sub(r"[_-]", "", value.lstrip("$")).casefold()
    return name if name in _FIELDS else None


def _field(token: _Token, language: str) -> str | None:
    value = token.value
    if token.kind == "string":
        value, expressions, uncertain = _string_parts(token, language)
        if expressions or uncertain:
            return None
    elif token.kind != "name":
        return None
    return _field_name(value)


def _source_field(tokens: list[_Token], start: int, language: str) -> tuple[str | None, int]:
    """折叠字段名中的静态字符串拼接；不求值变量或调用，不做数据流追踪。"""
    if language not in {"php", "python", "javascript"}:
        return _field(tokens[start], language), start + 1
    operator = "." if language == "php" else "+"

    def skip_lines(cursor):
        while cursor < len(tokens) and tokens[cursor].kind == "newline":
            cursor += 1
        return cursor

    def atom(cursor: int, depth: int):
        cursor = skip_lines(cursor)
        if cursor >= len(tokens) or depth > 16:
            return None
        token = tokens[cursor]
        if token.kind == "string":
            value, expressions, uncertain = _string_parts(token, language)
            if expressions or uncertain:
                return None
            return value, cursor + 1
        elif token.value == "(" and token.kind == "operator":
            result = literal(cursor + 1, depth + 1)
            if result is None:
                return None
            end = skip_lines(result[1])
            if end >= len(tokens) or tokens[end].value != ")":
                return None
            return result[0], end + 1
        return None

    def literal(cursor: int, depth: int = 0):
        result = atom(cursor, depth)
        if result is None:
            return None
        value, end = result
        pieces = [value]
        end = skip_lines(end)
        while end < len(tokens) and tokens[end].value == operator and tokens[end].kind == "operator":
            right = atom(end + 1, depth)
            if right is None:
                return None
            pieces.append(right[0])
            end = skip_lines(right[1])
        return "".join(pieces), end

    # 普通标识符仍使用原字段规则；只有完整的静态字符串表达式获得折叠结果。
    if tokens[start].kind == "name":
        return _field(tokens[start], language), start + 1
    if tokens[start].kind == "string":
        value, expressions, uncertain = _string_parts(tokens[start], language)
        prefix = re.sub(r"[_-]", "", value.lstrip("$")).casefold()
        # 不是凭证字段前缀的普通拼接无需反复遍历整条表达式。
        if expressions or uncertain or not any(name.startswith(prefix) for name in _FIELDS):
            return None, start + 1
    folded = literal(start)
    return (_field_name(folded[0]), folded[1]) if folded is not None else (None, start + 1)


def _expression(tokens: list[_Token], start: int, language: str) -> tuple[list[_Token], bool]:
    result = []
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    uncertain = False
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.kind == "newline":
            if language != "php" and not stack:
                # JS 下一行二元运算仍属于同一个赋值；不能把 ||/?? 默认值漏掉。
                following = next((part for part in tokens[index + 1:] if part.kind != "newline"), None)
                continuation = {"||", "&&", "??", "?", ".", "+", "-", "*", "/", "%", "**", "|", "&"}
                if (language != "javascript" or (result and not (
                        following is not None and following.kind == "operator" and following.value in continuation)
                        and not (result[-1].kind == "operator" and result[-1].value in continuation))):
                    break
            continue
        if token.value in {",", ";"} and not stack:
            break
        if token.value in pairs:
            if not stack:
                break
            if stack.pop() != pairs[token.value]:
                uncertain = True
        elif token.value in {"(", "[", "{"} and token.kind == "operator":
            stack.append(token.value)
        result.append(token)
    if stack or not result or any(not token.closed or token.kind == "unknown" for token in result):
        uncertain = True
    if result and result[-1].kind == "operator" and result[-1].value not in {")", "]", "}"}:
        uncertain = True
    return result, uncertain


def _lookup_literal(tokens: list[_Token], index: int) -> bool:
    token = tokens[index]
    # 对象键与索引键是名称，不是被赋予字段的值。
    if index + 1 < len(tokens) and tokens[index + 1].value in {":", "=>"}:
        return True
    if index > 1 and tokens[index - 1].value == "[" and (
            tokens[index - 2].kind == "name" or tokens[index - 2].value in {"]", ")"}):
        return True
    stack = []
    for cursor in range(index):
        value = tokens[cursor].value
        if value in {"(", "[", "{"}:
            stack.append(cursor)
        elif value in {")", "]", "}"} and stack:
            stack.pop()
    for opening in reversed(stack):
        if tokens[opening].value != "(" or opening == 0 or tokens[opening - 1].kind != "name":
            continue
        cursor = opening - 1
        pieces = [tokens[cursor].value.lstrip("$")]
        while cursor >= 2 and tokens[cursor - 1].value in {".", "->", "::"} and tokens[cursor - 2].kind == "name":
            pieces[:0] = [tokens[cursor - 2].value.lstrip("$"), "."]
            cursor -= 2
        function = "".join(pieces).casefold()
        if function not in _LOOKUPS and not function.endswith(".get") and function not in _REGEX_CALLS:
            continue
        depth = 0
        for part in tokens[opening + 1:index]:
            if part.value in {"(", "[", "{"}:
                depth += 1
            elif part.value in {")", "]", "}"}:
                depth -= 1
            elif part.value == "," and depth == 0:
                return False
        return True
    return False


def _classify(tokens: list[_Token], field: str, language: str, *, depth: int = 0) -> str | None:
    if depth > 16:
        return _UNCERTAIN
    if language == "unsupported" or not tokens or any(not token.closed or token.kind == "unknown" for token in tokens):
        return _UNCERTAIN
    if (field == "authorization" and len(tokens) >= 2 and tokens[0].kind == "name"
            and tokens[0].value.casefold() in {"bearer", "basic"}
            and tokens[1].kind in {"name", "number"}):
        return _HARDCODED
    literal_found = False
    dynamic = False
    for index, token in enumerate(tokens):
        if token.kind == "string":
            if _lookup_literal(tokens, index):
                continue
            value, expressions, uncertain = _string_parts(token, language)
            if uncertain:
                return _UNCERTAIN
            for expression in expressions:
                # PHP 已验证变量插值仅代表引用；JS/Python 插值可能带固定回退。
                if language != "php":
                    expression_tokens, incomplete = _expression(_lex(expression, language), 0, language)
                    rule = _UNCERTAIN if incomplete else _classify(expression_tokens, field, language, depth=depth + 1)
                    if rule:
                        return rule
            dynamic |= bool(expressions)
            if value and not (field == "authorization" and _SCHEME_ONLY.fullmatch(value)):
                literal_found = True
        elif token.kind == "name" and token.value.casefold() not in _NULLS:
            dynamic = True
    if literal_found:
        return _HARDCODED
    numbers = [index for index, token in enumerate(tokens) if token.kind == "number" and not _lookup_literal(tokens, index)]
    # 数字默认值、str/String(...) 等转换仍把固定值作为凭证；动态外观不能放行。
    # 索引键和 getenv/config 的首个查找参数已由 _lookup_literal 单独排除。
    if numbers:
        return _HARDCODED
    if language == "data" and any(token.kind == "name" and token.value.casefold() not in _NULLS for token in tokens):
        return _HARDCODED
    return None


def scan_source(relative_path: str, content: str | bytes, known_secrets=()) -> list[dict]:
    """返回 path/line/rule；不修改内容，不返回秘密片段、值、摘要或绝对路径。"""
    secrets = _known(known_secrets)
    path = _path(relative_path, secrets)
    text, valid_text = _text(content)
    findings = scan_deterministic(text, secrets, path)
    if not valid_text:
        findings.append(_finding(path, text, 0, _UNCERTAIN))
        return _deduplicate(findings)
    language = _language(relative_path)
    tokens = _lex(text, language)
    findings.extend(_finding(path, text, token.start, _UNCERTAIN) for token in tokens if token.kind == "limit")
    for index, token in enumerate(tokens):
        field, next_index = _source_field(tokens, index, language)
        # headers['Authorization'] = ... 等下标赋值。
        if next_index < len(tokens) and tokens[next_index].value == "]":
            next_index += 1
        while next_index < len(tokens) and tokens[next_index].kind == "newline":
            next_index += 1
        if (field and next_index < len(tokens) and tokens[next_index].kind == "operator"
                and tokens[next_index].value in {"=", "=>", ":", ":=", ".=", "+=", "-=", "*=", "/=", "%=", "**=", "||=", "&&=", "??="}):
            expression, incomplete = _expression(tokens, next_index + 1, language)
            rule = _UNCERTAIN if incomplete else _classify(expression, field, language)
            if rule:
                findings.append(_finding(path, text, token.start, rule))
        if (language == "php" and token.kind == "name" and token.value.casefold() == "define"
                and index + 2 < len(tokens) and tokens[index + 1].value == "("):
            first = index + 2
            while first < len(tokens) and tokens[first].kind == "newline":
                first += 1
            if first < len(tokens) and (tokens[first].kind == "string" or tokens[first].value == "("):
                constant_field, comma = _source_field(tokens, first, language)
                if constant_field and comma < len(tokens) and tokens[comma].value == ",":
                    expression, incomplete = _expression(tokens, comma + 1, language)
                    nesting, closed = 1, False
                    for part in tokens[index + 2:]:
                        if part.kind != "operator":
                            continue
                        if part.value == "(":
                            nesting += 1
                        elif part.value == ")":
                            nesting -= 1
                            if not nesting:
                                closed = True
                                break
                        elif part.value == ";" and nesting == 1:
                            break
                    rule = _UNCERTAIN if incomplete or not closed else _classify(expression, constant_field, language)
                    if rule:
                        findings.append(_finding(path, text, tokens[first].start, rule))
        if token.kind == "string":
            value, expressions, uncertain = _string_parts(token, language)
            header = _HEADER.fullmatch(value)
            if header and not _lookup_literal(tokens, index):
                rule = _classify([token], "authorization", language)
                if rule:
                    findings.append(_finding(path, text, token.start, rule))
        # 明确的数据文本头，不把它解释为变量或运行时代码。
    if language == "data":
        for match in re.finditer(r"(?im)^[ \t]*(?:authorization[ \t]*:[ \t]*)?(?:bearer|basic)[ \t]+\S[^\r\n]*", text):
            findings.append(_finding(path, text, match.start(), _HARDCODED))
    return _deduplicate(findings)
