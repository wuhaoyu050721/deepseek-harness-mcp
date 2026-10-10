"""本机可信配置与调用参数校验；MCP 参数不能改变执行权限或模型路由。"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tomllib
from urllib.parse import urlsplit

from .errors import HarnessError


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    path: Path
    allowed_paths: tuple[str, ...] = (".",)
    exclude_patterns: tuple[str, ...] = ()
    allow_untracked: tuple[str, ...] = ()
    allow_edits: bool = False


@dataclass(frozen=True)
class LimitsConfig:
    max_queue: int = 5
    max_parallel_jobs: int = 3
    default_timeout_seconds: int = 600
    max_timeout_seconds: int = 1800
    max_file_bytes: int = 2 * 1024 * 1024
    max_snapshot_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_prompt_chars: int = 32000


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/anthropic"
    credential_source: str = "desktop_store"
    credential_env: str = "DEEPSEEK_API_KEY"


@dataclass(frozen=True)
class ExecutorConfig:
    backend: str = "desktop"
    desktop_root: str = ""
    image: str = "deepseek-harness-worker:0.1.0"
    docker_command: str = "docker"
    cpus: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 128
    lease_seconds: int = 15
    heartbeat_seconds: int = 2
    termination_grace_seconds: int = 3


@dataclass(frozen=True)
class CompletionConfig:
    # 仅可信本机配置授权自动续跑；任务参数不能指定通知目标或命令。
    auto_continue: bool = False


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    default_mode: str = "review"
    completion: CompletionConfig = field(default_factory=CompletionConfig)


def normalize_relative_path(value: str, *, allow_root: bool = True) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise HarnessError("INVALID_PATH", "项目范围必须是非空相对路径。")
    value = value.replace("\\", "/")
    if value == "." and allow_root:
        return "."
    if value.startswith("/") or PureWindowsPath(value).drive or ":" in value:
        raise HarnessError("INVALID_PATH", "不允许绝对路径、盘符、UNC 或数据流路径。")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HarnessError("INVALID_PATH", "路径不允许越界或空组成部分。")
    if any(part.endswith((" ", ".")) for part in parts):
        raise HarnessError("INVALID_PATH", "路径末尾的空格或点不被允许。")
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        raise HarnessError("INVALID_PATH", "路径不允许 Windows 设备名。")
    return "/".join(parts)


def _known_keys(data: dict, allowed: set[str], section: str) -> None:
    if not isinstance(data, dict) or set(data) - allowed:
        raise HarnessError("INVALID_CONFIG", f"{section} 存在不支持的配置字段。")


def _bounded_int(value, lower: int, upper: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise HarnessError("INVALID_CONFIG", f"{name} 超出允许范围。")
    return value


def load_config(path: Path, *, source_bytes: bytes | None = None) -> AppConfig:
    try:
        # 热授权入口传入已校验句柄读出的同一批字节，避免验证后再次打开文件。
        content = path.read_text(encoding="utf-8") if source_bytes is None else source_bytes.decode("utf-8")
        data = tomllib.loads(content)
    except (OSError, ValueError, UnicodeError):
        raise HarnessError("INVALID_CONFIG", "配置文件无法读取或 TOML 格式不正确。") from None
    _known_keys(data, {"data_dir", "default_mode", "completion", "model", "executor", "limits", "projects"}, "根配置")
    default_mode = data.get("default_mode", "review")
    if not isinstance(default_mode, str) or default_mode not in {"review", "edit"}:
        raise HarnessError("INVALID_CONFIG", "default_mode 必须是 review 或 edit。")
    completion_values = data.get("completion", {})
    _known_keys(completion_values, set(CompletionConfig.__dataclass_fields__), "completion")
    completion = CompletionConfig(**completion_values)
    if not isinstance(completion.auto_continue, bool):
        raise HarnessError("INVALID_CONFIG", "completion.auto_continue 必须是本机配置中的布尔值。")
    if not isinstance(data.get("data_dir"), str):
        raise HarnessError("INVALID_CONFIG", "必须设置本实例 data_dir。")
    data_dir = Path(data["data_dir"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = path.resolve().parent / data_dir
    data_dir = data_dir.resolve()
    limit_values = data.get("limits", {})
    _known_keys(limit_values, set(LimitsConfig.__dataclass_fields__), "limits")
    limits = LimitsConfig(**limit_values)
    for key in ("max_file_bytes", "max_snapshot_bytes", "max_output_bytes", "max_prompt_chars"):
        _bounded_int(getattr(limits, key), 1, 1024 * 1024 * 1024, key)
    _bounded_int(limits.max_output_bytes, 1, 16*1024*1024, "max_output_bytes")
    _bounded_int(limits.max_prompt_chars, 1, 1024*1024, "max_prompt_chars")
    _bounded_int(limits.max_queue, 1, 5, "max_queue")
    _bounded_int(limits.max_parallel_jobs, 1, 8, "max_parallel_jobs")
    _bounded_int(limits.max_timeout_seconds, 1, 86400, "max_timeout_seconds")
    _bounded_int(limits.default_timeout_seconds, 1, limits.max_timeout_seconds, "default_timeout_seconds")
    model_values = data.get("model", {})
    _known_keys(model_values, set(ModelConfig.__dataclass_fields__), "model")
    model = ModelConfig(**model_values)
    if any(not isinstance(getattr(model, key), str) or not getattr(model, key) or any(ord(c) < 32 for c in getattr(model, key)) for key in ModelConfig.__dataclass_fields__):
        raise HarnessError("INVALID_CONFIG", "模型配置字段必须是非空字符串。")
    try:
        parsed = urlsplit(model.base_url)
        parsed.port
    except ValueError:
        raise HarnessError("INVALID_CONFIG", "模型地址格式不正确。") from None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HarnessError("INVALID_CONFIG", "模型地址必须是无内嵌凭证的 HTTP(S) 地址。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HarnessError("INVALID_CONFIG", "非本机模型地址需要 HTTPS。")
    if model.credential_source not in {"env", "desktop_store"} or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", model.credential_env):
        raise HarnessError("INVALID_CONFIG", "模型凭证来源不受支持。")
    if not model.model or len(model.model) > 200 or not model.provider or len(model.provider) > 200:
        raise HarnessError("INVALID_CONFIG", "模型和 provider 名称不正确。")
    execution_values = data.get("executor", {})
    _known_keys(execution_values, set(ExecutorConfig.__dataclass_fields__), "executor")
    executor = ExecutorConfig(**execution_values)
    if not isinstance(executor.backend, str) or executor.backend not in {"desktop", "docker"}:
        raise HarnessError("INVALID_CONFIG", "执行器 backend 必须是 desktop 或 docker。")
    if default_mode == "edit" and executor.backend != "desktop":
        raise HarnessError("INVALID_CONFIG", "默认修改模式当前仅支持 Windows desktop 执行器。")
    if not isinstance(executor.desktop_root, str) or any(ord(c) < 32 for c in executor.desktop_root):
        raise HarnessError("INVALID_CONFIG", "桌面安装路径不正确。")
    if executor.desktop_root and not Path(executor.desktop_root).is_absolute():
        raise HarnessError("INVALID_CONFIG", "桌面安装路径必须是绝对路径。")
    if not isinstance(executor.image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", executor.image):
        raise HarnessError("INVALID_CONFIG", "Worker 镜像名称不正确。")
    if isinstance(executor.cpus, bool) or not isinstance(executor.cpus, (int, float)) or not 0.1 <= executor.cpus <= 64:
        raise HarnessError("INVALID_CONFIG", "CPU 限制不正确。")
    for name, lower, upper in (("memory_mb",128,65536),("pids_limit",16,4096),("lease_seconds",5,120),("heartbeat_seconds",1,30),("termination_grace_seconds",1,30)):
        _bounded_int(getattr(executor, name), lower, upper, name)
    if executor.heartbeat_seconds * 3 > executor.lease_seconds:
        raise HarnessError("INVALID_CONFIG", "执行器租约必须至少覆盖三个心跳周期。")
    if not isinstance(executor.docker_command, str) or not executor.docker_command:
        raise HarnessError("INVALID_CONFIG", "Docker 命令路径不正确。")
    projects = {}
    project_values = data.get("projects", {})
    if not isinstance(project_values, dict):
        raise HarnessError("INVALID_CONFIG", "projects 必须是项目映射。")
    for project_id, value in project_values.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            raise HarnessError("INVALID_CONFIG", "项目编号不正确。")
        _known_keys(value, {"path", "allowed_paths", "exclude_patterns", "allow_untracked", "allow_edits"}, "project")
        if not isinstance(value.get("path"), str):
            raise HarnessError("INVALID_CONFIG", "项目必须设置 path。")
        project_path = Path(value["path"]).expanduser()
        if not project_path.is_absolute():
            raise HarnessError("INVALID_CONFIG", "授权项目必须使用本机绝对路径。")
        def scopes(key, default):
            values = value.get(key, default)
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise HarnessError("INVALID_CONFIG", "授权范围必须是路径列表。")
            return tuple(normalize_relative_path(item) for item in values)
        excludes = value.get("exclude_patterns", [])
        if not isinstance(excludes, list) or not all(isinstance(item,str) for item in excludes):
            raise HarnessError("INVALID_CONFIG", "排除规则必须是字符串列表。")
        allow_edits = value.get("allow_edits", False)
        if not isinstance(allow_edits, bool):
            raise HarnessError("INVALID_CONFIG", "allow_edits 必须是本机配置中的布尔值。")
        projects[project_id] = ProjectConfig(project_id, project_path, scopes("allowed_paths", ["."]), tuple(excludes), scopes("allow_untracked", []), allow_edits)
    return AppConfig(data_dir, projects, limits, model, executor, default_mode, completion)


def normalize_request(config: AppConfig, project_id: str, prompt: str, client_request_id: str, relative_paths=None, timeout_seconds=None, mode: str | None = None) -> dict:
    if not isinstance(project_id,str) or project_id not in config.projects:
        raise HarnessError("PROJECT_NOT_ALLOWED", "项目未在本机配置中授权。")
    # 省略模式采用可信默认值；显式 review 仍可创建只读任务。
    mode = config.default_mode if mode is None else mode
    if not isinstance(mode, str) or mode not in {"review", "edit"}:
        raise HarnessError("INVALID_MODE", "任务模式必须是 review 或 edit。")
    if mode == "edit":
        if not config.projects[project_id].allow_edits:
            raise HarnessError("EDIT_NOT_ALLOWED", "本项目尚未在可信配置中授权修改副本。")
        if config.executor.backend != "desktop":
            raise HarnessError("EDIT_BACKEND_UNSUPPORTED", "修改模式当前仅支持 Windows desktop 执行器。")
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt) > config.limits.max_prompt_chars:
        raise HarnessError("INVALID_PROMPT", "任务描述为空或超过长度限制。")
    if not isinstance(client_request_id,str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", client_request_id):
        raise HarnessError("INVALID_REQUEST_ID", "必须提供长度不超过 128 的有效请求编号。")
    if relative_paths is not None and (not isinstance(relative_paths,list) or len(relative_paths) > 256):
        raise HarnessError("INVALID_PATH", "relative_paths 必须是有界路径列表。")
    if relative_paths == []:
        raise HarnessError("EMPTY_SCOPE", "显式任务范围不能为空；省略时才使用配置默认范围。")
    paths = sorted(set(normalize_relative_path(item) for item in relative_paths)) if relative_paths else None
    allowed = config.projects[project_id].allowed_paths
    if not allowed:
        raise HarnessError("EMPTY_AUTHORIZATION", "项目未配置可读取范围。")
    if paths and any(not any(scope == "." or item == scope or item.startswith(scope + "/") for scope in allowed) for item in paths):
        raise HarnessError("PATH_NOT_AUTHORIZED", "请求的文件范围超出管理员授权。")
    timeout = config.limits.default_timeout_seconds if timeout_seconds is None else timeout_seconds
    if isinstance(timeout,bool) or not isinstance(timeout,int) or not 1 <= timeout <= config.limits.max_timeout_seconds:
        raise HarnessError("INVALID_TIMEOUT", "超时必须位于管理员配置的范围内。")
    return {"project_id":project_id,"prompt":prompt.replace("\r\n","\n"),"client_request_id":client_request_id,"relative_paths":paths,"timeout_seconds":timeout,"mode":mode}


def request_fingerprint(request: dict) -> str:
    # 历史只读记录没有 mode；默认 review 的指纹保持不变，不使旧编号失去幂等性。
    normalized = dict(request)
    if normalized.get("mode", "review") == "review":
        normalized.pop("mode", None)
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def frozen_execution(config: AppConfig, project_id: str) -> dict:
    project = asdict(config.projects[project_id]); project["path"] = str(project["path"])
    return {"project":project,"model":asdict(config.model),"limits":asdict(config.limits),"executor":asdict(config.executor)}


def resolve_credential(model: dict) -> str:
    """读取可信引用；调用方只能在进程环境中传递返回值，禁止持久化。"""
    name = model["credential_env"]
    if model["credential_source"] == "env":
        secret = os.environ.get(name, "")
    else:
        import yaml
        path = Path.home() / ".dsh" / ".credentials.yaml"
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError()
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            secret = (data.get("refs", {}) or {}).get(name, "")
        except Exception:
            raise HarnessError("CREDENTIAL_UNAVAILABLE", "本机模型凭证引用无法读取。") from None
    if not isinstance(secret,str) or not secret.strip() or "\x00" in secret:
        raise HarnessError("CREDENTIAL_UNAVAILABLE", "所配置的模型凭证引用为空或无效。")
    return secret

