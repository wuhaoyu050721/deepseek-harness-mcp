"""Docker argv 与生命周期的受控 fixture；不等同于真实容器隔离验收。"""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harness_mcp.config import AppConfig, ExecutorConfig, ModelConfig
from harness_mcp.errors import HarnessError
from harness_mcp.runner import DockerRunner, RunState
from harness_mcp.supervisor import JOB_LABEL, OWNER_LABEL, RUN_LABEL, SERVICE_LABEL, docker_args, lease_stop_reason, stop_owned
from harness_mcp.worker import adapter_environment, publish_result, supervise_worker


class DummyWatchdog:
    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None


class FixtureDockerRunner(DockerRunner):
    """替身只模拟 daemon 状态变化；绝不启动模型或真正 Docker。"""

    def __init__(self, config, owner_id="test-owner", *, complete=True, stop_fails=False, on_credential=None):
        super().__init__(config, owner_id, on_credential=on_credential)
        self.commands = []
        self.endpoints = []
        self.endpoint = "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
        self.running = False
        self.exists = False
        self.complete = complete
        self.stop_fails = stop_fails
        self.container_id = "a" * 64
        self.labels = {}
        self.output = None
        self.polls = 0
        self.start_entered = asyncio.Event()
        self.start_release = None
        self.create_entered = asyncio.Event()
        self.create_release = None
        self.lost_create_ack = False
        self.output_text = '{"summary":"fixture only","findings":[]}'

    async def _start_watchdog(self, state):
        state.control.joinpath("watchdog.json").write_text(json.dumps({"owner_id": self.owner_id, "job_id": state.job_id, "run_id": state.run_id, "docker_command": state.executor["docker_command"], "docker_host": state.executor.get("docker_host"), "container_id": state.container_name}), encoding="utf-8")
        state.watchdog = DummyWatchdog()

    async def _command(self, command, args, *, env=None, timeout=8):
        if args[:1] == ["--host"]:
            self.endpoints.append(args[1])
            args = args[2:]
        self.commands.append((command, list(args), dict(env or {})))
        if args[:2] == ["context", "inspect"]:
            return 0, json.dumps(self.endpoint)
        if args[0] == "version":
            return 0, "linux"
        if args[:2] == ["image", "inspect"]:
            return 0, "sha256:" + "b" * 64
        if args[0] == "create":
            self.create_entered.set()
            if self.create_release is not None:
                await self.create_release.wait()
            for index, item in enumerate(args):
                if item == "--label":
                    key, value = args[index + 1].split("=", 1)
                    self.labels[key] = value
                if item == "--mount" and ",target=/output" in args[index + 1]:
                    self.output = Path(args[index + 1].split("source=", 1)[1].split(",target=", 1)[0])
            if self.lost_create_ack:
                raise HarnessError("EXECUTOR_UNAVAILABLE", "create response lost")
            self.exists = True
            return 0, self.container_id
        if args[0] == "start":
            self.start_entered.set()
            if self.start_release is not None:
                await self.start_release.wait()
            self.running = True
            return 0, self.container_id
        if args[0] == "inspect":
            if not self.exists:
                return 1, ""
            self.polls += 1
            if self.complete and self.running and self.polls >= 2:
                self.running = False
                self.output.joinpath("result.json").write_text(json.dumps({"raw_text": self.output_text, "finish_reason": "completed", "usage": None, "error": None}), encoding="utf-8")
            return 0, json.dumps({"id": self.container_id, "state": {"Running": self.running, "ExitCode": 0}, "labels": self.labels})
        if args[0] in {"stop", "kill"}:
            if self.stop_fails:
                return 1, ""
            self.running = False
            return 0, self.container_id
        if args[0] == "ps":
            if not self.exists:
                return 0, ""
            if "{{.ID}} {{.Names}}" in args:
                return 0, self.container_id
            return 0, self.container_id
        if args[0] == "rm":
            self.exists = False
            return 0, self.container_id
        raise AssertionError(args)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Harness 中文 空格 ")
        self.data = Path(self.temp.name)
        self.job = self.data / "jobs" / "one"
        self.snapshot = self.job / "snapshot" / "files"
        self.snapshot.mkdir(parents=True)
        self.snapshot.joinpath("sample.py").write_text("print('fixture')\n", encoding="utf-8")
        self.config = AppConfig(self.data, model=ModelConfig(credential_source="env"), executor=ExecutorConfig())
        self.request = {
            "job_id": "one", "project_id": "sample", "prompt": "只读审查",
            "timeout_seconds": 60, "snapshot_dir": str(self.snapshot), "job_dir": str(self.job),
            "model": asdict(self.config.model), "limits": {"max_output_bytes": 100000, "termination_grace_seconds": 1},
        }
        self.secret = "sk-fixture-secret-value-1234567890"
        self.env_patch = patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.secret, "UNRELATED_SECRET": "unrelated"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_worker_contract_rejects_untrusted_error_keys_and_field_types(self):
        runner = FixtureDockerRunner(self.config)
        output = self.job / "untrusted-result.json"
        valid = {"raw_text": "fixture", "finish_reason": "completed", "usage": {"input_tokens": 3}, "error": None}
        output.write_text(json.dumps({**valid, self.secret: "discard", "execution_stopped": False}), encoding="utf-8")
        self.assertEqual(runner._result_file(output, 100000), valid)
        output.write_text(json.dumps({**valid, "error": {"code": "WORKER_ERROR", "message": "fixture"}}), encoding="utf-8")
        self.assertEqual(runner._result_file(output, 100000)["error"]["code"], "WORKER_ERROR")
        for updates in (
            {"error": {"code": "WORKER_ERROR", "message": "fixture", self.secret: "discard"}},
            {"error": {"code": "WORKER_ERROR", "message": "fixture", "details": {}}},
            {"error": {"code": self.secret, "message": "fixture"}},
            {"error": {"code": "WORKER_ERROR", "message": []}},
            {"raw_text": []}, {"finish_reason": self.secret + "!"}, {"usage": []},
        ):
            with self.subTest(fields=list(updates)):
                output.write_text(json.dumps({**valid, **updates}), encoding="utf-8")
                with self.assertRaises(HarnessError) as caught:
                    runner._result_file(output, 100000)
                self.assertEqual(caught.exception.code, "WORKER_RESULT_INVALID")
                self.assertNotIn(self.secret, str(caught.exception))

    async def test_bounded_success_and_secret_does_not_enter_argv_or_disk(self):
        runner = FixtureDockerRunner(self.config)
        stages = []
        runner.output_text = self.secret + " fixture only"
        result = await runner.run(self.request, stages.append)
        self.assertTrue(result["execution_stopped"])
        self.assertIsNone(result["error"])
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertIn("[REDACTED]", result["raw_text"])
        self.assertEqual(stages, ["checking_executor", "creating_container", "running"])
        create = next(item for item in runner.commands if item[1][0] == "create")
        self.assertNotIn(self.secret, json.dumps(create[1]))
        self.assertEqual(create[2]["DEEPSEEK_API_KEY"], self.secret)
        self.assertNotIn("UNRELATED_SECRET", create[2])
        for task in self.job.glob("control_worker/*/task.json"):
            self.assertNotIn(self.secret, task.read_text(encoding="utf-8"))
        self.assertFalse(runner.exists)
        self.assertFalse(runner._active)
        self.assertTrue(runner.endpoints)
        self.assertEqual(set(runner.endpoints), {runner.endpoint})

    async def test_missing_docker_fails_closed_before_credentials_or_model(self):
        runner = DockerRunner(self.config, "owner")
        async def unavailable(*args, **kwargs):
            raise HarnessError("EXECUTOR_UNAVAILABLE", "Docker unavailable")
        with patch.object(runner, "_command", side_effect=unavailable), patch("harness_mcp.runner.resolve_credential") as credential:
            result = await runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "EXECUTOR_UNAVAILABLE")
        self.assertTrue(result["execution_stopped"])
        credential.assert_not_called()
        self.assertFalse(runner._active)

    async def test_actual_rotated_credential_registered_before_container_creation(self):
        observed = []
        def registered(value):
            self.assertFalse(any(command[1][0] == "create" for command in runner.commands))
            self.assertFalse(list(self.job.glob("control_worker/*/task.json")))
            observed.append(value)
        runner = FixtureDockerRunner(self.config, on_credential=registered)
        rotated = "rotated-opaque-artificial-docker-value"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": rotated}):
            result = await runner.run(self.request, lambda stage: None)
        self.assertIsNone(result["error"])
        self.assertEqual(observed, [rotated])
        create = next(item for item in runner.commands if item[1][0] == "create")
        self.assertEqual(create[2]["DEEPSEEK_API_KEY"], rotated)
        self.assertNotIn(rotated, json.dumps(create[1]))
        for path in self.job.rglob("*.json"):
            self.assertNotIn(rotated, path.read_text(encoding="utf-8"))

    async def test_credential_registration_failure_prevents_container_creation(self):
        def rejected(_value):
            raise RuntimeError("artificial registration failure")
        runner = FixtureDockerRunner(self.config, on_credential=rejected)
        result = await runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "EXECUTOR_FAILED")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(any(command[1][0] == "create" for command in runner.commands))
        self.assertFalse(runner._active)

    async def test_timeout_stops_whole_container_and_confirms(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        self.request["deadline_at"] = time.time() + 0.05
        result = await runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "TIMED_OUT")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(runner.running)
        self.assertTrue(any(command[1][0] == "stop" for command in runner.commands))

    async def test_cancellation_race_during_start_waits_for_actual_stop(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        runner.start_release = asyncio.Event()
        running = asyncio.create_task(runner.run(self.request, lambda stage: None))
        await asyncio.wait_for(runner.start_entered.wait(), 2)
        cancellation = asyncio.create_task(runner.terminate("one"))
        await asyncio.sleep(0)
        self.assertFalse(cancellation.done())
        runner.start_release.set()
        self.assertTrue(await asyncio.wait_for(cancellation, 2))
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(result["error"]["code"], "CANCELLED")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(runner.running)

    async def test_cancellation_waits_for_pending_create_and_never_starts_model(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        runner.create_release = asyncio.Event()
        running = asyncio.create_task(runner.run(self.request, lambda stage: None))
        await asyncio.wait_for(runner.create_entered.wait(), 2)
        cancellation = asyncio.create_task(runner.terminate("one"))
        await asyncio.sleep(0)
        self.assertFalse(cancellation.done())
        runner.create_release.set()
        self.assertTrue(await asyncio.wait_for(cancellation, 2))
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(result["error"]["code"], "CANCELLED")
        self.assertTrue(result["execution_stopped"])
        self.assertFalse(any(command[1][0] == "start" for command in runner.commands))
        self.assertFalse(runner.exists)

    async def test_lost_create_reply_preserves_slot_until_late_owned_resource_confirmed(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        runner.lost_create_ack = True
        result = await runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "TERMINATION_FAILED")
        self.assertFalse(result["execution_stopped"])
        self.assertIn("one", runner._active)
        self.assertFalse(await runner.recover())
        self.assertFalse(any(command[1][0] == "start" for command in runner.commands))
        runner.exists = True  # daemon 在 CLI 失联后才落地，尚未启动任何进程。
        self.assertTrue(await runner.recover())
        self.assertFalse(runner._active)
        self.assertFalse(runner.exists)

    async def test_stop_failure_keeps_owner_and_blocks_new_execution(self):
        runner = FixtureDockerRunner(self.config, complete=False, stop_fails=True)
        self.request["deadline_at"] = time.time() + 0.05
        result = await runner.run(self.request, lambda stage: None)
        self.assertEqual(result["error"]["code"], "TERMINATION_FAILED")
        self.assertFalse(result["execution_stopped"])
        self.assertIn("one", runner._active)
        again = await runner.run(self.request, lambda stage: None)
        self.assertEqual(again["error"]["code"], "EXECUTOR_BUSY")
        self.assertFalse(again["execution_stopped"])
        self.assertFalse(await runner.close())
        runner.stop_fails = False
        self.assertTrue(await runner.close())

    async def test_ownership_mismatch_never_stops_foreign_container(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        runner.exists = runner.running = True
        runner.labels = {OWNER_LABEL: "foreign", RUN_LABEL: "r", JOB_LABEL: "j", SERVICE_LABEL: "1"}
        state = RunState("j", "r", "foreign", self.data, asdict(self.config.executor), time.time() + 60, runner.container_id)
        self.assertFalse(await runner._stop_state(state))
        self.assertFalse(any(command[1][0] in {"stop", "kill", "rm"} for command in runner.commands))

    async def test_recovery_filters_and_validates_persistent_owner(self):
        runner = FixtureDockerRunner(self.config, complete=False)
        runner.exists = runner.running = True
        runner.labels = {OWNER_LABEL: runner.owner_id, RUN_LABEL: "old-run", JOB_LABEL: "old-job", SERVICE_LABEL: "1"}
        self.assertTrue(await runner.recover())
        first_args = next(command[1] for command in runner.commands if command[1][0] == "ps")
        self.assertIn(f"label={OWNER_LABEL}={runner.owner_id}", first_args)
        self.assertIn(f"label={SERVICE_LABEL}=1", first_args)
        self.assertFalse(runner.running)
        self.assertFalse(runner.exists)

    async def test_remote_or_tcp_endpoint_rejected_before_credentials(self):
        for endpoint in ("tcp://example.com:2376", "tcp://127.0.0.1:2375", "ssh://host", "npipe:////server/pipe/docker_engine"):
            with self.subTest(endpoint=endpoint):
                runner = FixtureDockerRunner(self.config)
                runner.endpoint = endpoint
                with patch("harness_mcp.runner.resolve_credential") as credential:
                    result = await runner.run(self.request, lambda stage: None)
                self.assertEqual(result["error"]["code"], "EXECUTOR_ENDPOINT_UNSAFE")
                self.assertTrue(result["execution_stopped"])
                credential.assert_not_called()
                self.assertFalse(any(command[1][0] == "create" for command in runner.commands))

    async def test_recovery_checks_frozen_old_endpoint_after_context_switch(self):
        runner = FixtureDockerRunner(self.config)
        old_endpoint = "npipe:////./pipe/docker_old" if os.name == "nt" else "unix:///run/docker-old.sock"
        control = self.job / "control_host" / "old-run"
        control.mkdir(parents=True)
        control.joinpath("watchdog.json").write_text(json.dumps({"owner_id": runner.owner_id, "run_id": "old-run", "docker_command": "docker-original", "docker_host": old_endpoint}), encoding="utf-8")
        self.assertTrue(await runner.recover())
        self.assertIn(old_endpoint, runner.endpoints)
        self.assertTrue(any(command[0] == "docker-original" and command[1][0] == "ps" for command in runner.commands))
        runner.commands.clear(); runner.endpoints.clear()
        control.joinpath("confirmed_stopped.json").write_text(json.dumps({"run_id": "old-run", "execution_stopped": True}), encoding="utf-8")
        self.assertTrue(await runner.recover())
        self.assertNotIn(old_endpoint, runner.endpoints)

    def test_argv_uses_exact_four_mounts_and_resource_boundary(self):
        runner = DockerRunner(self.config, "owner")
        state = RunState("j", "r", "container", self.data / "lease-unmounted", asdict(self.config.executor), time.time() + 60)
        args = runner.build_create_args(state, snapshot=self.snapshot, worker_control=self.job / "workercontrol", home=self.job / "home", output=self.job / "output", credential_env="DEEPSEEK_API_KEY")
        mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]
        self.assertEqual(len(mounts), 4)
        self.assertTrue(next(item for item in mounts if "target=/source" in item).endswith(",readonly"))
        self.assertTrue(next(item for item in mounts if "target=/control" in item).endswith(",readonly"))
        self.assertNotIn(str(state.control), " ".join(mounts))
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertNotIn("--privileged", args)
        self.assertIn("10001:10001", args)
        self.assertIn("ALL", args)
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges:true", args)
        for option in ("--cpus", "--memory", "--memory-swap", "--pids-limit", "--tmpfs"):
            self.assertIn(option, args)
        self.assertEqual(args[args.index("--network") + 1], "bridge")
        self.assertEqual(args[-1], self.config.executor.image)

    def test_mount_comma_rejected_and_symlink_result_rejected(self):
        runner = DockerRunner(self.config, "owner")
        with self.assertRaises(HarnessError):
            runner._mount(self.data / "comma,path", "/source", True)
        external = self.data / "outside.json"; external.write_text("{}")
        link = self.data / "result.json"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("本机不允许创建 symbolic link")
        with self.assertRaises(HarnessError):
            runner._result_file(link, 1000)


class WatchdogWorkerTests(unittest.TestCase):
    def test_watchdog_missing_pending_create_is_not_stop_confirmation(self):
        with patch("harness_mcp.supervisor.inspect_owned", return_value=None):
            self.assertFalse(stop_owned({"creation_unresolved": True}))
            self.assertTrue(stop_owned({"creation_unresolved": False}))
        with patch("harness_mcp.supervisor.inspect_owned", return_value={"state": {"Running": False}}):
            self.assertTrue(stop_owned({"creation_unresolved": True}))

    def test_watchdog_uses_frozen_endpoint_for_every_command(self):
        self.assertEqual(docker_args({"docker_host": "unix:///var/run/docker.sock"}, ["stop", "id"]), ["--host", "unix:///var/run/docker.sock", "stop", "id"])

    def test_lease_binding_expiration_and_cancel(self):
        self.assertIsNone(lease_stop_reason({"run_id": "r", "expires_at": 105}, run_id="r", now=100, lease_seconds=5))
        for lease in ({"run_id": "other", "expires_at": 105}, {"run_id": "r", "expires_at": 99}, {"run_id": "r", "expires_at": 1000}):
            self.assertEqual(lease_stop_reason(lease, run_id="r", now=100, lease_seconds=5), "lease_lost")
        self.assertEqual(lease_stop_reason({"run_id": "r", "cancel_requested": True}, run_id="r", now=100, lease_seconds=5), "cancelled")

    def test_worker_rejects_host_execution_and_strips_environment(self):
        with self.assertRaises(RuntimeError):
            supervise_worker()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-value", "UNRELATED_SECRET": "other"}):
            env = adapter_environment({"model": {"credential_env": "DEEPSEEK_API_KEY"}})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "test-value")
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertEqual(env["DSH_HOME"], "/home/harness")

    def test_worker_result_is_bounded_and_exact_secret_redacted(self):
        with tempfile.TemporaryDirectory() as directory, patch("harness_mcp.worker.OUTPUT_DIR", Path(directory)), patch.dict(os.environ, {"KEY": "sensitive-credential"}):
            request = {"model": {"credential_env": "KEY"}, "limits": {"max_output_bytes": 100}}
            publish_result({"raw_text": "sensitive-credential", "error": None}, request)
            text = Path(directory, "result.json").read_text(encoding="utf-8")
            self.assertNotIn("sensitive-credential", text)
            self.assertIn("[REDACTED]", text)
            publish_result({"raw_text": "x" * 100000, "error": None}, request)
            self.assertEqual(json.loads(Path(directory, "result.json").read_text(encoding="utf-8"))["error"]["code"], "OUTPUT_LIMIT")

    def test_independent_monitor_expired_lease_stops_fixture_without_parent_heartbeat(self):
        # 真实独立 Python 进程 + 磁盘租约，Docker 调用仍为受控 fixture。
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            config = {"owner_id": "owner", "run_id": "r", "container_id": "a" * 64, "docker_command": "fixture", "lease_seconds": 3, "deadline_at": time.time() + 10, "grace_seconds": 1}
            control.joinpath("watchdog.json").write_text(json.dumps(config))
            control.joinpath("lease.json").write_text(json.dumps({"run_id": "r", "expires_at": time.time() + 0.3, "container_started": True}))
            control.joinpath("daemon.json").write_text(json.dumps({"Running": True}))
            child_code = """
import json, pathlib, subprocess, sys
import harness_mcp.supervisor as s
control=pathlib.Path(sys.argv[1]); cfg=json.loads((control/'watchdog.json').read_text())
def fixture(command,args,timeout=5):
    path=control/'daemon.json'; state=json.loads(path.read_text())
    if args[0]=='inspect':
        text=json.dumps({'id':cfg['container_id'],'state':state,'labels':{s.OWNER_LABEL:'owner',s.RUN_LABEL:'r',s.SERVICE_LABEL:'1'}})
    elif args[0] in {'stop','kill'}:
        state['Running']=False; path.write_text(json.dumps(state)); text=cfg['container_id']
    else: text=''
    return subprocess.CompletedProcess([command,*args],0,text,'')
s.docker_call=fixture
sys.exit(s.monitor(control))
"""
            child = subprocess.Popen([sys.executable, "-c", child_code, directory], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                _, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr.decode(errors="replace"))
                stopped = json.loads(control.joinpath("stopped.json").read_text())
                self.assertTrue(stopped["execution_stopped"])
                self.assertEqual(stopped["reason"], "lease_lost")
                self.assertFalse(json.loads(control.joinpath("daemon.json").read_text())["Running"])
            finally:
                if child.poll() is None:
                    child.kill(); child.wait()


if __name__ == "__main__":
    unittest.main()
