"""将可信项目配置中的已授权文件复制成可校验的只读快照。"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid
from typing import Any


class SnapshotError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


@dataclass(frozen=True)
class Snapshot:
    source_dir: Path
    snapshot_id: str
    manifest: list[dict[str, Any]]
    git_commit: str | None
    git_dirty: bool
    review_scope: list[str]
    excluded: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": str(self.source_dir), "snapshot_id": self.snapshot_id,
            "manifest": self.manifest, "git_commit": self.git_commit,
            "git_dirty": self.git_dirty, "review_scope": self.review_scope,
            "excluded": self.excluded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        try:
            source = Path(data["source_dir"])
            manifest = data["manifest"]
            if not source.is_absolute() or source.name != "files" or not isinstance(manifest, list) or not manifest:
                raise ValueError("invalid source or manifest")
            paths: set[str] = set()
            for row in manifest:
                relative = normalize_relative_path(row["path"])
                if relative != row["path"] or relative in paths:
                    raise ValueError("duplicate or noncanonical path")
                paths.add(relative)
                if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) or isinstance(row["size_bytes"], bool) or not isinstance(row["size_bytes"], int) or row["size_bytes"] < 0:
                    raise ValueError("invalid file digest or size")
            if not isinstance(data["git_dirty"], bool) or not isinstance(data["review_scope"], list) or not isinstance(data["excluded"], list):
                raise ValueError("invalid metadata types")
            return cls(
                source, str(data["snapshot_id"]), manifest,
                data.get("git_commit"), bool(data["git_dirty"]),
                data["review_scope"], data["excluded"],
            )
        except (KeyError, TypeError, ValueError, SnapshotError) as exc:
            raise SnapshotError("SNAPSHOT_METADATA_INVALID", "快照进程返回了无效元数据。") from exc


_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "dist", "build", "coverage", ".next", ".nuxt", ".tox", ".idea",
}
_SECRET_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml", "secret.json",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"}
_BINARY_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".class", ".jar", ".war",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".tar", ".iso",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".mp3", ".mp4", ".wav", ".woff", ".woff2", ".ttf", ".db",
    ".sqlite", ".sqlite3", ".sql", ".bak", ".dump", ".blend",
}
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]|CONIN\$|CONOUT\$)(?:\.|$)", re.I)


def normalize_relative_path(value: str, *, allow_root: bool = False) -> str:
    """统一跨平台路径；在任何平台都拒绝 Windows 的另一条逃逸语义。"""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SnapshotError("INVALID_PATH", "文件范围必须是非空项目内相对路径。")
    value = value.replace("\\", "/")
    if value.startswith("/") or ":" in value or any(ord(c) < 32 for c in value):
        raise SnapshotError("INVALID_PATH", "拒绝绝对路径、盘符、UNC、ADS 或控制字符。")
    segments = value.split("/")
    if ".." in segments:
        raise SnapshotError("INVALID_PATH", "文件范围不能包含上级目录。")
    segments = [s for s in segments if s not in ("", ".")]
    if not segments:
        if allow_root:
            return "."
        raise SnapshotError("INVALID_PATH", "问题引用必须定位到具体文件。")
    if any(s.endswith((".", " ")) or _RESERVED.match(s) for s in segments):
        raise SnapshotError("INVALID_PATH", "拒绝含 Windows 设备名或歧义尾部字符的路径。")
    return "/".join(segments)


def _within(relative: str, scope: str) -> bool:
    return scope == "." or relative == scope or relative.startswith(scope + "/")


def _check_path_chain(path: Path) -> None:
    # 在 resolve 前检查每层祖先，防止 junction 被规范化后隐藏。
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SnapshotError("UNSAFE_LINK", "项目路径包含符号链接或 Windows reparse point。")


_GIT_FIXED_READS = {
    ("rev-parse", "--is-inside-work-tree"),
    ("rev-parse", "--show-object-format"),
    ("rev-parse", "--verify", "--quiet", "HEAD"),
    ("ls-files", "--stage", "-z", "--cached"),
    ("symbolic-ref", "--quiet", "HEAD"),
}


def _git(root: Path, *args: str, missing_ok: bool = False) -> bytes | None:
    fixed_read = args in _GIT_FIXED_READS
    tree_read = len(args) == 5 and args[:4] == ("ls-tree", "-r", "-z", "--full-tree") and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args[4])
    unborn_read = len(args) == 4 and args[:3] == ("show-ref", "--verify", "--quiet") and args[3].startswith("refs/heads/")
    if not (fixed_read or tree_read or unborn_read):
        raise SnapshotError("GIT_COMMAND_INVALID", "快照只允许读取固定的 Git 元数据。")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    # status/diff/ls-files --modified 会调用工作树 clean filter。这里仅读
    # index/HEAD，不请求工作树转换；缺对象必须失败，不能 lazy fetch 远端 helper。
    # --no-lazy-fetch 不被旧版 Git 支持时同样失败关闭，不退回不安全命令。
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    command = ["git", "--no-lazy-fetch", "--no-optional-locks", "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("GIT_UNAVAILABLE", "Git 元数据读取失败或超时。") from exc
    if missing_ok and result.returncode == 1:
        return None
    if result.returncode:
        raise SnapshotError("GIT_REQUIRED", "首版只接受可读取 Git 元数据的项目。")
    if len(result.stdout) > 32 * 1024 * 1024:
        raise SnapshotError("GIT_METADATA_LIMIT", "Git 文件清单过大，请缩小项目范围。")
    return result.stdout


@dataclass(frozen=True)
class _GitState:
    commit: str | None
    object_format: str
    index: bytes
    tree: bytes


def _git_state(root: Path) -> _GitState:
    if _git(root, "rev-parse", "--is-inside-work-tree") != b"true\n":
        raise SnapshotError("GIT_REQUIRED", "快照项目必须是 Git 工作区。")
    object_format = _git(root, "rev-parse", "--show-object-format").decode("ascii").strip()
    if object_format not in {"sha1", "sha256"}:
        raise SnapshotError("GIT_METADATA_INVALID", "Git 对象格式不受支持。")
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", missing_ok=True)
    if head is None:
        # 仅当前分支确实不存在时视为 unborn；坏 HEAD、缺提交对象和坏引用
        # 不能伪装成空仓库，也不能借恢复分支检查触发仓库程序。
        branch = os.fsdecode(_git(root, "symbolic-ref", "--quiet", "HEAD")).strip()
        if not branch.startswith("refs/heads/") or _git(root, "show-ref", "--verify", "--quiet", branch, missing_ok=True) is not None:
            raise SnapshotError("GIT_METADATA_INVALID", "无法确认 Git 未提交分支状态。")
        commit = None
    else:
        commit = head.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}" if object_format == "sha1" else r"[0-9a-f]{64}", commit):
            raise SnapshotError("GIT_METADATA_INVALID", "Git 提交对象标识无效。")
    index = _git(root, "ls-files", "--stage", "-z", "--cached")
    tree = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit) if commit is not None else b""
    return _GitState(commit, object_format, index, tree)


def _git_entries(state: _GitState) -> tuple[dict[str, list[tuple[str, str, int]]], bool]:
    """只比较 index 和 HEAD 元数据，不为 dirty 标记读取授权外源文件。"""
    index: dict[str, list[tuple[str, str, int]]] = {}
    tree: dict[str, tuple[str, str]] = {}
    digest_pattern = r"[0-9a-f]{40}" if state.object_format == "sha1" else r"[0-9a-f]{64}"
    try:
        for record in state.index.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
            if not re.fullmatch(r"[0-7]{6}", mode) or not re.fullmatch(digest_pattern, oid) or stage not in {"0", "1", "2", "3"}:
                raise ValueError("invalid index entry")
            path = os.fsdecode(name)
            index.setdefault(path, []).append((mode, oid, int(stage)))
        for record in state.tree.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, object_type, oid = metadata.decode("ascii").split(" ")
            path = os.fsdecode(name)
            if not re.fullmatch(r"[0-7]{6}", mode) or not re.fullmatch(digest_pattern, oid) or object_type not in {"blob", "commit"} or path in tree:
                raise ValueError("invalid tree entry")
            tree[path] = (mode, oid)
    except (UnicodeError, ValueError) as exc:
        raise SnapshotError("GIT_METADATA_INVALID", "Git 文件条目元数据无效。") from exc
    staged = {path: rows[0][:2] for path, rows in index.items() if len(rows) == 1 and rows[0][2] == 0}
    dirty = any(len(rows) != 1 or rows[0][2] != 0 for rows in index.values()) or staged != tree
    return index, dirty


def _blob_digest(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    digest.update(data)
    return digest.hexdigest()


def _handle_final_path(fd: int) -> Path | None:
    if os.name != "nt":
        proc_path = Path(f"/proc/self/fd/{fd}")
        return Path(os.readlink(proc_path)) if proc_path.exists() else None
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    final_path.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = final_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise SnapshotError("PATH_CHECK_FAILED", "无法验证文件句柄对应的实际路径。")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _read_verified(path: Path, max_bytes: int) -> bytes:
    _check_path_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SnapshotError("SNAPSHOT_SOURCE_UNAVAILABLE", "快照源文件不可读取。") from exc
    try:
        final = _handle_final_path(fd)
        if final is not None and os.path.normcase(str(final)) != os.path.normcase(str(path)):
            raise SnapshotError("UNSAFE_LINK", "文件实际句柄路径与授权路径不符。")
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SnapshotError("UNSAFE_FILE", "快照只允许普通文件。")
        if before.st_nlink != 1:
            raise SnapshotError("UNSAFE_HARDLINK", "拒绝源硬链接文件，避免把其他路径内容带入快照。")
        if before.st_size > max_bytes:
            raise SnapshotError("FILE_SIZE_LIMIT", "单个文件超过管理员配置的大小上限。")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise SnapshotError("FILE_SIZE_LIMIT", "读取期间文件超过大小上限。")
        after = os.fstat(fd)
        signature = lambda info: (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_nlink)
        if signature(before) != signature(after):
            raise SnapshotError("SNAPSHOT_CHANGED", "复制期间源文件发生变化，请重新提交。")
        _check_path_chain(path)
        try:
            current = path.stat()
        except OSError as exc:
            raise SnapshotError("SNAPSHOT_CHANGED", "读取期间源文件被删除或替换。") from exc
        # Windows 的 stat 与 fstat 对 ctime 的兼容语义可能不同；同句柄
        # 前后仍比较 ctime，路径替换则用 inode、device、大小和修改时间。
        identity = lambda info: (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, info.st_nlink)
        if identity(after) != identity(current):
            raise SnapshotError("SNAPSHOT_CHANGED", "复制期间源文件被替换，请重新提交。")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _excluded_reason(relative: str, patterns: tuple[str, ...]) -> str | None:
    parts = relative.lower().split("/")
    name = parts[-1]
    if any(part in _EXCLUDED_DIRS for part in parts[:-1]):
        return "dependency_or_cache"
    if name.startswith(".env") or name in _SECRET_NAMES or name.startswith(("id_rsa", "id_ed25519", "service-account")):
        return "sensitive_filename"
    suffix = Path(name).suffix
    if suffix in _SECRET_SUFFIXES:
        return "private_key_or_credential"
    if suffix in _BINARY_SUFFIXES:
        return "binary_or_backup"
    if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
        return "configured_exclusion"
    return None


def _remove_owned_temp(path: Path, parent: Path) -> None:
    if path.parent.resolve() != parent.resolve() or not path.name.startswith(".snapshot-"):
        raise SnapshotError("CLEANUP_PATH_INVALID", "快照临时目录清理范围无效。")
    def writable_remove(function: Any, name: str, _info: Any) -> None:
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)
    shutil.rmtree(path, onerror=writable_remove)


def create_snapshot(
    project: Any, relative_paths: list[str] | None, destination: Path,
    max_file_bytes: int = 2097152, max_total_bytes: int = 104857600,
) -> Snapshot:
    if isinstance(max_file_bytes, bool) or isinstance(max_total_bytes, bool) or not isinstance(max_file_bytes, int) or not isinstance(max_total_bytes, int) or min(max_file_bytes, max_total_bytes) < 1:
        raise SnapshotError("INVALID_LIMIT", "快照大小上限必须为正整数。")
    if relative_paths is not None and not isinstance(relative_paths, list):
        raise SnapshotError("INVALID_SCOPE", "审查文件范围必须为相对路径数组。")
    root = Path(os.path.abspath(project.path))
    _check_path_chain(root)
    if not root.is_dir():
        raise SnapshotError("PROJECT_UNAVAILABLE", "授权项目目录不存在。")
    allowed = [normalize_relative_path(s, allow_root=True) for s in project.allowed_paths]
    if not allowed:
        raise SnapshotError("EMPTY_AUTHORIZATION", "项目未配置可读取范围。")
    scope = sorted(set(normalize_relative_path(s, allow_root=True) for s in (relative_paths if relative_paths is not None else allowed)))
    if not scope:
        raise SnapshotError("EMPTY_SCOPE", "请求的审查范围不能为空。")
    if any(not any(_within(s, a) for a in allowed) for s in scope):
        raise SnapshotError("PATH_NOT_AUTHORIZED", "请求的文件范围超出管理员授权。")
    for item in scope:
        _check_path_chain(root / item)
    before = _git_state(root)
    index, git_dirty = _git_entries(before)
    tracked = sorted(index)
    # 额外未跟踪文件只能由本地配置逐一授权，不能从工具请求扩大。
    extras = [normalize_relative_path(s) for s in getattr(project, "allow_untracked", ())]
    paths = sorted(set(tracked + extras))
    excluded: list[dict[str, str]] = []
    contents: dict[str, bytes] = {}
    total = 0
    for raw_path in paths:
        relative = normalize_relative_path(raw_path)
        if not any(_within(relative, a) for a in allowed) or not any(_within(relative, s) for s in scope):
            continue
        reason = _excluded_reason(relative, project.exclude_patterns)
        if reason:
            excluded.append({"path": relative, "reason": reason})
            continue
        path = root / relative
        _check_path_chain(path)
        if not path.exists():
            if relative in index:
                git_dirty = True
            excluded.append({"path": relative, "reason": "tracked_file_deleted"})
            continue
        if not path.is_file():
            excluded.append({"path": relative, "reason": "non_regular_file_or_submodule"})
            continue
        try:
            data = _read_verified(path, max_file_bytes)
        except SnapshotError as exc:
            if exc.code == "FILE_SIZE_LIMIT":
                exc.details = {"path": relative, "limit_bytes": max_file_bytes, "excluded": excluded}
            raise
        if b"\0" in data:
            excluded.append({"path": relative, "reason": "binary_content"})
            continue
        total += len(data)
        if total > max_total_bytes:
            raise SnapshotError("SNAPSHOT_SIZE_LIMIT", "授权文件总大小超限，请缩小审查范围。", {"path": relative, "limit_bytes": max_total_bytes, "excluded": excluded})
        contents[relative] = data
        entries = index.get(relative, [])
        if len(entries) != 1 or entries[0][2] != 0 or entries[0][0] not in {"100644", "100755"} or _blob_digest(data, before.object_format) != entries[0][1]:
            git_dirty = True
    if not contents:
        raise SnapshotError("EMPTY_SNAPSHOT", "授权范围中没有可审查的已保存文件。", {"excluded": excluded})
    manifest = [{"path": p, "size_bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(), "line_count": len(b.decode("utf-8", errors="replace").splitlines())} for p, b in contents.items()]
    # 先读取授权文件，再逐一复读；Git HEAD/index/tree 元数据也必须一致。
    for relative, data in contents.items():
        if _read_verified(root / relative, max_file_bytes) != data:
            raise SnapshotError("SNAPSHOT_CHANGED", "快照创建期间源文件变化，请重新提交。")
    if before != _git_state(root):
        raise SnapshotError("SNAPSHOT_CHANGED", "快照创建期间 Git 文件版本或工作区状态变化。")
    metadata = {"schema_version": 1, "project_id": project.id, "manifest": manifest, "git_commit": before.commit, "git_dirty": git_dirty, "review_scope": scope, "excluded": excluded}
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    snapshot_id = hashlib.sha256(serialized).hexdigest()
    destination = Path(os.path.abspath(destination))
    _check_path_chain(destination)
    if destination.exists():
        raise SnapshotError("SNAPSHOT_DESTINATION_EXISTS", "快照目标目录已存在，拒绝覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".snapshot-" + uuid.uuid4().hex)
    try:
        source_dir = temporary / "files"
        source_dir.mkdir(parents=True)
        for relative, data in contents.items():
            target = source_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        (temporary / "manifest.json").write_bytes(serialized)
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists():
            _remove_owned_temp(temporary, destination.parent)
        raise
    return Snapshot(destination / "files", snapshot_id, manifest, before.commit, git_dirty, scope, excluded)


def verify_snapshot(snapshot: Snapshot) -> bool:
    """执行前后核对副本，避免把被改写的副本继续当成原始证据。"""
    expected = {row["path"] for row in snapshot.manifest}
    actual: set[str] = set()
    _check_path_chain(snapshot.source_dir)
    for path in snapshot.source_dir.rglob("*"):
        _check_path_chain(path)
        if path.is_file():
            actual.add(path.relative_to(snapshot.source_dir).as_posix())
        elif not path.is_dir():
            raise SnapshotError("SNAPSHOT_CHANGED", "快照含有非普通文件。")
    if actual != expected:
        raise SnapshotError("SNAPSHOT_CHANGED", "快照文件清单发生变化。")
    for row in snapshot.manifest:
        data = _read_verified(snapshot.source_dir / row["path"], row["size_bytes"])
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise SnapshotError("SNAPSHOT_CHANGED", "快照内容校验失败。")
    return True
