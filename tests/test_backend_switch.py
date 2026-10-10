"""配置切换后，历史执行器未停止不能被新执行器的空列表代替。"""

import asyncio
from pathlib import Path

from harness_mcp.config import AppConfig, frozen_execution, ProjectConfig, request_fingerprint
from harness_mcp.job_store import JobStore
from harness_mcp.task_manager import TaskManager


def test_switch_preserves_blocked_legacy_backend(tmp_path, monkeypatch):
    config = AppConfig(tmp_path / "state", {"sample": ProjectConfig("sample", tmp_path)})
    store = JobStore(config.data_dir)
    request = {"project_id": "sample", "prompt": "read", "client_request_id": "old", "relative_paths": None, "timeout_seconds": 10}
    legacy = frozen_execution(config, "sample")
    legacy["executor"].pop("backend")
    job, _ = store.submit(request, request_fingerprint(request), legacy, 5)
    store.close()
    recovered = []
    class Runner:
        all_stopped = False
        def __init__(self, backend):
            self.backend = backend
        async def recover(self):
            recovered.append(self.backend)
            return self.all_stopped or self.backend == "desktop"
        async def close(self):
            return True
    monkeypatch.setattr(TaskManager, "_make_runner", lambda self, backend: Runner(backend))
    async def check():
        manager = TaskManager(config)
        try:
            assert not await manager._recover_owned()
            manager.store.recover_interrupted(False)
            assert manager.store.get(job["job_id"])["termination_failed"]
            assert recovered == ["desktop", "docker"]
        finally:
            # 本测试的固定停止失败夹具转为可确认，才能释放新的持锁关闭边界。
            Runner.all_stopped = True
            await manager.close()
    asyncio.run(check())
