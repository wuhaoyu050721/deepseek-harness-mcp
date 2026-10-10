"""构建可公开发布的源码包、Skill 包与校验清单；不读取个人配置或凭证。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_SOURCE_FILES = (
    "README.md", "AGENTS.md", ".gitignore", ".dockerignore", "pyproject.toml",
    "uv.lock", "worker-requirements.lock", "Dockerfile",
    "examples/config.example.toml", "examples/codex.mcp.example.toml",
    "examples/claude.mcp.example.json", "docs/IMPLEMENTATION.md",
    "docs/MCP-CONNECTION.zh-CN.md", "docs/RELEASE-NOTES.md", "docs/VALIDATION.md",
    "artifacts/public/validation-v0.3.1.json",
)
SKILL_ROOT = PurePosixPath(".agents/skills/deepseek-delegate")
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "data", "config", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", "logs",
})
# 这两个字面量是已有回归测试的人工输入，限定文件及完整字节；不得扩大为目录豁免。
ARTIFICIAL_TOKEN_ALLOWLIST = {
    "tests/test_edit_workspace.py": (b"sk-" + b"12345678901234567890",),
    "tests/test_runner.py": (b"sk-fixture-" + b"secret-value-1234567890",),
}
SECRET_PATTERNS = (
    ("api_token", re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("github_token", re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])")),
    ("github_fine_grained_token", re.compile(rb"github_pat_[A-Za-z0-9_]{30,}")),
    ("aws_access_key", re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    ("private_key", re.compile(
        rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----\s+[A-Za-z0-9+/=\r\n]{32,}\s*"
        rb"-----END (?:[A-Z ]*PRIVATE KEY)-----"
    )),
)
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


class ReleaseError(ValueError):
    """发布输入不完整，或发现路径、链接、凭证与资产冲突。"""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _member_name(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or path.is_absolute()
            or ":" in value or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ReleaseError("Archive member must be a normalized relative path")
    return path.as_posix()


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _checked_path(root: Path, relative: str, *, file: bool = True) -> Path:
    """逐级检查词法路径，不追随 symlink、junction 或其他 reparse point。"""
    relative = _member_name(relative)
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        try:
            info = path.lstat()
        except OSError as exc:
            raise ReleaseError(f"Required release path unavailable: {relative}") from exc
        if _linked(info):
            raise ReleaseError(f"Linked release path refused: {relative}")
        if path != root / relative and not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"Release parent is not a directory: {relative}")
    if path.resolve() != path.absolute():
        raise ReleaseError(f"Release path resolves through a link: {relative}")
    if file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ReleaseError(f"Release source must be a single-link regular file: {relative}")
    if not file and not stat.S_ISDIR(info.st_mode):
        raise ReleaseError(f"Release path must be a directory: {relative}")
    return path


def _read_source(root: Path, relative: str) -> bytes:
    path = _checked_path(root, relative)
    before = path.lstat()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _linked(opened) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ReleaseError(f"Release source changed into a link: {relative}")
        content = stream.read()
        after = os.fstat(stream.fileno())
    current = _checked_path(root, relative).lstat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
    if not (signature(before) == signature(opened) == signature(after) == signature(current)):
        raise ReleaseError(f"Release source changed during read: {relative}")
    return content


def _walk_sources(root: Path, directory: str, suffixes: set[str], *, recursive: bool) -> list[str]:
    _checked_path(root, directory, file=False)
    result: list[str] = []

    def visit(relative: str) -> None:
        folder = _checked_path(root, relative, file=False)
        for child in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            child_relative = f"{relative}/{child.name}"
            info = child.lstat()
            if _linked(info):
                raise ReleaseError(f"Linked release path refused: {child_relative}")
            if stat.S_ISDIR(info.st_mode):
                if recursive and child.name not in EXCLUDED_DIRECTORIES and not child.name.startswith("."):
                    visit(child_relative)
            elif (child.suffix in suffixes and not child.name.startswith(".env")
                  and ".local." not in child.name):
                _checked_path(root, child_relative)
                result.append(child_relative)

    visit(directory)
    return result


def _version(root: Path) -> str:
    project = tomllib.loads(_read_source(root, "pyproject.toml").decode("utf-8"))["project"]["version"]
    module = ast.parse(_read_source(root, "src/harness_mcp/__init__.py").decode("utf-8"))
    found = [ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", project) or found != [project]:
        raise ReleaseError("Project and runtime versions must agree and use X.Y.Z")
    return project


def _source_files(root: Path, version: str) -> list[str]:
    fixed = [name.replace("validation-v0.3.1.json", f"validation-v{version}.json")
             for name in FIXED_SOURCE_FILES]
    skill_files = [f"{SKILL_ROOT}/SKILL.md", f"{SKILL_ROOT}/agents/openai.yaml"]
    skill_files += _walk_sources(root, f"{SKILL_ROOT}/references", {".md"}, recursive=True)
    selected = fixed + skill_files
    selected += _walk_sources(root, "src", {".py"}, recursive=True)
    selected += _walk_sources(root, "tests", {".py"}, recursive=True)
    selected += _walk_sources(root, "scripts", {".py", ".ps1"}, recursive=False)
    if len({name.casefold() for name in selected}) != len(selected):
        raise ReleaseError("Case-insensitive duplicate release paths refused")
    for name in selected:
        _checked_path(root, name)
    return sorted(selected)


def _scan(relative: str, content: bytes) -> None:
    allow = ARTIFICIAL_TOKEN_ALLOWLIST.get(relative, ())
    for kind, pattern in SECRET_PATTERNS:
        if any(match.group(0) not in allow for match in pattern.finditer(content)):
            # 不输出命中字节，错误只有类型与公开候选相对路径。
            raise ReleaseError(f"Potential {kind} in release candidate: {relative}")


def _verify_wheel(content: bytes, version: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            regular = [name for name in names if not name.endswith("/")]
            normalized = [_member_name(name.rstrip("/")) for name in names]
            if len(set(name.casefold() for name in normalized)) != len(normalized):
                raise ReleaseError("Case-insensitive duplicate wheel paths refused")
            metadata = f"deepseek_harness_mcp-{version}.dist-info/METADATA"
            if metadata not in regular or f"Version: {version}\n".encode() not in archive.read(metadata):
                raise ReleaseError("Wheel metadata does not match project version")
            for item in archive.infolist():
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ReleaseError("Linked wheel member refused")
                if not item.is_dir():
                    _scan(f"wheel/{item.filename}", archive.read(item))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError("Release wheel is invalid") from exc


def _verify_wheel_source(content: bytes, modules: dict[str, bytes]) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = {name for name in archive.namelist()
                 if name.startswith("harness_mcp/") and name.endswith(".py")}
        if names != set(modules):
            raise ReleaseError("Wheel Python module paths do not match current source")
        for name in sorted(names):
            if archive.read(name) != modules[name]:
                raise ReleaseError(f"Wheel Python module bytes do not match current source: {name}")


def _zip_content(members: dict[str, bytes]) -> bytes:
    names = [_member_name(name) for name in members]
    if len({name.casefold() for name in names}) != len(names):
        raise ReleaseError("Case-insensitive duplicate archive paths refused")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(names):
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name], compresslevel=9)
    content = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if archive.testzip() is not None or archive.namelist() != sorted(names):
            raise ReleaseError("Release archive integrity verification failed")
        for name in names:
            if archive.read(name) != members[name]:
                raise ReleaseError(f"Release archive byte verification failed: {name}")
    return content


def _entries(members: dict[str, bytes]) -> list[dict]:
    return [{"path": name, "bytes": len(content), "sha256": _sha256(content)}
            for name, content in sorted(members.items())]


def _output_directory(root: Path, output: Path) -> Path:
    path = output.absolute() if output.is_absolute() else root / output
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ReleaseError("Release output must stay inside the project root") from exc
    _member_name(relative)
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            info = cursor.lstat()
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseError("Release output cannot use a linked or non-directory path")
        else:
            cursor.mkdir()
    return path


def _write_asset(path: Path, content: bytes, *, replace: bool) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError(f"Linked/non-regular release asset refused: {path.name}")
        if path.read_bytes() == content:
            return
        if not replace:
            raise ReleaseError(f"Existing release asset differs; use --replace: {path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if path.read_bytes() != content:
        raise ReleaseError(f"Written release asset byte verification failed: {path.name}")


def package_release(root: Path, output: Path | None = None, *, replace: bool = False) -> dict:
    root = root.absolute()
    if _linked(root.lstat()) or not root.is_dir() or root.resolve() != root:
        raise ReleaseError("Project root must be a real directory")
    version = _version(root)
    wheel_name = f"deepseek_harness_mcp-{version}-py3-none-any.whl"
    wheel = _read_source(root, f"artifacts/dist/{wheel_name}")
    _verify_wheel(wheel, version)
    source_files = _source_files(root, version)
    source_prefix = f"deepseek-harness-mcp-{version}"
    source_members = {}
    skill_members = {}
    module_members = {}
    for relative in source_files:
        content = _read_source(root, relative)
        _scan(relative, content)
        source_members[f"{source_prefix}/{relative}"] = content
        if PurePosixPath(relative).is_relative_to(SKILL_ROOT):
            skill_members[f"deepseek-delegate/{PurePosixPath(relative).relative_to(SKILL_ROOT)}"] = content
        if relative.startswith("src/harness_mcp/") and relative.endswith(".py"):
            module_members[relative.removeprefix("src/")] = content
    _verify_wheel_source(wheel, module_members)
    source_members[f"{source_prefix}/dist/{wheel_name}"] = wheel
    assets = {
        f"deepseek-harness-mcp-{version}-windows-source.zip": _zip_content(source_members),
        f"deepseek-delegate-{version}.zip": _zip_content(skill_members),
        wheel_name: wheel,
    }
    manifest = {
        "format_version": 1,
        "project": "deepseek-harness-mcp",
        "version": version,
        "source_files": len(source_files),
        "source_zip": {"root": source_prefix, "members": _entries(source_members)},
        "skill_zip": {"root": "deepseek-delegate", "members": _entries(skill_members)},
        "assets": _entries(assets),
        "verification": {
            "zip_members_read_back_equal_source_bytes": True,
            "normalized_relative_members_only": True,
            "source_links_and_case_duplicates_refused": True,
            "generic_token_and_private_key_scan": "passed",
            "known_artificial_fixture_exceptions": sorted(ARTIFICIAL_TOKEN_ALLOWLIST),
            "personal_credentials_read": False,
            "wheel_metadata_version": version,
            "wheel_source_bytes_match": True,
            "deterministic_zip_timestamp": list(ZIP_TIMESTAMP),
        },
    }
    manifest_name = f"manifest-v{version}.json"
    assets[manifest_name] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assets["SHA256SUMS"] = "".join(f"{_sha256(content)}  {name}\n" for name, content in sorted(assets.items())).encode("ascii")
    # 全批收集完成后再次核对，避免早读文件在稍后收集/压缩期间发生漂移。
    for relative in source_files:
        if _read_source(root, relative) != source_members[f"{source_prefix}/{relative}"]:
            raise ReleaseError(f"Release source changed during collection: {relative}")
    if _read_source(root, f"artifacts/dist/{wheel_name}") != wheel:
        raise ReleaseError("Release wheel changed during collection")
    output = _output_directory(root, output or root / "artifacts/releases" / f"v{version}")
    # 先检查全部已有资产，避免冲突发现前只写出半批文件。
    for name, content in assets.items():
        path = output / name
        if path.exists() or path.is_symlink():
            existing = _read_source(root, path.relative_to(root).as_posix())
            if existing != content and not replace:
                raise ReleaseError(f"Existing release asset differs; use --replace: {name}")
    for name, content in assets.items():
        _write_asset(output / name, content, replace=replace)
    return {"status": "passed", "version": version, "source_files": len(source_files),
            "skill_files": len(skill_members), "output": str(output), "assets": _entries(assets)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replace", action="store_true", help="重新生成本脚本的已有发布资产，不修改源码")
    args = parser.parse_args()
    try:
        result = package_release(args.root, args.output, replace=args.replace)
    except (ReleaseError, OSError, KeyError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        parser.exit(1, f"Release packaging failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
