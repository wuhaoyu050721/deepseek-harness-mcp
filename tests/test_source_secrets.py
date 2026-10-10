from __future__ import annotations

import json

import pytest

from harness_mcp.source_secrets import scan_deterministic, scan_source


@pytest.mark.parametrize("source", [
    "<?php return ['password' => null];",
    "<?php return ['api_key' => NULL, 'password' => ''];",
    "<?php return ['password' => $password];",
    "<?php return ['api_key' => getenv('API_KEY')];",
    "<?php $password = $config['password'];",
    "<?php $password = $config->get('password');",
    "<?php $headers = ['Authorization' => 'Bearer ' . $token];",
    "<?php $headers[] = 'Authorization: Bearer ' . $token;",
    "<?php $headers['Authorization'] = \"Bearer $token\";",
    "<?php $headers['Authorization'] = \"Bearer {$config['token']}\";",
    "<?php $pattern = '/^Authorization:\\s*(Bearer|Basic)\\s+(.+)$/i';",
    "<?php $authorization = preg_replace('/^Bearer\\s+/i', '', $token);",
])
def test_php_dynamic_empty_and_regex_values_are_not_secrets(source):
    assert scan_source("src/auth.php", source) == []


@pytest.mark.parametrize("path,source", [
    ("src/auth.py", "password = os.getenv('DB_PASSWORD')\n"),
    ("src/auth.py", "password = os.environ.get('DB_PASSWORD')\n"),
    ("src/auth.py", "password = config['password']\n"),
    ("src/auth.py", "password = None\n"),
    ("src/auth.py", "authorization = f'Bearer {token}'\n"),
    ("src/auth.py", "pattern = re.compile(r'Authorization:\\s*(Bearer|Basic)\\s+(.+)')\n"),
    ("src/auth.ts", "const headers = {Authorization: `Bearer ${token}`};"),
    ("src/auth.js", "const authRegex = /Authorization: (Bearer|Basic) [^\\r\\n]+/i;"),
    ("src/auth.ts", "const password = process.env.PASSWORD;"),
    ("config/example.json", '{"password":null,"api_key":""}'),
    ("config/example.toml", 'password = ""\n'),
    ("config/example.yaml", 'password: null\n'),
])
def test_language_aware_dynamic_values(path, source):
    assert scan_source(path, source) == []


@pytest.mark.parametrize("path,source", [
    ("src/auth.php", "<?php return ['password' => 'x'];"),
    ("src/auth.php", "<?php $api_key = 'artificial-' . 'fixture';"),
    ("src/auth.php", "<?php $password = getenv('PASSWORD') ?: 'x';"),
    ("src/auth.php", "<?php $password = env('PASSWORD', 'x');"),
    ("src/auth.php", "<?php $password = $prefix . 'x';"),
    ("src/auth.php", "<?php $password = '$password';"),
    ("src/auth.php", "<?php $authorization = 'Bearer artificial-fixture';"),
    ("src/auth.php", "<?php $headers[] = 'Authorization: Bearer artificial-fixture';"),
    ("src/auth.php", "<?php $password = \"fixed-$suffix\";"),
    ("src/auth.php", "<?php $password = <<<VALUE\nartificial-value\nVALUE;"),
    ("src/auth.php", "<?php $password = <<<'VALUE'\n$password\nVALUE;"),
    ("src/auth.py", "password = 'x'\n"),
    ("src/auth.py", "Authorization: Bearer artificial_token\n"),
    ("src/auth.py", "password = os.getenv('PASSWORD', 'x')\n"),
    ("src/auth.py", "password = config.get('password', 'x')\n"),
    ("src/auth.py", "password = os.getenv('PASSWORD') or 'x'\n"),
    ("src/auth.py", "authorization = f'Bearer {token or \"x\"}'\n"),
    ("src/auth.ts", "const authorization = `Bearer ${token || 'x'}`;"),
    ("src/auth.ts", "headers.push(`Authorization: Bearer ${token || 'x'}`);"),
    ("src/auth.py", "headers.append(f'Authorization: Bearer {token or \"x\"}')\n"),
    ("src/auth.ts", "const authorization = 'Bearer ${TOKEN}';"),
    ("src/auth.ts", "const api_key = 'artificial-' + 'fixture';"),
    ("src/auth.ts", "const password = 7;"),
    ("src/auth.ts", "const password = 0x12;"),
    ("src/auth.ts", "const password = 1e3;"),
    ("config/example.json", '{"password":"x"}'),
    ("config/example.yaml", 'password: artificial-fixture\n'),
    ("config/example.txt", 'Authorization: Bearer artificial-fixture\n'),
    ("config/example.txt", 'Bearer artificial-fixture\n'),
])
def test_literal_short_split_fallback_and_fake_variables_still_rejected(path, source):
    result = scan_source(path, source)
    assert any(row["rule"] == "SOURCE_HARDCODED_AUTH_LITERAL" for row in result)
    assert all(set(row) == {"path", "line", "rule"} for row in result)
    assert "artificial" not in json.dumps(result)


@pytest.mark.parametrize("path,source", [
    ("src/auth.php", "<?php return ['pass' . 'word' => 'artificial-fixture'];"),
    ("src/auth.php", "<?php $config['api' . '_key'] = 'artificial-fixture';"),
    ("src/auth.php", "<?php $headers['Auth' . 'orization'] = 'Bearer artificial-fixture';"),
    ("src/auth.php", "<?php return [('pass' . ('word')) => 'artificial-fixture'];"),
    ("src/auth.php", "<?php return ['pass' .\n 'word' => 'artificial-fixture'];"),
    ("src/auth.php", "<?php return ['a'.'_'.'u'.'_'.'t'.'_'.'h'.'_'.'o'.'_'.'r'.'_'.'i'.'_'.'z'.'_'.'a'.'_'.'t'.'_'.'i'.'_'.'o'.'_'.'n' => 'Bearer artificial-fixture'];"),
    ("src/auth.js", "const config = {['pass' + 'word']: 'artificial-fixture'};"),
    ("src/auth.py", "config = {'pass' + 'word': 'artificial-fixture'}\n"),
    ("src/auth.py", "config['pass' + ('word')] = 'artificial-fixture'\n"),
])
def test_static_concatenated_credential_field_names_are_not_a_bypass(path, source):
    assert any(row["rule"] == "SOURCE_HARDCODED_AUTH_LITERAL" for row in scan_source(path, source))


@pytest.mark.parametrize("path,source", [
    ("src/auth.php", "<?php $password = getenv('PASSWORD') ?: 7;"),
    ("src/auth.php", "<?php $password = env('PASSWORD', 7);"),
    ("src/auth.js", "const password = process.env.PASSWORD || 7;"),
    ("src/auth.js", "const password = String(123);"),
    ("src/auth.py", "password = str(123)\n"),
    ("src/auth.py", "password = config.get('PASSWORD', 7)\n"),
])
def test_numeric_credential_defaults_and_conversions_remain_literals(path, source):
    assert any(row["rule"] == "SOURCE_HARDCODED_AUTH_LITERAL" for row in scan_source(path, source))


@pytest.mark.parametrize("path,source", [
    ("src/auth.php", "<?php return ['pass' . 'word' => null, 'api' . '_key' => getenv('API_KEY')];"),
    ("src/auth.js", "const config = {['pass' + 'word']: process.env.PASSWORD};"),
    ("src/auth.py", "config['pass' + 'word'] = os.getenv('PASSWORD')\n"),
    ("src/business.php", "<?php $retry_count = getenv('RETRIES') ?: 7;"),
    ("src/business.js", "const retries = process.env.RETRIES || 7;"),
    ("src/business.py", "timeout = int(config.get('TIMEOUT', 123))\n"),
    ("config/business.json", '{"retries":7,"timeout":123}'),
    ("src/auth.php", "<?php $password = $config[7];"),
    ("src/auth.py", "password = passwords[7]\n"),
])
def test_static_dynamic_keys_and_noncredential_numbers_are_allowed(path, source):
    assert scan_source(path, source) == []


@pytest.mark.parametrize("path,source", [
    ("src/auth.js", "const password = process.env.PASSWORD\n || 'artificial-fixture';"),
    ("src/auth.js", "const password = process.env.PASSWORD ||\n 'artificial-fixture';"),
    ("src/auth.js", "const password =\n 'artificial-fixture';"),
    ("src/auth.js", "const password = process.env.PASSWORD\r\n ?? 7;"),
    ("src/auth.php", "<?php $password .= 'artificial-fixture';"),
    ("src/auth.php", "<?php $password ??= 'artificial-fixture';"),
    ("src/auth.js", "password ||= 'artificial-fixture';"),
    ("src/auth.js", "password ??= 'artificial-fixture';"),
    ("src/auth.py", "password += 'artificial-fixture'\n"),
    ("src/auth.php", "<?php define('PASSWORD', 'artificial-fixture');"),
    ("src/auth.php", "<?php define('API' . '_KEY', 'artificial-fixture');"),
    ("src/auth.php", "<?php define('PASSWORD', getenv('PASSWORD') ?: 7);"),
    ("src/auth.py", "password = os.getenv('PASSWORD') \\\r\n or 'artificial-fixture'\r\n"),
])
def test_continuations_compound_assignments_and_named_constants_reject_literals(path, source):
    assert any(row["rule"] == "SOURCE_HARDCODED_AUTH_LITERAL" for row in scan_source(path, source))


@pytest.mark.parametrize("path,source", [
    ("src/auth.js", "const password = process.env.PASSWORD\n || otherToken;"),
    ("src/auth.js", "const password = process.env.PASSWORD;\nconst ordinary = 'artificial-fixture';"),
    ("src/auth.php", "<?php $password .= $suffix;"),
    ("src/auth.js", "password ||= process.env.PASSWORD;"),
    ("src/auth.php", "<?php define('PASSWORD', getenv('PASSWORD'));"),
    ("src/auth.php", "<?php define('PASSWORD', $password);"),
    ("src/auth.php", "<?php define('PASSWORD', null);"),
    ("src/auth.php", "<?php define('RETRY_COUNT', 7);"),
    ("src/auth.py", "password = os.getenv('PASSWORD') \\\r\n or otherToken\r\n"),
])
def test_continuations_compound_assignments_and_named_constants_allow_references(path, source):
    assert scan_source(path, source) == []


def test_incomplete_named_credential_constant_is_uncertain():
    result = scan_source("src/auth.php", "<?php define('PASSWORD', $password")
    assert result == [{"path": "src/auth.php", "line": 1, "rule": "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN"}]


@pytest.mark.parametrize("source", [
    "<?php $password =",
    "<?php return ['password' => ];",
    "<?php $password = 'unterminated",
    "<?php $password = getenv('PASSWORD';",
    "<?php $password = <<<VALUE\nartificial-value\n",
    "<?php $password = <<<'VALUE'\nartificial-value\n",
    "<?php $password = $;",
    "<?php $password = $value .;",
])
def test_incomplete_credential_expression_is_stable_uncertainty(source):
    result = scan_source("src/auth.php", source)
    assert any(row["rule"] == "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN" for row in result)
    assert "unterminated" not in json.dumps(result)


@pytest.mark.parametrize("source", [
    "const authorization = `Bearer ${token`;",
    "const authorization = `Bearer ${token || 'unterminated}`;",
])
def test_incomplete_js_interpolation_is_uncertain(source):
    assert any(row["rule"] == "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN" for row in scan_source("auth.js", source))


def test_known_credentials_match_globally_without_returning_values():
    secret = "artificial-sensitive-value"
    source = "<?php\n// " + secret + "\n$password = $token;\n"
    result = scan_source("src/auth.php", source, (secret,))
    assert result == [{"path": "src/auth.php", "line": 2, "rule": "SOURCE_KNOWN_SECRET"}]
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("text,rule", [
    ("token='" + "sk-" + "x" * 24 + "'", "SOURCE_PROVIDER_TOKEN"),
    ("token='" + "ghp_" + "x" * 24 + "'", "SOURCE_PROVIDER_TOKEN"),
    ("token='" + "github_pat_" + "x" * 34 + "'", "SOURCE_PROVIDER_TOKEN"),
    ("token='" + "AKIA" + "X" * 16 + "'", "SOURCE_PROVIDER_TOKEN"),
    ("https://user:artificial-password@example.test", "SOURCE_CREDENTIAL_URL"),
    ("-----BEGIN PRIVATE KEY-----\n" + "Y" * 64 + "\n-----END PRIVATE KEY-----", "SOURCE_PRIVATE_KEY"),
])
def test_deterministic_features_apply_to_source_and_diff(text, rule):
    assert any(row["rule"] == rule for row in scan_source("src/auth.php", "<?php\n" + text))
    assert any(row["rule"] == rule for row in scan_deterministic("--- a/auth.php\n-" + text, relative_path="auth.php"))


def test_deterministic_scan_does_not_parse_diff_or_auth_regex():
    diff = "--- a/auth.php\n+++ b/auth.php\n-password => null\n+password => $password\n+Authorization: Bearer $token\n"
    assert scan_deterministic(diff, relative_path="auth.php") == []
    assert scan_deterministic("r'Authorization:\\s*(Bearer|Basic)\\s+(.+)'", relative_path="auth.py") == []


def test_metadata_is_safe_stable_and_uses_lf_lines():
    source = "<?php\r\n// 标记\u2028still same LF line\r\n$password = 'x';\r\n"
    original = source.encode("utf-8")
    expected = [{"path": "src/auth.php", "line": 3, "rule": "SOURCE_HARDCODED_AUTH_LITERAL"}]
    assert scan_source("src\\auth.php", original) == expected
    assert original == source.encode("utf-8")
    assert scan_source("src/auth.php", source) == expected


@pytest.mark.parametrize("path", ["/secret.php", "C:/secret.php", "../secret.php", "src/../secret.php",
                                  "\\\\server\\share\\secret.php", "src/auth.php:stream", "a\nsecret.php"])
def test_absolute_and_unsafe_paths_never_enter_metadata(path):
    with pytest.raises(ValueError, match="safe relative path") as error:
        scan_source(path, "$password = 'x';")
    assert path not in str(error.value)


def test_sensitive_path_is_replaced_with_relative_placeholder():
    secret = "artificial-sensitive-path"
    result = scan_source("src/" + secret + ".php", "$password = 'x';", (secret,))
    assert result == [{"path": "redacted-source", "line": 1, "rule": "SOURCE_HARDCODED_AUTH_LITERAL"}]
    assert secret not in json.dumps(result)


def test_invalid_utf8_and_binary_content_are_uncertain_without_raw_data():
    for content in (b"\xffcredential", b"hello\0credential"):
        assert scan_source("src/auth.php", content) == [{"path": "src/auth.php", "line": 1,
                                                        "rule": "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN"}]


def test_unsupported_language_credential_expression_is_uncertain():
    assert scan_source("src/auth.go", 'password := os.Getenv("PASSWORD")') == [
        {"path": "src/auth.go", "line": 1, "rule": "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN"}]


def test_token_limit_fails_closed_without_content(monkeypatch):
    import harness_mcp.source_secrets as scanner
    monkeypatch.setattr(scanner, "_MAX_TOKENS", 5)
    result = scan_source("src/auth.php", "<?php $ordinary = 1; $password = 'x';")
    assert any(row["rule"] == "SOURCE_CREDENTIAL_SYNTAX_UNCERTAIN" for row in result)
    assert all(set(row) == {"path", "line", "rule"} for row in result)
