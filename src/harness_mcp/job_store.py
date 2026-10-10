"""SQLite 事务统一裁决幂等、队列容量和终态，避免后台竞态重放模型。"""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .errors import HarnessError, error_from_payload, error_payload


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})
ACTIVE = frozenset({"preparing", "running", "cancelling"})
STATES = TERMINAL | ACTIVE | {"queued"}


class JobStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "jobs.sqlite3", isolation_level=None, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.completion_wakeup = None
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, request_json TEXT NOT NULL, execution_json TEXT NOT NULL,
                status TEXT NOT NULL, phase TEXT NOT NULL,
                created_at REAL NOT NULL, started_at REAL, finished_at REAL, last_activity_at REAL NOT NULL,
                cancel_reason TEXT, error_json TEXT, report_id TEXT, snapshot_json TEXT,
                termination_failed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS completion_bindings (
                job_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                host_id TEXT NOT NULL, pipe_path TEXT NOT NULL, bound_at REAL NOT NULL,
                authorization INTEGER NOT NULL CHECK(authorization=1),
                client TEXT NOT NULL DEFAULT 'codex', destination_id TEXT NOT NULL DEFAULT '', origin_json TEXT
            );
            CREATE TABLE IF NOT EXISTS completion_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
                lease_token TEXT, lease_until REAL, delivered_at REAL, uncertain_at REAL,
                acknowledged_at REAL, last_error_code TEXT, delivery_status TEXT
            );
            CREATE INDEX IF NOT EXISTS completion_pending ON completion_events(state,next_attempt_at,event_id);
        """)
        with self.transaction() as cursor:
            # 旧版只有 Codex 四字段；逐列迁移并保留全部终态和投递状态。
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(completion_bindings)").fetchall()}
            for name, declaration in (("client", "TEXT NOT NULL DEFAULT 'codex'"),
                                      ("destination_id", "TEXT NOT NULL DEFAULT ''"), ("origin_json", "TEXT")):
                if name not in columns:
                    cursor.execute(f"ALTER TABLE completion_bindings ADD COLUMN {name} {declaration}")
            cursor.execute("UPDATE completion_bindings SET destination_id=thread_id WHERE client='codex' AND destination_id=''")
            cursor.execute("""UPDATE completion_bindings SET origin_json=json_object(
                'thread_id',thread_id,'turn_id',turn_id,'host_id',host_id,'pipe_path',pipe_path)
                WHERE client='codex' AND origin_json IS NULL""")
            cursor.execute("CREATE INDEX IF NOT EXISTS completion_destination ON completion_bindings(client,destination_id)")
            event_columns = {row[1] for row in cursor.execute("PRAGMA table_info(completion_events)").fetchall()}
            if "delivery_status" not in event_columns:
                cursor.execute("ALTER TABLE completion_events ADD COLUMN delivery_status TEXT")
            cursor.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('owner_id',?)", (uuid.uuid4().hex,))
            self.owner_id = cursor.execute("SELECT value FROM metadata WHERE key='owner_id'").fetchone()[0]

    @contextmanager
    def transaction(self):
        with self._lock:
            cursor = self.connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _decode(row):
        if row is None:
            raise HarnessError("JOB_NOT_FOUND", "未找到指定任务。")
        value = dict(row)
        for original, target in (("request_json", "request"), ("execution_json", "execution"), ("error_json", "error"), ("snapshot_json", "snapshot")):
            raw = value.pop(original)
            value[target] = json.loads(raw) if raw else None
        value["termination_failed"] = bool(value["termination_failed"])
        return value

    def get(self, job_id: str) -> dict:
        if not isinstance(job_id,str) or len(job_id) > 128:
            raise HarnessError("JOB_NOT_FOUND", "未找到指定任务。")
        with self._lock:
            return self._decode(self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def submit(self, request: dict, fingerprint: str, execution: dict, max_queue: int, *, completion_origin: dict | None = None) -> tuple[dict, bool]:
        completion_origin = self._completion_origin(completion_origin) if completion_origin is not None else None
        now = time.time()
        with self.transaction() as cursor:
            existing = cursor.execute("SELECT * FROM jobs WHERE client_request_id=?", (request["client_request_id"],)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一请求编号已经绑定了不同的任务参数。")
                if completion_origin is not None:
                    self._bind_completion(cursor, existing["job_id"], completion_origin)
                return self._decode(existing), False
            if cursor.execute("SELECT 1 FROM jobs WHERE termination_failed=1 AND status='cancelling'").fetchone():
                raise HarnessError("EXECUTION_BLOCKED", "旧执行器尚未确认停止，当前不接受新任务。")
            active = cursor.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('preparing','running','cancelling')").fetchone()[0]
            waiting = cursor.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if not active and waiting:
                oldest = cursor.execute("SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()[0]
                cursor.execute("UPDATE jobs SET status='preparing',phase='preparing',started_at=?,last_activity_at=? WHERE job_id=?", (now,now,oldest))
                active = 1
                waiting -= 1
            # 为第一个已接受任务保留执行位置，其他最多五个等待；无需等待调度 tick。
            if active and waiting >= max_queue:
                raise HarnessError("QUEUE_FULL", "本实例等待队列已满。")
            status = "queued" if active else "preparing"
            job_id = uuid.uuid4().hex
            cursor.execute("""INSERT INTO jobs(job_id,client_request_id,fingerprint,request_json,execution_json,
                status,phase,created_at,started_at,last_activity_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (job_id, request["client_request_id"], fingerprint, json.dumps(request,ensure_ascii=False),
                 json.dumps(execution,ensure_ascii=False), status, status, now, now if status=="preparing" else None, now))
            if completion_origin is not None:
                self._bind_completion(cursor, job_id, completion_origin)
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def next_job(self) -> dict | None:
        with self.transaction() as cursor:
            if cursor.execute("SELECT 1 FROM jobs WHERE termination_failed=1 AND status='cancelling'").fetchone():
                return None
            active = cursor.execute("SELECT * FROM jobs WHERE status IN ('preparing','running','cancelling') ORDER BY created_at LIMIT 1").fetchone()
            if active:
                return self._decode(active)
            row = cursor.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            now = time.time()
            cursor.execute("UPDATE jobs SET status='preparing',phase='preparing',started_at=?,last_activity_at=? WHERE job_id=? AND status='queued'", (now, now, row["job_id"]))
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())

    def submit_parallel(self, request: dict, fingerprint: str, execution: dict, max_queue: int, max_parallel_jobs: int, *, completion_origin: dict | None = None) -> tuple[dict, bool]:
        """在同一事务中保留并发槽；幂等和停止失败门仍适用于整个实例。"""
        if type(max_parallel_jobs) is not int or max_parallel_jobs < 1:
            raise ValueError("positive parallel job count required")
        completion_origin = self._completion_origin(completion_origin) if completion_origin is not None else None
        now = time.time()
        with self.transaction() as cursor:
            existing = cursor.execute("SELECT * FROM jobs WHERE client_request_id=?", (request["client_request_id"],)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一请求编号已经绑定了不同的任务参数。")
                if completion_origin is not None:
                    self._bind_completion(cursor, existing["job_id"], completion_origin)
                return self._decode(existing), False
            if cursor.execute("SELECT 1 FROM jobs WHERE termination_failed=1 AND status='cancelling'").fetchone():
                raise HarnessError("EXECUTION_BLOCKED", "旧执行器尚未确认停止，当前不接受新任务。")
            active = cursor.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('preparing','running','cancelling')").fetchone()[0]
            waiting = cursor.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            # 已排队的任务优先获得空闲槽，后提交的任务不能跳过 FIFO。
            while active < max_parallel_jobs and waiting:
                oldest = cursor.execute("SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()[0]
                cursor.execute("UPDATE jobs SET status='preparing',phase='preparing',started_at=?,last_activity_at=? WHERE job_id=?", (now, now, oldest))
                active += 1
                waiting -= 1
            if active >= max_parallel_jobs and waiting >= max_queue:
                raise HarnessError("QUEUE_FULL", "本实例等待队列已满。")
            status = "preparing" if active < max_parallel_jobs else "queued"
            job_id = uuid.uuid4().hex
            cursor.execute("""INSERT INTO jobs(job_id,client_request_id,fingerprint,request_json,execution_json,
                status,phase,created_at,started_at,last_activity_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (job_id, request["client_request_id"], fingerprint, json.dumps(request,ensure_ascii=False),
                 json.dumps(execution,ensure_ascii=False), status, status, now, now if status == "preparing" else None, now))
            if completion_origin is not None:
                self._bind_completion(cursor, job_id, completion_origin)
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()), True

    def next_parallel_job(self, active_ids: set[str], max_parallel_jobs: int) -> dict | None:
        """仅父调度器调用；返回尚未有执行上下文的保留槽或最早排队任务。"""
        if len(active_ids) >= max_parallel_jobs:
            return None
        with self.transaction() as cursor:
            if cursor.execute("SELECT 1 FROM jobs WHERE termination_failed=1 AND status='cancelling'").fetchone():
                return None
            rows = cursor.execute("SELECT * FROM jobs WHERE status IN ('preparing','running','cancelling') ORDER BY created_at,rowid").fetchall()
            for row in rows:
                if row["job_id"] not in active_ids:
                    return self._decode(row)
            if len(rows) >= max_parallel_jobs:
                return None
            row = cursor.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            now = time.time()
            cursor.execute("UPDATE jobs SET status='preparing',phase='preparing',started_at=?,last_activity_at=? WHERE job_id=? AND status='queued'", (now, now, row["job_id"]))
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())

    def has_unconfirmed_execution(self) -> bool:
        with self._lock:
            return bool(self.connection.execute("SELECT 1 FROM jobs WHERE termination_failed=1 AND status='cancelling'").fetchone())

    def activity(self, job_id: str, phase: str) -> None:
        phase = phase[:80]
        with self.transaction() as cursor:
            cursor.execute("UPDATE jobs SET phase=?,last_activity_at=? WHERE job_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')", (phase,time.time(),job_id))

    def set_running(self, job_id: str, snapshot: dict) -> bool:
        with self.transaction() as cursor:
            cursor.execute("UPDATE jobs SET status='running',phase='running',snapshot_json=?,last_activity_at=? WHERE job_id=? AND status='preparing'", (json.dumps(snapshot,ensure_ascii=False),time.time(),job_id))
            return cursor.rowcount == 1

    def request_cancel(self, job_id: str, reason: str = "cancelled") -> dict:
        if reason not in {"cancelled", "timed_out", "interrupted"}:
            raise ValueError("invalid stop reason")
        with self.transaction() as cursor:
            row = cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            current = self._decode(row)
            if current["status"] in TERMINAL or current["status"] == "cancelling":
                return current
            now = time.time()
            if current["status"] == "queued":
                cursor.execute("UPDATE jobs SET status=?,phase=?,cancel_reason=?,finished_at=?,last_activity_at=? WHERE job_id=?", (reason,reason,reason,now,now,job_id))
                self._completion_event(cursor, job_id)
            else:
                cursor.execute("UPDATE jobs SET status='cancelling',phase='stopping',cancel_reason=?,last_activity_at=? WHERE job_id=?", (reason,now,job_id))
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def finish(self, job_id: str, status: str, *, error=None, report_id=None) -> dict:
        if status not in TERMINAL:
            raise ValueError("terminal status required")
        with self.transaction() as cursor:
            row = cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            current = self._decode(row)
            if current["status"] in TERMINAL:
                self._completion_event(cursor, job_id)
                return current
            # 已接受停止请求优先于稍后到达的正常完成，不能将 cancelling 覆盖成成功。
            resolved = current["cancel_reason"] if current["status"] == "cancelling" else status
            now = time.time()
            cursor.execute("UPDATE jobs SET status=?,phase=?,finished_at=?,last_activity_at=?,error_json=?,report_id=?,termination_failed=0 WHERE job_id=?", (resolved,resolved,now,now,json.dumps(error,ensure_ascii=False) if error else None,report_id,job_id))
            self._completion_event(cursor, job_id)
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def termination_failed(self, job_id: str, *, error=None) -> None:
        with self.transaction() as cursor:
            retained = {"code":"TERMINATION_FAILED","message":"执行器停止尚未确认，后续执行已暂停。"}
            row = cursor.execute("SELECT error_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            try:
                previous = json.loads(row["error_json"]) if row is not None and row["error_json"] else None
            except (TypeError, ValueError):
                previous = None
            # 重复停止或失败恢复不能覆盖最早的有效诊断；旧无诊断调用保持固定文案。
            for candidate in (previous, error):
                if type(candidate) is dict and type(candidate.get("diagnostic_id")) is str and re.fullmatch(r"[0-9a-fA-F]{32}", candidate["diagnostic_id"]):
                    restored = error_from_payload(candidate)
                    if restored.diagnostic_id == candidate["diagnostic_id"].lower():
                        retained = error_payload(restored)
                        break
            cursor.execute("UPDATE jobs SET status='cancelling',phase='termination_failed',termination_failed=1,cancel_reason=COALESCE(cancel_reason,'interrupted'),error_json=?,last_activity_at=? WHERE job_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')", (json.dumps(retained,ensure_ascii=False),time.time(),job_id))

    def recover_interrupted(self, stopped: bool) -> None:
        with self.transaction() as cursor:
            now = time.time()
            if stopped:
                interrupted = cursor.execute("SELECT job_id,error_json FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')").fetchall()
                for row in interrupted:
                    retained = {"code":"SERVICE_RESTARTED","message":"服务重启，旧任务未自动重放。"}
                    try:
                        previous = json.loads(row["error_json"]) if row["error_json"] else None
                    except (TypeError, ValueError):
                        previous = None
                    if type(previous) is dict and type(previous.get("diagnostic_id")) is str and re.fullmatch(r"[0-9a-fA-F]{32}", previous["diagnostic_id"]):
                        restored = error_from_payload(previous)
                        if restored.diagnostic_id == previous["diagnostic_id"].lower():
                            retained = error_payload(restored)
                    cursor.execute("UPDATE jobs SET status='interrupted',phase='interrupted',finished_at=?,last_activity_at=?,termination_failed=0,error_json=? WHERE job_id=?", (now,now,json.dumps(retained,ensure_ascii=False),row["job_id"]))
                    self._completion_event(cursor, row["job_id"])
            else:
                cursor.execute("UPDATE jobs SET status='cancelling',phase='termination_failed',termination_failed=1,cancel_reason='interrupted' WHERE status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')")

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self.connection.execute("SELECT 1 FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')").fetchone())

    def pending_backends(self) -> set[str]:
        """历史任务按冻结的执行器核对归属，切换配置不能抹掉旧容器执行记录。"""
        with self._lock:
            rows = self.connection.execute("SELECT execution_json FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')").fetchall()
            return {json.loads(row[0]).get("executor", {}).get("backend", "docker") for row in rows}

    @staticmethod
    def normalized_origin(origin: dict) -> dict:
        """这里只校验受信宿主的路由形状；不能从模型参数构造或授权目标。"""
        if isinstance(origin, dict) and origin.get("client") == "claude_code":
            if set(origin) != {"client", "connection_id", "session_id"}:
                raise HarnessError("COMPLETION_ORIGIN_INVALID", "Claude 完成通知缺少可信连接信息。")
            for field in ("connection_id", "session_id"):
                value = origin[field]
                try:
                    valid = isinstance(value, str) and str(uuid.UUID(value)) == value
                except (ValueError, TypeError, AttributeError):
                    valid = False
                if not valid:
                    raise HarnessError("COMPLETION_ORIGIN_INVALID", "Claude 完成通知的连接或会话编号无效。")
            return {key: origin[key] for key in ("client", "connection_id", "session_id")}
        if not isinstance(origin, dict) or set(origin) != {"thread_id", "turn_id", "host_id", "pipe_path"}:
            raise HarnessError("COMPLETION_ORIGIN_INVALID", "完成通知来源缺少可信的本机对话信息。")
        thread_id, turn_id = origin["thread_id"], origin["turn_id"]
        try:
            valid_thread = isinstance(thread_id, str) and str(uuid.UUID(thread_id)) == thread_id
        except (ValueError, TypeError, AttributeError):
            valid_thread = False
        if not valid_thread or not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", turn_id):
            raise HarnessError("COMPLETION_ORIGIN_INVALID", "完成通知来源的对话或轮次编号无效。")
        if origin["host_id"] != "local":
            raise HarnessError("COMPLETION_ORIGIN_INVALID", "完成通知只支持当前本机 Codex 对话。")
        pipe_path = origin["pipe_path"]
        prefix = "\\\\.\\pipe\\"
        if not isinstance(pipe_path, str) or not pipe_path.casefold().startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", pipe_path[len(prefix):]):
            raise HarnessError("COMPLETION_ORIGIN_INVALID", "完成通知来源的本机通信句柄无效。")
        return {key: origin[key] for key in ("thread_id", "turn_id", "host_id", "pipe_path")}

    @staticmethod
    def _completion_origin(origin: dict) -> dict:
        """保留旧私有方法调用契约，所有客户端统一经过规范化校验。"""
        return JobStore.normalized_origin(origin)

    @staticmethod
    def get_origin_identity(origin: dict) -> tuple[str, str]:
        origin = JobStore.normalized_origin(origin)
        if origin.get("client") == "claude_code":
            return origin["session_id"], "claude_code"
        return origin["thread_id"], origin["host_id"]

    @staticmethod
    def _binding_origin(row) -> dict:
        try:
            if row["origin_json"] is not None:
                return JobStore.normalized_origin(json.loads(row["origin_json"]))
            # 原始 Codex 行的迁移恢复后仍返回原四字段，不伪造 Claude 管道。
            return JobStore.normalized_origin({key: row[key] for key in ("thread_id", "turn_id", "host_id", "pipe_path")})
        except (ValueError, TypeError, KeyError):
            raise HarnessError("COMPLETION_ORIGIN_INVALID", "保存的完成通知来源无效。") from None

    @staticmethod
    def _completion_now(now=None) -> float:
        value = time.time() if now is None else now
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知的时间参数无效。")
        return float(value)

    @staticmethod
    def _completion_id(event_id) -> int:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or not 1 <= event_id <= 2**63 - 1:
            raise HarnessError("COMPLETION_NOT_FOUND", "未找到指定完成事件。")
        return event_id

    @staticmethod
    def _completion_limit(limit) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HarnessError("INVALID_PAGE_SIZE", "完成事件列表数量须在1到100之间。")
        return limit

    def _completion_notify(self) -> None:
        callback = self.completion_wakeup
        if callable(callback):
            try:
                callback()
            except Exception:
                # 通知桥失效不能回滚已经被接纳的模型任务或终态。
                pass

    def _completion_event(self, cursor, job_id: str) -> None:
        # 事件与终态共用事务；没有明确授权绑定的历史任务不会被批量唤醒。
        cursor.execute("""INSERT OR IGNORE INTO completion_events(job_id,created_at)
            SELECT j.job_id,j.finished_at FROM jobs j JOIN completion_bindings b ON b.job_id=j.job_id
            WHERE j.job_id=? AND j.status IN ('succeeded','failed','cancelled','timed_out','interrupted')""", (job_id,))
        if cursor.rowcount:
            self._completion_notify()

    def _bind_completion(self, cursor, job_id: str, origin: dict) -> dict:
        origin = self.normalized_origin(origin)
        destination_id, namespace = self.get_origin_identity(origin)
        client = "claude_code" if namespace == "claude_code" else "codex"
        if cursor.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is None:
            raise HarnessError("JOB_NOT_FOUND", "未找到指定任务。")
        existing = cursor.execute("SELECT * FROM completion_bindings WHERE job_id=?", (job_id,)).fetchone()
        if existing is None:
            cursor.execute("""INSERT INTO completion_bindings(job_id,thread_id,turn_id,host_id,pipe_path,bound_at,
                authorization,client,destination_id,origin_json) VALUES(?,?,?,?,?,?,1,?,?,?)""",
                (job_id, destination_id, origin.get("turn_id", ""), namespace, origin.get("pipe_path", ""), time.time(),
                 client, destination_id, json.dumps(origin, ensure_ascii=False)))
        else:
            if existing["client"] != client or existing["destination_id"] != destination_id:
                raise HarnessError("COMPLETION_ORIGIN_CONFLICT", "该任务已经绑定另一主对话，不能改变完成通知目标。")
            previous = self._binding_origin(existing)
            if client == "codex":
                # 桌面重启可刷新本机管道，但保留首次提交轮次和主对话。
                origin = {**previous, "pipe_path": origin["pipe_path"]}
            else:
                # Claude 使用真实认证连接路由；固定私有 frontend 会话不随重连改变。
                origin = {**previous, "connection_id": origin["connection_id"]}
            cursor.execute("UPDATE completion_bindings SET pipe_path=?,origin_json=? WHERE job_id=?",
                           (origin.get("pipe_path", ""), json.dumps(origin, ensure_ascii=False), job_id))
            if previous != origin and cursor.execute("SELECT 1 FROM completion_events WHERE job_id=? AND state='pending'", (job_id,)).fetchone():
                self._completion_notify()
        self._completion_event(cursor, job_id)
        return origin

    def bind_completion(self, job_id: str, origin: dict, authorization: bool = True) -> dict:
        """将明确授权的作业绑定到宿主提供的主对话；不启动或重放模型任务。"""
        if authorization is not True:
            raise HarnessError("COMPLETION_AUTHORIZATION_REQUIRED", "完成后自动继续需要明确授权。")
        origin = self._completion_origin(origin)
        with self.transaction() as cursor:
            origin = self._bind_completion(cursor, job_id, origin)
            event = self._completion_row(cursor, job_id=job_id, required=False)
            return {"job_id": job_id, "origin": origin, "completion": self._completion_decode(event) if event else None}

    @staticmethod
    def _completion_row(cursor, *, event_id=None, job_id=None, required=True):
        row = cursor.execute("""SELECT e.*,b.thread_id,b.turn_id,b.host_id,b.pipe_path,b.client,b.destination_id,b.origin_json,
            j.request_json,j.status,j.report_id,j.finished_at
            FROM completion_events e JOIN completion_bindings b ON b.job_id=e.job_id
            JOIN jobs j ON j.job_id=e.job_id WHERE (? IS NULL OR e.event_id=?) AND (? IS NULL OR e.job_id=?)""",
            (event_id, event_id, job_id, job_id)).fetchone()
        if row is None and required:
            raise HarnessError("COMPLETION_NOT_FOUND", "未找到指定完成事件。")
        return row

    @staticmethod
    def _completion_decode(row) -> dict:
        value = dict(row)
        request = json.loads(value.pop("request_json"))
        value["origin"] = JobStore._binding_origin(row)
        for key in ("thread_id", "turn_id", "host_id", "pipe_path", "client", "destination_id", "origin_json"):
            value.pop(key)
        value.update(project_id=request["project_id"], session_id=request.get("session_id"), mode=request.get("mode", "review"))
        return value

    def completion_get(self, job_id: str) -> dict | None:
        with self._lock:
            self.get(job_id)
            row = self._completion_row(self.connection, job_id=job_id, required=False)
            return self._completion_decode(row) if row else None

    @staticmethod
    def _completion_expire(cursor, now: float) -> None:
        # 发出请求后崩溃无法确认是否入队；租约到期不能直接重试制造重复续跑。
        cursor.execute("""UPDATE completion_events SET state='uncertain',uncertain_at=?,last_error_code='DELIVERY_OUTCOME_UNKNOWN'
            WHERE state='claimed' AND lease_until<=?""", (now, now))

    def completion_pending(self, limit: int = 20, *, now=None) -> list[dict]:
        limit, now = self._completion_limit(limit), self._completion_now(now)
        with self.transaction() as cursor:
            self._completion_expire(cursor, now)
            ids = cursor.execute("SELECT event_id FROM completion_events WHERE state='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,event_id LIMIT ?", (now, limit)).fetchall()
            return [self._completion_decode(self._completion_row(cursor, event_id=row[0])) for row in ids]

    def completion_claim(self, event_id: int, *, lease_seconds: float = 60, now=None) -> dict | None:
        event_id, now = self._completion_id(event_id), self._completion_now(now)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知投递租约无效。")
        with self.transaction() as cursor:
            self._completion_expire(cursor, now)
            row = self._completion_row(cursor, event_id=event_id)
            if row["state"] != "pending" or row["next_attempt_at"] > now:
                return None
            cursor.execute("""UPDATE completion_events SET state='claimed',lease_token=?,lease_until=?,attempts=attempts+1,
                last_error_code=NULL WHERE event_id=?""", (uuid.uuid4().hex, now + lease_seconds, event_id))
            return self._completion_decode(self._completion_row(cursor, event_id=event_id))

    @staticmethod
    def _completion_owned(row, lease_token) -> None:
        if not isinstance(lease_token, str) or not re.fullmatch(r"[0-9a-f]{32}", lease_token) or row["lease_token"] != lease_token:
            raise HarnessError("COMPLETION_LEASE_CONFLICT", "完成通知投递归属已经变化。")

    def completion_delivered(self, event_id: int, lease_token: str, *, now=None, delivery_status: str = "accepted") -> dict:
        event_id, now = self._completion_id(event_id), self._completion_now(now)
        if not isinstance(delivery_status, str) or delivery_status not in {"accepted", "channel_written"}:
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知投递回执无效。")
        with self.transaction() as cursor:
            row = self._completion_row(cursor, event_id=event_id)
            self._completion_owned(row, lease_token)
            if row["state"] not in {"claimed", "uncertain", "delivered", "acknowledged"}:
                raise HarnessError("COMPLETION_LEASE_CONFLICT", "该完成通知当前没有有效投递归属。")
            if row["state"] in {"claimed", "uncertain"}:
                cursor.execute("UPDATE completion_events SET state='delivered',delivered_at=?,lease_until=NULL,last_error_code=NULL,delivery_status=? WHERE event_id=?", (now, delivery_status, event_id))
            return self._completion_decode(self._completion_row(cursor, event_id=event_id))

    def completion_failed(self, event_id: int, lease_token: str, error_code: str, *, retry_after_seconds: float = 5, uncertain: bool = False, now=None) -> dict:
        """只有确认没有发送成功时可重试；无法确认宿主是否接收必须标记 uncertain。"""
        event_id, now = self._completion_id(event_id), self._completion_now(now)
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", error_code) or type(uncertain) is not bool:
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知投递错误参数无效。")
        if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, (int, float)) or not math.isfinite(retry_after_seconds) or not 0 <= retry_after_seconds <= 3600:
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知重试间隔无效。")
        with self.transaction() as cursor:
            row = self._completion_row(cursor, event_id=event_id)
            self._completion_owned(row, lease_token)
            if row["state"] == "acknowledged":
                return self._completion_decode(row)
            if row["state"] not in {"claimed", "uncertain"}:
                raise HarnessError("COMPLETION_LEASE_CONFLICT", "该完成通知当前没有有效投递归属。")
            if uncertain:
                cursor.execute("UPDATE completion_events SET state='uncertain',uncertain_at=?,lease_until=NULL,last_error_code=? WHERE event_id=?", (now, error_code, event_id))
            else:
                # 明确失败才释放归属，旧回执不能覆盖下一次投递。
                cursor.execute("UPDATE completion_events SET state='pending',next_attempt_at=?,lease_token=NULL,lease_until=NULL,last_error_code=? WHERE event_id=?", (now + retry_after_seconds, error_code, event_id))
            return self._completion_decode(self._completion_row(cursor, event_id=event_id))

    def completion_ack(self, event_id: int, thread_id: str, *, host_id: str = "local", now=None) -> dict:
        """由绑定主对话确认完整处理；读取报告或通知送出不等于确认。"""
        event_id, now = self._completion_id(event_id), self._completion_now(now)
        with self.transaction() as cursor:
            row = self._completion_row(cursor, event_id=event_id)
            if self.get_origin_identity(self._binding_origin(row)) != (thread_id, host_id):
                raise HarnessError("COMPLETION_TARGET_MISMATCH", "该完成事件属于另一主对话。")
            if row["state"] != "acknowledged":
                cursor.execute("UPDATE completion_events SET state='acknowledged',acknowledged_at=?,lease_until=NULL WHERE event_id=?", (now, event_id))
            return self._completion_decode(self._completion_row(cursor, event_id=event_id))

    def completion_list(self, thread_id: str, *, state: str | None = None, limit: int = 20, host_id: str = "local") -> list[dict]:
        limit = self._completion_limit(limit)
        if state is not None and (not isinstance(state, str) or state not in {"pending", "claimed", "delivered", "uncertain", "acknowledged"}):
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知投递状态无效。")
        if not isinstance(thread_id, str) or not isinstance(host_id, str):
            raise HarnessError("COMPLETION_ARGUMENT_INVALID", "完成通知目标无效。")
        client = {"local": "codex", "claude_code": "claude_code"}.get(host_id)
        if client is None:
            return []
        with self._lock:
            ids = self.connection.execute("""SELECT e.event_id FROM completion_events e
                JOIN completion_bindings b ON b.job_id=e.job_id
                WHERE b.destination_id=? AND b.client=? AND (? IS NULL OR e.state=?) ORDER BY e.event_id DESC LIMIT ?""",
                (thread_id, client, state, state, limit)).fetchall()
            return [self._completion_decode(self._completion_row(self.connection, event_id=row[0])) for row in ids]

    def close(self) -> None:
        with self._lock:
            self.connection.close()

