"""本机只读环境盘点；本入口不启动 Harness 或调用模型。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
from typing import Any


COMMAND_TIMEOUT_SECONDS = 5
OUTPUT_LIMIT_BYTES = 8192
ASAR_HEADER_LIMIT_BYTES = 8 * 1024 * 1024
CLI_ASAR_PATH = "dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
SUPPORTED_DESKTOP_SDK = "0.1.5rc1"
_VERSION = r"[0-9]+(?:\.[0-9A-Za-z]+)+(?:[+_-][0-9A-Za-z._-]+)?"
_VERSION_VALUE = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,99}\Z")
_VERSION_PATTERNS = {
    "git": rf"^git version\s+({_VERSION})\b",
    "uv": rf"^uv\s+({_VERSION})\b",
    "codex": rf"^(?:codex-cli|codex)\s+({_VERSION})\b",
    "claude": rf"^(?:Claude Code\s+)?({_VERSION})(?:\s|$)",
    "docker": rf"^Docker version\s+({_VERSION})(?:,|\s|$)",
    "podman": rf"^podman version\s+({_VERSION})\b",
    "wsl": rf"^(?:WSL version|WSL 版本)\s*[:：]\s*({_VERSION})\b",
}


def _decode_output(raw: bytes) -> str:
    # WSL 在部分 Windows 版本中返回 UTF-16，普通 CLI 通常返回 UTF-8。
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw and raw.count(b"\x00") > len(raw) // 5:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _bounded_command(command: list[str]) -> dict[str, Any]:
    """限时执行盘点命令；内部输出有界，调用方禁止直接返回原始内容。"""
    output: dict[str, Any] = {
        "status": "unavailable", "return_code": None,
        "stdout": "", "stderr": "", "output_truncated": False,
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                room = OUTPUT_LIMIT_BYTES - len(buffers[name])
                buffers[name].extend(chunk[:max(0, room)])
                if len(chunk) > room:
                    truncated[name] = True
        except (OSError, ValueError):
            # 超时关闭可能打断读取；不把系统错误文本带入交付输出。
            pass

    launch_command: list[str] | str = command
    if os.name == "nt" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
        # npm 的 Codex/Claude 入口可能是 cmd 包装；仅允许固定版本参数。
        # 显式双层引号保护中文、空格和路径中的 shell 元字符。
        if command[1:] != ["--version"] or any(char in command[0] for char in ('"', "\r", "\n")):
            return output
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        shell = windows / "System32" / "cmd.exe"
        launch_command = f'"{shell}" /d /s /c ""{command[0]}" --version"'
    try:
        process = subprocess.Popen(
            launch_command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, ValueError):
        return output

    threads = [
        threading.Thread(target=drain, args=(getattr(process, name), name), daemon=True)
        for name in ("stdout", "stderr")
    ]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
        output["status"] = "ok" if process.returncode == 0 else "command_failed"
    except subprocess.TimeoutExpired:
        output["status"] = "timed_out"
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    output["return_code"] = process.returncode
    for thread, name in zip(threads, ("stdout", "stderr")):
        thread.join(timeout=0.2)
        if not thread.is_alive():
            getattr(process, name).close()
    for name in ("stdout", "stderr"):
        output[name] = _decode_output(bytes(buffers[name]))
    output["output_truncated"] = any(truncated.values())
    return output


def _version_from_output(name: str, result: dict[str, Any]) -> str | None:
    if result["status"] != "ok":
        return None
    pattern = re.compile(_VERSION_PATTERNS[name], flags=re.MULTILINE | re.IGNORECASE)
    for channel in ("stdout", "stderr"):
        match = pattern.search(result[channel])
        if match:
            return match.group(1)
    return None


def _windows_candidates() -> dict[str, list[Path]]:
    if os.name != "nt":
        return {}
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    user_root = Path.home()
    return {
        "git": [program_files / "Git" / "cmd" / "git.exe", program_files_x86 / "Git" / "cmd" / "git.exe"],
        "uv": [user_root / ".local" / "bin" / "uv.exe", user_root / ".cargo" / "bin" / "uv.exe"],
        "codex": [roaming / "npm" / "codex.cmd", local / "Microsoft" / "WindowsApps" / "codex.exe"],
        "claude": [user_root / ".local" / "bin" / "claude.exe", roaming / "npm" / "claude.cmd"],
        "docker": [program_files / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"],
        "podman": [program_files / "RedHat" / "Podman" / "podman.exe", local / "Programs" / "Podman" / "podman.exe"],
        "wsl": [windows / "System32" / "wsl.exe"],
        "harness": [
            local / "Programs" / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd",
            program_files / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd",
        ],
    }


def _find_command(name: str, candidates: dict[str, list[Path]]) -> str | None:
    discovered = shutil.which(name)
    if discovered:
        return discovered
    for candidate in candidates.get(name, []):
        if candidate.is_file():
            return str(candidate)
    return None


def _cli_version(name: str, executable: str | None) -> dict[str, Any]:
    if not executable:
        return {"installed": False, "executable": None, "version": None, "status": "missing"}
    result = _bounded_command([executable, "--version"])
    return {
        "installed": True, "executable": executable,
        "version": _version_from_output(name, result), "status": result["status"],
        "return_code": result["return_code"], "output_truncated": result["output_truncated"],
    }


def _package_version(name: str) -> dict[str, Any]:
    # 只读 distribution metadata，不能为了检查版本而导入或启动 SDK。
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return {"installed": False, "version": None}
    return {"installed": True, "version": version if _VERSION_VALUE.fullmatch(version) else None}


def _docker_server(executable: str | None) -> dict[str, Any]:
    if not executable:
        return {"available": False, "status": "missing", "os_type": None, "server_version": None, "linux_server": False}
    # 只向 Docker 请求两个专用字段，不读取容器列表、标签、代理或 registry 配置。
    template = '{"server_version":{{json .ServerVersion}},"os_type":{{json .OSType}}}'
    result = _bounded_command([executable, "info", "--format", template])
    report = {
        "available": False, "status": result["status"], "os_type": None,
        "server_version": None, "linux_server": False,
        "return_code": result["return_code"], "output_truncated": result["output_truncated"],
    }
    if result["status"] != "ok" or result["output_truncated"]:
        return report
    try:
        data = json.loads(result["stdout"])
    except (ValueError, TypeError):
        report["status"] = "invalid_response"
        return report
    if not isinstance(data, dict):
        report["status"] = "invalid_response"
        return report
    os_type = data.get("os_type")
    version = data.get("server_version")
    if not isinstance(os_type, str) or os_type not in {"linux", "windows"} or not isinstance(version, str) or not _VERSION_VALUE.fullmatch(version):
        report["status"] = "invalid_response"
        return report
    report.update(available=True, os_type=os_type, server_version=version, linux_server=os_type == "linux")
    return report


def _wsl_status(executable: str | None) -> dict[str, Any]:
    if os.name != "nt":
        return {"installed": False, "status": "not_applicable", "default_version": None, "version": None}
    if not executable:
        return {"installed": False, "status": "missing", "default_version": None, "version": None}
    status = _bounded_command([executable, "--status"])
    version = _bounded_command([executable, "--version"])
    default_version = None
    if status["status"] == "ok":
        # 只提取版本号，不输出发行版名称或未经审阅的 WSL 原始文本。
        match = re.search(r"(?:Default Version|默认版本)\s*[:：]\s*([12])\b", status["stdout"], flags=re.IGNORECASE)
        if match:
            default_version = int(match.group(1))
    return {
        "installed": True, "executable": executable, "status": status["status"],
        "return_code": status["return_code"], "default_version": default_version,
        "version": _version_from_output("wsl", version), "version_status": version["status"],
    }


def _asar_entries(archive: Path) -> tuple[bool, str | None]:
    """只读有界 ASAR 索引，cli.js 位于归档内，不能用 is_file 判定它不存在。"""
    try:
        with archive.open("rb") as stream:
            first = stream.read(16)
            if len(first) != 16:
                return False, None
            size_pickle, header_size, payload_size, json_size = struct.unpack("<4I", first)
            if size_pickle != 4 or payload_size != header_size - 4 or not 0 < json_size <= min(payload_size - 4, ASAR_HEADER_LIMIT_BYTES):
                return False, None
            header = json.loads(stream.read(json_size))
            node = header
            for part in CLI_ASAR_PATH.split("/"):
                node = node.get("files", {}).get(part, {})
            payload_start = 8 + header_size
            file_size = archive.stat().st_size
            def contained_file(entry: Any) -> bool:
                if not isinstance(entry, dict) or entry.get("link") or entry.get("unpacked"):
                    return False
                size, offset = entry.get("size"), entry.get("offset")
                return isinstance(size, int) and not isinstance(size, bool) and size > 0 and isinstance(offset, str) and offset.isascii() and offset.isdigit() and payload_start + int(offset) + size <= file_size
            cli_present = contained_file(node)
            package = header.get("files", {}).get("package.json", {})
            version = None
            if contained_file(package) and package["size"] <= OUTPUT_LIMIT_BYTES:
                stream.seek(payload_start + int(package["offset"]))
                value = json.loads(stream.read(package["size"]))
                candidate = value.get("version") if isinstance(value, dict) else None
                if isinstance(candidate, str) and _VERSION_VALUE.fullmatch(candidate):
                    version = candidate
            return cli_present, version
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return False, None


def _desktop_harness(candidates: dict[str, list[Path]], desktop_root: str = "") -> dict[str, Any]:
    """仅验证桌面安装文件和包版本，不启动 runtime 或模型。"""
    # 空配置与生产 launcher 一致，只自动使用 LOCALAPPDATA 安装位置。
    roots = [Path(desktop_root)] if desktop_root else [path.parents[4] for path in candidates.get("harness", [])[:1]]
    report: dict[str, Any] = {"installed": False, "install_root": None, "runtime_entry": None, "public_cli_available": False, "gui_executable": None, "cli_entry": None, "cli_entry_verified": False, "version": None, "version_source": None}
    for root in roots:
        entry = root / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
        executable = root / "DeepSeek Harness.exe"
        archive = root / "resources" / "app.asar"
        cli_present, archive_version = _asar_entries(archive)
        if not executable.is_file() or not cli_present:
            continue
        report.update(installed=True, install_root=str(root), runtime_entry=str(entry) if entry.is_file() else None, public_cli_available=entry.is_file(), gui_executable=str(executable), cli_entry=str(archive / CLI_ASAR_PATH), cli_entry_verified=True)
        if archive_version:
            report.update(version=archive_version, version_source=str(archive / "package.json"))
            break
        for manifest in (root / "resources" / "app" / "package.json", root / "resources" / "runtime" / "cli" / "package.json"):
            if not manifest.is_file():
                continue
            try:
                with manifest.open("rb") as stream:
                    raw = stream.read(OUTPUT_LIMIT_BYTES + 1)
                if len(raw) > OUTPUT_LIMIT_BYTES:
                    continue
                package = json.loads(raw.decode("utf-8"))
                version = package.get("version") if isinstance(package, dict) else None
                if isinstance(version, str) and _VERSION_VALUE.fullmatch(version):
                    report.update(version=version, version_source=str(manifest))
                    break
            except (OSError, ValueError, UnicodeError):
                continue
        break
    return report


def _desktop_credential_store_nonempty() -> bool:
    # 只看文件 metadata；禁止打开、解析或复制用户个人凭证内容。
    credential_path = Path.home() / ".dsh" / ".credentials.yaml"
    try:
        return credential_path.is_file() and credential_path.stat().st_size > 0
    except OSError:
        return False


def collect_checks(config: Any = None) -> dict[str, Any]:
    """前置条件就绪与真实验收分开记录；desktop 路线不以 Docker 为条件。"""
    candidates = _windows_candidates()
    executable = {name: _find_command(name, candidates) for name in ("git", "uv", "codex", "claude", "docker", "podman", "wsl")}
    commands = {name: _cli_version(name, executable[name]) for name in ("git", "uv", "codex", "claude", "docker", "podman")}
    packages = {name: _package_version(name) for name in ("mcp", "deepseek-harness-sdk")}
    container = _docker_server(executable["docker"])
    credential_present = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    backend = getattr(getattr(config, "executor", None), "backend", "desktop")
    desktop_root = getattr(getattr(config, "executor", None), "desktop_root", "")
    desktop_harness = _desktop_harness(candidates, desktop_root)
    desktop_store_nonempty = _desktop_credential_store_nonempty()
    from dataclasses import asdict
    try:
        from harness_mcp.config import ModelConfig, resolve_credential
        model = asdict(config.model) if config is not None else asdict(ModelConfig())
        credential_reference_available = bool(resolve_credential(model))
    except Exception:
        credential_reference_available = False
    if config is not None:
        credential_source = config.model.credential_source + "_reference_not_validated"
    elif credential_present:
        credential_source = "environment_detected_not_validated"
    elif desktop_harness["installed"] and desktop_store_nonempty:
        credential_source = "desktop_store_detected_not_validated"
    else:
        credential_source = "no_verified_source"
    blocking: list[dict[str, str]] = []
    if sys.version_info < (3, 11):
        blocking.append({"code": "PYTHON_VERSION_UNSUPPORTED", "message": "需要 Python 3.11 或更新的经测试兼容版本。"})
    packages["mcp"].update(observed_scope="host", required_scope="host")
    packages["deepseek-harness-sdk"].update(observed_scope="host", required_scope="host" if backend == "desktop" else "worker_container", expected_version=SUPPORTED_DESKTOP_SDK)
    for name, package in packages.items():
        if package["required_scope"] != "host":
            continue
        if not package["installed"]:
            blocking.append({"code": "PACKAGE_MISSING", "package": name, "message": f"当前解释器尚未安装 {name}。"})
    if not commands["git"]["installed"]:
        blocking.append({"code": "GIT_UNAVAILABLE", "message": "Git 未安装，无法创建首版授权文件快照。"})
    if backend == "desktop":
        if platform.system() != "Windows":
            blocking.append({"code": "DESKTOP_OS_UNSUPPORTED", "message": "本机 desktop 执行后端仅支持 Windows。"})
        if not desktop_harness["installed"]:
            blocking.append({"code": "DESKTOP_RUNTIME_UNAVAILABLE", "message": "桌面 exe 或 ASAR 内 cli.js 不完整。"})
        sdk = packages["deepseek-harness-sdk"]
        if sdk["installed"] and sdk["version"] != SUPPORTED_DESKTOP_SDK:
            blocking.append({"code": "SDK_VERSION_UNSUPPORTED", "message": "desktop 后端需要锁定的 Harness SDK 0.1.5rc1。"})
    else:
        if not container["available"]:
            blocking.append({"code": "CONTAINER_RUNTIME_UNAVAILABLE", "message": "Docker Linux server 未验证可用。"})
        elif not container["linux_server"]:
            blocking.append({"code": "CONTAINER_OS_UNSUPPORTED", "message": "Docker 当前不是 Linux 容器模式。"})
        blocking.append({"code": "WORKER_EXECUTION_UNVERIFIED", "message": "可选 Docker Worker 安装与实际执行尚未验收。"})
    if not credential_reference_available:
        blocking.append({"code": "MODEL_CREDENTIAL_UNAVAILABLE", "message": "可信配置指定的模型凭证引用不可用；未输出凭证值。"})
    return {
        "schema_version": 1,
        "service_version": __import__("harness_mcp").__version__,
        "implementation_available": True,
        "inventory_complete": True,
        "phase0_ready": False,
        "execution_backend": backend,
        "execution_ready": not blocking,
        "execution_ready_scope": "prerequisites_only",
        "acceptance_verified": False,
        "os": {"system": platform.system(), "release": platform.release(), "architecture": platform.machine()},
        "python": {"version": platform.python_version(), "executable": sys.executable, "minimum_supported": sys.version_info >= (3, 11)},
        "packages": packages,
        "commands": commands,
        "container": container,
        "desktop_harness": desktop_harness,
        "wsl": _wsl_status(executable["wsl"]),
        "windows_install_locations": [
            {"command": name, "path": str(path), "exists": path.is_file()}
            for name, paths in candidates.items() for path in paths
        ],
        "credential_presence": {"DEEPSEEK_API_KEY": credential_present, "desktop_store_nonempty": desktop_store_nonempty, "configured_reference_available": credential_reference_available},
        "credential_source": credential_source,
        "blocking": blocking,
        "pending_verification": [
            "当前后端的 Harness 真实模型调用与源文件引用。",
            "Codex 与 Claude Code 分别完成实际 MCP 工具调用。",
            "当前后端整棵执行器停止、总超时与服务崩溃生命周期。",
            "可选 Docker Worker 镜像构建、只读隔离与真实调用。",
        ],
        "notes": [
            "本盘点不导入或启动 Harness SDK/runtime，不调用模型；只检查安装元数据和凭证引用可用布尔值。",
            "stdout/stderr 仅在内存中限量读取，输出仅包含白名单版本与状态，凭证仅显示存在布尔值。",
            "desktop 后端为用户选择的 Windows 本机执行路线：独立快照/会话和 JobObject 生命周期监管不提供严格文件访问沙箱。",
            "execution_ready 仅表示所选后端前置条件，不能证明模型有效性或两个客户端验收通过。",
            "本自检不读取或认证独立验收结果；真实模型/客户端/生命周期的最新证据另见对应 artifacts。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek Harness MCP 本机只读环境盘点")
    parser.add_argument("--output", type=Path, help="可选的 JSON 报告路径；不会写入凭证值")
    parser.add_argument("--config", type=Path, help="可选：检查可信项目配置和凭证引用，不调用模型")
    parser.add_argument("--require-execution", action="store_true", help="执行验收未就绪时返回退出码 1")
    args = parser.parse_args(argv)
    config = None
    configuration_error = False
    if args.config is not None:
        try:
            from harness_mcp.config import load_config
            config = load_config(args.config.resolve())
        except Exception:
            configuration_error = True
    report = collect_checks(config)
    if config is not None:
        from dataclasses import asdict
        from .service_state import configuration_state
        from .service_diagnostics import service_diagnostics
        local_state = configuration_state(config, args.config.resolve())
        backend_state = service_diagnostics(config)
        report["service_state"] = {**local_state, "configuration_scope": "doctor_current_disk",
                                   "restart_required": True if backend_state.get("restart_required") is True
                                   else local_state["restart_required"], "shared_backend": backend_state}
        report["configuration"] = {"valid":True,"project_ids":sorted(config.projects),"data_dir":str(config.data_dir),"model":asdict(config.model),"backend":config.executor.backend,"default_mode":config.default_mode,"completion":asdict(config.completion),"credential_reference_available":report["credential_presence"]["configured_reference_available"],"credential_validity":"not_verified"}
        if config.executor.backend == "docker":
            report["configuration"].update(worker_image=config.executor.image,worker_image_build_verified=False)
    elif configuration_error:
        report["configuration"] = {"valid":False,"error":"INVALID_CONFIG"}
        report["blocking"].append({"code":"INVALID_CONFIG","message":"本机可信配置无法通过校验。"})
        report["execution_ready"] = False
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        except OSError:
            print("无法写入 doctor JSON 报告，请检查输出目录权限。", file=sys.stderr)
            return 2
    # Windows 终端可能采用旧代码页；替换无法编码的字符，不泄露原始命令错误。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(payload, end="")
    return 1 if args.require_execution and not report["execution_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
