"""单实例内的有界并发；每作业保持独立执行器和停止确认责任。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from .config import frozen_execution, normalize_request, request_fingerprint
from .errors import HarnessError
from .job_store import TERMINAL
from .task_manager import TaskManager, _UNCONFIRMED_MANAGERS


class _ExecutionContext(TaskManager):
    def __init__(self, parent: "ParallelTaskManager", job_id: str):
        # 上下文不获取第二把锁、不建立数据库、不启动第二个调度器。
        # 父管理器完成全实例恢复后才创建它；这里复用经过审计的单作业 _execute。
        self.parent = parent
        self.config = parent.config
        self.store = parent.store
        self.secrets = parent.secrets
        self.injected_runner = parent.injected_runner
        self.runner = parent._job_runner(job_id)
        self.accepting = True
        self.stopping = False
        self.closed = False
        self.scheduler = None
        self.current_job_id = job_id
        self.preparation_process = None
        self.preparation_record = None
        self.preparation_recovery_confirmed = True
        self.recovery_confirmed = True
        self.operations = set()
        self.wakeup = asyncio.Event()
        self.stop_lock = asyncio.Lock()
        self.execution_task: asyncio.Task | None = None
        self.ownership_failed = False

    async def _recover_owned(self):
        raise HarnessError("SCOPED_RECOVERY_NOT_ALLOWED", "单作业上下文不能恢复其他作业的执行器。")

    async def _recover_preparations(self):
        raise HarnessError("SCOPED_RECOVERY_NOT_ALLOWED", "单作业上下文不能扫描全实例准备进程。")

    async def close_owned(self) -> bool:
        async with self.stop_lock:
            if self.closed:
                return True
            # 只确认本作业：不调用 DesktopRunner.close/recover 的全局记录扫描。
            stopped = await self._terminate(self.current_job_id)
            if not stopped:
                return False
            pending = list(self.operations)
            for operation in pending:
                operation.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # _snapshot/_edit_changes 的 finally 可能再次报告归属归档失败。
            if self.preparation_process is not None or self.preparation_record is not None:
                return False
            recover_job = getattr(self.runner, "recover_job", None)
            if callable(recover_job) and await recover_job(self.current_job_id) is not True:
                # run 的 finally 可能已移除内存 active，但 home/归属记录尚未归档。
                # 只能检查本作业，不能用全局 recover 扫到另一个正常运行上下文。
                return False
            # 标记仅用于拒绝复用；实际停止依据是上面的 terminate 与操作收敛。
            # 全实例记录恢复和 runner.close 仍由唯一父管理器承担。
            if hasattr(self.runner, "_closed"):
                self.runner._closed = True
            self.closed = True
            return True

    async def close(self) -> None:
        await self.close_owned()


class ParallelTaskManager(TaskManager):
    def __init__(self, config, runner_factory=None):
        """factory(job_id) 返回独立执行器；job_id=None 是父级恢复协调器。"""
        self.runner_factory = runner_factory
        self.contexts: dict[str, _ExecutionContext] = {}
        coordinator = runner_factory(None) if runner_factory is not None else None
        super().__init__(config, coordinator)

    def _job_runner(self, job_id: str):
        if self.runner_factory is not None:
            return self.runner_factory(job_id)
        return self._make_runner(self.config.executor.backend)

    async def _recover_owned(self):
        # 活跃上下文的停止由该上下文承担，禁止全局恢复碰到其他并发作业。
        if self.contexts:
            return False
        return await super()._recover_owned()

    def _refresh_acceptance(self):
        self.accepting = (
            not self.stopping
            and self.recovery_confirmed
            and self.preparation_recovery_confirmed
            and not self.store.has_unconfirmed_execution()
            and not any(context.ownership_failed for context in self.contexts.values())
        )

    async def submit(self, project_id: str, prompt: str, client_request_id: str, relative_paths=None, timeout_seconds=None, mode: str = "review") -> dict:
        if not self.accepting:
            raise HarnessError("SERVICE_STOPPING", "服务正在关闭或确认旧执行停止，不再接受新任务。")
        request = normalize_request(self.config, project_id, prompt, client_request_id, relative_paths, timeout_seconds, mode)
        job, created = self.store.submit_parallel(request, request_fingerprint(request), frozen_execution(self.config, project_id),
                                                 self.config.limits.max_queue, self.config.limits.max_parallel_jobs)
        self.wakeup.set()
        return {**self._receipt(job), "idempotent_replay": not created}

    async def _finish_context(self, job_id: str, context: _ExecutionContext) -> bool:
        task = context.execution_task
        if task is not None and not task.done():
            return False
        if task is not None and not task.cancelled():
            with suppress(BaseException):
                task.exception()
        try:
            stopped = await context.close_owned()
        except Exception:
            stopped = False
        if not stopped:
            context.ownership_failed = True
            self.store.termination_failed(job_id)
            self.accepting = False
            return False
        context.ownership_failed = False
        if self.store.get(job_id)["status"] not in TERMINAL:
            self.store.finish(job_id, "interrupted")
        if self.contexts.get(job_id) is context:
            self.contexts.pop(job_id)
        self._refresh_acceptance()
        return True

    async def _schedule(self) -> None:
        while not self.stopping:
            for job_id, context in list(self.contexts.items()):
                if context.execution_task is not None and context.execution_task.done():
                    await self._finish_context(job_id, context)
            self._refresh_acceptance()
            if self.accepting:
                while len(self.contexts) < self.config.limits.max_parallel_jobs:
                    job = self.store.next_parallel_job(set(self.contexts), self.config.limits.max_parallel_jobs)
                    if job is None:
                        break
                    job_id = job["job_id"]
                    try:
                        context = _ExecutionContext(self, job_id)
                    except Exception as error:
                        # 构造未产生执行进程；由同一 store 保存稳定错误，不重放任务。
                        from .errors import error_payload
                        from .redaction import redact_value
                        self.store.finish(job_id, "failed", error=redact_value(error_payload(error), self.secrets))
                        continue
                    self.contexts[job_id] = context
                    context.execution_task = asyncio.create_task(context._execute(job), name=f"harness-job-{job_id}")
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    async def cancel(self, job_id: str) -> dict:
        job = self.store.request_cancel(job_id)
        context = self.contexts.get(job_id)
        if context is not None:
            if context.execution_task is not None and context.execution_task.done():
                await self._finish_context(job_id, context)
                job = self.store.get(job_id)
        elif not self.contexts and (job["termination_failed"] or not self.recovery_confirmed or not self.preparation_recovery_confirmed):
            # 只有全实例无活跃作业，才允许重新确认启动前遗留的归属。
            stopped = await self._recover_owned()
            self.recovery_confirmed = stopped
            if stopped:
                self.store.recover_interrupted(True)
                job = self.store.get(job_id)
            self._refresh_acceptance()
        self.wakeup.set()
        return {**self._receipt(job), "termination_failed": job["termination_failed"]}

    async def close(self) -> None:
        if self.closed:
            return
        self.accepting = False
        self.stopping = True
        self.wakeup.set()
        stopped = False
        try:
            if self.scheduler is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await self.scheduler
            for job_id, context in list(self.contexts.items()):
                context.stopping = True
                self.store.request_cancel(job_id, "interrupted")
            tasks = [context.execution_task for context in self.contexts.values() if context.execution_task is not None]
            if tasks:
                completed, pending = await asyncio.wait(tasks, timeout=15)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=15)
                for task in completed:
                    if not task.cancelled():
                        with suppress(BaseException):
                            task.exception()
            for job_id, context in list(self.contexts.items()):
                await self._finish_context(job_id, context)
            if not self.contexts:
                # 直到所有独立上下文停止，父级才能扫描历史和关闭全局执行器。
                recovered = await super()._recover_owned()
                self.recovery_confirmed = recovered
                stopped = await self.runner.close() and recovered
            if stopped:
                pending_operations = list(self.operations)
                for operation in pending_operations:
                    operation.cancel()
                await asyncio.gather(*pending_operations, return_exceptions=True)
        finally:
            if not stopped:
                _UNCONFIRMED_MANAGERS.add(self)
            self.store.recover_interrupted(stopped)
            if stopped:
                self.store.close()
                self.lock.close()
                self.closed = True
                _UNCONFIRMED_MANAGERS.discard(self)
