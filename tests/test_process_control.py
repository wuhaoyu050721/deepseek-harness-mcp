"""实际进程验证：父退出后子孙仍活、正常返回清理、超限停止与归属失败。"""

import asyncio
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from harness_mcp.errors import HarnessError
from harness_mcp.process_control import spawn_owned, stop_process_tree


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("artificial process fixture did not become ready")


def create_family(tmp_path: Path) -> tuple[list[str], Path, Path]:
    child_marker, grandchild_marker = tmp_path / "child.pid", tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text("import os,pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(100)\n", encoding="utf-8")
    child = tmp_path / "child.py"
    child.write_text("import os,pathlib,subprocess,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\nsubprocess.Popen([sys.executable,sys.argv[2],sys.argv[3]])\ntime.sleep(100)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text("import pathlib,subprocess,sys,time\nsubprocess.Popen([sys.executable,*sys.argv[1:]])\nmarker=pathlib.Path(sys.argv[4])\nwhile not marker.exists():time.sleep(0.01)\nprint('parent-result',flush=True)\n", encoding="utf-8")
    return [sys.executable, str(parent), str(child), str(child_marker), str(grandchild), str(grandchild_marker)], child_marker, grandchild_marker


class WindowsProcessHandles:
    def __init__(self, pids, *, allow_terminate=False):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.allow_terminate = allow_terminate
        access = 0x00100000 | (0x0001 if allow_terminate else 0)
        self.handles = [self.kernel.OpenProcess(access, False, pid) for pid in pids]
        assert all(self.handles)

    def all_exited(self):
        return all(self.kernel.WaitForSingleObject(handle, 0) == 0 for handle in self.handles)

    def close(self):
        for handle in self.handles:
            self.kernel.CloseHandle(handle)

    def terminate_if_running(self):
        # 仅测试兜底使用已持有的精确进程句柄，绝不按保存的 PID 再打开/清理。
        assert self.allow_terminate
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 0) != 0:
                assert self.kernel.TerminateProcess(handle, 1)


@pytest.mark.skipif(os.name != "nt", reason="Windows JobObject real descendant assertion")
def test_parent_exit_does_not_mean_child_and_grandchild_stopped(tmp_path):
    argv, child_marker, grandchild_marker = create_family(tmp_path)
    owned = spawn_owned(argv, dict(os.environ))
    handles = None
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(100)"], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        wait_for(lambda: grandchild_marker.exists() and owned.returncode is not None)
        assert owned.returncode == 0
        assert not owned.tree_stopped
        assert owned.active_count() >= 2
        handles = WindowsProcessHandles([int(child_marker.read_text()), int(grandchild_marker.read_text())])
        assert not handles.all_exited()
        assert asyncio.run(stop_process_tree(owned))
        assert owned.tree_stopped
        assert owned.active_count() == 0
        assert handles.all_exited()
        assert unrelated.poll() is None  # 未按名称/所有 Python 进程清理。
        owned.close()
    finally:
        owned.stop()
        if owned.tree_stopped:
            owned.close()
        if handles:
            handles.close()
        unrelated.kill()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows real family normal communication assertion")
def test_normal_parent_result_cleans_up_live_descendants(tmp_path):
    argv, _, grandchild_marker = create_family(tmp_path)
    owned = spawn_owned(argv, dict(os.environ))
    try:
        output, error = owned.communicate()
        assert output.strip() == b"parent-result"
        assert error is None
        assert grandchild_marker.exists()
        assert owned.returncode == 0
        assert owned.tree_stopped
        assert owned.active_count() == 0
        assert asyncio.run(stop_process_tree(owned))
    finally:
        owned.stop()
        if owned.tree_stopped:
            owned.close()


def test_binary_communication_returns_only_after_tree_stop():
    owned = spawn_owned([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"], dict(os.environ))
    data = "中文输入".encode() * 20000
    output, error = owned.communicate(data)
    assert output == data
    assert error is None
    assert owned.returncode == 0
    assert owned.tree_stopped
    assert owned.active_count() == 0


def test_output_limit_terminates_writer_and_reports_fixed_safe_error():
    owned = spawn_owned([sys.executable, "-c", "import sys,time;sys.stdout.buffer.write(b'x'*1000000);sys.stdout.buffer.flush();time.sleep(100)"], dict(os.environ), max_output=1024)
    try:
        with pytest.raises(HarnessError) as caught:
            owned.communicate()
        assert caught.value.code == "SNAPSHOT_OUTPUT_LIMIT"
        assert owned.tree_stopped
        assert owned.active_count() == 0
        assert owned.returncode is not None
    finally:
        owned.stop()
        if owned.tree_stopped:
            owned.close()


def test_close_cannot_release_unconfirmed_running_tree():
    owned = spawn_owned([sys.executable, "-c", "import time;time.sleep(100)"], dict(os.environ))
    try:
        with pytest.raises(HarnessError) as caught:
            owned.close()
        assert caught.value.code == "TERMINATION_FAILED"
        assert not owned.tree_stopped
        assert owned.returncode is None
        assert asyncio.run(stop_process_tree(owned))
        assert asyncio.run(stop_process_tree(owned))
    finally:
        owned.stop()
        if owned.tree_stopped:
            owned.close()


def test_plain_process_object_is_not_trusted_for_tree_stop():
    class Unowned:
        returncode = 0
        pid = os.getpid()
    assert not asyncio.run(stop_process_tree(Unowned()))


def test_spawn_failure_does_not_echo_argv_or_environment():
    with pytest.raises(HarnessError) as caught:
        spawn_owned(["does-not-exist-secret-command"], {"API_KEY": "secret-value"})
    assert caught.value.code == "PREPARATION_PROCESS_UNAVAILABLE"
    assert "secret" not in str(caught.value)


def test_embedded_nul_arguments_cannot_truncate_child_command(tmp_path):
    marker = tmp_path / "truncated-command-must-not-run"
    with pytest.raises(HarnessError):
        spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()",
                     str(marker), "\0ignored"], dict(os.environ))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed suspension assertion")
def test_atomic_job_creation_failure_never_runs_child(tmp_path, monkeypatch):
    from harness_mcp import process_control
    marker = tmp_path / "must-not-exist.txt"

    def failed_creation(self, command_line, env, handles):
        raise OSError("synthetic atomic creation failure")

    monkeypatch.setattr(process_control._WindowsJob, "create_suspended", failed_creation)
    with pytest.raises(HarnessError):
        spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran')", str(marker)], dict(os.environ))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic Job attribute failure")
def test_unsupported_job_attribute_fails_before_process_creation(tmp_path, monkeypatch):
    from harness_mcp import process_control
    marker = tmp_path / "unsupported-must-not-run"
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    initialize = process_control._WindowsJob.__init__
    attempted = []

    def unsupported_job(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        update = self.kernel.UpdateProcThreadAttribute

        def update_attribute(attributes, flags, attribute, values, size, previous, result):
            if attribute == 0x0002000D:
                ctypes.set_last_error(50)  # ERROR_NOT_SUPPORTED
                return False
            return update(attributes, flags, attribute, values, size, previous, result)

        def must_not_create(*args):
            attempted.append(True)
            raise AssertionError("unsupported Job attribute must not fall back to ordinary creation")

        self.kernel.UpdateProcThreadAttribute = update_attribute
        self.kernel.CreateProcessW = must_not_create

    monkeypatch.setattr(process_control._WindowsJob, "__init__", unsupported_job)
    with pytest.raises(HarnessError) as caught:
        spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
                    dict(os.environ), job_name=name)
    assert caught.value.code == "PREPARATION_PROCESS_UNAVAILABLE"
    assert not attempted
    assert not marker.exists()
    monkeypatch.setattr(process_control._WindowsJob, "__init__", initialize)
    assert asyncio.run(process_control.named_job_stopped(name))


@pytest.mark.skipif(os.name != "nt", reason="Windows exact primary-thread recovery failure")
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_resume_failure_terminates_suspended_job_child(tmp_path, monkeypatch, failure):
    from harness_mcp import process_control
    marker = tmp_path / "resume-failure-must-not-run"
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    retained = []
    jobs = []

    def failed_resume(self, thread):
        assert self.active_count() == 1  # 主线程尚未运行，子进程已属于 Job。
        retained.extend(self.process_handles())
        jobs.append(self)
        raise failure("synthetic resume failure")

    monkeypatch.setattr(process_control._WindowsJob, "resume_primary_thread", failed_resume)
    try:
        with pytest.raises(HarnessError if failure is OSError else KeyboardInterrupt):
            spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
                        dict(os.environ), job_name=name)
        assert len(retained) == 1
        wait_for(lambda: jobs[0].handles_exited(retained))
        assert not marker.exists()
        assert asyncio.run(process_control.named_job_stopped(name))
    finally:
        if jobs:
            jobs[0].release_process_handles(retained)


@pytest.mark.skipif(os.name != "nt", reason="Windows native post-create handle ownership")
def test_attribute_cleanup_interrupt_closes_untransferred_native_handles(tmp_path, monkeypatch):
    from harness_mcp import process_control
    import _winapi
    marker = tmp_path / "native-handle-failure-must-not-run"
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    initialize = process_control._WindowsJob.__init__
    native_handles = []
    retained = []
    jobs = []

    def interrupted_cleanup(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        jobs.append(self)
        create = self.kernel.CreateProcessW
        delete = self.kernel.DeleteProcThreadAttributeList

        def capture_created(*args):
            create.argtypes = capture_created.argtypes
            create.restype = wintypes.BOOL
            result = create(*args)
            if result:
                info = args[-1]._obj
                native_handles.extend((info.hProcess, info.hThread))
                current = _winapi.GetCurrentProcess()
                retained.append(_winapi.DuplicateHandle(current, info.hProcess, current, 0, False,
                                                       _winapi.DUPLICATE_SAME_ACCESS))
            return result

        def fail_after_delete(attributes):
            delete.argtypes = [ctypes.c_void_p]
            delete.restype = None
            delete(attributes)
            raise KeyboardInterrupt("synthetic interrupt after native creation")

        self.kernel.CreateProcessW = capture_created
        self.kernel.DeleteProcThreadAttributeList = fail_after_delete

    monkeypatch.setattr(process_control._WindowsJob, "__init__", interrupted_cleanup)
    try:
        with pytest.raises(KeyboardInterrupt):
            spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
                        dict(os.environ), job_name=name)
        assert len(native_handles) == 2 and len(retained) == 1
        kernel = jobs[0].kernel
        kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetHandleInformation.restype = wintypes.BOOL
        for handle in native_handles:
            flags = wintypes.DWORD()
            assert not kernel.GetHandleInformation(handle, ctypes.byref(flags))
            assert ctypes.get_last_error() == 6  # ERROR_INVALID_HANDLE
        assert _winapi.WaitForSingleObject(retained[0], 5000) == _winapi.WAIT_OBJECT_0
        assert not marker.exists()
        monkeypatch.setattr(process_control._WindowsJob, "__init__", initialize)
        assert asyncio.run(process_control.named_job_stopped(name))
    finally:
        for handle in retained:
            _winapi.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows fresh host real preparation/collection compatibility")
def test_fresh_host_runs_snapshot_and_edit_collection_with_real_manager_commands(tmp_path):
    fixture = r'''
import asyncio, json, subprocess, sys
from dataclasses import asdict
from pathlib import Path
from harness_mcp.config import AppConfig, ProjectConfig
from harness_mcp.task_manager import TaskManager
class NoModelRunner:
    async def close(self): return True
    async def recover(self): return True
async def check():
    root = Path(sys.argv[1])
    project_path = root / "source"
    project_path.mkdir()
    before, after = "value = 1\n", "value = 2\n"
    (project_path / 'sample.py').write_bytes(before.encode())
    subprocess.run(['git', 'init', '-q', str(project_path)], check=True)
    subprocess.run(['git', '-C', str(project_path), 'add', 'sample.py'], check=True)
    project = ProjectConfig('fixture', project_path, allow_edits=True)
    config = AppConfig(root / 'state', {'fixture': project})
    manager = TaskManager(config, NoModelRunner())
    project_fields = asdict(project)
    project_fields['path'] = str(project_path)
    try:
        for number in range(3):
            job = {'job_id': f'worker-cycle-{number}', 'request': {'mode': 'edit', 'relative_paths': ['sample.py']},
                   'execution': {'project': project_fields, 'limits': asdict(config.limits)}}
            directory = config.data_dir / 'jobs' / job['job_id']
            directory.mkdir(parents=True)
            snapshot = await manager._snapshot(job, directory)
            workspace = directory / 'workspace'
            (workspace / 'sample.py').write_bytes(after.encode())
            changes = await manager._edit_changes(job, snapshot, workspace)
            assert changes['changes'][0]['after_text'] == after
            assert (project_path / 'sample.py').read_bytes() == before.encode()
            assert manager.preparation_process is None
        print(json.dumps({'ok': True, 'snapshot_workers': 3, 'collection_workers': 3}))
    finally:
        await manager.close()
        assert manager.closed
asyncio.run(check())
'''
    result = subprocess.run([sys.executable, "-c", fixture, str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert __import__("json").loads(result.stdout) == {"ok": True, "snapshot_workers": 3, "collection_workers": 3}


@pytest.mark.skipif(os.name != "nt", reason="Windows real native process creation owner-crash boundary")
def test_owner_crash_after_atomic_creation_before_resume_has_no_orphan(tmp_path):
    from harness_mcp.process_control import named_job_stopped
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    child_pid = tmp_path / "suspended.pid"
    must_not_run = tmp_path / "must-not-run"
    control = tmp_path / "owner-at-create.py"
    control.write_text(
        "import os,pathlib,sys,time\n"
        "from harness_mcp.process_control import spawn_owned,_WindowsJob\n"
        "original=_WindowsJob.create_suspended\n"
        "def pause_after_create(self,*args):\n"
        "    info=original(self,*args)\n"
        "    pathlib.Path(sys.argv[2]).write_text(str(info[2]))\n"
        "    time.sleep(100)\n"
        "    return info\n"
        "_WindowsJob.create_suspended=pause_after_create\n"
        "spawn_owned([sys.executable,'-c','import pathlib,sys;pathlib.Path(sys.argv[1]).touch()',sys.argv[3]],"
        "dict(os.environ),job_name=sys.argv[1])\n", encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(control), name, str(child_pid), str(must_not_run)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    handles = None
    try:
        wait_for(child_pid.exists)
        handles = WindowsProcessHandles([int(child_pid.read_text())], allow_terminate=True)
        assert not handles.all_exited()
        assert not must_not_run.exists()
        assert not asyncio.run(named_job_stopped(name))
        owner.kill()
        owner.wait(timeout=5)
        wait_for(handles.all_exited)
        wait_for(lambda: asyncio.run(named_job_stopped(name)))
        assert not must_not_run.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if handles:
            handles.terminate_if_running()
            handles.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows stop confirmation retry assertion")
def test_unconfirmed_stop_keeps_wait_handles_for_retry(monkeypatch):
    owned = spawn_owned([sys.executable, "-c", "import time;time.sleep(100)"], dict(os.environ))
    checker = owned._job.handles_exited
    try:
        monkeypatch.setattr(owned._job, "handles_exited", lambda handles: False)
        assert not owned.stop(timeout=0.05)
        assert not owned.tree_stopped
        assert owned._wait_handles
        with pytest.raises(HarnessError):
            owned.close()
        monkeypatch.setattr(owned._job, "handles_exited", checker)
        assert owned.stop()
        assert owned.tree_stopped
        assert not owned._wait_handles
        owned.close()
    finally:
        monkeypatch.setattr(owned._job, "handles_exited", checker)
        owned.stop()
        if owned.tree_stopped:
            owned.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows named JobObject recovery")
def test_named_job_queries_are_read_only_and_duplicate_ownership_is_rejected(tmp_path):
    from harness_mcp.process_control import named_job_stopped
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    owned = spawn_owned([sys.executable, "-c", "import time;time.sleep(100)"], dict(os.environ), job_name=name)
    marker = tmp_path / "duplicate-must-not-run"
    try:
        assert not asyncio.run(named_job_stopped(name))
        assert owned.returncode is None
        with pytest.raises(HarnessError):
            spawn_owned([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)], dict(os.environ), job_name=name)
        assert not marker.exists()
        assert owned.returncode is None
        assert asyncio.run(stop_process_tree(owned))
        assert asyncio.run(named_job_stopped(name))
        assert not asyncio.run(named_job_stopped("Local\\arbitrary-unowned-job"))
    finally:
        owned.stop()
        if owned.tree_stopped:
            owned.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows actual owner crash kill-on-close")
def test_owner_crash_stops_named_job_descendants(tmp_path):
    from harness_mcp.process_control import named_job_stopped
    argv, child_marker, grandchild_marker = create_family(tmp_path)
    name = f"Local\\deepseek-harness-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    control = tmp_path / "owner.py"
    control.write_text("import json,os,sys,time\nfrom harness_mcp.process_control import spawn_owned\nowned=spawn_owned(json.loads(sys.argv[1]),dict(os.environ),job_name=sys.argv[2])\ntime.sleep(100)\n", encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(control), __import__('json').dumps(argv), name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    handles = None
    try:
        wait_for(grandchild_marker.exists)
        handles = WindowsProcessHandles([int(child_marker.read_text()), int(grandchild_marker.read_text())])
        assert not asyncio.run(named_job_stopped(name))
        owner.kill()
        owner.wait(timeout=5)
        wait_for(handles.all_exited)
        wait_for(lambda: asyncio.run(named_job_stopped(name)))
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if handles:
            handles.close()
