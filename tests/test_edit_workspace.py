"""修改副本的边界与实际差异专项，不运行模型或原项目脚本。"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from harness_mcp import edit_workspace as editing
from harness_mcp.edit_workspace import EditWorkspaceError, collect_edit_changes, prepare_edit_workspace
from harness_mcp.workspace import Snapshot, verify_snapshot


def baseline(tmp_path, files=None, *, scope=None, excluded=None):
    files = files or {"src/main.py": b"value = 1\n", "src/remove.py": b"old = True\n", "docs/readme.md": "说明\n".encode()}
    root = tmp_path / "snapshot" / "files"
    root.mkdir(parents=True)
    manifest = []
    for name, data in sorted(files.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(stat.S_IRUSR)
        manifest.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "line_count": len(data.splitlines())})
    snapshot_id = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return Snapshot(root, snapshot_id, manifest, None, False, scope or ["."], excluded or [])


def collect(snapshot, work, **kwargs):
    options = dict(allowed_paths=(".",), relative_paths=None, max_file_bytes=2048,
                   max_snapshot_bytes=32768, max_output_bytes=65536)
    options.update(kwargs)
    return collect_edit_changes(snapshot, work, **options)


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(EditWorkspaceError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code


def test_prepare_is_independent_writable_and_baseline_immutable(tmp_path):
    snapshot = baseline(tmp_path)
    original = snapshot.source_dir / "src/main.py"
    work = prepare_edit_workspace(snapshot, tmp_path / "可写 project")
    copied = work / "src/main.py"
    assert copied.read_bytes() == original.read_bytes()
    assert copied.stat().st_nlink == 1
    assert (copied.stat().st_dev, copied.stat().st_ino) != (original.stat().st_dev, original.stat().st_ino)
    copied.write_bytes(b"value = 2\n")
    assert original.read_bytes() == b"value = 1\n"
    assert verify_snapshot(snapshot)


def test_actual_add_modify_delete_and_digest_are_reproducible(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/main.py").write_bytes(b"value = 2\n")
    (work / "src/remove.py").unlink()
    (work / "src/new.py").write_bytes("label = '中文'\n".encode())
    result = collect(snapshot, work)
    assert result["source_applied"] is False
    assert result["workspace_dir"] == str(work)
    changes = {row["path"]: row for row in result["changes"]}
    assert changes["src/main.py"]["change_type"] == "modify"
    assert changes["src/new.py"]["change_type"] == "add"
    assert changes["src/remove.py"]["change_type"] == "delete"
    assert changes["src/new.py"]["before_sha256"] is None
    assert changes["src/remove.py"]["after_sha256"] is None
    assert changes["src/remove.py"]["after_text"] is None
    for row in result["changes"]:
        if row["after_text"] is not None:
            data = row["after_text"].encode()
            assert row["after_sha256"] == hashlib.sha256(data).hexdigest()
            assert row["after_size_bytes"] == len(data)
    assert result["patch_text"] == "".join(row["diff"] for row in result["changes"])
    assert "--- /dev/null\n+++ b/src/new.py\n" in result["patch_text"]
    assert "--- a/src/remove.py\n+++ /dev/null\n" in result["patch_text"]
    canonical = json.dumps({"snapshot_id": snapshot.snapshot_id, "changes": result["changes"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert result["change_set_id"] == hashlib.sha256(canonical).hexdigest()
    assert collect(snapshot, work) == result
    assert verify_snapshot(snapshot)


def test_unchanged_workspace_has_empty_changes_and_patch(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    result = collect(snapshot, work)
    assert result["changes"] == [] and result["patch_text"] == ""


def test_empty_file_creation_and_deletion_are_not_lost(tmp_path):
    snapshot = baseline(tmp_path, {"empty.py": b"", "main.py": b"value = 1\n"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "empty.py").unlink()
    (work / "new.py").write_bytes(b"")
    changes = collect(snapshot, work)["changes"]
    assert [(row["path"], row["change_type"]) for row in changes] == [("empty.py", "delete"), ("new.py", "add")]
    assert changes[1]["after_text"] == "" and changes[1]["after_size_bytes"] == 0


def test_missing_final_newline_is_explicit_and_after_text_is_exact(tmp_path):
    snapshot = baseline(tmp_path, {"main.py": b"old"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "main.py").write_bytes(b"new")
    change = collect(snapshot, work)["changes"][0]
    assert change["after_text"] == "new"
    assert change["diff"].count("\\ No newline at end of file\n") == 2
    assert "-old\n\\ No newline at end of file\n+new\n" in change["diff"]


@pytest.mark.parametrize("before,after", [
    ("x\u2028y\n", "x\u2028z\n"),
    ("x\u0085y\n", "x\u0085z\n"),
    ("x\ry\r", "x\rz\r"),
    ("old", "new"),
    ("x\r\ny\r\n", "x\r\nz\r\n"),
    ("x\u2028y\r\nlast", "x\u2028z\r\nlast changed"),
])
def test_real_git_apply_uses_lf_lines_and_preserves_exact_after_bytes(tmp_path, before, after):
    snapshot = baseline(tmp_path, {"space name.py": before.encode("utf-8")})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    expected = after.encode("utf-8")
    (work / "space name.py").write_bytes(expected)
    result = collect(snapshot, work)
    patch = result["patch_text"].encode("utf-8")
    apply_copy = tmp_path / "apply-copy"
    apply_copy.mkdir()
    (apply_copy / "space name.py").write_bytes(before.encode("utf-8"))
    # 只在人工副本检查/应用生成补丁；不运行项目代码、hook 或模型。
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.attributesFile=/dev/null", "apply"]
    checked = subprocess.run([*command, "--check"], input=patch, cwd=apply_copy, capture_output=True)
    assert checked.returncode == 0, checked.stderr.decode("utf-8", errors="replace")
    applied = subprocess.run(command, input=patch, cwd=apply_copy, capture_output=True)
    assert applied.returncode == 0, applied.stderr.decode("utf-8", errors="replace")
    actual = (apply_copy / "space name.py").read_bytes()
    assert actual == expected
    assert hashlib.sha256(actual).hexdigest() == result["changes"][0]["after_sha256"]
    assert result["changes"][0]["after_text"].encode("utf-8") == expected
    assert verify_snapshot(snapshot)


def test_single_file_scope_cannot_change_sibling(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/remove.py").write_bytes(b"wrong = True\n")
    assert_error("EDIT_PATH_NOT_AUTHORIZED", collect, snapshot, work, relative_paths=["src/main.py"])


def test_directory_scope_allows_new_nested_file_without_untracked_grant(tmp_path):
    snapshot = baseline(tmp_path, scope=["src"])
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/new").mkdir()
    (work / "src/new/fix.py").write_bytes(b"safe = True\n")
    result = collect(snapshot, work, allowed_paths=("src",), relative_paths=["src"])
    assert result["changes"][0]["path"] == "src/new/fix.py"


def test_new_single_file_scope_is_exact(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/new.py").write_bytes(b"safe = True\n")
    assert collect(snapshot, work, relative_paths=["src/new.py"])["changes"][0]["change_type"] == "add"
    (work / "src/new.py").unlink()
    (work / "src/new.py").mkdir()
    assert_error("EDIT_FILE_REPLACED_BY_DIRECTORY", collect, snapshot, work, relative_paths=["src/new.py"])
    (work / "src/new.py/evil.py").write_bytes(b"not allowed\n")
    assert_error("EDIT_FILE_REPLACED_BY_DIRECTORY", collect, snapshot, work, relative_paths=["src/new.py"])


def test_requested_scope_cannot_expand_snapshot_or_administrator_scope(tmp_path):
    snapshot = baseline(tmp_path, scope=["src"])
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    assert_error("EDIT_PATH_NOT_AUTHORIZED", collect, snapshot, work, relative_paths=["."])
    assert_error("EDIT_PATH_NOT_AUTHORIZED", collect, snapshot, work, allowed_paths=("src",), relative_paths=["docs"])


def test_baseline_file_cannot_become_directory(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/main.py").unlink()
    (work / "src/main.py").mkdir()
    (work / "src/main.py/evil.py").write_bytes(b"evil\n")
    assert_error("EDIT_FILE_REPLACED_BY_DIRECTORY", collect, snapshot, work)


@pytest.mark.parametrize("path", [".env.local", "private.pem", "credentials.json", "node_modules/new.js", ".git/config", "cache.db", "secrets.json", "id_rsa", ".env.local/hidden.py"])
def test_sensitive_dependency_binary_and_excluded_parent_paths_refused(tmp_path, path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"secret content must not be returned\n")
    assert_error("EDIT_EXCLUDED_PATH", collect, snapshot, work)


def test_original_exclusion_and_custom_pattern_block_new_files(tmp_path):
    snapshot = baseline(tmp_path, excluded=[{"path": "src/excluded.py", "reason": "configured_exclusion"}])
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/excluded.py").write_bytes(b"blocked = True\n")
    assert_error("EDIT_EXCLUDED_PATH", collect, snapshot, work)
    (work / "src/excluded.py").unlink()
    (work / "src/private-new.py").write_bytes(b"blocked = True\n")
    assert_error("EDIT_EXCLUDED_PATH", collect, snapshot, work, exclude_patterns=("src/private*",))


def test_new_unauthorized_empty_directory_is_not_silently_ignored(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "unrelated").mkdir()
    assert_error("EDIT_PATH_NOT_AUTHORIZED", collect, snapshot, work, relative_paths=["src/main.py"])


@pytest.mark.parametrize("content,code", [(b"bad\0binary", "EDIT_BINARY_CONTENT"), (b"bad\x01binary", "EDIT_BINARY_CONTENT"), (b"bad\xfftext", "EDIT_NON_UTF8")])
def test_non_utf8_and_binary_modifications_refused(tmp_path, content, code):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/main.py").write_bytes(content)
    assert_error(code, collect, snapshot, work)


def test_unchanged_non_utf8_baseline_is_preserved_but_cannot_be_modified(tmp_path):
    snapshot = baseline(tmp_path, {"main.py": b"legacy\xff\n"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    assert collect(snapshot, work)["changes"] == []
    (work / "main.py").write_bytes(b"now UTF8\n")
    assert_error("EDIT_NON_UTF8", collect, snapshot, work)


@pytest.mark.parametrize("content,secrets", [("token = 'real-local-credential'\n", ("real-local-credential",)), ("Authorization: Bearer secret_token\n", ()), ("password='should-not-be-returned'\n", ()), ("token = 'sk-12345678901234567890'\n", ())])
def test_secret_detection_rejects_instead_of_mutating_merge_content(tmp_path, content, secrets):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/main.py").write_bytes(content.encode())
    with pytest.raises(EditWorkspaceError) as exc:
        collect(snapshot, work, secrets=secrets)
    assert exc.value.code == "EDIT_SECRET_DETECTED"
    assert "should-not-be-returned" not in str(exc.value)
    assert "real-local-credential" not in str(exc.value)


def test_deleted_secret_content_is_not_exposed_through_diff(tmp_path):
    snapshot = baseline(tmp_path, {"main.py": b"password='source-secret'\n"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "main.py").unlink()
    assert_error("EDIT_SECRET_DETECTED", collect, snapshot, work)


def test_hardlink_to_unrelated_file_refused_without_reading_or_changing_it(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    outside = tmp_path / "private.py"
    outside.write_bytes(b"unrelated content\n")
    os.link(outside, work / "src/new.py")
    assert_error("EDIT_UNSAFE_HARDLINK", collect, snapshot, work)
    assert outside.read_bytes() == b"unrelated content\n"


def test_hardlinked_baseline_is_refused_before_copy(tmp_path):
    snapshot = baseline(tmp_path)
    os.link(snapshot.source_dir / "src/main.py", tmp_path / "alias.py")
    assert_error("EDIT_UNSAFE_HARDLINK", prepare_edit_workspace, snapshot, tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


def test_symlink_file_and_parent_are_refused(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_bytes(b"outside\n")
    try:
        (work / "src/linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("此环境无创建符号链接权限。")
    assert_error("EDIT_UNSAFE_LINK", collect, snapshot, work)
    (work / "src/linked").unlink()
    (work / "src/linked.py").symlink_to(outside / "private.py")
    assert_error("EDIT_UNSAFE_LINK", collect, snapshot, work)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 专项")
def test_windows_junction_and_ads_are_refused(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = work / "linked"
    process = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True)
    if process.returncode:
        pytest.skip("此环境无法创建 junction。")
    try:
        assert_error("EDIT_UNSAFE_LINK", collect, snapshot, work)
    finally:
        os.rmdir(linked)  # 只解除测试创建的 junction，不删除目标。
    stream = Path(str(work / "src/main.py") + ":hidden")
    stream.write_bytes(b"hidden data")
    try:
        assert_error("EDIT_UNSAFE_ADS", collect, snapshot, work)
    finally:
        stream.unlink()


@pytest.mark.parametrize("path", ["../outside", "C:/secret", "C:relative", "\\\\server\\share", "/etc/passwd", "src/main.py:hidden", "src/CON.py", "src/ambiguous. "])
def test_unsafe_requested_paths_are_rejected(tmp_path, path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    assert_error("EDIT_INVALID_PATH", collect, snapshot, work, relative_paths=[path])


def test_case_only_rename_is_rejected_for_portable_merge(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "src/main.py").rename(work / "src/MAIN.py")
    assert_error("EDIT_PATH_ALIAS", collect, snapshot, work)


def test_file_total_output_and_count_limits_refuse_without_truncation(tmp_path, monkeypatch):
    snapshot = baseline(tmp_path, {"main.py": b"value = 1\n"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    (work / "main.py").write_bytes(b"a" * 100)
    assert_error("EDIT_FILE_SIZE_LIMIT", collect, snapshot, work, max_file_bytes=64)
    assert_error("EDIT_SNAPSHOT_SIZE_LIMIT", collect, snapshot, work, max_snapshot_bytes=64)
    assert_error("EDIT_OUTPUT_LIMIT", collect, snapshot, work, max_output_bytes=128)
    (work / "main.py").write_bytes(b"value = 1\n")
    (work / "extra.py").write_bytes(b"extra = True\n")
    monkeypatch.setattr(editing, "MAX_EDIT_FILES", 1)
    assert_error("EDIT_FILE_COUNT_LIMIT", collect, snapshot, work)


def test_directory_entry_limit_is_bounded(tmp_path, monkeypatch):
    snapshot = baseline(tmp_path, {"main.py": b"value = 1\n"})
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    for index in range(6):
        (work / f"empty{index}").mkdir()
    monkeypatch.setattr(editing, "MAX_EDIT_ENTRIES", 4)
    assert_error("EDIT_ENTRY_LIMIT", collect, snapshot, work)


@pytest.mark.parametrize("field,value", [("max_file_bytes", 0), ("max_snapshot_bytes", True), ("max_output_bytes", -1), ("max_output_bytes", "100"), ("relative_paths", []), ("allowed_paths", ())])
def test_limit_and_scope_types_are_explicit(tmp_path, field, value):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    code = "EDIT_EMPTY_SCOPE" if field in ("relative_paths", "allowed_paths") else "EDIT_INVALID_LIMIT"
    assert_error(code, collect, snapshot, work, **{field: value})


def test_preparing_cannot_overwrite_baseline_or_existing_directory(tmp_path):
    snapshot = baseline(tmp_path)
    assert_error("EDIT_DESTINATION_INVALID", prepare_edit_workspace, snapshot, snapshot.source_dir / "child")
    assert_error("EDIT_DESTINATION_INVALID", prepare_edit_workspace, snapshot, snapshot.source_dir.parent)
    existing = tmp_path / "exists"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    assert_error("EDIT_DESTINATION_EXISTS", prepare_edit_workspace, snapshot, existing)
    assert (existing / "keep.txt").read_text() == "keep"


def test_tampered_baseline_refused_before_and_after_execution(tmp_path):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    source = snapshot.source_dir / "src/main.py"
    source.chmod(stat.S_IRUSR | stat.S_IWUSR)
    source.write_bytes(b"changed baseline\n")
    assert_error("EDIT_BASELINE_CHANGED", collect, snapshot, work)
    assert_error("EDIT_BASELINE_CHANGED", prepare_edit_workspace, snapshot, tmp_path / "other")


def test_workspace_mutation_during_comparison_is_refused(tmp_path, monkeypatch):
    snapshot = baseline(tmp_path)
    work = prepare_edit_workspace(snapshot, tmp_path / "workspace")
    original_scan = editing._scan
    changed = False
    def changing(root, max_file, max_total):
        nonlocal changed
        result = original_scan(root, max_file, max_total)
        if root == work and not changed:
            changed = True
            (work / "src/main.py").write_bytes(b"late change\n")
        return result
    monkeypatch.setattr(editing, "_scan", changing)
    assert_error("EDIT_WORKSPACE_CHANGED", collect, snapshot, work)


def test_invalid_manifest_is_a_stable_error(tmp_path):
    snapshot = baseline(tmp_path)
    invalid = replace(snapshot, manifest=[{"path": "src/main.py", "size_bytes": 1, "sha256": "invalid"}])
    assert_error("EDIT_BASELINE_INVALID", prepare_edit_workspace, invalid, tmp_path / "workspace")
