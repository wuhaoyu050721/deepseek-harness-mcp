"""持久化子代理会话：绑定项目与范围，逐轮执行并携带本会话的有界历史。"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .config import frozen_execution, normalize_relative_path, normalize_request, request_fingerprint
from .errors import HarnessError
from .job_store import TERMINAL
from .parallel_manager import ParallelTaskManager
from .redaction import redact_value


HISTORY_TURNS = 4
HISTORY_CHARS = 16000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _execution(config, project_id):
    # 配置中的tuple在SQLite JSON内成为list；两侧使用相同的持久化形状。
    return json.loads(json.dumps(frozen_execution(config, project_id), ensure_ascii=False))


class SessionManager(ParallelTaskManager):
    def __init__(self, config, runner_factory=None):
        super().__init__(config, runner_factory=runner_factory)
        self.store.connection.execute("""CREATE TABLE IF NOT EXISTS agent_sessions (
            session_id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, binding_json TEXT NOT NULL,
            project_id TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL
        )""")

    def _session(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HarnessError("SESSION_NOT_FOUND", "未找到指定的 DeepSeek 子代理会话。")
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise HarnessError("SESSION_NOT_FOUND", "未找到指定的 DeepSeek 子代理会话。")
        value = dict(row)
        value["binding"] = json.loads(value.pop("binding_json"))
        return value

    def _binding_valid(self, session, config):
        from .permissions import runtime_execution

        project_id = session["project_id"]
        if project_id not in config.projects or runtime_execution(session["binding"]["execution"]) != runtime_execution(_execution(config, project_id)):
            raise HarnessError("SESSION_CONFIG_CHANGED", "该会话绑定的项目或配置已变更，请为新配置创建新会话。")

    def _jobs(self, session_id, *, limit=8):
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM jobs WHERE json_extract(request_json,'$.session_id')=? ORDER BY rowid DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [self.store._decode(row) for row in rows]

    async def session_open(self, project_id, client_request_id, title="", mode=None, relative_paths=None):
        if not isinstance(title, str) or len(title) > 200 or any(ord(char) < 32 for char in title):
            raise HarnessError("INVALID_SESSION_TITLE", "会话标题须为不超过200字的单行文本。")
        if not isinstance(client_request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", client_request_id):
            raise HarnessError("INVALID_REQUEST_ID", "必须提供长度不超过 128 的有效请求编号。")
        with self.store.transaction() as cursor:
            if cursor.execute("SELECT 1 FROM jobs WHERE client_request_id=?", (client_request_id,)).fetchone():
                raise HarnessError("IDEMPOTENCY_CONFLICT", "该请求编号已用于任务，不能再创建会话。")
            row = cursor.execute("SELECT * FROM agent_sessions WHERE client_request_id=?", (client_request_id,)).fetchone()
            if row is not None:
                # 原请求先按原绑定恢复；撤销权限或修改默认模式不能造成第二个会话。
                binding = json.loads(row["binding_json"])
                if relative_paths is not None and (not isinstance(relative_paths, list) or len(relative_paths) > 256):
                    raise HarnessError("INVALID_PATH", "relative_paths 必须是有界路径列表。")
                if relative_paths == []:
                    raise HarnessError("EMPTY_SCOPE", "显式任务范围不能为空；省略时才使用配置默认范围。")
                paths = sorted(set(normalize_relative_path(item) for item in relative_paths)) if relative_paths else None
                restored_mode = binding["mode"] if mode is None else mode
                if (row["project_id"], row["title"], binding["mode"], binding["relative_paths"]) != (project_id, title, restored_mode, paths):
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一会话请求编号已经绑定了不同的参数。")
                return redact_value({"session_id": row["session_id"], "project_id": project_id, "title": title, "mode": binding["mode"], "relative_paths": binding["relative_paths"], "idempotent_replay": True}, self.secrets)
            if not self.accepting:
                raise HarnessError("SERVICE_STOPPING", "服务正在关闭，不再接受新会话。")
            config = self._permission_config()
            validated = normalize_request(config, project_id, "创建子代理会话", client_request_id, relative_paths, None, mode)
            mode = validated["mode"]
            binding = {"mode": mode, "relative_paths": validated["relative_paths"], "execution": _execution(config, project_id)}
            fingerprint = _digest({"project_id": project_id, "title": title, "binding": binding})
            session_id = uuid.uuid4().hex
            cursor.execute("INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,?)", (session_id, client_request_id, fingerprint, json.dumps(binding, ensure_ascii=False), project_id, title, time.time()))
        return redact_value({"session_id": session_id, "project_id": project_id, "title": title, "mode": mode, "relative_paths": binding["relative_paths"], "idempotent_replay": False}, self.secrets)

    def _context(self, session_id):
        from .report import read_report, render_markdown
        context, remaining = [], HISTORY_CHARS
        for job in self._jobs(session_id, limit=HISTORY_TURNS):
            if job["status"] not in TERMINAL:
                continue
            user = redact_value(job["request"]["prompt"], self.secrets)[:min(2000, remaining)]
            remaining -= len(user)
            assistant = ""
            if job["report_id"] and remaining:
                report = read_report(self.config.data_dir / "jobs" / job["job_id"], max_bytes=max(2 * 1024 * 1024, self.config.limits.max_output_bytes * 3), expected_report_id=job["report_id"])
                assistant = redact_value(render_markdown(report), self.secrets)[:min(6000, remaining)]
                remaining -= len(assistant)
            context.append({"user": user, "assistant": assistant, "status": job["status"], "job_id": job["job_id"]})
            if not remaining:
                break
        # 先挑选最近轮次，再恢复时间顺序；仅保存已导出文本，绝不保存reasoning。
        return redact_value(list(reversed(context)), self.secrets)

    async def session_send(self, session_id, prompt, client_request_id, timeout_seconds=None, *, completion_origin=None):
        from .permissions import session_config

        session = self._session(session_id)
        binding = session["binding"]
        replay = self._replay_request(session["project_id"], prompt, client_request_id, binding["relative_paths"], timeout_seconds, binding["mode"], session_id=session_id)
        if replay is not None:
            if completion_origin is not None:
                self.store.bind_completion(replay["job_id"], self._accepted_origin(completion_origin))
            return {**self._with_completion(replay), "idempotent_replay": True, "auto_continue": completion_origin is not None}
        if not self.accepting:
            raise HarnessError("SERVICE_STOPPING", "服务正在关闭，不再接受新任务。")
        current = self._permission_config()
        self._binding_valid(session, current)
        # 当前授权与最初会话授权求交，放宽项目不能自动扩大会话范围。
        config = session_config(current, binding)
        request = normalize_request(config, session["project_id"], prompt, client_request_id, binding["relative_paths"], timeout_seconds, binding["mode"])
        request["session_id"] = session_id
        fingerprint = request_fingerprint(request)
        # 先恢复原回执，再检查忙状态。历史增长不改变原请求的指纹，也不重放模型。
        with self.store._lock:
            if self.store.connection.execute("SELECT 1 FROM agent_sessions WHERE client_request_id=?", (client_request_id,)).fetchone():
                raise HarnessError("IDEMPOTENCY_CONFLICT", "该请求编号已用于创建会话，不能再提交任务。")
            existing = self.store.connection.execute("SELECT * FROM jobs WHERE client_request_id=?", (client_request_id,)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一请求编号已经绑定了不同的任务参数。")
                if completion_origin is not None:
                    self.store.bind_completion(existing["job_id"], self._accepted_origin(completion_origin))
                return {**self._with_completion(self._receipt(self.store._decode(existing))), "idempotent_replay": True, "auto_continue": completion_origin is not None}
            pending = self.store.connection.execute("SELECT 1 FROM jobs WHERE json_extract(request_json,'$.session_id')=? AND status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')", (session_id,)).fetchone()
        if pending:
            raise HarnessError("SESSION_BUSY", "该子代理仍有任务未结束，请查询或取消原任务后继续对话。")
        request["conversation_context"] = self._context(session_id)
        # 到入库之前没有await，因此同一事件循环上的并发send不能交叉接纳两轮。
        job, created = self.store.submit_parallel(request, fingerprint, frozen_execution(config, session["project_id"]), config.limits.max_queue, config.limits.max_parallel_jobs, completion_origin=self._accepted_origin(completion_origin))
        self.wakeup.set()
        return {**self._with_completion(self._receipt(job)), "idempotent_replay": not created, "auto_continue": completion_origin is not None}

    async def submit(self, project_id, prompt, client_request_id, relative_paths=None, timeout_seconds=None, mode=None, *, completion_origin=None):
        with self.store._lock:
            if self.store.connection.execute("SELECT 1 FROM agent_sessions WHERE client_request_id=?", (client_request_id,)).fetchone():
                raise HarnessError("IDEMPOTENCY_CONFLICT", "该请求编号已用于创建会话，不能再提交任务。")
        return await super().submit(project_id, prompt, client_request_id, relative_paths, timeout_seconds, mode, completion_origin=completion_origin)

    def session_list(self, project_id=None, limit=20):
        from .permissions import session_config

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HarnessError("INVALID_PAGE_SIZE", "会话列表数量须在1到100之间。")
        config = None
        permission_error = None
        try:
            config = self._permission_config()
        except HarnessError as error:
            permission_error = error.code
        with self.store._lock:
            if project_id is not None and (config is None or project_id not in config.projects):
                known = self.store.connection.execute("SELECT 1 FROM agent_sessions WHERE project_id=?", (project_id,)).fetchone()
                if not known:
                    raise HarnessError("PROJECT_NOT_ALLOWED", "项目未在本机配置中授权。")
            rows = self.store.connection.execute("SELECT * FROM agent_sessions WHERE (? IS NULL OR project_id=?) ORDER BY rowid DESC LIMIT ?", (project_id, project_id, limit)).fetchall()
        sessions = []
        for row in rows:
            binding = json.loads(row["binding_json"])
            jobs = self._jobs(row["session_id"])
            configuration_current = None
            permissions_error = permission_error
            if config is not None:
                try:
                    self._binding_valid({"project_id": row["project_id"], "binding": binding}, config)
                    configuration_current = True
                    scoped = session_config(config, binding)
                    normalize_request(scoped, row["project_id"], "检查会话授权", "session-authorization", binding["relative_paths"], mode=binding["mode"])
                except HarnessError as error:
                    if configuration_current is None:
                        configuration_current = False
                    permissions_error = error.code
            sessions.append({"session_id": row["session_id"], "project_id": row["project_id"], "title": row["title"], "mode": binding["mode"], "relative_paths": binding["relative_paths"], "created_at": row["created_at"], "configuration_current": configuration_current, "permissions_available": permissions_error is None, "permissions_error": permissions_error, "recent_jobs": [self._receipt(job) for job in jobs]})
        return {"sessions": redact_value(sessions, self.secrets), "limit": limit, "max_parallel_jobs": self.config.limits.max_parallel_jobs, "history_turns": HISTORY_TURNS, "history_chars": HISTORY_CHARS}
