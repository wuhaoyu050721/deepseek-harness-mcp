"""有界真实调度与人工模型执行器；不发出模型网络请求。"""

import asyncio
import json
import subprocess
import time

import pytest

from harness_mcp.config import AppConfig, LimitsConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.job_store import JobStore, TERMINAL
from harness_mcp.parallel_manager import ParallelTaskManager
from harness_mcp.task_manager import TaskManager


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "人工 project"
    project.mkdir()
    (project / "sample.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "sample.py"], check=True)
    return AppConfig(tmp_path / "data", {"fixture": ProjectConfig("fixture", project)},
                     LimitsConfig(max_queue=2, max_parallel_jobs=2, default_timeout_seconds=15, max_timeout_seconds=30),
                     ModelConfig(credential_source="env", credential_env="PARALLEL_TEST_UNUSED_KEY"))


class _Probe:
    def __init__(self):
        self.runners = {}
        self.active = 0
        self.maximum = 0
        self.blocked_stops = set()
        self.blocked_cleanup = set()
        self.terminated = []

    def factory(self, job_id):
        assert job_id not in self.runners
        value = _Runner(self, job_id)
        self.runners[job_id] = value
        return value


class _Runner:
    def __init__(self, probe, job_id):
        self.probe, self.job_id = probe, job_id
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._closed = False
        self.recover_calls = 0
        self.close_calls = 0
        self.report_stopped = None
        self.scoped_recover_calls = 0

    async def recover(self):
        self.recover_calls += 1
        assert self.job_id is None, "单作业执行器不能扫描其他作业记录"
        return not self.probe.blocked_stops

    async def close(self):
        self.close_calls += 1
        assert self.job_id is None, "单作业执行器不能全局 close"
        self._closed = True
        return not self.probe.blocked_stops

    async def terminate(self, job_id):
        assert self.job_id in (None, job_id), "停止影响了另一个作业"
        self.probe.terminated.append(job_id)
        return job_id not in self.probe.blocked_stops

    async def recover_job(self, job_id):
        assert self.job_id == job_id
        self.scoped_recover_calls += 1
        return job_id not in self.probe.blocked_cleanup

    async def run(self, request, on_activity):
        assert request["job_id"] == self.job_id
        self.probe.active += 1
        self.probe.maximum = max(self.probe.maximum, self.probe.active)
        self.started.set()
        try:
            await self.release.wait()
            return {"raw_text": json.dumps({"summary": "fixture", "findings": []}), "finish_reason": "completed",
                    "usage": None, "execution_stopped": self.job_id not in self.probe.blocked_stops if self.report_stopped is None else self.report_stopped}
        finally:
            self.probe.active -= 1


async def _wait(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("人工任务没有达到预期状态。")


async def _started(probe, *job_ids):
    await _wait(lambda: all(job_id in probe.runners and probe.runners[job_id].started.is_set() for job_id in job_ids))


async def _terminal(manager, job_id):
    await _wait(lambda: manager.status(job_id)["status"] in TERMINAL)
    return manager.status(job_id)


def test_parallel_execution_is_bounded_with_single_lock_and_scoped_runners(config):
    async def check():
        probe = _Probe()
        manager = ParallelTaskManager(config, probe.factory)
        await manager.start()
        try:
            one = await manager.submit("fixture", "one", "one")
            two = await manager.submit("fixture", "two", "two")
            third = await manager.submit("fixture", "three", "three")
            fourth = await manager.submit("fixture", "four", "four")
            with pytest.raises(HarnessError) as queue:
                await manager.submit("fixture", "five", "five")
            assert queue.value.code == "QUEUE_FULL"
            await _started(probe, one["job_id"], two["job_id"])
            assert probe.maximum == 2
            assert third["status"] == fourth["status"] == "queued"
            assert third["job_id"] not in probe.runners
            with pytest.raises(HarnessError) as lock:
                TaskManager(config, _Runner(_Probe(), None))
            assert lock.value.code == "INSTANCE_LOCKED"
            probe.runners[one["job_id"]].release.set()
            assert (await _terminal(manager, one["job_id"]))["status"] == "succeeded"
            await _started(probe, third["job_id"])
            assert fourth["job_id"] not in probe.runners
            probe.runners[two["job_id"]].release.set()
            await _terminal(manager, two["job_id"])
            await _started(probe, fourth["job_id"])
            for job in (third, fourth):
                probe.runners[job["job_id"]].release.set()
                await _terminal(manager, job["job_id"])
            assert probe.maximum == 2
            assert all(runner.recover_calls == runner.close_calls == 0 for job_id, runner in probe.runners.items() if job_id is not None)
        finally:
            probe.blocked_stops.clear()
            await manager.close()
        assert manager.closed
    asyncio.run(check())


def test_cancelling_one_job_keeps_other_conversation_running(config):
    async def check():
        probe = _Probe()
        manager = ParallelTaskManager(config, probe.factory)
        await manager.start()
        try:
            one = await manager.submit("fixture", "one", "one")
            two = await manager.submit("fixture", "two", "two")
            await _started(probe, one["job_id"], two["job_id"])
            await manager.cancel(one["job_id"])
            assert (await _terminal(manager, one["job_id"]))["status"] == "cancelled"
            assert manager.status(two["job_id"])["status"] == "running"
            assert two["job_id"] not in probe.terminated
            probe.runners[two["job_id"]].release.set()
            assert (await _terminal(manager, two["job_id"]))["status"] == "succeeded"
        finally:
            probe.blocked_stops.clear()
            await manager.close()
    asyncio.run(check())


def test_unconfirmed_execution_keeps_slot_and_blocks_new_tasks_until_own_stop_confirmed(config):
    async def check():
        probe = _Probe()
        manager = ParallelTaskManager(config, probe.factory)
        await manager.start()
        try:
            one = await manager.submit("fixture", "one", "one")
            two = await manager.submit("fixture", "two", "two")
            await _started(probe, one["job_id"], two["job_id"])
            probe.blocked_stops.add(one["job_id"])
            probe.runners[one["job_id"]].release.set()
            await _wait(lambda: manager.status(one["job_id"])["termination_failed"])
            with pytest.raises(HarnessError):
                await manager.submit("fixture", "new", "new")
            assert one["job_id"] in manager.contexts
            assert manager.status(two["job_id"])["status"] == "running"
            assert two["job_id"] not in probe.terminated
            probe.blocked_stops.clear()
            await manager.cancel(one["job_id"])
            await _terminal(manager, one["job_id"])
            probe.runners[two["job_id"]].release.set()
            await _terminal(manager, two["job_id"])
            await _wait(lambda: manager.accepting)
            new = await manager.submit("fixture", "after recovery", "after")
            await _started(probe, new["job_id"])
            probe.runners[new["job_id"]].release.set()
            await _terminal(manager, new["job_id"])
        finally:
            probe.blocked_stops.clear()
            await manager.close()
    asyncio.run(check())


def test_close_requires_all_contexts_before_releasing_lock_and_recovery(config):
    async def check():
        probe = _Probe()
        manager = ParallelTaskManager(config, probe.factory)
        await manager.start()
        one = await manager.submit("fixture", "one", "one")
        two = await manager.submit("fixture", "two", "two")
        await _started(probe, one["job_id"], two["job_id"])
        probe.blocked_stops.add(one["job_id"])
        try:
            await manager.close()
            assert not manager.closed and manager.contexts
            assert probe.runners[None].close_calls == 0
            with pytest.raises(HarnessError) as lock:
                TaskManager(config, _Runner(_Probe(), None))
            assert lock.value.code == "INSTANCE_LOCKED"
        finally:
            probe.blocked_stops.clear()
            await manager.close()
        assert manager.closed and not manager.contexts
        assert probe.runners[None].close_calls == 1
        assert probe.active == 0
    asyncio.run(check())


def test_terminal_report_does_not_reopen_queue_when_owned_cleanup_is_unconfirmed(config):
    async def check():
        probe = _Probe()
        manager = ParallelTaskManager(config, probe.factory)
        await manager.start()
        try:
            job = await manager.submit("fixture", "one", "terminal")
            await _started(probe, job["job_id"])
            runner = probe.runners[job["job_id"]]
            runner.report_stopped = True
            probe.blocked_cleanup.add(job["job_id"])
            runner.release.set()
            assert (await _terminal(manager, job["job_id"]))["status"] == "succeeded"
            await _wait(lambda: job["job_id"] in manager.contexts and manager.contexts[job["job_id"]].ownership_failed)
            assert not manager.store.has_unconfirmed_execution()
            assert runner.scoped_recover_calls >= 1
            await asyncio.sleep(0.15)
            assert not manager.accepting
            with pytest.raises(HarnessError):
                await manager.submit("fixture", "next", "next")
            probe.blocked_cleanup.clear()
            await manager.cancel(job["job_id"])
            await _wait(lambda: manager.accepting)
            assert manager.status(job["job_id"])["status"] == "succeeded"
        finally:
            probe.blocked_stops.clear()
            probe.blocked_cleanup.clear()
            await manager.close()
    asyncio.run(check())


def test_parallel_store_reserves_capacity_and_preserves_original_request_identity(config):
    store = JobStore(config.data_dir)
    try:
        def submit(name):
            request = {"client_request_id": name, "project_id": "fixture", "mode": "review"}
            return store.submit_parallel(request, name, {"executor": {"backend": "desktop"}}, 2, 2)

        first, _ = submit("one")
        second, _ = submit("two")
        third, _ = submit("three")
        fourth, _ = submit("four")
        replay, created = submit("one")
        assert replay["job_id"] == first["job_id"] and not created
        with pytest.raises(HarnessError) as queue:
            submit("five")
        assert queue.value.code == "QUEUE_FULL"
        assert store.next_parallel_job({first["job_id"], second["job_id"]}, 2) is None
        store.finish(first["job_id"], "succeeded")
        claim = store.next_parallel_job({second["job_id"]}, 2)
        assert claim["job_id"] == third["job_id"]
        store.termination_failed(third["job_id"])
        assert store.has_unconfirmed_execution()
        with pytest.raises(HarnessError) as blocked:
            submit("blocked")
        assert blocked.value.code == "EXECUTION_BLOCKED"
        assert store.next_parallel_job(set(), 2) is None
        assert store.get(fourth["job_id"])["status"] == "queued"
    finally:
        store.close()
