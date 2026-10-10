"""稳定事件夹具验证预检关闭归属；不启动准备进程、不调用模型。"""

import asyncio

import pytest

from harness_mcp.config import AppConfig, ModelConfig, ProjectConfig
from harness_mcp.errors import HarnessError
from harness_mcp.parallel_manager import ParallelTaskManager
from harness_mcp.task_manager import TaskManager, _UNCONFIRMED_MANAGERS


class NoModel:
    async def run(self, *args):
        pytest.fail("生命周期夹具不能调用模型")

    async def recover(self):
        return True

    async def close(self):
        return True


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    return AppConfig(tmp_path / "data", {"demo": ProjectConfig("demo", source)},
                     model=ModelConfig(credential_source="env", credential_env="PREFLIGHT_LIFECYCLE_UNUSED_KEY"))


class HelperProbe:
    def __init__(self):
        self.snapshot_entered = asyncio.Event()
        self.snapshot_release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.confirmed = True
        self.cleanup_error = False
        self.helpers = []

    def factory(self, config, runner):
        probe = self

        class Helper:
            def __init__(self):
                self.closed = False
                self.secrets = []

            def _register_secret(self, value):
                self.secrets.append(value)

            async def _snapshot(self, *args, **kwargs):
                probe.snapshot_entered.set()
                await probe.snapshot_release.wait()
                return {"checked_files": 1, "model_called": False}

            async def close(self):
                probe.cleanup_entered.set()
                await probe.cleanup_release.wait()
                if probe.cleanup_error:
                    raise RuntimeError("人工清理异常")
                self.closed = probe.confirmed

        helper = Helper()
        self.helpers.append(helper)
        return helper


def _manager(config, parallel):
    value = ParallelTaskManager(config, lambda job_id: NoModel()) if parallel else TaskManager(config, NoModel())
    value.accepting = True
    return value


def _assert_parent_still_owned(manager, config):
    assert manager.closed is False
    assert manager.store.connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(HarnessError) as caught:
        TaskManager(config, NoModel())
    assert caught.value.code == "INSTANCE_LOCKED"


@pytest.mark.parametrize("parallel", [False, True])
def test_service_close_waits_for_preflight_cleanup_before_database_and_lock_release(config, monkeypatch, parallel):
    async def scenario():
        manager, probe = _manager(config, parallel), HelperProbe()
        monkeypatch.setattr("harness_mcp.task_manager.TaskManager", probe.factory)
        calling = asyncio.create_task(manager.preflight("demo", ["main.py"], "review"))
        closing = None
        try:
            await asyncio.wait_for(probe.snapshot_entered.wait(), 1)
            closing = asyncio.create_task(manager.close())
            await asyncio.wait_for(probe.cleanup_entered.wait(), 1)
            assert not closing.done() and not probe.helpers[0].closed
            _assert_parent_still_owned(manager, config)
            probe.cleanup_release.set()
            await asyncio.wait_for(closing, 1)
            result = await asyncio.gather(calling, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert manager.closed and probe.helpers[0].closed
            assert not manager._preflight_tasks and not manager._preflight_helpers
            assert manager not in _UNCONFIRMED_MANAGERS
        finally:
            probe.snapshot_release.set()
            probe.cleanup_release.set()
            await asyncio.gather(calling, *([closing] if closing is not None else []), return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [False, True])
def test_cancelled_preflight_completes_owned_cleanup_without_closing_parent(config, monkeypatch, parallel):
    async def scenario():
        manager, probe = _manager(config, parallel), HelperProbe()
        monkeypatch.setattr("harness_mcp.task_manager.TaskManager", probe.factory)
        calling = asyncio.create_task(manager.preflight("demo", ["main.py"], "review"))
        try:
            await asyncio.wait_for(probe.snapshot_entered.wait(), 1)
            calling.cancel()
            await asyncio.wait_for(probe.cleanup_entered.wait(), 1)
            assert not calling.done() and not probe.helpers[0].closed
            _assert_parent_still_owned(manager, config)
            probe.cleanup_release.set()
            result = await asyncio.gather(calling, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert probe.helpers[0].closed and not manager.closed
            assert not manager._preflight_tasks and not manager._preflight_helpers
        finally:
            probe.snapshot_release.set()
            probe.cleanup_release.set()
            await asyncio.gather(calling, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [False, True])
def test_repeated_cancellation_does_not_detach_helper_cleanup_from_parent(config, monkeypatch, parallel):
    async def scenario():
        manager, probe = _manager(config, parallel), HelperProbe()
        monkeypatch.setattr("harness_mcp.task_manager.TaskManager", probe.factory)
        calling = asyncio.create_task(manager.preflight("demo", ["main.py"], "review"))
        closing = None
        try:
            await asyncio.wait_for(probe.snapshot_entered.wait(), 1)
            calling.cancel()
            await asyncio.wait_for(probe.cleanup_entered.wait(), 1)
            calling.cancel()
            result = await asyncio.gather(calling, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert manager._preflight_helpers and manager._preflight_cleanup_tasks
            closing = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            assert not closing.done()
            _assert_parent_still_owned(manager, config)
            probe.cleanup_release.set()
            await asyncio.wait_for(closing, 1)
            assert manager.closed and probe.helpers[0].closed
            assert not manager._preflight_helpers and not manager._preflight_cleanup_tasks
        finally:
            probe.snapshot_release.set()
            probe.cleanup_release.set()
            await asyncio.gather(calling, *([closing] if closing is not None else []), return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_unconfirmed_helper_cleanup_retains_parent_lock_and_blocks_new_tasks(config, monkeypatch, parallel, cleanup_error):
    async def scenario():
        manager, probe = _manager(config, parallel), HelperProbe()
        probe.confirmed = False
        probe.cleanup_error = cleanup_error
        probe.snapshot_release.set()
        probe.cleanup_release.set()
        monkeypatch.setattr("harness_mcp.task_manager.TaskManager", probe.factory)
        try:
            with pytest.raises(HarnessError) as caught:
                await manager.preflight("demo", ["main.py"], "review")
            assert caught.value.code == "TERMINATION_FAILED" and caught.value.stage == "preflight"
            assert manager.accepting is False and manager.recovery_confirmed is False
            if parallel:
                manager._refresh_acceptance()
                assert manager.accepting is False
            await manager.close()
            _assert_parent_still_owned(manager, config)
            assert manager in _UNCONFIRMED_MANAGERS and manager._preflight_helpers
            assert manager.accepting is False
            with pytest.raises(HarnessError) as stopped:
                await manager.preflight("demo", ["main.py"], "review")
            assert stopped.value.code == "SERVICE_STOPPING"
            probe.confirmed = True
            probe.cleanup_error = False
            await manager.close()
            assert manager.closed and probe.helpers[0].closed
            assert manager not in _UNCONFIRMED_MANAGERS
        finally:
            probe.confirmed = True
            probe.cleanup_error = False
            probe.snapshot_release.set()
            probe.cleanup_release.set()
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("parallel", [False, True])
def test_interrupted_parent_close_retains_preflight_ownership_until_retry(config, monkeypatch, parallel):
    async def scenario():
        manager, probe = _manager(config, parallel), HelperProbe()
        monkeypatch.setattr("harness_mcp.task_manager.TaskManager", probe.factory)
        calling = asyncio.create_task(manager.preflight("demo", ["main.py"], "review"))
        closing = None
        try:
            await asyncio.wait_for(probe.snapshot_entered.wait(), 1)
            closing = asyncio.create_task(manager.close())
            await asyncio.wait_for(probe.cleanup_entered.wait(), 1)
            closing.cancel()
            result = await asyncio.gather(closing, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            _assert_parent_still_owned(manager, config)
            assert manager in _UNCONFIRMED_MANAGERS and manager._preflight_helpers
            probe.cleanup_release.set()
            await asyncio.gather(calling, return_exceptions=True)
            await manager.close()
            assert manager.closed and probe.helpers[0].closed
            assert manager not in _UNCONFIRMED_MANAGERS
        finally:
            probe.snapshot_release.set()
            probe.cleanup_release.set()
            await asyncio.gather(calling, *([closing] if closing is not None else []), return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())
