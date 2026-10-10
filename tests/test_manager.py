"""确定性假执行器仅用于验证调度契约；不作为真实模型/容器验收。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from harness_mcp.config import AppConfig, LimitsConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.job_store import JobStore, TERMINAL
from harness_mcp.task_manager import TaskManager


class FixtureRunner:
    def __init__(self, *, delay=0, fail=False, stopped=True):
        self.delay, self.fail, self.stopped = delay, fail, stopped
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.started = asyncio.Event()
        self.closed = False

    async def recover(self):
        return self.stopped

    async def run(self, request, on_activity):
        self.calls.append(request)
        self.active += 1
        self.maximum = max(self.maximum,self.active)
        self.started.set()
        on_activity("reviewing")
        try:
            await asyncio.sleep(self.delay)
            return {"raw_text":json.dumps({"summary":"fixture summary","findings":[]}),"finish_reason":"completed","usage":None,"error":{"code":"MODEL_ERROR","message":"fixture failure"} if self.fail else None,"execution_stopped":True}
        finally:
            self.active -= 1

    async def terminate(self, job_id):
        return self.stopped

    async def close(self):
        self.closed = True
        return self.stopped


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "中文 project"
    project.mkdir()
    (project / "sample.py").write_text("def divide(a, b):\n    return a / b\n",encoding="utf-8")
    subprocess.run(["git","init","-q",str(project)],check=True)
    subprocess.run(["git","-C",str(project),"add","sample.py"],check=True)
    return AppConfig(tmp_path / "state", {"demo":ProjectConfig("demo",project)}, LimitsConfig(default_timeout_seconds=10,max_timeout_seconds=30), ModelConfig(credential_source="env",credential_env="FIXTURE_MODEL_KEY"))


async def terminal(manager, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.status(job_id)
        if state["status"] in TERMINAL:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(manager.status(job_id))


def test_success_idempotency_report_and_frozen_reference(config):
    async def check():
        runner = FixtureRunner()
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            first = await manager.submit("demo","read only","same")
            second = await manager.submit("demo","read only","same")
            assert first["job_id"] == second["job_id"] and second["idempotent_replay"]
            assert (await terminal(manager,first["job_id"]))["status"] == "succeeded"
            report = manager.result(first["job_id"])
            assert report["ready"] and report["tests"] == "not_run" and report["changed_files"] == []
            assert report["usage"] is None and report["summary"] == "fixture summary"
            assert len(runner.calls) == 1 and runner.calls[0]["model"]["credential_source"] == "env"
            with pytest.raises(HarnessError,match="同一请求编号"):
                await manager.submit("demo","changed","same")
        finally:
            await manager.close()
    asyncio.run(check())


def test_one_active_five_waiting_queue_and_queued_cancel(config):
    async def check():
        runner = FixtureRunner(delay=0.08)
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            jobs = [await manager.submit("demo","read",str(i)) for i in range(6)]
            with pytest.raises(HarnessError) as error:
                await manager.submit("demo","read","overflow")
            assert error.value.code == "QUEUE_FULL"
            assert (await manager.cancel(jobs[-1]["job_id"]))["status"] == "cancelled"
            for job in jobs[:-1]:
                assert (await terminal(manager,job["job_id"],10))["status"] == "succeeded"
            assert runner.maximum == 1 and len(runner.calls) == 5
            assert [r["client_request_id"] for r in runner.calls] == [str(i) for i in range(5)]
        finally:
            await manager.close()
    asyncio.run(check())


@pytest.mark.parametrize("reason",["cancelled","timed_out"])
def test_running_stop_and_timeout(config,reason):
    # 此用例验证运行阶段；快照单独准备，避免 Windows 解释器启动时间吃掉 1 秒期限。
    from harness_mcp.workspace import create_snapshot
    snapshot = create_snapshot(config.projects["demo"], None, config.data_dir / "prepared-running", max_file_bytes=config.limits.max_file_bytes, max_total_bytes=config.limits.max_snapshot_bytes)
    async def check():
        runner = FixtureRunner(delay=100)
        manager = TaskManager(config,runner)
        async def prepared(*_args):
            return snapshot
        manager._snapshot = prepared
        await manager.start()
        try:
            job = await manager.submit("demo","read","stop",timeout_seconds=1 if reason=="timed_out" else 10)
            await asyncio.wait_for(runner.started.wait(),3)
            if reason == "cancelled":
                reply = await manager.cancel(job["job_id"])
                assert reply["status"] == "cancelling"
            assert (await terminal(manager,job["job_id"]))["status"] == reason
            assert runner.active == 0
            assert (await manager.cancel(job["job_id"]))["status"] == reason
            assert manager.result(job["job_id"])["ready"]
        finally:
            await manager.close()
    asyncio.run(check())


def test_stop_failure_blocks_then_retry_confirms(config):
    async def check():
        runner = FixtureRunner(delay=100)
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            job = await manager.submit("demo","read","stop-failure")
            await asyncio.wait_for(runner.started.wait(),3)
            runner.stopped = False
            await manager.cancel(job["job_id"])
            await asyncio.sleep(0.15)
            status = manager.status(job["job_id"])
            assert status["status"] == "cancelling" and status["termination_failed"]
            with pytest.raises(HarnessError) as error:
                await manager.submit("demo","read","blocked")
            assert error.value.code == "EXECUTION_BLOCKED"
            runner.stopped = True
            await manager.cancel(job["job_id"])
            assert (await terminal(manager,job["job_id"]))["status"] == "cancelled"
        finally:
            runner.stopped = True
            await manager.close()
    asyncio.run(check())


def test_completed_task_cannot_skip_elapsed_deadline(config):
    async def check():
        runner = FixtureRunner()
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            job = await manager.submit("demo","read","deadline")
            # 已完成 awaitable 专门复现 done() 跳过 while 的期限边界。
            task = asyncio.create_task(asyncio.sleep(0,result="done"))
            await task
            value, stopped = await manager._wait_operation(task,job["job_id"],time.time()-1)
            assert stopped and value == "done"
            assert manager.store.get(job["job_id"])["cancel_reason"] == "timed_out"
        finally:
            await manager.close()
    asyncio.run(check())


def test_restart_interrupts_pending_without_replay_and_preserves_owner(config):
    store = JobStore(config.data_dir)
    request = {"project_id":"demo","prompt":"read","client_request_id":"old","relative_paths":None,"timeout_seconds":10}
    job,_ = store.submit(request,"fixed",{},5)
    owner = store.owner_id
    store.close()
    async def check():
        runner = FixtureRunner()
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            assert manager.store.owner_id == owner
            assert manager.status(job["job_id"])["status"] == "interrupted"
            await asyncio.sleep(0.1)
            assert runner.calls == []
        finally:
            await manager.close()
    asyncio.run(check())


def test_recover_failure_and_explicit_cancel_retry(config):
    store = JobStore(config.data_dir)
    job,_ = store.submit({"client_request_id":"old"},"fixed",{},5)
    store.close()
    async def check():
        runner = FixtureRunner(stopped=False)
        manager = TaskManager(config,runner)
        await manager.start()
        try:
            assert manager.status(job["job_id"])["termination_failed"]
            runner.stopped = True
            assert (await manager.cancel(job["job_id"]))["status"] == "interrupted"
            assert runner.calls == []
        finally:
            await manager.close()
    asyncio.run(check())


def test_empty_database_does_not_override_failed_backend_recovery(config):
    async def check():
        runner = FixtureRunner(stopped=False)
        manager = TaskManager(config, runner)
        try:
            assert not manager.store.has_pending()
            await manager.start()
            assert not manager.accepting and not manager.recovery_confirmed
            with pytest.raises(HarnessError) as error:
                await manager.submit("demo", "blocked", "empty-unconfirmed-backend")
            assert error.value.code == "SERVICE_STOPPING"
            assert not runner.calls
        finally:
            runner.stopped = True
            await manager.close()
            assert manager.closed
    asyncio.run(check())


def test_instance_lock_and_clean_close(config):
    async def check():
        manager = TaskManager(config,FixtureRunner())
        with pytest.raises(HarnessError) as error:
            TaskManager(config,FixtureRunner())
        assert error.value.code == "INSTANCE_LOCKED"
        await manager.start()
        await manager.close()
        await manager.close()
        replacement = TaskManager(config,FixtureRunner())
        await replacement.close()
    asyncio.run(check())


def test_secret_redaction_and_result_digest(config,monkeypatch):
    secret = "fixture-credential-must-not-leak"
    monkeypatch.setenv("FIXTURE_MODEL_KEY",secret)
    class SecretRunner(FixtureRunner):
        async def run(self,request,on_activity):
            return {"raw_text":f"raw {secret}","finish_reason":"completed","error":None,"execution_stopped":True,"usage":None}
    async def check():
        manager = TaskManager(config,SecretRunner())
        await manager.start()
        try:
            job = await manager.submit("demo","read","secret")
            await terminal(manager,job["job_id"])
            assert secret not in json.dumps(manager.result(job["job_id"]))
            directory = config.data_dir / "jobs" / job["job_id"]
            assert all(secret not in p.read_text(encoding="utf-8") for p in directory.glob("report.*"))
            path = directory / "report.json"
            path.write_bytes(path.read_bytes().replace(b"raw ",b"bad "))
            with pytest.raises(Exception) as error:
                manager.result(job["job_id"])
            assert error.value.code == "REPORT_TAMPERED"
        finally:
            await manager.close()
    asyncio.run(check())


def test_preparation_cancel_stops_owned_process_tree(config):
    from harness_mcp.process_control import spawn_owned, stop_process_tree
    class SlowPreparation(TaskManager):
        async def _snapshot(self,job,directory):
            self.preparation_process = spawn_owned([sys.executable,"-c","import time; time.sleep(100)"],{})
            self.owned = self.preparation_process
            self.prepared.set()
            try:
                await asyncio.to_thread(self.owned.communicate,b"")
            finally:
                if await stop_process_tree(self.owned):
                    self.preparation_process = None
    async def check():
        runner = FixtureRunner()
        manager = SlowPreparation(config,runner)
        manager.prepared = asyncio.Event()
        await manager.start()
        try:
            job = await manager.submit("demo","read","prepare-stop")
            await asyncio.wait_for(manager.prepared.wait(),3)
            await manager.cancel(job["job_id"])
            assert (await terminal(manager,job["job_id"]))["status"] == "cancelled"
            assert manager.owned.tree_stopped and manager.owned.returncode is not None
            assert not runner.calls
        finally:
            await manager.close()
    asyncio.run(check())


def test_close_interrupts_active_and_waiting_without_replay(config):
    async def check():
        runner = FixtureRunner(delay=100)
        manager = TaskManager(config,runner)
        await manager.start()
        jobs = [await manager.submit("demo","read",f"close-{i}") for i in range(2)]
        await asyncio.wait_for(runner.started.wait(),3)
        await manager.close()
        assert runner.closed and runner.active == 0
        store = JobStore(config.data_dir)
        try:
            assert all(store.get(job["job_id"])["status"] == "interrupted" for job in jobs)
        finally:
            store.close()
    asyncio.run(check())


def test_close_cannot_clear_unknown_old_executor(config):
    store = JobStore(config.data_dir)
    job,_ = store.submit({"client_request_id":"unconfirmed"},"fixed",{},5)
    store.close()
    class UnknownOldExecutor(FixtureRunner):
        recovered = False
        async def recover(self):
            return self.recovered
        async def close(self):
            return True  # 此次没有 active，仍不能证明旧 daemon 中没有容器。
    async def check():
        manager = TaskManager(config,UnknownOldExecutor())
        await manager.start()
        await manager.close()
        persisted = JobStore(config.data_dir)
        try:
            state = persisted.get(job["job_id"])
            assert state["status"] == "cancelling" and state["termination_failed"]
        finally:
            persisted.close()
            # 停止未知时管理器应保留锁；测试最后提供真实成功判定以显式关闭。
            manager.runner.recovered = True
            await manager.close()
    asyncio.run(check())


def test_host_rejects_completed_empty_worker_output(config):
    class EmptyWorker(FixtureRunner):
        async def run(self,request,on_activity):
            return {"raw_text":"","finish_reason":"completed","error":None,"execution_stopped":True}
    async def check():
        manager = TaskManager(config,EmptyWorker())
        await manager.start()
        try:
            job = await manager.submit("demo","read","empty")
            state = await terminal(manager,job["job_id"])
            assert state["status"] == "failed" and state["error"]["code"] == "EMPTY_OUTPUT"
            assert manager.result(job["job_id"])["ready"]
        finally:
            await manager.close()
    asyncio.run(check())


def test_report_processing_counts_toward_total_timeout(config,monkeypatch):
    import harness_mcp.workspace as workspace
    snapshot = workspace.create_snapshot(config.projects["demo"], None, config.data_dir / "prepared-report", max_file_bytes=config.limits.max_file_bytes, max_total_bytes=config.limits.max_snapshot_bytes)
    original = workspace.verify_snapshot
    calls = 0
    def slow_second_check(snapshot):
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(1.1)
        original(snapshot)
    monkeypatch.setattr(workspace,"verify_snapshot",slow_second_check)
    async def check():
        manager = TaskManager(config,FixtureRunner())
        async def prepared(*_args):
            return snapshot
        manager._snapshot = prepared
        await manager.start()
        try:
            job = await manager.submit("demo","read","slow-report",timeout_seconds=1)
            state = await terminal(manager,job["job_id"])
            assert state["status"] == "timed_out" and state["error"]["code"] == "TIMED_OUT"
            assert manager.result(job["job_id"])["error"]["code"] == "TIMED_OUT"
        finally:
            await manager.close()
    asyncio.run(check())
