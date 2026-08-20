"""Kill-on-close Windows Job Object: long-lived children die with this process.

Why this exists: several subsystems keep a *long-lived local adb client* alive
for the whole run (scrcpy video stream, gesture `getevent` stream, rolling
`screenrecord` / `tcpdump` segments). When the main process is killed hard
(taskkill, Ctrl-Break, a crashed MCP host) Python's `atexit` / `finally`
handlers never run, so those clients survive as orphans — and keep the device
side busy with them.

A Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is the one mechanism
Windows offers that survives a hard kill: when the last handle to the job goes
away — which the kernel does for us when this process dies, however it dies —
every process still assigned to the job is terminated. So the job handle is
created once, never closed on purpose, and every long-lived child is assigned
to it right after `Popen`.

Hard rule for callers: **the process bound here must never be the adb client
that auto-starts the adb daemon.** On Windows a client that finds no server
spawns one as its own child, which would land in the job too and take the
machine-wide adb daemon down with us. Call `core.adb_daemon.ensure_adb_daemon()`
*before* the `Popen` you bind (see that module's docstring).

Everything here is best effort: on non-Windows, when the API is unavailable, or
when any call fails, `bind()` degrades to a logged no-op and returns False. It
never raises — losing the orphan guard must never break a run.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import threading
from typing import Any, Optional

from core.logger import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

# winnt.h
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS enum value
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

_lock = threading.Lock()
_job_handle: Optional[int] = None
# Latched after a failed CreateJobObject/SetInformationJobObject: retrying per
# child would only repeat the same warning once per segment rotation.
_job_unavailable = False


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _is_windows() -> bool:
    return sys.platform == "win32"


def _kernel32() -> Optional[Any]:
    """kernel32 with last-error tracking, or None when unusable (also the test seam)."""
    if not _is_windows():
        return None
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:
        logger.warning("job object unavailable (kernel32 not loadable: %s)", exc)
        return None


def _get_job(kernel32: Any) -> Optional[int]:
    """Create (once) the process-wide kill-on-job-close job; None when unavailable."""
    global _job_handle, _job_unavailable
    if _job_handle is not None:
        return _job_handle
    if _job_unavailable:
        return None
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        _job_unavailable = True
        logger.warning("CreateJobObject failed (err %s); orphan guard off",
                       ctypes.get_last_error())
        return None
    info = _JobExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        _job_unavailable = True
        logger.warning("SetInformationJobObject failed (err %s); orphan guard off",
                       ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        return None
    # The handle is deliberately kept open for the life of the process: closing
    # it is exactly what kills the children, and that must happen only when this
    # process goes away.
    _job_handle = handle
    return handle


def bind(proc: "subprocess.Popen[Any]") -> bool:
    """Assign a long-lived child to the kill-on-close job.

    Returns True only when the child is now guarded. Non-Windows hosts, a
    missing API, an already-exited child or any failing call degrade to a logged
    no-op returning False — never an exception.
    """
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        logger.debug("job bind skipped: process has no usable pid (%r)", pid)
        return False
    if proc.poll() is not None:
        logger.warning("job bind skipped: pid %s already exited", pid)
        return False
    with _lock:
        kernel32 = _kernel32()
        if kernel32 is None:
            return False
        try:
            job = _get_job(kernel32)
            if job is None:
                return False
            handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not handle:
                logger.warning("OpenProcess failed for pid %s (err %s); child unguarded",
                               pid, ctypes.get_last_error())
                return False
            try:
                if not kernel32.AssignProcessToJobObject(job, handle):
                    logger.warning(
                        "AssignProcessToJobObject failed for pid %s (err %s); child unguarded",
                        pid, ctypes.get_last_error(),
                    )
                    return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception as exc:  # noqa: BLE001 - the guard must never break a run
            logger.warning("job bind failed for pid %s (%s); child unguarded", pid, exc)
            return False
    logger.debug("pid %s bound to the kill-on-close job", pid)
    return True


def reset_state() -> None:
    """Forget the cached job handle / failure latch (tests only)."""
    global _job_handle, _job_unavailable
    _job_handle = None
    _job_unavailable = False
