"""SQLite 事务统一裁决幂等、队列容量和终态，避免后台竞态重放模型。"""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .errors import HarnessError


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})
ACTIVE = frozenset({"preparing", "running", "cancelling"})
STATES = TERMINAL | ACTIVE | {"queued"}


class JobStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "jobs.sqlite3", isolation_level=None, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
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
        """)
        with self.transaction() as cursor:
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

    def submit(self, request: dict, fingerprint: str, execution: dict, max_queue: int) -> tuple[dict, bool]:
        now = time.time()
        with self.transaction() as cursor:
            existing = cursor.execute("SELECT * FROM jobs WHERE client_request_id=?", (request["client_request_id"],)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一请求编号已经绑定了不同的任务参数。")
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

    def submit_parallel(self, request: dict, fingerprint: str, execution: dict, max_queue: int, max_parallel_jobs: int) -> tuple[dict, bool]:
        """在同一事务中保留并发槽；幂等和停止失败门仍适用于整个实例。"""
        if type(max_parallel_jobs) is not int or max_parallel_jobs < 1:
            raise ValueError("positive parallel job count required")
        now = time.time()
        with self.transaction() as cursor:
            existing = cursor.execute("SELECT * FROM jobs WHERE client_request_id=?", (request["client_request_id"],)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "同一请求编号已经绑定了不同的任务参数。")
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
                return current
            # 已接受停止请求优先于稍后到达的正常完成，不能将 cancelling 覆盖成成功。
            resolved = current["cancel_reason"] if current["status"] == "cancelling" else status
            now = time.time()
            cursor.execute("UPDATE jobs SET status=?,phase=?,finished_at=?,last_activity_at=?,error_json=?,report_id=?,termination_failed=0 WHERE job_id=?", (resolved,resolved,now,now,json.dumps(error,ensure_ascii=False) if error else None,report_id,job_id))
            return self._decode(cursor.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def termination_failed(self, job_id: str) -> None:
        with self.transaction() as cursor:
            cursor.execute("UPDATE jobs SET status='cancelling',phase='termination_failed',termination_failed=1,cancel_reason=COALESCE(cancel_reason,'interrupted'),error_json=?,last_activity_at=? WHERE job_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')", (json.dumps({"code":"TERMINATION_FAILED","message":"执行器停止尚未确认，后续执行已暂停。"},ensure_ascii=False),time.time(),job_id))

    def recover_interrupted(self, stopped: bool) -> None:
        with self.transaction() as cursor:
            now = time.time()
            if stopped:
                cursor.execute("UPDATE jobs SET status='interrupted',phase='interrupted',finished_at=?,last_activity_at=?,termination_failed=0,error_json=? WHERE status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')", (now,now,json.dumps({"code":"SERVICE_RESTARTED","message":"服务重启，旧任务未自动重放。"},ensure_ascii=False)))
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

    def close(self) -> None:
        with self._lock:
            self.connection.close()

