"""共享后台的私有登记与隐藏启动；不抢占旧数据锁、不终止其他客户端。"""

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time

from . import __version__
from .config import load_config
from .errors import HarnessError
from .instance_lock import InstanceLock

_DIRECTORY_IDENTITIES = {}
_SID = None
_ENVIRONMENT_NAMES = frozenset({"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _unsafe():
    return HarnessError("SHARED_STORAGE_UNSAFE", "共享服务登记目录或权限不安全，请检查本机目录。")


def _identity(info):
    return info.st_dev, info.st_ino


def _assert_path(path, *, file=False, missing=False):
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                continue
            raise _unsafe() from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _unsafe()
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise _unsafe()
        saved = _DIRECTORY_IDENTITIES.get(str(part))
        if saved is not None and saved != _identity(info):
            raise _unsafe()
        if part == path and file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise _unsafe()
    if path.resolve() != path:
        raise _unsafe()
    return path


def _powershell_script(script, *, timeout=8):
    import base64
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    environment = dict(os.environ)
    # Codex 的 PowerShell 7 模块路径不能交给系统 PowerShell 5 自动加载。
    environment["PSMODULEPATH"] = str(executable.parent / "Modules")
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False, env=environment, **options)
        if result.returncode != 0 or len(result.stdout) > 32 * 1024:
            raise _unsafe()
        return json.loads(result.stdout.decode("utf-8-sig"))
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        raise _unsafe() from None


def _current_sid():
    global _SID
    if _SID is None:
        value = _powershell_script("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); @{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value}|ConvertTo-Json -Compress")
        if not isinstance(value, dict) or not isinstance(value.get("sid"), str) or not re.fullmatch(r"S-1-[0-9-]{1,200}", value["sid"]):
            raise _unsafe()
        _SID = value["sid"]
    return _SID


def _permissions_safe(path, *, directory=False):
    info = path.stat()
    if os.name != "nt":
        return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
    literal = str(path).replace("'", "''")
    value = _powershell_script("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        f"$acl=Get-Acl -LiteralPath '{literal}'; "
        "$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])|ForEach-Object {@{sid=$_.IdentityReference.Value;type=[int]$_.AccessControlType;rights=[int]$_.FileSystemRights}}); "
        "@{protected=$acl.AreAccessRulesProtected;owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;rules=$rules}|ConvertTo-Json -Compress -Depth 4")
    if not isinstance(value, dict) or value.get("owner") not in {_current_sid(), "S-1-5-18"} or (directory and value.get("protected") is not True):
        return False
    rules = value.get("rules")
    return isinstance(rules, list) and bool(rules) and all(isinstance(rule, dict) and rule.get("sid") in {_current_sid(), "S-1-5-18"} and rule.get("type") == 0 and isinstance(rule.get("rights"), int) for rule in rules) and any(rule.get("sid") == _current_sid() and rule.get("rights", 0) & 0x1F01FF == 0x1F01FF for rule in rules)


def _restrict_permissions(path, *, directory=False):
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
    else:
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/icacls.exe"
        rights = "(OI)(CI)F" if directory else "F"
        try:
            operations = [["/reset"], ["/inheritance:r", "/grant:r", f"*{_current_sid()}:{rights}", f"*S-1-5-18:{rights}"], ["/setowner", f"*{_current_sid()}"]]
            for operation in operations:
                result = subprocess.run([str(executable), str(path), *operation], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise _unsafe()
        except (OSError, subprocess.SubprocessError):
            raise _unsafe() from None
    if not _permissions_safe(path, directory=directory):
        raise _unsafe()


def prepare_private_runtime(path):
    path = Path(path).absolute()
    # 数据目录由可信配置指定；这里只创建最后一层，不穿越重解析祖先。
    missing = []
    existing = path.parent
    while not os.path.lexists(existing):
        missing.append(existing)
        existing = existing.parent
    _assert_path(existing)
    for item in reversed(missing):
        item.mkdir()
        _assert_path(item)
    _assert_path(path.parent)
    _DIRECTORY_IDENTITIES.setdefault(str(path.parent), _identity(path.parent.stat()))
    _assert_path(path, missing=True)
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    _assert_path(path)
    if not path.is_dir():
        raise _unsafe()
    if created:
        _restrict_permissions(path, directory=True)
    elif not _permissions_safe(path, directory=True):
        raise _unsafe()
    _DIRECTORY_IDENTITIES[str(path)] = _identity(path.stat())
    return path


def read_private_json(path):
    path = _assert_path(path, file=True)
    if not _permissions_safe(path):
        raise _unsafe()
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or opened.st_nlink != 1:
                raise _unsafe()
            raw = stream.read(32 * 1024 + 1)
        _assert_path(path, file=True)
        after = path.lstat()
        if _identity(before) != _identity(after) or before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size or len(raw) > 32 * 1024:
            raise _unsafe()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise _unsafe()
        return value
    except (OSError, ValueError, UnicodeError):
        raise _unsafe() from None


def write_private_json(path, value):
    path = Path(path).absolute()
    _assert_path(path.parent)
    if not _permissions_safe(path.parent, directory=True):
        raise _unsafe()
    _assert_path(path, file=True, missing=True)
    if path.exists() and not _permissions_safe(path):
        raise _unsafe()
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > 32 * 1024:
        raise _unsafe()
    temporary = path.parent / (".private-" + secrets.token_hex(16) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        _restrict_permissions(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_path(temporary, file=True)
        _assert_path(path, file=True, missing=True)
        os.replace(temporary, path)
        if not _permissions_safe(path):
            raise _unsafe()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # 仅删除本次随机创建且归属明确的临时文件。
        if temporary.exists():
            _assert_path(temporary, file=True)
            temporary.unlink()


def configuration_digest(config):
    """权限、路由、解释器与实现任一变化都不能静默复用旧后台。"""
    module_dir = Path(__file__).resolve().parent
    sources = {}
    for path in sorted(module_dir.glob("*.py")):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _unsafe()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read()
        after = path.lstat()
        if _identity(info) != _identity(opened) or _identity(info) != _identity(after) or (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
            raise _unsafe()
        # 安装代码可信；uv 的 wheel 缓存普通硬链接不属于私有令牌文件。
        sources[path.name] = hashlib.sha256(raw).hexdigest()
    value = {"configuration": asdict(config), "service_version": __version__, "python": {"executable": str(Path(sys.executable).absolute()), "prefix": sys.prefix, "base_prefix": sys.base_prefix, "version": list(sys.version_info[:3]), "implementation": sys.implementation.name}, "module_path": str(module_dir), "sources": sources}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=lambda item: str(item) if isinstance(item, Path) else None).encode("ascii")).hexdigest()


def _validated_registry(value, config):
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("host") != "127.0.0.1"
        or isinstance(value.get("port"), bool) or not isinstance(value.get("port"), int) or not 1 <= value["port"] <= 65535
        or isinstance(value.get("pid"), bool) or not isinstance(value.get("pid"), int) or value["pid"] < 1
        or any(not isinstance(value.get(name), str) or not _HEX.fullmatch(value[name]) for name in ("token", "config_digest"))
        or value.get("data_dir") != str(config.data_dir.resolve()) or not isinstance(value.get("service_version"), str)):
        raise _unsafe()
    return value


def _data_lock_available(data_dir):
    _assert_path(data_dir / "service.lock", file=True, missing=True)
    try:
        lock = InstanceLock(data_dir)
    except HarnessError as error:
        if error.code == "INSTANCE_LOCKED":
            return False
        raise
    lock.close()
    return True


def _launch_daemon(config_path, bootstrap_path):
    argv = [str(Path(sys.executable).absolute()), "-I", "-m", "harness_mcp.shared_service", "--config", str(config_path), "--bootstrap", str(bootstrap_path)]
    if os.name != "nt":
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            return process.pid
        except OSError:
            raise HarnessError("SHARED_LAUNCH_FAILED", "共享后台无法启动，请检查当前 Python 安装。") from None
    # WMI 由系统服务创建进程，使后台不属于当前 stdio/Codex 的 kill-on-close Job。
    command_line = subprocess.list2cmdline(argv).replace("'", "''")
    working_dir = str(Path(sys.executable).absolute().parent).replace("'", "''")
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
    script += "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
    script += f"$created=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{command_line}';CurrentDirectory='{working_dir}';ProcessStartupInformation=$startup}}; "
    script += "@{pid=[int64]$created.ProcessId;return_value=[int]$created.ReturnValue}|ConvertTo-Json -Compress"
    try:
        value = _powershell_script(script, timeout=12)
        if not isinstance(value, dict) or value.get("return_value") != 0 or not isinstance(value.get("pid"), int) or value["pid"] <= 0:
            raise ValueError()
        return value["pid"]
    except (HarnessError, ValueError):
        raise HarnessError("SHARED_LAUNCH_FAILED", "系统无法独立启动共享后台；请检查本机 WMI 服务。") from None


def _bootstrap_payload(config, config_path, digest):
    names = {item for item in _ENVIRONMENT_NAMES if item.upper() != config.model.credential_env.upper()}
    environment = {name: os.environ[name] for name in names if name in os.environ and "\x00" not in os.environ[name]}
    return {"version": 1, "config_path": str(config_path), "config_digest": digest, "startup_id": secrets.token_hex(16), "token": secrets.token_hex(32), "idle_seconds": 30, "nonsecret_environment": environment}


async def connect_or_start(config, config_path):
    from .shared_client import open_authenticated
    deadline = time.monotonic() + 20
    runtime = await asyncio.to_thread(prepare_private_runtime, config.data_dir / "shared-service")
    digest = configuration_digest(config)
    config_path = Path(config_path).absolute()
    if not config_path.is_absolute() or configuration_digest(load_config(config_path)) != digest:
        raise HarnessError("SHARED_CONFIG_MISMATCH", "启动配置已变化，请重新加载 MCP 配置。")

    known_registry = None

    async def existing():
        nonlocal known_registry
        registry_path = runtime / "registry.json"
        if not registry_path.exists():
            _assert_path(registry_path, file=True, missing=True)
            return None
        registry = _validated_registry(await asyncio.to_thread(read_private_json, registry_path), config)
        known_registry = registry
        try:
            reader, writer = await open_authenticated(registry)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, HarnessError):
            return None
        if registry["config_digest"] != digest:
            writer.close()
            await writer.wait_closed()
            raise HarnessError("SHARED_CONFIG_MISMATCH", "已有会话使用不同的项目权限或模型配置；等待后台空闲退出后重新连接。")
        return reader, writer, registry

    connected = await existing()
    if connected is not None:
        return connected
    lock_dir = runtime / "start-lock"
    await asyncio.to_thread(prepare_private_runtime, lock_dir)
    start_lock = None
    while start_lock is None:
        _assert_path(lock_dir / "service.lock", file=True, missing=True)
        try:
            start_lock = InstanceLock(lock_dir)
            try:
                await asyncio.to_thread(_restrict_permissions, lock_dir / "service.lock")
            except BaseException:
                start_lock.close()
                raise
        except HarnessError as error:
            if error.code != "INSTANCE_LOCKED":
                raise
            connected = await existing()
            if connected is not None:
                return connected
            if time.monotonic() >= deadline:
                raise HarnessError("SHARED_START_TIMEOUT", "共享后台启动超时，请查看脱敏诊断。") from None
            await asyncio.sleep(0.1)
    try:
        connected = await existing()
        if connected is not None:
            return connected
        lock_available = _data_lock_available(config.data_dir)
        if not lock_available and known_registry is not None and known_registry["config_digest"] == digest:
            # 空闲后台已拒新连接但仍在收敛退出时，短暂等锁释放；不终止它。
            shutdown_deadline = min(deadline, time.monotonic() + 2)
            while not lock_available and time.monotonic() < shutdown_deadline:
                await asyncio.sleep(0.1)
                connected = await existing()
                if connected is not None:
                    return connected
                lock_available = _data_lock_available(config.data_dir)
        if not lock_available:
            raise HarnessError("SHARED_BACKEND_UNAVAILABLE", "此数据目录仍由旧服务或无法确认的后台使用；请关闭旧连接后重试，不能另建目录绕过归属检查。")
        if time.monotonic() >= deadline:
            raise HarnessError("SHARED_START_TIMEOUT", "共享后台启动超时，请查看脱敏诊断。")
        bootstrap = _bootstrap_payload(config, config_path, digest)
        bootstrap_path = runtime / "bootstrap.json"
        await asyncio.to_thread(write_private_json, bootstrap_path, bootstrap)
        launched_pid = await asyncio.to_thread(_launch_daemon, config_path, bootstrap_path)
        while time.monotonic() < deadline:
            failure_path = runtime / "startup-error.json"
            if failure_path.exists():
                failure = await asyncio.to_thread(read_private_json, failure_path)
                if failure.get("version") == 1 and launched_pid in {failure.get("pid"), failure.get("parent_pid")} and failure.get("config_digest") == digest and failure.get("startup_id") == bootstrap["startup_id"]:
                    code = failure.get("code")
                    if code not in {"SHARED_STORAGE_UNSAFE", "SHARED_CONFIG_MISMATCH", "INSTANCE_LOCKED", "INVALID_CONFIG", "SHARED_PROTOCOL_ERROR", "SHARED_PRIVATE_PATH_UNSAFE", "SHARED_RUNTIME_UNSAFE", "DESKTOP_OS_UNSUPPORTED", "DESKTOP_RUNTIME_UNAVAILABLE", "SDK_VERSION_UNSUPPORTED", "DESKTOP_NOT_FOUND", "DOCKER_NOT_FOUND", "DOCKER_UNAVAILABLE", "EXECUTION_BLOCKED", "SERVICE_STOPPING", "INTERNAL_ERROR"}:
                        code = "SHARED_BACKEND_UNAVAILABLE"
                    from .errors import error_from_payload
                    raise error_from_payload({key: value for key, value in
                        {**failure, "code": code, "message": "共享后台启动失败，请查看本机环境诊断。"}.items()
                        if key in {"code", "message", "stage", "diagnostic_id", "exception_type", "details"}})
            connected = await existing()
            if connected is not None:
                if connected[2]["token"] != bootstrap["token"]:
                    connected[1].close()
                    await connected[1].wait_closed()
                    raise HarnessError("SHARED_BACKEND_UNAVAILABLE", "共享后台归属与本次启动不匹配。")
                return connected
            await asyncio.sleep(0.1)
        raise HarnessError("SHARED_START_TIMEOUT", "共享后台启动超时，请查看脱敏诊断。")
    finally:
        start_lock.close()
