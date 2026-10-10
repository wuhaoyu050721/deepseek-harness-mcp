"""在独立临时目录验证源码 ZIP 的初始化，不接触用户本机配置。"""

from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest


SETUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_local.py"


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("初始化人工样例需要 Git")
    root = tmp_path / "发布目录 with spaces"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(SETUP_SCRIPT, root / "scripts" / "setup_local.py")
    return root


def run_setup(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "setup_local.py")],
        cwd=root.parent,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def read_config(root: Path, client: str) -> dict:
    return tomllib.loads((root / "config" / f"{client}.local.toml").read_text(encoding="utf-8"))


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    ).stdout.strip()


def test_source_zip_initializes_only_sample_and_absolute_codex_paths(release_root: Path) -> None:
    result = run_setup(release_root)

    assert not (release_root / ".git").exists()
    assert "只授权人工示例 sample" in result.stdout
    assert "没有调用模型" in result.stdout
    sample = release_root / "data" / "示例 project"
    assert git(sample, "rev-parse", "HEAD")
    assert set(git(sample, "ls-files").splitlines()) == {"README.md", "calculator.py"}
    assert git(sample, "status", "--porcelain") == ""
    for client in ("codex", "claude"):
        config = read_config(release_root, client)
        assert set(config["projects"]) == {"sample"}
        assert config["projects"]["sample"]["path"] == sample.as_posix()
        assert config["projects"]["sample"]["allow_untracked"] == []
        assert config["data_dir"] == (release_root / "data" / client).as_posix()
        assert config["model"]["credential_source"] == "desktop_store"
        assert "api_key" not in config["model"]
    snippet = tomllib.loads((release_root / "examples" / "codex.local.toml").read_text(encoding="utf-8"))
    server = snippet["mcp_servers"]["deepseek_harness"]
    assert server["cwd"] == str(release_root)
    assert Path(server["command"]).is_absolute()
    assert server["args"][-1] == str(release_root / "config" / "codex.local.toml")
    assert "--shared" in server["args"]


def test_repeat_setup_preserves_existing_configs_snippets_and_sample(release_root: Path) -> None:
    run_setup(release_root)
    sample = release_root / "data" / "示例 project"
    initial_head = git(sample, "rev-parse", "HEAD")
    paths = [
        release_root / "config" / "codex.local.toml",
        release_root / "config" / "claude.local.toml",
        release_root / "examples" / "codex.local.toml",
        release_root / "examples" / "claude.local.json",
        sample / "calculator.py",
    ]
    for index, path in enumerate(paths):
        path.write_text(f"custom-content-{index}\n", encoding="utf-8")
    expected = {path: path.read_bytes() for path in paths}

    run_setup(release_root)

    assert {path: path.read_bytes() for path in paths} == expected
    assert git(sample, "rev-parse", "HEAD") == initial_head


def test_parent_git_does_not_authorize_untracked_release_root(release_root: Path) -> None:
    git(release_root.parent, "init", "-q")

    run_setup(release_root)

    assert set(read_config(release_root, "codex")["projects"]) == {"sample"}


def test_git_root_keeps_scopes_and_only_authorizes_listed_untracked_files(release_root: Path) -> None:
    git(release_root, "init", "-q")
    (release_root / "src").mkdir()
    (release_root / "src" / "new.py").write_text("pass\n", encoding="utf-8")
    (release_root / "private-not-authorized.txt").write_text("not project source\n", encoding="utf-8")
    (release_root / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
    (release_root / "src" / "ignored.py").write_text("pass\n", encoding="utf-8")

    run_setup(release_root)

    for client in ("codex", "claude"):
        config = read_config(release_root, client)
        assert set(config["projects"]) == {"sample", "mcp"}
        project = config["projects"]["mcp"]
        assert project["path"] == release_root.as_posix()
        assert project["allowed_paths"] == ["src", "tests", "scripts", "docs", "README.md", "pyproject.toml", "worker-requirements.lock"]
        assert project["allow_untracked"] == ["scripts/setup_local.py", "src/new.py"]
