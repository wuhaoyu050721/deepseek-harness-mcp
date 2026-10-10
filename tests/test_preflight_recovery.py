"""预检重启恢复仅使用临时数据库和固定 Job 查询；不启动进程或模型。"""

import asyncio
from dataclasses import replace
import json
import os

import pytest

from harness_mcp.config import AppConfig, ProjectConfig, frozen_execution, request_fingerprint
from harness_mcp.errors import HarnessError
from harness_mcp.task_manager import TaskManager, _PreflightRunner, _UNCONFIRMED_MANAGERS
import harness_mcp.task_manager as task_module


class NoModel:
    def __init__(self):
        self.recover_calls = 0
        self.close_calls = 0

    async def recover(self):
        self.recover_calls += 1
        return True

    async def close(self):
        self.close_calls += 1
        return True

    async def run(self, *args, **kwargs):
        pytest.fail("预检恢复夹具不能调用模型")


@pytest.mark.skipif(os.name != "nt", reason="持久化命名 Windows Job 的恢复契约")
def test_recovery_reuses_unconfirmed_helper_until_exact_job_stops(tmp_path, monkeypatch):
    async def scenario():
        config = AppConfig(tmp_path / "data")
        runner = NoModel()
        parent = TaskManager(config, runner)
        root = config.data_dir / "preflights" / ("a" * 32)
        helper_config = replace(config, data_dir=root)
        original = TaskManager(helper_config, _PreflightRunner())
        owner = original.store.owner_id
        await original.close()
        assert original.closed
        run_id = "b" * 32
        record = root / "jobs/preflight/preparation_runs" / run_id / "execution.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({
            "version": 1, "owner_id": owner, "job_id": "preflight", "run_id": run_id,
            "phase": "preparing", "job_name": f"Local\\deepseek-harness-{owner}-{run_id}",
        }), encoding="utf-8")
        recorded_bytes = record.read_bytes()
        stopped = False
        queried = []
        constructed = []
        manager_class = TaskManager

        async def job_stopped(name):
            queried.append(name)
            return stopped

        def factory(config, supplied_runner=None):
            value = manager_class(config, supplied_runner)
            constructed.append(value)
            return value

        monkeypatch.setattr(task_module, "named_job_stopped", job_stopped)
        monkeypatch.setattr(task_module, "TaskManager", factory)
        retained = None
        try:
            assert await parent._recover_preflight_records() is False
            retained = parent._preflight_recovery_helpers[root]
            assert retained in _UNCONFIRMED_MANAGERS and retained.closed is False
            assert len(constructed) == 1 and constructed[0] is retained
            assert record.read_bytes() == recorded_bytes
            with pytest.raises(HarnessError) as caught:
                manager_class(helper_config, _PreflightRunner())
            assert caught.value.code == "INSTANCE_LOCKED"
            assert runner.recover_calls == runner.close_calls == 0

            stopped = True
            assert await parent._recover_preflight_records() is True
            assert len(constructed) == 1 and constructed[0] is retained
            assert retained.closed and retained not in _UNCONFIRMED_MANAGERS
            assert parent._preflight_recovery_helpers == {}
            assert not record.exists()
            assert (record.parent / "stopped/execution.json").read_bytes() == recorded_bytes
            assert queried and set(queried) == {f"Local\\deepseek-harness-{owner}-{run_id}"}
            assert runner.recover_calls == runner.close_calls == 0

            # 实际辅助锁已经释放，且复用恢复没有重新生成数据库归属。
            reopened = manager_class(helper_config, _PreflightRunner())
            assert reopened.store.owner_id == owner
            await reopened.close()
            assert reopened.closed
        finally:
            stopped = True
            if retained is not None and not retained.closed:
                await retained.close()
            await parent.close()

    asyncio.run(scenario())


def test_preflight_recovery_does_not_scan_normal_job_records_or_touch_parent_tasks(tmp_path, monkeypatch):
    async def scenario():
        source = tmp_path / "source"
        source.mkdir()
        config = AppConfig(tmp_path / "data", {"demo": ProjectConfig("demo", source)})
        runner = NoModel()
        parent = TaskManager(config, runner)
        request = {"project_id": "demo", "prompt": "fixture", "client_request_id": "queued-production",
                   "relative_paths": None, "timeout_seconds": 600, "mode": "review"}
        job, _ = parent.store.submit(request, request_fingerprint(request), frozen_execution(config, "demo"), 5)
        record = config.data_dir / "jobs" / job["job_id"] / "preparation_runs" / ("c" * 32) / "execution.json"
        record.parent.mkdir(parents=True)
        record.write_bytes(b'{"fixture":"unrelated ordinary job"}')
        other_record = tmp_path / "other-production/jobs/preflight/preparation_runs" / ("d" * 32) / "execution.json"
        other_record.parent.mkdir(parents=True)
        other_record.write_bytes(b'{"fixture":"another data directory"}')
        before = parent.store.get(job["job_id"])

        async def unexpected_query(name):
            pytest.fail("预检恢复不能查询普通作业的 Windows Job")

        def unexpected_helper(*args, **kwargs):
            pytest.fail("没有预检归属记录时不能创建辅助管理器")

        monkeypatch.setattr(task_module, "named_job_stopped", unexpected_query)
        monkeypatch.setattr(task_module, "TaskManager", unexpected_helper)
        try:
            assert await parent._recover_preflight_records() is True
            assert parent.store.get(job["job_id"]) == before
            assert parent.store.get(job["job_id"])["status"] == "preparing"
            assert record.read_bytes() == b'{"fixture":"unrelated ordinary job"}'
            assert other_record.read_bytes() == b'{"fixture":"another data directory"}'
            assert parent.closed is False and parent._preflight_recovery_helpers == {}
            assert runner.recover_calls == runner.close_calls == 0
        finally:
            record.unlink(missing_ok=True)
            await parent.close()

    asyncio.run(scenario())
