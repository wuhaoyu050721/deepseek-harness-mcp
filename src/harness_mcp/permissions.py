"""本机授权热更新：固定运行身份，每次新请求捕获同一份可信权限。"""

from dataclasses import asdict, replace
import copy
import hashlib
import json
from pathlib import Path

from .config import load_config
from .errors import HarnessError
from .service_state import config_version


AUTHORIZATION_FIELDS = frozenset({"allowed_paths", "exclude_patterns", "allow_untracked", "allow_edits"})


def runtime_configuration(config) -> dict:
    value = asdict(config)
    for project in value["projects"].values():
        for name in AUTHORIZATION_FIELDS:
            project.pop(name, None)
    return value


def runtime_execution(execution) -> dict:
    value = copy.deepcopy(execution)
    for name in AUTHORIZATION_FIELDS:
        value["project"].pop(name, None)
    # SQLite 中 tuple 已变为 list；统一序列化形状后再比较运行身份。
    return json.loads(json.dumps(value, default=str))


def permission_version(config) -> str:
    value = {key:{name:getattr(project, name) for name in sorted(AUTHORIZATION_FIELDS)}
             for key, project in config.projects.items()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _within(path, scope):
    return scope == "." or path == scope or path.startswith(scope + "/")


def _intersection(first, second):
    values = set()
    for left in first:
        for right in second:
            if _within(left, right):
                values.add(left)
            elif _within(right, left):
                values.add(right)
    return tuple(sorted(value for value in values if not any(value != other and _within(value, other) for other in values)))


def session_config(config, binding):
    """原会话和当前项目权限求交；放宽本机上限不能悄悄扩大旧会话。"""
    saved = binding["execution"]["project"]
    project = config.projects[saved["id"]]
    bounded = replace(project,
        allowed_paths=_intersection(saved["allowed_paths"], project.allowed_paths),
        exclude_patterns=tuple(sorted(set(saved["exclude_patterns"]) | set(project.exclude_patterns))),
        # 未跟踪许可是逐文件名单，目录前缀不能借求交变成新增文件授权。
        allow_untracked=tuple(sorted(set(saved["allow_untracked"]) & set(project.allow_untracked))),
        allow_edits=saved.get("allow_edits", False) and project.allow_edits)
    return replace(config, projects={**config.projects, project.id:bounded})


class PermissionController:
    def __init__(self, config):
        self.startup = config
        self.current = config
        self.source = None
        self.error = None
        self.disk_version = None
        self.reload_state = "unconfigured"

    def refresh(self, config_path=None):
        if config_path is None:
            if self.source is not None:
                self.error = HarnessError("PERMISSIONS_CONFIG_INVALID", "不能移除已绑定的本机授权配置来源。")
                self.reload_state = "invalid"
                raise self.error
            return self.current
        # 来源只能在初次绑定时确定；普通配置文件原地替换合法，链接及硬链接不合法。
        path = Path(config_path).absolute()
        if self.source is not None and path != self.source:
            self.error = HarnessError("PERMISSIONS_CONFIG_INVALID", "本机授权配置来源与启动绑定不同。")
            self.reload_state = "invalid"
            raise self.error
        self.source = path
        try:
            from .workspace import _read_verified
            raw = _read_verified(path, 1024 * 1024)
            candidate = load_config(path, source_bytes=raw)
        except Exception:
            self.disk_version = None
            self.reload_state = "invalid"
            self.error = HarnessError("PERMISSIONS_CONFIG_INVALID", "本机授权配置不可读取或无效，新任务未获接受。")
            raise self.error from None
        self.disk_version = config_version(candidate)
        if runtime_configuration(candidate) != runtime_configuration(self.startup):
            self.reload_state = "restart_required"
            self.error = HarnessError("CONFIG_RESTART_REQUIRED", "项目根或运行配置已变化；完成原任务并重新连接后才能提交新任务。")
            raise self.error
        changed = config_version(candidate) != config_version(self.current)
        self.current = candidate
        self.reload_state = "updated" if changed else "current"
        self.error = None
        return candidate

    def information(self, config_path=None):
        try:
            self.refresh(config_path)
        except HarnessError:
            pass
        return {
            "config_version":config_version(self.current), "disk_config_version":self.disk_version,
            "config_state":"invalid" if self.reload_state == "invalid" else "changed" if self.error else "current" if self.source else "unknown",
            "restart_required":self.reload_state == "restart_required" if self.source else None,
            "permissions_hot_reload":self.source is not None,
            "permissions_version":permission_version(self.current),
            "permissions_reload_state":self.reload_state,
            "new_task_permissions_ready":self.error is None,
            "permissions_error":self.error.code if self.error else None,
            "existing_jobs_permissions":"frozen_at_submission",
        }
