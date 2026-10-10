"""公开发布只收集明确范围，且归档成员必须等于安全读取的源字节。"""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package_release.py"
SPEC = importlib.util.spec_from_file_location("package_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def put(root, name, content=b"public fixture\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def wheel_bytes(version="0.3.1", extra=None):
    output = io.BytesIO()
    members = {
        f"deepseek_harness_mcp-{version}.dist-info/METADATA":
            f"Metadata-Version: 2.4\nName: deepseek-harness-mcp\nVersion: {version}\n",
        "harness_mcp/__init__.py": f'__version__ = "{version}"\n',
        **(extra or {}),
    }
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.fixture
def project(tmp_path):
    for name in release.FIXED_SOURCE_FILES:
        put(tmp_path, name)
    put(tmp_path, "pyproject.toml", b'[project]\nname = "deepseek-harness-mcp"\nversion = "0.3.1"\n')
    put(tmp_path, "src/harness_mcp/__init__.py", b'__version__ = "0.3.1"\n')
    put(tmp_path, "tests/test_example.py", b"def test_example():\n    assert True\n")
    put(tmp_path, "scripts/example.py", b"print('example')\n")
    put(tmp_path, "scripts/example.ps1", b"Write-Output 'example'\n")
    put(tmp_path, ".agents/skills/deepseek-delegate/SKILL.md", b"---\nname: deepseek-delegate\n---\n")
    put(tmp_path, ".agents/skills/deepseek-delegate/agents/openai.yaml", b"interface:\n  display_name: Delegate\n")
    put(tmp_path, ".agents/skills/deepseek-delegate/references/contract.md", b"Public contract\n")
    put(tmp_path, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel_bytes())
    return tmp_path


def test_public_release_has_exact_source_bytes_hashes_and_no_private_candidates(project):
    sentinel = b"LOCAL_ONLY_DO_NOT_PUBLISH"
    excluded = (
        "config/codex.local.toml", ".env", "data/private.py", ".venv/private.py",
        ".git/private.py", "artifacts/local/job.json", "DELIVERY.md", "docs/original-mvp-plan.md",
        "docs/PHASE0.md", "tests/__pycache__/private.py", "scripts/nested/private.py",
        "scripts/config.local.py", "tests/data/private.py", ".agents/skills/deepseek-delegate/private.txt",
    )
    for name in excluded:
        put(project, name, sentinel)
    result = release.package_release(project)
    output = Path(result["output"])
    assert result["status"] == "passed"
    assert result["skill_files"] == 3
    assert {item["path"] for item in result["assets"]} == {
        "deepseek-harness-mcp-0.3.1-windows-source.zip", "deepseek-delegate-0.3.1.zip",
        "deepseek_harness_mcp-0.3.1-py3-none-any.whl", "manifest-v0.3.1.json", "SHA256SUMS",
    }
    manifest = json.loads((output / "manifest-v0.3.1.json").read_bytes())
    assert manifest["verification"]["personal_credentials_read"] is False
    assert manifest["verification"]["wheel_source_bytes_match"] is True
    with zipfile.ZipFile(output / "deepseek-harness-mcp-0.3.1-windows-source.zip") as archive:
        assert set(archive.namelist()) == {item["path"] for item in manifest["source_zip"]["members"]}
        assert all(sentinel not in archive.read(name) for name in archive.namelist())
        for item in manifest["source_zip"]["members"]:
            content = archive.read(item["path"])
            assert item["bytes"] == len(content)
            assert item["sha256"] == hashlib.sha256(content).hexdigest()
            relative = item["path"].split("/", 1)[1]
            origin = "artifacts/" + relative if relative.startswith("dist/") else relative
            assert content == (project / origin).read_bytes()
    with zipfile.ZipFile(output / "deepseek-delegate-0.3.1.zip") as archive:
        assert set(archive.namelist()) == {
            "deepseek-delegate/SKILL.md", "deepseek-delegate/agents/openai.yaml",
            "deepseek-delegate/references/contract.md",
        }
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_repeated_build_is_byte_identical_and_does_not_rewrite_assets(project):
    first = release.package_release(project)
    output = Path(first["output"])
    old = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
    assert release.package_release(project) == first
    assert old == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}


def test_existing_changed_asset_requires_explicit_replace_before_any_writes(project):
    output = project / "artifacts/releases/v0.3.1"
    output.mkdir(parents=True)
    path = output / "deepseek-delegate-0.3.1.zip"
    path.write_bytes(b"older release")
    with pytest.raises(release.ReleaseError, match="Existing release asset differs"):
        release.package_release(project)
    assert list(output.iterdir()) == [path]
    assert path.read_bytes() == b"older release"
    assert release.package_release(project, replace=True)["status"] == "passed"


def test_missing_required_connection_guide_fails_before_output(project):
    (project / "docs/MCP-CONNECTION.zh-CN.md").unlink()
    with pytest.raises(release.ReleaseError, match="Required release path unavailable"):
        release.package_release(project)
    assert not (project / "artifacts/releases").exists()


@pytest.mark.parametrize("name", ["../a", "/absolute", "C:/absolute", "a\\b", "a/../b", "a//b", "a/./b", "", "a\x00b"])
def test_archive_member_paths_cannot_escape(name):
    with pytest.raises(release.ReleaseError, match="normalized relative path"):
        release._member_name(name)


def test_case_collisions_are_refused_in_archive_and_selection(project, monkeypatch):
    with pytest.raises(release.ReleaseError, match="Case-insensitive duplicate"):
        release._zip_content({"Readme.md": b"one", "README.md": b"two"})
    original = release._walk_sources
    monkeypatch.setattr(release, "_walk_sources", lambda root, folder, suffixes, **options:
                        ["README.md"] if folder == "src" else original(root, folder, suffixes, **options))
    with pytest.raises(release.ReleaseError, match="Case-insensitive duplicate"):
        release.package_release(project)


def test_hardlinked_source_is_refused(project):
    alias = project / "alias.txt"
    os.link(project / "README.md", alias)
    with pytest.raises(release.ReleaseError, match="single-link regular file"):
        release.package_release(project)
    assert not (project / "artifacts/releases").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_symlinked_source_or_directory_is_refused(project, directory):
    target = put(project, "outside/file.py") if not directory else project / "outside"
    if directory:
        target.mkdir()
    path = project / ("src/alias" if directory else "src/alias.py")
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Host does not permit symbolic links")
    with pytest.raises(release.ReleaseError, match="Linked release path refused"):
        release.package_release(project)


def test_junction_and_reparse_attributes_are_rejected():
    info = SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
    assert release._linked(info)


def test_source_change_during_read_is_refused(project, monkeypatch):
    original = release._checked_path
    calls = 0

    def checked(root, name, **options):
        nonlocal calls
        if name == "README.md":
            calls += 1
            if calls == 2:
                (root / name).write_bytes(b"changed after the opened file was read")
        return original(root, name, **options)

    monkeypatch.setattr(release, "_checked_path", checked)
    with pytest.raises(release.ReleaseError, match="changed during read"):
        release._read_source(project, "README.md")


@pytest.mark.parametrize("content", [
    b"sk-" + b"A" * 40,
    b"ghp_" + b"A" * 40,
    b"github_pat_" + b"A" * 40,
    b"AKIA" + b"A" * 16,
    b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 64 + b"\n-----END PRIVATE KEY-----\n",
])
def test_token_or_private_key_never_enters_release(project, content):
    put(project, "README.md", content)
    with pytest.raises(release.ReleaseError, match="Potential") as error:
        release.package_release(project)
    assert content.decode().strip() not in str(error.value)
    assert not (project / "artifacts/releases").exists()


def test_test_fixture_exemption_is_exact_and_does_not_allow_secrets_in_docs(project):
    artificial = release.ARTIFICIAL_TOKEN_ALLOWLIST["tests/test_runner.py"][0]
    put(project, "tests/test_runner.py", artificial)
    assert release.package_release(project)["status"] == "passed"
    put(project, "README.md", artificial)
    with pytest.raises(release.ReleaseError, match="Potential api_token"):
        release.package_release(project)


def test_token_inside_wheel_is_scanned(project):
    wheel = wheel_bytes(extra={"harness_mcp/private.py": b"sk-" + b"A" * 40})
    put(project, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel)
    with pytest.raises(release.ReleaseError, match="wheel/harness_mcp/private.py"):
        release.package_release(project)


@pytest.mark.parametrize("extra", [{"../escape": b"bad"}, {"A.py": b"one", "a.py": b"two"}])
def test_wheel_members_cannot_escape_or_collide(project, extra):
    put(project, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel_bytes(extra=extra))
    with pytest.raises(release.ReleaseError):
        release.package_release(project)


def test_runtime_and_wheel_version_must_match_project(project):
    put(project, "src/harness_mcp/__init__.py", b'__version__ = "0.3.0"\n')
    with pytest.raises(release.ReleaseError, match="versions must agree"):
        release.package_release(project)
    put(project, "src/harness_mcp/__init__.py", b'__version__ = "0.3.1"\n')
    put(project, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel_bytes("0.3.0"))
    with pytest.raises(release.ReleaseError, match="Wheel metadata"):
        release.package_release(project)


def test_stale_wheel_module_bytes_are_refused(project):
    wheel = wheel_bytes(extra={"harness_mcp/__init__.py": b'__version__ = "0.3.1"\n# stale build\n'})
    put(project, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel)
    with pytest.raises(release.ReleaseError, match="module bytes do not match"):
        release.package_release(project)
    assert not (project / "artifacts/releases").exists()


@pytest.mark.parametrize("missing_from_wheel", [True, False])
def test_omitted_or_extra_wheel_module_is_refused(project, missing_from_wheel):
    if missing_from_wheel:
        put(project, "src/harness_mcp/additional.py", b"value = 1\n")
    else:
        wheel = wheel_bytes(extra={"harness_mcp/unexpected.py": b"value = 1\n"})
        put(project, "artifacts/dist/deepseek_harness_mcp-0.3.1-py3-none-any.whl", wheel)
    with pytest.raises(release.ReleaseError, match="module paths do not match"):
        release.package_release(project)
    assert not (project / "artifacts/releases").exists()


def test_late_collection_drift_cannot_publish_old_early_file(project, monkeypatch):
    original = release._read_source
    changed = False

    def read(root, name):
        nonlocal changed
        if name == "tests/test_example.py" and not changed:
            changed = True
            (root / "README.md").write_bytes(b"Changed while the rest was being collected\n")
        return original(root, name)

    monkeypatch.setattr(release, "_read_source", read)
    with pytest.raises(release.ReleaseError, match="source changed during collection: README.md"):
        release.package_release(project)
    assert not (project / "artifacts/releases").exists()


def test_output_cannot_escape_root_or_overwrite_through_link(project, tmp_path):
    with pytest.raises(release.ReleaseError, match="inside the project root"):
        release.package_release(project, project.parent / "outside-output")
    output = project / "artifacts/releases/v0.3.1"
    output.mkdir(parents=True)
    existing = put(project, "private.txt", b"keep existing file")
    os.link(existing, output / "SHA256SUMS")
    with pytest.raises(release.ReleaseError, match="single-link regular file"):
        release.package_release(project, replace=True)
    assert existing.read_bytes() == b"keep existing file"
