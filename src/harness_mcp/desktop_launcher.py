"""桌面安装包 CLI 的 SDK 启动兼容层，不操作 GUI 或个人桌面会话。

普通 Windows 路径优先使用官方公开 dsh_bin。批处理会解释特殊路径字符，
这类路径集中在此模块使用已验证 SDK0.1.5rc1 的 argv 兼容入口，直接启动
Electron Node 模式。所有方式仍由外层 DesktopRunner 持有整棵进程树。
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re


class DesktopLaunchError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def resolve_desktop_root(value: str | None = None) -> Path:
    if os.name != "nt":
        raise DesktopLaunchError("DESKTOP_PLATFORM_UNSUPPORTED", "桌面执行器需要 Windows。")
    if value is not None and not isinstance(value, str):
        raise DesktopLaunchError("DESKTOP_ROOT_INVALID", "桌面安装目录配置不正确。")
    if value:
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise DesktopLaunchError("DESKTOP_ROOT_INVALID", "桌面安装目录必须使用绝对路径。")
    else:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise DesktopLaunchError("DESKTOP_NOT_FOUND", "无法自动定位已安装的 Harness 桌面端。")
        root = Path(local) / "Programs" / "DeepSeek Harness"
    root = root.resolve()
    # cli.js 位于 ASAR 内，不能用普通 Path.is_file() 判断其归档成员。
    if not (root / "DeepSeek Harness.exe").is_file() or not (root / "resources/app.asar").is_file():
        raise DesktopLaunchError("DESKTOP_NOT_FOUND", "Harness 桌面安装包不完整或未找到。")
    return root


def _cmd_safe(paths: tuple[str, ...]) -> bool:
    return not any(re.search(r'[&|<>^()%!"\r\n\x00]', path) for path in paths)


def prepare_desktop_launch(config, desktop_root: str | None):
    """返回固定 SDK 配置及必要的 argv；可由无模型探针核对相同组合。"""
    root = resolve_desktop_root(desktop_root)
    if config.profile != "sdk-minimal" or not config.dsh_home or not config.patches:
        raise DesktopLaunchError("DESKTOP_CONFIG_INVALID", "桌面任务必须使用专用 home 和固定 sdk-minimal 补丁。")
    home = str(Path(config.dsh_home).resolve())
    patches = tuple(str(Path(patch).resolve()) for patch in config.patches)
    environment = dict(config.env or {})
    environment.update(DSH_HOME=home, ELECTRON_RUN_AS_NODE="1")
    config = replace(config, dsh_home=home, patches=patches, env=environment)
    command = root / "resources/runtime/cli/bin/dsh.cmd"
    if command.is_file() and _cmd_safe((str(command), home, *patches)):
        return replace(config, dsh_bin=str(command)), None
    # SDK 0.1.5rc1 公开 dsh_bin 不接收固定前置参数。此处是唯一生产兼容入口；
    # 直接 argv 避免 .cmd 对 %, &, ! 等路径字符进行展开或命令解释。
    executable = root / "DeepSeek Harness.exe"
    entry = root / "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    launch = (str(executable), "--expose-internals", str(entry), "--profile", "sdk-minimal")
    launch += tuple(argument for patch in patches for argument in ("--patch", patch))
    return config, launch


def create_desktop_client(config, desktop_root: str | None, client_type):
    """集中构造一个 SDK 客户端；不启动进程，不接收工具端动态 argv。"""
    config, launch = prepare_desktop_launch(config, desktop_root)
    if launch is None:
        return client_type(config)
    return client_type(config, _launch_args=launch)
