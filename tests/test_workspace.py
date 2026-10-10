from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from harness_mcp import workspace
from harness_mcp.workspace import SnapshotError, create_snapshot, normalize_relative_path, verify_snapshot


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "项目 有空格"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.invalid", "commit", "-qm", "initial")
    return root


def project(root: Path, **kwargs):
    return SimpleNamespace(id="demo", path=root, allowed_paths=(".",), exclude_patterns=(), allow_untracked=(), **kwargs)


def test_tracked_only_snapshot_has_hash_and_git_provenance(repository, tmp_path):
    (repository / "untracked.py").write_text("untracked\n")
    (repository / "src" / "main.py").write_text("def add(a, b):\n    return a - b\n")
    snapshot = create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert [r["path"] for r in snapshot.manifest] == ["src/main.py"]
    assert snapshot.git_commit and snapshot.git_dirty
    assert len(snapshot.snapshot_id) == 64
    assert snapshot.manifest[0]["line_count"] == 2
    assert snapshot.source_dir.joinpath("src/main.py").read_text().endswith("a - b\n")
    assert verify_snapshot(snapshot)
    assert snapshot.to_dict()["source_dir"] == str(snapshot.source_dir)
    assert workspace.Snapshot.from_dict(snapshot.to_dict()) == snapshot


@pytest.mark.parametrize("bad", ["../other", "src/../main.py", "C:/Users/a", "C:relative", "\\\\server\\share", "/etc/passwd", "src/main.py:secret", "src/CON.txt", "src/file. ", "src/\x00bad"])
def test_windows_and_posix_escape_paths_rejected(bad):
    with pytest.raises(SnapshotError) as exc:
        normalize_relative_path(bad)
    assert exc.value.code == "INVALID_PATH"


def test_authorization_cannot_be_expanded_by_relative_paths(repository, tmp_path):
    config = project(repository)
    config.allowed_paths = ("src",)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(config, ["."], tmp_path / "snapshot")
    assert exc.value.code == "PATH_NOT_AUTHORIZED"
    snapshot = create_snapshot(config, ["src/main.py"], tmp_path / "snapshot")
    assert snapshot.review_scope == ["src/main.py"]


def test_sensitive_dependency_binary_and_custom_exclusions_recorded(repository, tmp_path):
    files = {".env.local": "token=secret", "private.pem": "secret", "node_modules/dependency.js": "code", "data.bin": "a\x00b", "docs/private.md": "private", "secrets.json": "{}"}
    for name, text in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repository, "add", "-f", ".")
    config = project(repository)
    config.exclude_patterns = ("docs/*",)
    snapshot = create_snapshot(config, None, tmp_path / "snapshot")
    assert {e["path"] for e in snapshot.excluded} == set(files)
    assert [m["path"] for m in snapshot.manifest] == ["src/main.py"]


def test_size_limits_refuse_instead_of_truncating(repository, tmp_path):
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "one", max_file_bytes=5)
    assert exc.value.code == "FILE_SIZE_LIMIT"
    assert exc.value.details["path"] == "src/main.py"
    assert not (tmp_path / "one").exists()
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "two", max_total_bytes=5)
    assert exc.value.code == "SNAPSHOT_SIZE_LIMIT"
    assert not (tmp_path / "two").exists()


def test_source_mutation_during_snapshot_is_refused(repository, tmp_path, monkeypatch):
    original = workspace._read_verified
    calls = 0
    def changing(path, max_bytes):
        nonlocal calls
        data = original(path, max_bytes)
        calls += 1
        if calls == 1:
            path.write_bytes(b"changed while copying\n")
        return data
    monkeypatch.setattr(workspace, "_read_verified", changing)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "SNAPSHOT_CHANGED"


def test_symlink_file_and_symlink_parent_rejected(repository, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("private\n")
    link = repository / "src" / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("该测试环境未提供符号链接创建权限。")
    git(repository, "add", "src/link.py")
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "UNSAFE_LINK"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 专项")
def test_windows_junction_parent_rejected(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("secret\n")
    junction = repository / "linked"
    created = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    if created.returncode:
        pytest.skip("该测试环境无法创建 Windows junction。")
    try:
        with pytest.raises(SnapshotError) as exc:
            create_snapshot(project(repository), ["linked/hidden.py"], tmp_path / "snapshot")
        assert exc.value.code == "UNSAFE_LINK"
    finally:
        # 只解除当前测试创建的 junction，不遍历或删除它指向的目录。
        os.rmdir(junction)
    assert (outside / "hidden.py").exists()


def test_untracked_files_require_local_explicit_authorization(repository, tmp_path):
    extra = repository / "src" / "extra.py"
    extra.write_text("allowed only by administrator\n")
    config = project(repository)
    config.allow_untracked = ("src/extra.py",)
    snapshot = create_snapshot(config, ["src"], tmp_path / "snapshot")
    assert {row["path"] for row in snapshot.manifest} == {"src/main.py", "src/extra.py"}


def test_snapshot_copy_modification_is_detected(repository, tmp_path):
    snapshot = create_snapshot(project(repository), None, tmp_path / "snapshot")
    target = snapshot.source_dir / "src/main.py"
    target.chmod(0o600)
    target.write_text("tampered\n")
    with pytest.raises(SnapshotError):
        verify_snapshot(snapshot)


def test_snapshot_rejects_added_symlink_directory(repository, tmp_path):
    snapshot = create_snapshot(project(repository), None, tmp_path / "snapshot")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    try:
        (snapshot.source_dir / "extra").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("该环境无法创建符号链接目录。")
    with pytest.raises(SnapshotError) as exc:
        verify_snapshot(snapshot)
    assert exc.value.code == "UNSAFE_LINK"


def test_snapshot_worker_metadata_rejects_malformed_paths():
    with pytest.raises(SnapshotError) as exc:
        workspace.Snapshot.from_dict({"source_dir": ".", "manifest": []})
    assert exc.value.code == "SNAPSHOT_METADATA_INVALID"


def configure_marker_filter(root: Path, tmp_path: Path, name: str) -> Path:
    """真实仓库 clean helper 只写测试标记并透传内容，不接触其他文件。"""
    marker = tmp_path / (name + "-marker.txt")
    helper = tmp_path / (name + "-helper.py")
    helper.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('filter executed')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8",
    )
    command = shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(helper.as_posix())
    git(root, "config", "filter.snapshot-marker.clean", command)
    git(root, "config", "filter.snapshot-marker.required", "true")
    return marker


def test_snapshot_does_not_execute_real_clean_filter(repository, tmp_path):
    marker = configure_marker_filter(repository, tmp_path, "authorized")
    (repository / ".gitattributes").write_text("src/main.py filter=snapshot-marker\n")
    saved = b"def add(a, b):\n    return a - b\n"
    (repository / "src/main.py").write_bytes(saved)
    config = project(repository)
    config.allowed_paths = ("src/main.py",)
    snapshot = create_snapshot(config, None, tmp_path / "snapshot")
    assert snapshot.git_dirty
    assert snapshot.source_dir.joinpath("src/main.py").read_bytes() == saved
    assert not marker.exists()
    # 确认标记 helper 确实可执行，且旧 status 路径会触发它。
    git(repository, "status", "--porcelain=v1", "--untracked-files=no")
    assert marker.read_text() == "filter executed"


def test_outside_scope_filter_and_source_are_not_read(repository, tmp_path, monkeypatch):
    outside = repository / "outside.py"
    outside.write_bytes(b"outside original\n")
    git(repository, "add", "outside.py")
    git(repository, "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.invalid", "commit", "-qm", "outside")
    marker = configure_marker_filter(repository, tmp_path, "outside")
    (repository / ".gitattributes").write_text("outside.py filter=snapshot-marker\n")
    outside.write_bytes(b"outside modified\n")
    original_read = workspace._read_verified
    reads = []
    def check_scope(path, max_bytes):
        reads.append(path)
        assert path == repository / "src/main.py"
        return original_read(path, max_bytes)
    monkeypatch.setattr(workspace, "_read_verified", check_scope)
    config = project(repository)
    config.allowed_paths = ("src/main.py",)
    snapshot = create_snapshot(config, None, tmp_path / "snapshot")
    assert not snapshot.git_dirty
    assert reads and not marker.exists()
    git(repository, "status", "--porcelain=v1", "--untracked-files=no")
    assert marker.exists()


def test_missing_promisor_tree_fails_without_remote_helper(repository, tmp_path):
    marker = tmp_path / "remote-marker.txt"
    helper = tmp_path / "remote-helper.py"
    helper.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('remote executed')\n", encoding="utf-8")
    # ext 远端在这里仅启动受控测试 helper。先验证缺对象情形，快照不得联网或执行它。
    remote_command = "ext::" + sys.executable.replace("\\", "/").replace(" ", "% ") + " " + helper.as_posix().replace(" ", "% ")
    git(repository, "config", "remote.origin.url", remote_command)
    git(repository, "config", "remote.origin.promisor", "true")
    git(repository, "config", "remote.origin.partialclonefilter", "blob:none")
    git(repository, "config", "protocol.ext.allow", "always")
    tree = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True).stdout.decode().strip()
    tree_path = repository / ".git/objects" / tree[:2] / tree[2:]
    assert tree_path.is_file()
    tree_path.chmod(0o600)
    tree_path.unlink()
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "GIT_REQUIRED"
    assert not marker.exists() and not (tmp_path / "snapshot").exists()
    # 不带保护的真实 Git 会尝试远端 helper，证明回归确实覆盖 lazy fetch。
    unsafe_probe = subprocess.run(["git", "-C", str(repository), "ls-tree", "-r", "HEAD"], capture_output=True, timeout=15)
    assert unsafe_probe.returncode != 0
    assert marker.read_text() == "remote executed"


def test_git_metadata_commands_force_lazy_fetch_off(repository, monkeypatch):
    original = workspace.subprocess.run
    observed = []
    def checked(command, **kwargs):
        assert "--no-lazy-fetch" in command
        assert "--no-optional-locks" in command
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert "GIT_TRACE" not in kwargs["env"]
        observed.append(command)
        return original(command, **kwargs)
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setattr(workspace.subprocess, "run", checked)
    workspace._git_state(repository)
    assert observed
    for command in observed:
        assert "status" not in command and "diff" not in command and "--modified" not in command


def test_git_without_no_lazy_fetch_support_fails_closed(repository, tmp_path, monkeypatch):
    calls = []
    def old_git(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 129, b"", b"unknown option: --no-lazy-fetch")
    monkeypatch.setattr(workspace.subprocess, "run", old_git)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "GIT_REQUIRED"
    assert len(calls) == 1 and "--no-lazy-fetch" in calls[0]


@pytest.mark.parametrize("command", [("status",), ("diff",), ("ls-files", "--modified"), ("ls-files", "--eol")])
def test_git_helper_rejects_worktree_commands_before_spawning(repository, monkeypatch, command):
    def unexpected(*args, **kwargs):
        raise AssertionError("不应启动工作树转换命令")
    monkeypatch.setattr(workspace.subprocess, "run", unexpected)
    with pytest.raises(SnapshotError) as exc:
        workspace._git(repository, *command)
    assert exc.value.code == "GIT_COMMAND_INVALID"


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_git_object_formats_and_raw_crlf_dirty(tmp_path, object_format):
    root = tmp_path / object_format
    root.mkdir()
    git(root, "init", "-q", "--object-format=" + object_format)
    git(root, "config", "core.autocrlf", "false")
    source = root / "main.py"
    source.write_bytes(b"source\n")
    git(root, "add", "main.py")
    git(root, "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.invalid", "commit", "-qm", "initial")
    clean = create_snapshot(project(root), None, tmp_path / (object_format + "-clean"))
    assert not clean.git_dirty
    assert len(clean.git_commit) == (40 if object_format == "sha1" else 64)
    source.write_bytes(b"source\r\n")
    dirty = create_snapshot(project(root), None, tmp_path / (object_format + "-dirty"))
    assert dirty.git_dirty
    assert dirty.source_dir.joinpath("main.py").read_bytes() == b"source\r\n"


def test_unborn_branch_staged_and_allowed_untracked_files(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=未提交")
    (root / "main.py").write_bytes(b"saved staged source\n")
    git(root, "add", "main.py")
    snapshot = create_snapshot(project(root), None, tmp_path / "staged")
    assert snapshot.git_commit is None and snapshot.git_dirty
    git(root, "rm", "--cached", "main.py")
    config = project(root)
    config.allow_untracked = ("main.py",)
    snapshot = create_snapshot(config, None, tmp_path / "untracked")
    assert snapshot.git_commit is None and snapshot.git_dirty


@pytest.mark.parametrize("change", ["staged_change", "staged_delete", "unstaged_delete"])
def test_global_staged_and_authorized_deleted_dirty(repository, tmp_path, change):
    (repository / "another.py").write_bytes(b"another\n")
    git(repository, "add", "another.py")
    git(repository, "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.invalid", "commit", "-qm", "second")
    if change == "staged_change":
        (repository / "src/main.py").write_bytes(b"modified\n")
        git(repository, "add", "src/main.py")
    elif change == "staged_delete":
        git(repository, "rm", "src/main.py")
    else:
        (repository / "src/main.py").unlink()
    snapshot = create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert snapshot.git_dirty
    if change == "unstaged_delete":
        assert {"path": "src/main.py", "reason": "tracked_file_deleted"} in snapshot.excluded


def test_staged_change_outside_scope_sets_dirty_without_reading_source(repository, tmp_path, monkeypatch):
    outside = repository / "outside.py"
    outside.write_bytes(b"staged addition\n")
    git(repository, "add", "outside.py")
    config = project(repository)
    config.allowed_paths = ("src/main.py",)
    original = workspace._read_verified
    def reading(path, max_bytes):
        assert path == repository / "src/main.py"
        return original(path, max_bytes)
    monkeypatch.setattr(workspace, "_read_verified", reading)
    snapshot = create_snapshot(config, None, tmp_path / "snapshot")
    assert snapshot.git_dirty
    assert [row["path"] for row in snapshot.manifest] == ["src/main.py"]


def test_unmerged_index_sets_dirty_and_copies_current_saved_bytes(repository, tmp_path):
    content = b"conflicted current source\n"
    source = repository / "src/main.py"
    source.write_bytes(content)
    # 构造真实 stage 1/2/3 index；不运行 merge helper 或工作区转换。
    oid = workspace._blob_digest(content, "sha1")
    process = subprocess.run(["git", "-C", str(repository), "hash-object", "-w", "--stdin"], input=content, check=True, capture_output=True)
    assert process.stdout.decode().strip() == oid
    entries = f"0 {'0' * 40}\tsrc/main.py\n" + "".join(f"100644 {oid} {stage}\tsrc/main.py\n" for stage in (1, 2, 3))
    subprocess.run(["git", "-C", str(repository), "update-index", "--index-info"], input=entries.encode(), check=True, capture_output=True)
    snapshot = create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert snapshot.git_dirty
    assert snapshot.source_dir.joinpath("src/main.py").read_bytes() == content
    assert len(snapshot.manifest) == 1


@pytest.mark.parametrize("metadata", ["index", "head"])
def test_git_metadata_change_while_copying_is_refused(repository, tmp_path, monkeypatch, metadata):
    original = workspace._read_verified
    changed = False
    def changing(path, max_bytes):
        nonlocal changed
        data = original(path, max_bytes)
        if not changed:
            changed = True
            (repository / "outside.py").write_bytes(b"new staged source\n")
            git(repository, "add", "outside.py")
            if metadata == "head":
                git(repository, "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.invalid", "commit", "-qm", "changed head")
        return data
    monkeypatch.setattr(workspace, "_read_verified", changing)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "SNAPSHOT_CHANGED"
    assert not (tmp_path / "snapshot").exists()


def test_source_hardlink_is_refused(repository, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside private source\n")
    linked = repository / "src/linked.py"
    os.link(outside, linked)
    config = project(repository)
    config.allow_untracked = ("src/linked.py",)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(config, None, tmp_path / "snapshot")
    assert exc.value.code == "UNSAFE_HARDLINK"
    assert not (tmp_path / "snapshot").exists()
    assert outside.read_bytes() == b"outside private source\n"


def test_source_link_count_change_while_reading_is_refused(repository, tmp_path, monkeypatch):
    original = workspace.os.read
    source = repository / "src/main.py"
    linked = tmp_path / "new-hardlink.py"
    linked_once = False
    def changed_link(fd, size):
        nonlocal linked_once
        chunk = original(fd, size)
        if chunk and not linked_once:
            linked_once = True
            os.link(source, linked)
        return chunk
    monkeypatch.setattr(workspace.os, "read", changed_link)
    with pytest.raises(SnapshotError) as exc:
        create_snapshot(project(repository), None, tmp_path / "snapshot")
    assert exc.value.code == "SNAPSHOT_CHANGED"
    assert not (tmp_path / "snapshot").exists()
