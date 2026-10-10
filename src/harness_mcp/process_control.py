"""准备和桌面执行进程的所有权与停止确认，不按程序名称或裸 PID 清理。

Windows 在创建子进程时原子加入 kill-on-close JobObject；父退出不能证明
整棵树停止。POSIX 使用独立 session/process group 并确认组消失。
此组件监管生命周期，不提供文件系统访问隔离。
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import os
import re
import signal
import subprocess
import sys
import threading
import time

from .errors import HarnessError


class _WindowsJob:
    def __init__(self, name: str | None = None, *, existing: bool = False) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS)] + [
                (name, ctypes.c_size_t) for name in ("ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed")
            ]

        class ACCOUNTING(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in (
                "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime",
            )] + [(name, wintypes.DWORD) for name in (
                "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses",
            )]

        self.ACCOUNTING = ACCOUNTING
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        if existing:
            self.kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.OpenJobObjectW.restype = wintypes.HANDLE
            self.handle = self.kernel.OpenJobObjectW(0x0004, False, name)  # 只查询，不能终止旧进程。
        else:
            ctypes.set_last_error(0)
            self.handle = self.kernel.CreateJobObjectW(None, name)
            if self.handle and name and ctypes.get_last_error() == 183:
                self.close()
                raise RuntimeError("job ownership name already exists")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if existing:
            return
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def resume_primary_thread(self, thread: int) -> None:
        """恢复 CreateProcess 返回的精确主线程句柄，不枚举或打开裸线程编号。"""
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        if self.kernel.ResumeThread(thread) != 1:
            raise RuntimeError("unexpected child suspension count")

    def create_suspended(self, command_line: str, env: dict[str, str], handles: list[int]):
        """创建与 Job 归属由同一次系统调用完成；不支持时拒绝创建。

        CPython 3.11--3.14 的 _winapi 仅接受 handle_list，不能传入 job_list。
        此处只替换进程创建；标准管道、poll/wait/kill 仍由 Popen 管理。
        """
        class STARTUPINFO(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR)] + [
                (name, wintypes.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize",
                    "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")
            ] + [("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                 ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                 ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

        class STARTUPINFOEX(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        self.kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                                 wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self.kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        self.kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self.kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        self.kernel.DeleteProcThreadAttributeList.restype = None
        self.kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOEX), ctypes.POINTER(PROCESS_INFORMATION)]
        self.kernel.CreateProcessW.restype = wintypes.BOOL

        if not handles or any(not handle or handle == -1 for handle in handles):
            raise RuntimeError("standard pipe handles unavailable")
        entries = []
        for key, value in env.items():
            if (not isinstance(key, str) or not isinstance(value, str) or not key
                    or "\0" in key or "\0" in value or "=" in key[1:]):
                raise ValueError("invalid child environment")
            entries.append((key, value))
        # Windows 环境块必须有序、双 NUL 结尾；不记录或回显值。
        entries.sort(key=lambda entry: entry[0].upper())
        environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in entries) + "\0\0")
        command = ctypes.create_unicode_buffer(command_line)
        inherited = (wintypes.HANDLE * len(handles))(*handles)
        jobs = (wintypes.HANDLE * 1)(self.handle)
        size = ctypes.c_size_t()
        ctypes.set_last_error(0)
        if (self.kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
                or ctypes.get_last_error() != 122 or not size.value):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        if not self.kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        info = PROCESS_INFORMATION()
        try:
            try:
                for attribute, values in ((0x00020002, inherited), (0x0002000D, jobs)):
                    if not self.kernel.UpdateProcThreadAttribute(attributes, 0, attribute, values,
                            ctypes.sizeof(values), None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                startup = STARTUPINFOEX()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = handles
                startup.lpAttributeList = ctypes.addressof(attributes)
                # Job 句柄不在继承白名单，宿主退出时最后一个句柄关闭会杀掉挂起子进程。
                flags = subprocess.CREATE_NO_WINDOW | 0x4 | 0x80000 | 0x400
                if not self.kernel.CreateProcessW(None, command, None, None, True, flags,
                        environment, None, ctypes.byref(startup), ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.DeleteProcThreadAttributeList(attributes)
            # 属性清理也成功后才把两个原生句柄交给 Popen。
            return info.hProcess, info.hThread, info.dwProcessId
        except BaseException:
            try:
                if info.hProcess:
                    self.terminate()
                    self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                    self.kernel.WaitForSingleObject.restype = wintypes.DWORD
                    if self.kernel.WaitForSingleObject(info.hProcess, 5000) != 0:
                        raise RuntimeError("created child stop unconfirmed")
            finally:
                if info.hThread:
                    self.kernel.CloseHandle(info.hThread)
                if info.hProcess:
                    self.kernel.CloseHandle(info.hProcess)
            raise

    def active_count(self) -> int:
        info = self.ACCOUNTING()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses

    def process_handles(self, previous: list[int] = ()) -> list[int]:
        """持有当前树的等待句柄；Query 的计数归零可能早于这些进程 signaled。"""
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.restype = wintypes.DWORD
        known = {self.kernel.GetProcessId(handle) for handle in previous}
        capacity = 32
        handles = []
        try:
            while True:
                buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
                if self.kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None):
                    break
                if ctypes.get_last_error() != 234 or capacity >= 65536:
                    raise ctypes.WinError(ctypes.get_last_error())
                capacity *= 2
            count = wintypes.DWORD.from_buffer(buffer, 4).value
            pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
            for pid in pids:
                if pid in known:
                    continue
                handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
                if not handle:
                    # 在枚举与 OpenProcess 之间已退出的进程无需再等；其他错误失败关闭。
                    if ctypes.get_last_error() == 87:
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                member = wintypes.BOOL()
                if not self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                    self.kernel.CloseHandle(handle)
                    raise ctypes.WinError(ctypes.get_last_error())
                if member.value:
                    handles.append(handle)
                else:
                    # PID 若已被其他进程重用，只关闭句柄，绝不操作非本树进程。
                    self.kernel.CloseHandle(handle)
            return handles
        except Exception:
            self.release_process_handles(handles)
            raise

    def handles_exited(self, handles: list[int]) -> bool:
        return all(self.kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles)

    def release_process_handles(self, handles: list[int]) -> None:
        for handle in handles:
            self.kernel.CloseHandle(handle)

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _WindowsPopen(subprocess.Popen):
    """仅供 spawn_owned 的固定二进制管道调用；复用 Popen 的句柄与通信协议。"""

    def __init__(self, argv: list[str], env: dict[str, str], job: _WindowsJob):
        self._owned_job = job
        super().__init__(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, close_fds=True)

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env,
                       startupinfo, creationflags, shell, p2cread, p2cwrite,
                       c2pread, c2pwrite, errread, errwrite, *unused):
        if (executable is not None or preexec_fn is not None or not close_fds or pass_fds
                or cwd is not None or startupinfo is not None or creationflags or shell):
            raise RuntimeError("unsupported owned Windows process options")
        handles = [int(p2cread), int(c2pwrite), int(errwrite)]
        if set(self._filter_handle_list(handles)) != set(handles):
            raise RuntimeError("standard pipe handles cannot be inherited")
        command = subprocess.list2cmdline(args)
        if "\0" in command:
            raise ValueError("invalid child command")
        sys.audit("subprocess.Popen", None, command, None, env)
        sys.audit("_winapi.CreateProcess", None, command, None)
        thread = None
        try:
            process, thread, pid = self._owned_job.create_suspended(command, env, handles)
            self._handle = subprocess.Handle(process)
            self.pid = pid
            self._child_created = True
            self._owned_job.resume_primary_thread(thread)
        except BaseException:
            if self._child_created:
                # 构造失败时 spawn_owned 尚未取得 Popen 对象，必须在此等待精确进程句柄。
                self._owned_job.terminate()
                self.wait(timeout=5)
            raise
        finally:
            if thread:
                self._owned_job.kernel.CloseHandle(thread)
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


class OwnedProcess:
    """持有已受监管的准备进程树；阻塞通信在 asyncio.to_thread 内执行。"""

    def __init__(self, process: subprocess.Popen, job: _WindowsJob | None, max_output: int):
        self._process, self._job, self.max_output = process, job, max_output
        self._lock = threading.RLock()
        self._tree_stopped = False
        self._closed = False
        self._communicating = False
        # 停止失败后保留等待句柄，避免计数先归零时下一次重试遗失未 signaled 的进程。
        self._wait_handles: list[int] = []

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    @property
    def tree_stopped(self) -> bool:
        with self._lock:
            return self._tree_stopped

    def _group_exists(self) -> bool:
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def active_count(self) -> int:
        with self._lock:
            if self._tree_stopped:
                return 0
            return self._job.active_count() if self._job else int(self._group_exists())

    def stop(self, timeout: float = 5.0) -> bool:
        """强制停止并确认为空；失败时保留所有权供上层重试。"""
        with self._lock:
            if self._tree_stopped:
                return True
            try:
                if self._job:
                    self._wait_handles.extend(self._job.process_handles(self._wait_handles))
                    self._job.terminate()
                else:
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    self._process.poll()
                    active = self._job.active_count() if self._job else int(self._group_exists())
                    if not active and (not self._job or self._job.handles_exited(self._wait_handles)):
                        self._tree_stopped = True
                        return True
                    time.sleep(0.02)
            except (OSError, RuntimeError):
                return False
            finally:
                if self._job and self._tree_stopped:
                    self._job.release_process_handles(self._wait_handles)
                    self._wait_handles.clear()
            return False

    def communicate(self, input: bytes | None = None) -> tuple[bytes, None]:
        """stdout 有界；父正常退出后也清理后代，树停止才返回。"""
        with self._lock:
            if self._communicating or self._closed:
                raise HarnessError("PROCESS_STATE_INVALID", "准备进程通信已开始或进程已关闭。")
            self._communicating = True
        chunks: list[bytes] = []
        output_limited, read_failed = threading.Event(), threading.Event()

        def collect() -> None:
            total = 0
            try:
                assert self._process.stdout is not None
                while chunk := self._process.stdout.read(65536):
                    allowed = max(0, self.max_output - total)
                    if allowed:
                        chunks.append(chunk[:allowed])
                    total += len(chunk)
                    if total > self.max_output:
                        output_limited.set()
                        self.stop()
                        return
            except OSError:
                read_failed.set()

        reader = threading.Thread(target=collect, name=f"snapshot-stdout-{self.pid}", daemon=True)
        reader.start()
        try:
            if self._process.stdin:
                try:
                    if input:
                        self._process.stdin.write(input)
                        self._process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        self._process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
            self._process.wait()
            if not self.stop():
                raise HarnessError("TERMINATION_FAILED", "准备进程树停止未确认。")
            reader.join(timeout=5)
            if reader.is_alive() or read_failed.is_set():
                raise HarnessError("SNAPSHOT_PROTOCOL_ERROR", "准备进程输出无法完整读取。")
            if output_limited.is_set():
                raise HarnessError("SNAPSHOT_OUTPUT_LIMIT", "准备进程输出超过安全上限。")
            return b"".join(chunks), None
        finally:
            if self.tree_stopped:
                self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._tree_stopped:
                raise HarnessError("TERMINATION_FAILED", "尚未确认准备进程树停止，不能释放所有权。")
            if self._job:
                self._job.close()
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                if stream and not stream.closed:
                    stream.close()
            self._closed = True


def spawn_owned(argv: list[str], env: dict[str, str], *, max_output: int = 8 * 1024 * 1024, job_name: str | None = None) -> OwnedProcess:
    """同步建立所有权；调用方在任何 await 前登记返回对象。"""
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 1:
        raise HarnessError("INVALID_PROCESS_LIMIT", "准备进程输出上限不正确。")
    if job_name is not None and (os.name != "nt" or not _valid_job_name(job_name)):
        raise HarnessError("INVALID_PROCESS_OWNER", "执行进程归属名称不正确。")
    process = None
    job = None
    try:
        if os.name == "nt":
            job = _WindowsJob(job_name)
            process = _WindowsPopen(argv, dict(env), job)
        else:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=dict(env), start_new_session=True)
        return OwnedProcess(process, job, max_output)
    except BaseException as error:
        if job:
            try:
                job.terminate()
            except OSError:
                pass
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        if not isinstance(error, Exception):
            raise
        raise HarnessError("PREPARATION_PROCESS_UNAVAILABLE", "无法建立准备进程树的安全监管。") from None


async def stop_process_tree(process: OwnedProcess) -> bool:
    if not isinstance(process, OwnedProcess):
        return False
    stopped = await asyncio.to_thread(process.stop)
    if stopped:
        process.close()
    return stopped


def _valid_job_name(name: str) -> bool:
    return isinstance(name, str) and bool(re.fullmatch(r"Local\\deepseek-harness-[0-9a-f]{32}-[0-9a-f]{32}", name))


def _named_job_stopped(name: str) -> bool:
    """重启后只核对预先持久化的归属对象；不打开旧 PID，也不终止任何进程。"""
    if os.name != "nt" or not _valid_job_name(name):
        return False
    job = None
    handles = []
    try:
        job = _WindowsJob(name, existing=True)
        handles = job.process_handles()
        return job.active_count() == 0 and job.handles_exited(handles)
    except OSError as error:
        # Job 不存在表示父句柄与其所属进程引用都已释放。
        return getattr(error, "winerror", None) == 2
    except RuntimeError:
        return False
    finally:
        if job:
            job.release_process_handles(handles)
            job.close()


async def named_job_stopped(name: str) -> bool:
    return await asyncio.to_thread(_named_job_stopped, name)

