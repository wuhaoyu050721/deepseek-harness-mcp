"""桌面入口兼容层：路径、专用 home、固定 argv 和错误边界。"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_mcp import desktop_launcher as launcher


@dataclass
class Config:
    profile: str = "sdk-minimal"
    dsh_home: str = ""
    cwd: str = ""
    patches: tuple[str, ...] = ()
    env: dict | None = None
    dsh_bin: str | None = None


class Client:
    def __init__(self, config, **kwargs):
        self.config, self.arguments = config, kwargs


def installation(tmp_path: Path, monkeypatch):
    root = tmp_path / "Programs" / "DeepSeek Harness"
    (root / "resources/runtime/cli/bin").mkdir(parents=True)
    (root / "DeepSeek Harness.exe").write_bytes(b"fixture")
    (root / "resources/app.asar").write_bytes(b"fixture")
    (root / "resources/runtime/cli/bin/dsh.cmd").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", environ={"LOCALAPPDATA": str(tmp_path)}))
    return root


def configuration(tmp_path: Path, name="private-home"):
    home = tmp_path / name
    return Config(dsh_home=str(home), cwd=str(tmp_path / "snapshot"),
                  patches=(str(home / "privacy.yml"),), env={"DEEPSEEK_API_KEY": "fake-value"})


def test_auto_detects_installation_without_treating_asar_member_as_file(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    assert launcher.resolve_desktop_root() == root.resolve()
    assert not (root / "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js").is_file()


def test_public_sdk_bin_uses_new_home_and_fixed_patch(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    config = configuration(tmp_path)
    client = launcher.create_desktop_client(config, str(root), Client)
    assert client.arguments == {}
    assert client.config.dsh_bin == str(root / "resources/runtime/cli/bin/dsh.cmd")
    assert client.config.env == {"DEEPSEEK_API_KEY": "fake-value", "DSH_HOME": config.dsh_home, "ELECTRON_RUN_AS_NODE": "1"}
    assert client.config.cwd == config.cwd
    assert client.config.patches == config.patches
    assert config.env == {"DEEPSEEK_API_KEY": "fake-value"}  # 不反向污染原配置。


@pytest.mark.parametrize("name", ["home & child", "home!", "home%", "home(child)", "home^child"])
def test_cmd_metacharacters_use_direct_argv_without_interpretation(tmp_path, monkeypatch, name):
    root = installation(tmp_path, monkeypatch)
    config = configuration(tmp_path, name)
    client = launcher.create_desktop_client(config, str(root), Client)
    assert client.config.dsh_bin is None
    assert client.config.env["DSH_HOME"] == config.dsh_home
    assert client.arguments["_launch_args"] == (
        str(root / "DeepSeek Harness.exe"), "--expose-internals",
        str(root / "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"),
        "--profile", "sdk-minimal", "--patch", config.patches[0],
    )


def test_missing_batch_can_use_bundled_archive_entry(tmp_path, monkeypatch):
    root = installation(tmp_path, monkeypatch)
    (root / "resources/runtime/cli/bin/dsh.cmd").unlink()
    assert "_launch_args" in launcher.create_desktop_client(configuration(tmp_path), str(root), Client).arguments


@pytest.mark.parametrize("change", [{"profile": "desktop"}, {"dsh_home": ""}, {"patches": ()}])
def test_never_accepts_personal_profile_or_implicit_home(tmp_path, monkeypatch, change):
    root = installation(tmp_path, monkeypatch)
    config = configuration(tmp_path)
    for field, value in change.items():
        setattr(config, field, value)
    with pytest.raises(launcher.DesktopLaunchError) as error:
        launcher.create_desktop_client(config, str(root), Client)
    assert error.value.code == "DESKTOP_CONFIG_INVALID"


@pytest.mark.parametrize("value,code", [("relative/root", "DESKTOP_ROOT_INVALID"), (5, "DESKTOP_ROOT_INVALID")])
def test_invalid_install_config_returns_safe_error(tmp_path, monkeypatch, value, code):
    installation(tmp_path, monkeypatch)
    with pytest.raises(launcher.DesktopLaunchError) as error:
        launcher.resolve_desktop_root(value)
    assert error.value.code == code
    assert str(value) not in error.value.message


def test_missing_installation_and_nonwindows_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", environ={}))
    with pytest.raises(launcher.DesktopLaunchError, match="无法自动定位"):
        launcher.resolve_desktop_root()
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="posix", environ={}))
    with pytest.raises(launcher.DesktopLaunchError) as error:
        launcher.resolve_desktop_root(str(tmp_path))
    assert error.value.code == "DESKTOP_PLATFORM_UNSUPPORTED"
