"""实际 Windows JobObject + 人工 Python SDK 替身，不调用模型或桌面 runtime。"""

import asyncio
from dataclasses import asdict
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from harness_mcp.config import AppConfig, ExecutorConfig, ModelConfig
from harness_mcp.desktop_launcher import DesktopLaunchError
from harness_mcp.desktop_runner import DesktopRunner, _write_record, worker_environment
from harness_mcp.desktop_worker import encode_result, execute
from harness_mcp.errors import HarnessError
from harness_mcp.process_control import named_job_stopped, spawn_owned, stop_process_tree


FIXTURE_WORKER = r'''
import json,os,pathlib,subprocess,sys,time
from harness_mcp import harness_adapter
def run(request,snapshot,home,on_activity):
    behavior=request.get('_fixture_behavior','success')
    on_activity('initializing')
    print('accidental Python stdout must be discarded',flush=True)
    if behavior in ('hang','family'):
        child=pathlib.Path(request['_fixture_child'])
        subprocess.Popen([sys.executable,str(child),request['_fixture_child_marker'],request['_fixture_grandchild'],request['_fixture_grandchild_marker']])
        deadline=time.monotonic()+5
        while not pathlib.Path(request['_fixture_grandchild_marker']).exists():
            if time.monotonic()>deadline:raise RuntimeError('fixture setup failed')
            time.sleep(.01)
    on_activity('editing' if request.get('mode')=='edit' else 'reviewing')
    if request.get('mode')=='edit':
        workspace=pathlib.Path(request['workspace_dir'])
        (workspace/'sample.py').write_text("print('edited fixture')\n",encoding='utf-8')
    if behavior=='heartbeat':
        end=time.monotonic()+2.2
        while time.monotonic()<end:
            on_activity('initializing')
            on_activity('reviewing')
            time.sleep(.003)
    (home/'raw-session.jsonl').write_text('fixture raw reasoning only',encoding='utf-8')
    if behavior=='hang':time.sleep(100)
    if behavior=='invalid':return {'raw_text':[], 'finish_reason':'completed','error':None}
    text=os.environ[request['model']['credential_env']]+' 人工审查结果'
    if behavior=='oversized':text='中'*100000
    return {'raw_text':text,'finish_reason':'completed','usage':None,'error':None,'timing':{'elapsed_seconds':.1}}
harness_adapter.run_harness=run
from harness_mcp.desktop_worker import main
main()
'''


@unittest.skipUnless(os.name == "nt", "actual Windows named JobObject lifecycle")
class DesktopRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Harness native 中文 & ! % ")
        self.data = Path(self.temp.name).resolve()
        self.job = self.data / "jobs" / "one"
        self.snapshot = self.job / "snapshot" / "files"
        self.snapshot.mkdir(parents=True)
        self.source = self.snapshot / "sample.py"
        self.source.write_text("print('fixture')\n", encoding="utf-8")
        self.config = AppConfig(self.data, model=ModelConfig(credential_source="env"), executor=ExecutorConfig(backend="desktop"))
        self.owner = "a" * 32
        self.runner = DesktopRunner(self.config, self.owner)
        self.secret = "fixture-credential-that-is-never-a-personal-key"
        self.fixture = self.data / "fixture.py"
        self.fixture.write_text(FIXTURE_WORKER, encoding="utf-8")
        self.child = self.data / "child.py"
        self.child.write_text("import os,pathlib,subprocess,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\nsubprocess.Popen([sys.executable,sys.argv[2],sys.argv[3]])\ntime.sleep(100)\n", encoding="utf-8")
        self.grandchild = self.data / "grandchild.py"
        self.grandchild.write_text("import os,pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(100)\n", encoding="utf-8")
        self.request = {
            "job_id": "one", "project_id": "sample", "prompt": "只读人工 fixture",
            "timeout_seconds": 30, "deadline_at": time.time() + 30,
            "snapshot_dir": str(self.snapshot), "job_dir": str(self.job),
            "model": asdict(self.config.model), "executor": asdict(self.config.executor),
            "limits": {"max_output_bytes": 100000, "termination_grace_seconds": 1},
            "_fixture_child": str(self.child), "_fixture_grandchild": str(self.grandchild),
            "_fixture_child_marker": str(self.data / "child.pid"),
            "_fixture_grandchild_marker": str(self.data / "grandchild.pid"),
        }
        self.started = asyncio.Event()
        self.captured = []

        def fixture_spawn(argv, environment, **kwargs):
            state = self.runner._active["one"]
            self.assertTrue(state.record.is_file())  # 创建进程前已有持久归属。
            record = json.loads(state.record.read_text(encoding="utf-8"))
            self.assertEqual(record["job_name"], kwargs["job_name"])
            owned = spawn_owned([sys.executable, str(self.fixture)], environment, **kwargs)
            self.captured.append((argv, environment, kwargs, owned))
            self.started.set()
            return owned

        self.env_patch = patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.secret, "UNRELATED_SECRET": "must-not-propagate", "ELECTRON_RUN_AS_NODE": "must-not-propagate"})
        self.spawn_patch = patch("harness_mcp.desktop_runner.spawn_owned", side_effect=fixture_spawn)
        self.root_patch = patch("harness_mcp.desktop_launcher.resolve_desktop_root", return_value=self.data / "人工 桌面安装")
        for item in (self.env_patch, self.spawn_patch, self.root_patch):
            item.start()

    async def asyncTearDown(self):
        for _, _, _, owned in self.captured:
            await stop_process_tree(owned)
        await self.runner.close()
        for item in (self.root_patch, self.spawn_patch, self.env_patch):
            item.stop()
        self.temp.cleanup()

    async def wait_for_family(self):
        deadline = time.monotonic() + 5
        marker = Path(self.request["_fixture_grandchild_marker"])
        while not marker.exists():
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.02)

    def edit_workspace(self):
        workspace = self.job / "workspace"
        workspace.mkdir()
        (workspace / "sample.py").write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        self.request.update(mode="edit", workspace_dir=str(workspace))
        return workspace

    async def test_success_redacts_secret_filters_environment_and_cleans_home(self):
        stages = []
        result = await self.runner.run(self.request, stages.append)
        self.assertIsNone(result["error"])
        self.assertTrue(result["execution_stopped"])
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertIn("[REDACTED]", result["raw_text"])
        argv, environment, _, owned = self.captured[0]
        self.assertNotIn(self.secret, json.dumps(argv))
        self.assertEqual(environment["DEEPSEEK_API_KEY"], self.secret)
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertNotIn("ELECTRON_RUN_AS_NODE", environment)
        self.assertEqual(environment["DSH_HOME"], environment["HOME"])
        self.assertTrue(owned.tree_stopped)
        self.assertFalse(Path(environment["HOME"]).exists())
        self.assertFalse(list(self.job.glob("desktop_runs/*/execution.json")))
        record = next(self.job.glob("desktop_runs/*/stopped/execution.json"))
        self.assertTrue(json.loads(record.read_text(encoding="utf-8"))["confirmed_stopped"])
        self.assertNotIn(self.secret, record.read_text(encoding="utf-8"))
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print('fixture')\n")
        self.assertEqual(stages[:3], ["checking_executor", "starting_desktop", "running"])
        self.assertTrue(await self.runner.recover())

    async def test_normal_parent_exit_also_stops_live_child_and_grandchild(self):
        self.request["_fixture_behavior"] = "family"
        result = await self.runner.run(self.request, lambda stage: None)
        owned = self.captured[0][3]
        self.assertIsNone(result["error"])
        self.assertTrue(Path(self.request["_fixture_child_marker"]).exists())
        self.assertTrue(Path(self.request["_fixture_grandchild_marker"]).exists())
        self.assertTrue(owned.tree_stopped)
        self.assertEqual(owned.active_count(), 0)

    async def test_actual_rotated_credential_registered_before_worker_creation(self):
        observed = []
        def registered(value):
            self.assertFalse(self.captured)
            self.assertFalse(list(self.job.glob("desktop_runs/*/execution.json")))
            observed.append(value)
        self.runner = DesktopRunner(self.config, self.owner, on_credential=registered)
        rotated = "rotated-opaque-artificial-desktop-value"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": rotated}):
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertIsNone(result["error"])
        self.assertEqual(observed, [rotated])
        argv, environment, _, owned = self.captured[0]
        self.assertEqual(environment["DEEPSEEK_API_KEY"], rotated)
        self.assertNotIn(rotated, json.dumps(argv))
        self.assertNotIn(rotated, json.dumps(result))
        self.assertTrue(owned.tree_stopped)
        for path in self.job.rglob("*.json"):
            self.assertNotIn(rotated, path.read_text(encoding="utf-8"))

    async def test_credential_registration_failure_prevents_worker_creation(self):
        def rejected(_value):
            raise RuntimeError("artificial registration failure")
        self.runner = DesktopRunner(self.config, self.owner, on_credential=rejected)
        result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "WORKER_FAILED")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(self.captured)
        self.assertFalse(self.runner._active)

    async def test_activity_callback_and_initialization_heartbeat_share_atomic_writer(self):
        self.request["_fixture_behavior"] = "heartbeat"
        stages = []
        result = await self.runner.run(self.request, stages.append)
        self.assertIsNone(result["error"])
        self.assertTrue(result["execution_stopped"])
        self.assertGreaterEqual(sum(stage in {"initializing", "reviewing"} for stage in stages), 5)
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())

    async def test_edit_preserves_baseline_and_written_workspace_after_home_cleanup(self):
        workspace = self.edit_workspace()
        stages = []
        result = await self.runner.run(self.request, stages.append)
        self.assertIsNone(result["error"])
        self.assertTrue(result["execution_stopped"])
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print('fixture')\n")
        self.assertEqual((workspace / "sample.py").read_text(encoding="utf-8"), "print('edited fixture')\n")
        home = Path(self.captured[0][1]["HOME"])
        self.assertFalse(home.exists())
        self.assertFalse(home.is_relative_to(workspace))
        self.assertIn("editing", stages)
        self.assertNotIn("reviewing", stages)
        self.assertNotIn("changes", result)  # runner 不相信模型自述文件清单。
        self.assertNotIn(self.secret, json.dumps(result))

    async def test_edit_cancel_stops_family_and_preserves_only_workspace_copy(self):
        workspace = self.edit_workspace()
        self.request["_fixture_behavior"] = "hang"
        task = asyncio.create_task(self.runner.run(self.request, lambda stage: None))
        await self.started.wait()
        await self.wait_for_family()
        self.assertTrue(await self.runner.terminate("one"))
        result = await asyncio.wait_for(task, 8)
        self.assertEqual(result["error"]["code"], "CANCELLED")
        self.assertTrue(result["execution_stopped"])
        self.assertTrue(self.captured[0][3].tree_stopped)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print('fixture')\n")
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())
        self.assertNotIn("changes", result)

    async def test_edit_deadline_keeps_baseline_and_confirms_process_tree_stop(self):
        workspace = self.edit_workspace()
        self.request.update(_fixture_behavior="hang", deadline_at=time.time() + 0.7)
        result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "TIMED_OUT")
        self.assertTrue(result["execution_stopped"])
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print('fixture')\n")
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())
        self.assertNotIn("changes", result)

    async def test_edit_rejects_missing_outside_or_overlapping_workspace_before_credentials(self):
        external = self.data / "outside-job"
        external.mkdir()
        values = [None, "relative-workspace", str(self.snapshot), str(self.snapshot / "inside"), str(self.job), str(external)]
        self.request["mode"] = "edit"
        for value in values:
            with self.subTest(workspace=value):
                self.request["workspace_dir"] = value
                with patch("harness_mcp.desktop_runner.resolve_credential") as credentials:
                    result = await self.runner.run(self.request, lambda stage: None)
                self.assertEqual(result["error"]["code"], "INVALID_EXECUTOR_PATH")
                self.assertTrue(result["execution_stopped"])
                credentials.assert_not_called()
                self.assertFalse(self.captured)

    async def test_invalid_mode_fails_without_worker_or_credentials(self):
        for mode in ("other", "EDIT", None, []):
            with self.subTest(mode=mode):
                self.request["mode"] = mode
                with patch("harness_mcp.desktop_runner.resolve_credential") as credentials:
                    result = await self.runner.run(self.request, lambda stage: None)
                self.assertEqual(result["error"]["code"], "INVALID_MODE")
                self.assertTrue(result["execution_stopped"])
                credentials.assert_not_called()
                self.assertFalse(self.captured)

    async def test_review_does_not_forward_optional_workspace_as_model_cwd(self):
        self.request.update(mode="review", workspace_dir=str(self.data / "unapproved-unused-directory"))
        state, _, encoded, _ = self.runner._prepare(self.request)
        frozen = json.loads(encoded)
        self.assertEqual(frozen["mode"], "review")
        self.assertEqual(frozen["snapshot_dir"], str(self.snapshot))
        self.assertNotIn("workspace_dir", frozen)
        self.assertTrue(await named_job_stopped(state.job_name))
        self.assertTrue(await self.runner.recover())

    async def test_cancel_confirms_entire_family_without_touching_unrelated_process(self):
        self.request["_fixture_behavior"] = "hang"
        task = asyncio.create_task(self.runner.run(self.request, lambda stage: None))
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(100)"], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            await self.started.wait()
            await self.wait_for_family()
            owned = self.captured[0][3]
            self.assertGreaterEqual(owned.active_count(), 3)
            self.assertTrue(await self.runner.terminate("one"))
            result = await asyncio.wait_for(task, 8)
            self.assertTrue(result["execution_stopped"])
            self.assertEqual(result["error"]["code"], "CANCELLED")
            self.assertTrue(owned.tree_stopped)
            self.assertEqual(owned.active_count(), 0)
            self.assertIsNone(unrelated.poll())
            self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())
        finally:
            unrelated.kill()
            unrelated.wait(timeout=5)
            if not task.done():
                await self.runner.terminate("one")
                await task

    async def test_outer_coroutine_cancel_still_stops_and_cleans_family(self):
        self.request["_fixture_behavior"] = "hang"
        task = asyncio.create_task(self.runner.run(self.request, lambda stage: None))
        await self.started.wait()
        await self.wait_for_family()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        self.assertTrue(self.captured[0][3].tree_stopped)
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())
        self.assertFalse(self.runner._active)

    async def test_absolute_deadline_stops_family_and_removes_raw_session(self):
        self.request.update(_fixture_behavior="hang", deadline_at=time.time() + 0.7)
        result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "TIMED_OUT")
        self.assertTrue(result["execution_stopped"])
        self.assertTrue(self.captured[0][3].tree_stopped)
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())

    async def test_worker_deadline_without_runner_monitoring_stops_family(self):
        self.request.update(_fixture_behavior="hang", deadline_at=time.time() + 0.7)
        state, environment, encoded, _ = self.runner._prepare(self.request)
        # 此处没有 runner.run 的期限循环；只运行真实 Worker 的独立硬时限。
        owned = spawn_owned([sys.executable, str(self.fixture)], environment, job_name=state.job_name)
        self.captured.append(([], environment, {}, owned))
        started = time.monotonic()
        output, _ = await asyncio.wait_for(asyncio.to_thread(owned.communicate, encoded), 8)
        self.assertEqual(output, b"")
        self.assertEqual(owned.returncode, 124)
        self.assertTrue(owned.tree_stopped)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(await named_job_stopped(state.job_name))
        self.assertTrue(await self.runner.recover())
        self.assertFalse(state.home.exists())

    async def test_stop_failure_retains_ownership_and_raw_home_for_retry(self):
        self.request.update(_fixture_behavior="hang", deadline_at=time.time() + 0.7)
        with patch("harness_mcp.desktop_runner.stop_process_tree", return_value=False):
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertFalse(result["execution_stopped"])
        self.assertIn("one", self.runner._active)
        self.assertTrue(Path(self.captured[0][1]["HOME"]).exists())
        self.assertTrue(await self.runner.recover())
        self.assertFalse(self.runner._active)
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())

    async def test_recover_checks_actual_named_object_even_if_record_claims_stopped(self):
        self.request["_fixture_behavior"] = "hang"
        task = asyncio.create_task(self.runner.run(self.request, lambda stage: None))
        await self.started.wait()
        await self.wait_for_family()
        record = next(self.job.glob("desktop_runs/*/execution.json"))
        value = json.loads(record.read_text(encoding="utf-8"))
        value["confirmed_stopped"] = True
        _write_record(record, value)
        restarted = DesktopRunner(self.config, self.owner)
        self.assertFalse(await restarted.recover())
        self.assertFalse(self.captured[0][3].tree_stopped)
        self.assertTrue(await self.runner.terminate("one"))
        await task
        self.assertTrue(await restarted.recover())

    async def test_invalid_ownership_record_blocks_recovery_without_querying_other_job(self):
        state, _, _, _ = self.runner._prepare(self.request)
        record = state.record
        value = json.loads(record.read_text(encoding="utf-8"))
        for field, invalid in (("owner_id", "b" * 32), ("run_id", "invalid"), ("job_name", "Local\\other-job"), ("job_id", "../other")):
            with self.subTest(field=field):
                changed = dict(value)
                changed[field] = invalid
                _write_record(record, changed)
                with patch("harness_mcp.desktop_runner.named_job_stopped") as query:
                    self.assertFalse(await self.runner.recover())
                    query.assert_not_called()
                with self.assertRaises(HarnessError) as error:
                    self.runner._prepare(self.request)
                self.assertEqual(error.exception.code, "EXECUTION_BLOCKED")
        _write_record(record, value)

    async def test_missing_installation_fails_before_credential_resolution(self):
        with patch("harness_mcp.desktop_launcher.resolve_desktop_root", side_effect=DesktopLaunchError("DESKTOP_NOT_FOUND", "桌面安装未找到。")), patch("harness_mcp.desktop_runner.resolve_credential") as credential:
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "DESKTOP_NOT_FOUND")
        self.assertTrue(result["execution_stopped"])
        credential.assert_not_called()
        self.assertFalse(self.captured)

    async def test_missing_credentials_never_spawns_worker(self):
        with patch("harness_mcp.desktop_runner.resolve_credential", side_effect=HarnessError("CREDENTIAL_UNAVAILABLE", "凭证不可用。")):
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "CREDENTIAL_UNAVAILABLE")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(self.captured)

    async def test_invalid_worker_result_is_fixed_protocol_error(self):
        self.request["_fixture_behavior"] = "invalid"
        result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "EXECUTOR_PROTOCOL_ERROR")
        self.assertTrue(result["execution_stopped"])
        self.assertNotIn(self.secret, json.dumps(result))

    async def test_oversized_utf8_output_is_bounded_and_cleanup_still_runs(self):
        self.request.update(_fixture_behavior="oversized")
        self.request["limits"]["max_output_bytes"] = 128
        result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "OUTPUT_LIMIT")
        self.assertLessEqual(len(result["raw_text"].encode("utf-8")), 128)
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())

    async def test_cleanup_failure_is_reported_after_confirmed_stop_and_recover_retries(self):
        with patch("harness_mcp.desktop_runner.shutil.rmtree", side_effect=OSError("fixture-only-private-diagnostic")):
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "DESKTOP_HOME_CLEANUP_FAILED")
        self.assertTrue(result["execution_stopped"])
        self.assertNotIn("private-diagnostic", json.dumps(result))
        self.assertTrue(Path(self.captured[0][1]["HOME"]).exists())
        self.assertEqual(len(list(self.job.glob("desktop_runs/*/execution.json"))), 1)
        self.assertFalse(list(self.job.glob("desktop_runs/*/stopped/execution.json")))
        self.assertTrue(await self.runner.recover())
        self.assertFalse(Path(self.captured[0][1]["HOME"]).exists())
        self.assertFalse(list(self.job.glob("desktop_runs/*/execution.json")))
        self.assertEqual(len(list(self.job.glob("desktop_runs/*/stopped/execution.json"))), 1)

    async def test_confirmed_history_above_10000_does_not_block_desktop_recovery(self):
        state, _, _, _ = self.runner._prepare(self.request)
        original_glob = Path.glob
        archived = []
        def records(root, pattern, **kwargs):
            if root == self.config.data_dir and pattern == "jobs/*/desktop_runs/*/execution.json":
                return itertools.repeat(state.record, 10001)
            return original_glob(root, pattern, **kwargs)
        with patch.object(Path, "glob", records), patch("harness_mcp.desktop_runner.named_job_stopped", return_value=True) as query, \
                patch.object(self.runner, "_clean_home", return_value=True) as cleanup, \
                patch("harness_mcp.desktop_runner._archive_record", side_effect=archived.append):
            self.assertTrue(await self.runner.recover())
            self.assertEqual(query.await_count, 10001)
            self.assertEqual(cleanup.call_count, 10001)
            self.assertEqual(len(archived), 10001)

    async def test_desktop_history_limit_bounds_only_unconfirmed_records(self):
        state, _, _, _ = self.runner._prepare(self.request)
        original_glob = Path.glob
        def records(root, pattern, **kwargs):
            if root == self.config.data_dir and pattern == "jobs/*/desktop_runs/*/execution.json":
                return itertools.repeat(state.record, 10001)
            return original_glob(root, pattern, **kwargs)
        with patch.object(Path, "glob", records), patch("harness_mcp.desktop_runner.named_job_stopped", return_value=False) as query, \
                patch.object(self.runner, "_clean_home") as cleanup, patch("harness_mcp.desktop_runner._archive_record") as archive:
            self.assertFalse(await self.runner.recover())
            self.assertEqual(query.await_count, 10000)
            cleanup.assert_not_called()
            archive.assert_not_called()
            self.assertTrue(state.record.is_file())

    async def test_desktop_archive_failure_blocks_recovery_and_retains_record(self):
        state, _, _, _ = self.runner._prepare(self.request)
        original = state.record.read_bytes()
        with patch("harness_mcp.desktop_runner._archive_record", side_effect=OSError("controlled archive failure")):
            self.assertFalse(await self.runner.recover())
            self.assertEqual(state.record.read_bytes(), original)
        self.assertTrue(await self.runner.recover())
        self.assertFalse(state.record.exists())
        self.assertEqual((state.record.parent / "stopped" / state.record.name).read_bytes(), original)

    async def test_normal_desktop_archive_failure_retains_active_until_retry(self):
        with patch("harness_mcp.desktop_runner._archive_record", side_effect=OSError("controlled archive failure")):
            result = await self.runner.run(self.request, lambda stage: None)
            self.assertFalse(result["execution_stopped"])
            self.assertTrue(self.captured[0][3].tree_stopped)
            self.assertIn("one", self.runner._active)
            self.assertEqual(len(list(self.job.glob("desktop_runs/*/execution.json"))), 1)
            with self.assertRaises(HarnessError) as error:
                self.runner._prepare(self.request)
            self.assertEqual(error.exception.code, "EXECUTION_BLOCKED")
        self.assertTrue(await self.runner.recover())
        self.assertFalse(self.runner._active)
        self.assertFalse(list(self.job.glob("desktop_runs/*/execution.json")))
        self.assertEqual(len(list(self.job.glob("desktop_runs/*/stopped/execution.json"))), 1)

    async def test_desktop_archive_preserves_existing_audit_and_refuses_outside_symlink(self):
        outside = self.data / "outside-audit"
        outside.mkdir()
        sentinel = outside / "execution.json"
        sentinel.write_bytes(b"existing audit must remain")
        for obstacle in ("existing-audit", "outside-symlink"):
            with self.subTest(obstacle=obstacle):
                state, _, _, _ = self.runner._prepare(self.request)
                original = state.record.read_bytes()
                archive = state.record.parent / "stopped"
                if obstacle == "existing-audit":
                    archive.mkdir()
                    target = archive / "execution.json"
                    target.write_bytes(b"existing audit must remain")
                else:
                    os.symlink(outside, archive, target_is_directory=True)
                    target = sentinel
                try:
                    self.assertFalse(await self.runner.recover())
                    self.assertEqual(state.record.read_bytes(), original)
                    self.assertEqual(target.read_bytes(), b"existing audit must remain")
                    self.assertEqual(sentinel.read_bytes(), b"existing audit must remain")
                finally:
                    if obstacle == "existing-audit":
                        target.unlink()  # 只移除本用例创建的碰撞替身。
                    else:
                        archive.unlink()  # 只移除本用例链接，不跟随目标。
                    self.assertTrue(await self.runner.recover())
                self.assertEqual((archive / "execution.json").read_bytes(), original)
                self.assertEqual(sentinel.read_bytes(), b"existing audit must remain")

    async def test_cleanup_handles_real_windows_long_session_path(self):
        state, _, _, _ = self.runner._prepare(self.request)
        child = state.home / "sessions" / ("cwd-" + "x" * 170) / ("f" * 32)
        extended = "\\\\?\\" + str(child)
        os.makedirs(extended)
        with open(extended + "\\session.v4.jsonl", "w", encoding="utf-8") as stream:
            stream.write("artificial raw session")
        self.assertGreater(len(str(child / "session.v4.jsonl")), 330)
        self.assertTrue(await self.runner.recover())  # 未创建 named Job，确认不存在后清理。
        self.assertFalse(state.home.exists())
        self.assertFalse(state.record.exists())
        self.assertTrue((state.record.parent / "stopped" / state.record.name).is_file())
        self.assertTrue(self.source.is_file())

    async def test_cleanup_refuses_outside_home_and_symlink(self):
        state, _, _, _ = self.runner._prepare(self.request)
        outside = self.data / "personal-home"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("must remain", encoding="utf-8")
        self.assertFalse(self.runner._clean_home(outside, "one", state.run_id))
        self.assertTrue(await named_job_stopped(state.job_name))
        self.assertTrue(self.runner._clean_home(state.home, "one", state.run_id))
        os.symlink(outside, state.home, target_is_directory=True)
        self.assertFalse(self.runner._clean_home(state.home, "one", state.run_id))
        self.assertTrue(sentinel.is_file())
        state.home.unlink()

    async def test_process_ownership_failure_uses_fixed_error_and_removes_unstarted_home(self):
        with patch("harness_mcp.desktop_runner.spawn_owned", side_effect=HarnessError("PREPARATION_PROCESS_UNAVAILABLE", "监管建立失败。")):
            result = await self.runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "EXECUTOR_UNAVAILABLE")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(self.runner._active)
        self.assertFalse(list(self.job.glob("desktop_runs/*/home")))


class DesktopScopedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scoped desktop recovery ")
        self.root = Path(self.temporary.name).resolve()
        self.config = AppConfig(self.root)
        self.runner = DesktopRunner(self.config, "a" * 32)
        self.job_id, self.other_job_id = "b" * 32, "c" * 32
        self.own_record = self.record(self.job_id, "d" * 32)
        self.other_record = self.record(self.other_job_id, "e" * 32)

    def tearDown(self):
        self.temporary.cleanup()

    def record(self, job_id, run_id):
        record = self.root / "jobs" / job_id / "desktop_runs" / run_id / "execution.json"
        record.parent.mkdir(parents=True)
        (record.parent / "home").mkdir()
        (record.parent / "home" / "session.txt").write_text("own artificial session", encoding="utf-8")
        _write_record(record, {"version": 1, "backend": "desktop", "owner_id": "a" * 32, "job_id": job_id, "run_id": run_id,
                              "job_name": f"Local\\deepseek-harness-{'a' * 32}-{run_id}", "confirmed_stopped": True})
        return record

    async def test_own_home_failure_blocks_then_retry_cleans_without_touching_other_active_job(self):
        original_other = self.other_record.read_bytes()
        active_other = object()
        self.runner._active[self.other_job_id] = active_other
        own_name = f"Local\\deepseek-harness-{'a' * 32}-{'d' * 32}"
        with patch.object(self.runner, "terminate", AsyncMock(return_value=True)) as terminate, patch("harness_mcp.desktop_runner.named_job_stopped", AsyncMock(return_value=True)) as stopped:
            with patch.object(self.runner, "_clean_home", return_value=False) as clean:
                self.assertFalse(await self.runner.recover_job(self.job_id))
                clean.assert_called_once_with(self.own_record.parent / "home", self.job_id, "d" * 32)
            self.assertTrue(self.own_record.is_file())
            self.assertTrue(await self.runner.recover_job(self.job_id))
            self.assertFalse(self.own_record.is_file())
            self.assertTrue((self.own_record.parent / "stopped" / "execution.json").is_file())
            self.assertFalse((self.own_record.parent / "home").exists())
            self.assertEqual([call.args for call in terminate.call_args_list], [(self.job_id,), (self.job_id,)])
            self.assertEqual([call.args for call in stopped.call_args_list], [(own_name,), (own_name,)])
        self.assertEqual(self.other_record.read_bytes(), original_other)
        self.assertTrue((self.other_record.parent / "home" / "session.txt").is_file())
        self.assertIs(self.runner._active[self.other_job_id], active_other)

    async def test_invalid_job_id_does_not_access_process_or_records(self):
        with patch.object(self.runner, "terminate", AsyncMock()) as terminate, patch("harness_mcp.desktop_runner.named_job_stopped", AsyncMock()) as stopped:
            for invalid in ("../" + self.job_id, "one", self.job_id.upper(), None):
                self.assertFalse(await self.runner.recover_job(invalid))
            terminate.assert_not_called()
            stopped.assert_not_called()

    async def test_live_named_job_is_never_cleaned_or_archived(self):
        with patch.object(self.runner, "terminate", AsyncMock(return_value=True)), patch("harness_mcp.desktop_runner.named_job_stopped", AsyncMock(return_value=False)), patch.object(self.runner, "_clean_home") as clean:
            self.assertFalse(await self.runner.recover_job(self.job_id))
            clean.assert_not_called()
        self.assertTrue(self.own_record.is_file())
        self.assertFalse((self.own_record.parent / "stopped").exists())

    async def test_job_directory_symlink_cannot_read_outside_record(self):
        real = self.own_record.parent.parent.parent
        moved = real.with_name("retained-real-job")
        real.rename(moved)
        try:
            real.symlink_to(moved, target_is_directory=True)
        except OSError:
            self.skipTest("account cannot create symlink")
        try:
            with patch.object(self.runner, "terminate", AsyncMock(return_value=True)), patch("harness_mcp.desktop_runner._read_record") as read, patch("harness_mcp.desktop_runner.named_job_stopped", AsyncMock()) as stopped:
                self.assertFalse(await self.runner.recover_job(self.job_id))
                read.assert_not_called()
                stopped.assert_not_called()
        finally:
            real.unlink()  # 仅移除本测试创建的链接；不跟随其目标。


class DesktopWorkerContractTests(unittest.TestCase):
    def test_unowned_worker_refuses_before_adapter(self):
        with patch("harness_mcp.harness_adapter.run_harness") as adapter:
            result = execute({"_desktop_job_name": "Local\\deepseek-harness-" + "f" * 32 + "-" + "e" * 32})
        self.assertEqual(result["error"]["code"], "PROCESS_OWNERSHIP_INVALID")
        adapter.assert_not_called()

    def test_owned_edit_worker_independently_rejects_workspace_escape_before_adapter(self):
        with tempfile.TemporaryDirectory(prefix="native edit worker ") as temporary:
            root = Path(temporary).resolve()
            job = root / "job"
            snapshot, home, outside = job / "snapshot" / "files", job / "private-home", root / "outside"
            for directory in (snapshot, home, outside):
                directory.mkdir(parents=True)
            request = {"mode": "edit", "executor": {"backend": "desktop"}, "deadline_at": time.time() + 20,
                       "job_dir": str(job), "snapshot_dir": str(snapshot), "workspace_dir": str(outside),
                       "_desktop_home": str(home), "_desktop_job_name": "fixture-owned-name"}
            with patch("harness_mcp.desktop_worker.owned_by_job", return_value=True), patch("harness_mcp.harness_adapter.run_harness") as adapter:
                result = execute(request)
            self.assertEqual(result["error"]["code"], "INVALID_EXECUTOR_PATH")
            adapter.assert_not_called()

    def test_worker_result_exact_secret_redaction_and_encoding_limit(self):
        request = {"model": {"credential_env": "ARTIFICIAL_FIXTURE_KEY"}, "limits": {"max_output_bytes": 128}}
        with patch.dict(os.environ, {"ARTIFICIAL_FIXTURE_KEY": "test-only-long-credential"}):
            raw = encode_result({"raw_text": "test-only-long-credential中文", "error": None}, request)
            self.assertNotIn(b"test-only-long-credential", raw)
            self.assertIn("[REDACTED]", raw.decode("utf-8"))
            oversized = encode_result({"raw_text": "x" * 100000, "error": None}, request)
        self.assertEqual(json.loads(oversized)["error"]["code"], "OUTPUT_LIMIT")
