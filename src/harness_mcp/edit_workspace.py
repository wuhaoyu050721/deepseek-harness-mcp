"""从不可变快照建立修改副本，停止模型后根据真实文件生成有界差异。

调用者必须先确认本次执行的整个进程树已停止，再收集差异。这里核对文件和
授权范围，不提供 Windows 文件访问沙箱，也不会把差异应用到原项目。
"""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
from typing import Any

from .redaction import redact_text as redact_diagnostic
from .workspace import (
    Snapshot, SnapshotError, _check_path_chain, _excluded_reason,
    _handle_final_path, normalize_relative_path,
)


MAX_EDIT_FILES = 10000
MAX_EDIT_ENTRIES = 20000
MAX_EDIT_DEPTH = 128
_MAX_PREPARE_BYTES = 1024 * 1024 * 1024


class EditWorkspaceError(SnapshotError):
    """错误只含稳定标签，不回显文件内容或凭证。"""


def _error(code: str, message: str) -> EditWorkspaceError:
    return EditWorkspaceError(code, message)


def _relative(value: str, *, allow_root: bool = False) -> str:
    try:
        return normalize_relative_path(value, allow_root=allow_root)
    except SnapshotError as exc:
        raise _error("EDIT_INVALID_PATH", "修改范围包含无效或不安全的相对路径。") from exc


def _chain(path: Path) -> None:
    try:
        _check_path_chain(path)
    except SnapshotError as exc:
        raise _error("EDIT_UNSAFE_LINK", "修改工作区或快照包含符号链接或 reparse point。") from exc


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _overlap(left: Path, right: Path) -> bool:
    # 此前已拒绝链接；Windows 还要忽略路径大小写，防止基线目录别名。
    first, second = os.path.normcase(str(left)), os.path.normcase(str(right))
    return first == second or os.path.commonpath((first, second)) in (first, second)


def _no_streams(path: Path) -> None:
    """NTFS ADS 不出现在 scandir 中，因此必须另外检查默认数据流之外的流。"""
    if os.name != "nt":
        return

    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", ctypes.c_wchar * 296)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    first = kernel.FindFirstStreamW
    first.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(StreamData), ctypes.c_uint32]
    first.restype = ctypes.c_void_p
    next_stream = kernel.FindNextStreamW
    next_stream.argtypes = [ctypes.c_void_p, ctypes.POINTER(StreamData)]
    next_stream.restype = ctypes.c_int
    close = kernel.FindClose
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    info = StreamData()
    handle = first(str(path), 0, ctypes.byref(info), 0)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() == 38:  # ERROR_HANDLE_EOF：目录可以没有数据流。
            return
        raise _error("EDIT_PATH_CHECK_FAILED", "无法验证修改文件的数据流。")
    try:
        while True:
            if info.name != "::$DATA":
                raise _error("EDIT_UNSAFE_ADS", "拒绝含 Windows ADS 的修改工作区。")
            if not next_stream(handle, ctypes.byref(info)):
                if ctypes.get_last_error() != 38:
                    raise _error("EDIT_PATH_CHECK_FAILED", "修改文件的数据流枚举失败。")
                break
    finally:
        close(handle)


def _read_file(path: Path, max_bytes: int) -> bytes:
    _chain(path)
    _no_streams(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _error("EDIT_FILE_UNAVAILABLE", "修改文件不可读取。") from exc
    try:
        final = _handle_final_path(fd)
        if final is not None and os.path.normcase(str(final)) != os.path.normcase(str(path)):
            raise _error("EDIT_UNSAFE_LINK", "修改文件句柄指向授权路径之外。")
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _error("EDIT_UNSAFE_FILE", "修改模式只接受普通文件。")
        if before.st_nlink != 1:
            raise _error("EDIT_UNSAFE_HARDLINK", "拒绝硬链接文件，避免修改影响其他路径。")
        if before.st_size > max_bytes:
            raise _error("EDIT_FILE_SIZE_LIMIT", "修改文件超过单文件大小上限。")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _error("EDIT_FILE_SIZE_LIMIT", "读取期间修改文件超过大小上限。")
        after = os.fstat(fd)
        signature = lambda item: (item.st_size, item.st_mtime_ns, item.st_ctime_ns, item.st_ino, item.st_nlink)
        if signature(before) != signature(after):
            raise _error("EDIT_WORKSPACE_CHANGED", "收集差异期间修改文件发生变化。")
        _chain(path)
        _no_streams(path)
        current = path.stat()
        identity = lambda item: (item.st_size, item.st_mtime_ns, item.st_ino, item.st_dev, item.st_nlink)
        if identity(after) != identity(current):
            raise _error("EDIT_WORKSPACE_CHANGED", "收集差异期间修改文件被替换。")
        return b"".join(chunks)
    except OSError as exc:
        raise _error("EDIT_WORKSPACE_CHANGED", "收集差异期间修改文件不可用。") from exc
    finally:
        os.close(fd)


def _scan(root: Path, max_file_bytes: int, max_total_bytes: int) -> tuple[dict[str, bytes], set[str]]:
    _chain(root)
    if not root.is_dir():
        raise _error("EDIT_WORKSPACE_UNAVAILABLE", "修改工作区不存在。")
    _no_streams(root)
    contents: dict[str, bytes] = {}
    directories: set[str] = set()
    identities: set[tuple[int, int]] = set()
    aliases: set[str] = set()
    stack = [(root, "", 0)]
    entries_count = total = 0
    while stack:
        directory, prefix, depth = stack.pop()
        _chain(directory)
        if depth > MAX_EDIT_DEPTH:
            raise _error("EDIT_ENTRY_LIMIT", "修改目录层数超过上限。")
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_count += 1
                    if entries_count > MAX_EDIT_ENTRIES:
                        raise _error("EDIT_ENTRY_LIMIT", "修改工作区目录项数量超过上限。")
                    entries.append(entry)
            for entry in sorted(entries, key=lambda value: value.name):
                relative = entry.name if not prefix else prefix + "/" + entry.name
                if _relative(relative) != relative or relative.casefold() in aliases:
                    raise _error("EDIT_PATH_ALIAS", "修改工作区存在歧义或大小写冲突路径。")
                aliases.add(relative.casefold())
                path = root / relative
                _chain(path)
                _no_streams(path)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    directories.add(relative)
                    stack.append((path, relative, depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    if len(contents) >= MAX_EDIT_FILES:
                        raise _error("EDIT_FILE_COUNT_LIMIT", "修改文件数量超过上限。")
                    identity = (info.st_dev, info.st_ino)
                    if info.st_nlink != 1 or identity in identities:
                        raise _error("EDIT_UNSAFE_HARDLINK", "修改文件存在硬链接或文件身份别名。")
                    identities.add(identity)
                    data = _read_file(path, max_file_bytes)
                    total += len(data)
                    if total > max_total_bytes:
                        raise _error("EDIT_SNAPSHOT_SIZE_LIMIT", "修改工作区总文件大小超过上限。")
                    contents[relative] = data
                else:
                    raise _error("EDIT_UNSAFE_FILE", "修改工作区包含非普通文件。")
        except OSError as exc:
            raise _error("EDIT_WORKSPACE_CHANGED", "收集期间修改工作区目录发生变化。") from exc
    return contents, directories


def _baseline(snapshot: Snapshot, max_file_bytes: int, max_total_bytes: int) -> tuple[dict[str, bytes], set[str]]:
    rows: dict[str, dict[str, Any]] = {}
    try:
        for row in snapshot.manifest:
            name = _relative(row["path"])
            size = row["size_bytes"]
            if name != row["path"] or name in rows or isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError("invalid manifest")
            rows[name] = row
        if not rows:
            raise ValueError("empty manifest")
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("EDIT_BASELINE_INVALID", "修改基线元数据无效。") from exc
    contents, directories = _scan(_absolute(snapshot.source_dir), max_file_bytes, max_total_bytes)
    if set(contents) != set(rows):
        raise _error("EDIT_BASELINE_CHANGED", "不可变快照的文件清单发生变化。")
    for name, data in contents.items():
        if len(data) != rows[name]["size_bytes"] or hashlib.sha256(data).hexdigest() != rows[name]["sha256"]:
            raise _error("EDIT_BASELINE_CHANGED", "不可变快照的文件内容发生变化。")
    return contents, directories


def prepare_edit_workspace(snapshot: Snapshot, destination: Path) -> Path:
    """复制实际字节并赋予副本写权限；不用硬链接或同目录覆盖基线。"""
    source, destination = _absolute(snapshot.source_dir), _absolute(destination)
    _chain(source)
    _chain(destination)
    try:
        overlap = _overlap(source, destination)
    except ValueError:  # Windows 不同盘符没有公共父路径。
        overlap = False
    if overlap:
        raise _error("EDIT_DESTINATION_INVALID", "修改目录必须独立于不可变快照。")
    if destination.exists():
        raise _error("EDIT_DESTINATION_EXISTS", "修改目录已存在，拒绝覆盖。")
    contents, _ = _baseline(snapshot, _MAX_PREPARE_BYTES, _MAX_PREPARE_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _chain(destination.parent)
    temporary = destination.parent / (".edit-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    try:
        for relative, data in contents.items():
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _chain(target.parent)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        copied, _ = _scan(temporary, _MAX_PREPARE_BYTES, _MAX_PREPARE_BYTES)
        if copied != contents or _baseline(snapshot, _MAX_PREPARE_BYTES, _MAX_PREPARE_BYTES)[0] != contents:
            raise _error("EDIT_BASELINE_CHANGED", "建立修改副本期间不可变快照发生变化。")
        _chain(destination)
        if destination.exists():
            raise _error("EDIT_DESTINATION_EXISTS", "建立副本期间修改目录已被创建。")
        os.rename(temporary, destination)
    except Exception:
        # 只清本函数刚创建的随机目录；无模型在准备阶段参与运行。
        if temporary.exists() and temporary.parent == destination.parent and temporary.name.startswith(".edit-"):
            _chain(temporary)
            shutil.rmtree(temporary)
        raise
    return destination


def _scopes(values: Any, baseline_files: set[str], baseline_dirs: set[str]) -> list[tuple[str, bool]]:
    if not isinstance(values, (tuple, list)) or not values:
        raise _error("EDIT_EMPTY_SCOPE", "修改授权或请求范围不能为空。")
    result = []
    for value in values:
        path = _relative(value, allow_root=True)
        # 基线中的文件只能 exact 匹配。缺失的带扩展名文件也按 exact；
        # 缺失目录使用目录前缀，显式尾斜线可表达带点目录。
        directory = path == "." or path in baseline_dirs or (
            path not in baseline_files and (str(value).endswith(("/", "\\")) or not Path(path).suffix)
        )
        result.append((path, directory))
    return sorted(set(result))


def _within(path: str, scope: tuple[str, bool]) -> bool:
    value, directory = scope
    return value == "." or path == value or (directory and path.startswith(value + "/"))


def _blocked(path: str, snapshot: Snapshot, patterns: tuple[str, ...]) -> bool:
    if _excluded_reason(path, patterns):
        return True
    parts = path.split("/")
    for index in range(1, len(parts) + 1):
        prefix = "/".join(parts[:index])
        if _excluded_reason(prefix, ()) or _excluded_reason(prefix + "/__edit_entry__", ()):
            return True
    return any(path == row.get("path") or path.startswith(str(row.get("path")) + "/") for row in snapshot.excluded)


def _text(data: bytes) -> str:
    if any(value < 32 and value not in (9, 10, 13) or value == 127 for value in data):
        raise _error("EDIT_BINARY_CONTENT", "修改差异只接受无二进制内容的 UTF-8 文件。")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("EDIT_NON_UTF8", "修改文件不是合法 UTF-8，拒绝生成可合并差异。") from exc


def _no_secret(text: str, secrets: tuple[str, ...]) -> None:
    # 两条保存链必须一致：完整可合并内容不能在保存后才被脱敏改变哈希。
    from .report import redact_text as redact_report
    if redact_report(text, secrets) != text or redact_diagnostic(text, secrets, max_chars=max(4096, len(text) * 2 + 1024)) != text:
        raise _error("EDIT_SECRET_DETECTED", "修改内容或差异命中凭证规则，拒绝回传可合并内容。")


def _diff(path: str, before: bytes | None, after: bytes | None) -> str:
    first, second = _text(before or b""), _text(after or b"")
    def lf_lines(text: str) -> list[str]:
        # unified diff/Git 仅以 LF 定义行；str.splitlines 还会拆 U+2028、
        # NEL、CR-only，导致 hunk 行数和原文件的真实字节不一致。
        parts = text.split("\n")
        return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    generated = difflib.unified_diff(
        lf_lines(first), lf_lines(second),
        fromfile="/dev/null" if before is None else "a/" + path,
        tofile="/dev/null" if after is None else "b/" + path, lineterm="\n",
    )
    lines = []
    for line in generated:
        lines.append(line)
        if not line.endswith("\n"):
            lines.append("\n\\ No newline at end of file\n")
    return "".join(lines)


def collect_edit_changes(
    snapshot: Snapshot, workspace_dir: Path, *, allowed_paths: tuple[str, ...],
    relative_paths: list[str] | None, max_file_bytes: int, max_snapshot_bytes: int,
    max_output_bytes: int, secrets: tuple[str, ...] = (), exclude_patterns: tuple[str, ...] = (),
) -> dict[str, Any]:
    """只采信停止后工作区的真实字节，拒绝越权及不能完整回传的差异。"""
    for limit in (max_file_bytes, max_snapshot_bytes, max_output_bytes):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PREPARE_BYTES:
            raise _error("EDIT_INVALID_LIMIT", "修改模式大小上限必须为有界正整数。")
    if relative_paths is not None and not isinstance(relative_paths, list):
        raise _error("EDIT_INVALID_SCOPE", "修改范围必须是相对路径数组。")
    if not isinstance(exclude_patterns, tuple) or any(not isinstance(item, str) for item in exclude_patterns):
        raise _error("EDIT_INVALID_SCOPE", "修改排除规则必须来自可信配置。")
    root, source = _absolute(workspace_dir), _absolute(snapshot.source_dir)
    _chain(root)
    _chain(source)
    try:
        overlap = _overlap(source, root)
    except ValueError:
        overlap = False
    if overlap:
        raise _error("EDIT_DESTINATION_INVALID", "修改工作区不能与不可变快照重叠。")
    before, original_dirs = _baseline(snapshot, max_file_bytes, max_snapshot_bytes)
    allowed = _scopes(allowed_paths, set(before), original_dirs)
    baseline_scope = _scopes(snapshot.review_scope, set(before), original_dirs)
    requested = _scopes(relative_paths if relative_paths is not None else snapshot.review_scope, set(before), original_dirs)
    if any(not any(_within(scope[0], auth) for auth in allowed) or not any(_within(scope[0], auth) for auth in baseline_scope) for scope in requested):
        raise _error("EDIT_PATH_NOT_AUTHORIZED", "修改请求范围超出授权或原始快照范围。")
    after, after_dirs = _scan(root, max_file_bytes, max_snapshot_bytes)
    baseline_aliases = {path.casefold(): path for path in before}
    for path in after:
        if path.casefold() in baseline_aliases and baseline_aliases[path.casefold()] != path:
            raise _error("EDIT_PATH_ALIAS", "不接受仅大小写不同的文件替换。")
        if _blocked(path, snapshot, exclude_patterns):
            raise _error("EDIT_EXCLUDED_PATH", "修改工作区包含已排除或敏感路径。")
    for directory in after_dirs:
        if directory in before:
            raise _error("EDIT_FILE_REPLACED_BY_DIRECTORY", "基线文件不能被替换成目录。")
        if any(directory == value and not is_directory for scopes in (allowed, baseline_scope, requested) for value, is_directory in scopes):
            raise _error("EDIT_FILE_REPLACED_BY_DIRECTORY", "单文件修改范围不能被替换成目录。")
        if _blocked(directory, snapshot, exclude_patterns):
            raise _error("EDIT_EXCLUDED_PATH", "修改工作区包含已排除目录。")
        if directory not in original_dirs and not all(
            any(_within(directory, scope) or scope[0].startswith(directory + "/") for scope in scopes)
            for scopes in (allowed, baseline_scope, requested)
        ):
            raise _error("EDIT_PATH_NOT_AUTHORIZED", "新增目录超出修改授权范围。")
    changes = []
    changes_encoded_bytes = 2
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if not all(any(_within(path, scope) for scope in scopes) for scopes in (allowed, baseline_scope, requested)):
            raise _error("EDIT_PATH_NOT_AUTHORIZED", "真实文件变化超出授权修改范围。")
        if _blocked(path, snapshot, exclude_patterns):
            raise _error("EDIT_EXCLUDED_PATH", "拒绝修改或创建已排除的源路径。")
        diff = _diff(path, old, new)
        after_text = None if new is None else _text(new)
        for text in (path, diff, after_text or ""):
            _no_secret(text, secrets)
        change = {
            "path": path, "change_type": "add" if old is None else "delete" if new is None else "modify",
            "before_sha256": None if old is None else hashlib.sha256(old).hexdigest(),
            "after_sha256": None if new is None else hashlib.sha256(new).hexdigest(),
            "before_size_bytes": len(old or b""), "after_size_bytes": len(new or b""),
            "diff": diff, "after_text": after_text,
        }
        changes_encoded_bytes += len(json.dumps(change, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + (1 if changes else 0)
        if changes_encoded_bytes > max_output_bytes:
            raise _error("EDIT_OUTPUT_LIMIT", "完整差异与修改后文本超过回传上限，拒绝截断。")
        changes.append(change)
    # 模型应已停止，但仍复扫，避免把变化中的副本或被篡改基线作为合并证据。
    if _scan(root, max_file_bytes, max_snapshot_bytes) != (after, after_dirs):
        raise _error("EDIT_WORKSPACE_CHANGED", "收集差异期间工作区发生变化。")
    if _baseline(snapshot, max_file_bytes, max_snapshot_bytes) != (before, original_dirs):
        raise _error("EDIT_BASELINE_CHANGED", "收集差异期间不可变快照发生变化。")
    digest_input = {"snapshot_id": snapshot.snapshot_id, "changes": changes}
    change_set_id = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    result = {"schema_version": 1, "snapshot_id": snapshot.snapshot_id, "changes": changes,
              "patch_text": "".join(change["diff"] for change in changes), "workspace_dir": str(root),
              "change_set_id": change_set_id, "source_applied": False}
    _no_secret(str(root), secrets)
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_output_bytes:
        raise _error("EDIT_OUTPUT_LIMIT", "完整修改结果超过回传上限，拒绝截断。")
    return result
