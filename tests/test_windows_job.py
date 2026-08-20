"""core.windows_job: the kill-on-close Job Object that stops orphan children.

No real kernel objects are created — kernel32 is replaced by a fake recording
the call sequence — and no real process is spawned (a stand-in Popen carries
just pid/poll). Covers the happy path, the caching of the job handle, and every
degradation path (non-Windows, no API, dead child, failing call), which must all
be silent no-ops returning False.
"""

from __future__ import annotations

import pytest

from core.windows_job import (
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    bind,
    reset_state,
)
from core import windows_job


class FakeProc:
    """Stand-in for subprocess.Popen: only pid + poll() are used by bind()."""

    def __init__(self, pid=4321, rc=None):
        self.pid = pid
        self._rc = rc

    def poll(self):
        return self._rc


class FakeKernel32:
    """Records calls; every API succeeds unless a failure is configured."""

    def __init__(self, create_job=1000, set_info=1, open_process=2000, assign=1):
        self._create_job = create_job
        self._set_info = set_info
        self._open_process = open_process
        self._assign = assign
        self.calls = []

    def CreateJobObjectW(self, attrs, name):
        self.calls.append(("CreateJobObjectW", attrs, name))
        return self._create_job

    def SetInformationJobObject(self, job, info_class, info, size):
        self.calls.append(("SetInformationJobObject", job, info_class, info, size))
        return self._set_info

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, inherit, pid))
        return self._open_process

    def AssignProcessToJobObject(self, job, handle):
        self.calls.append(("AssignProcessToJobObject", job, handle))
        return self._assign

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_k32(monkeypatch):
    """Windows + a fake kernel32, with the module's cached job state reset."""
    reset_state()
    kernel32 = FakeKernel32()
    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: kernel32)
    yield kernel32
    reset_state()


# ---------- happy path ----------


def test_bind_creates_kill_on_close_job_and_assigns_child(fake_k32):
    assert bind(FakeProc(pid=4321)) is True

    assert fake_k32.names() == [
        "CreateJobObjectW",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
        "CloseHandle",  # the *process* handle; the job handle stays open
    ]
    _, job, info_class, info, size = fake_k32.calls[1]
    assert job == 1000
    assert info_class == JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
    assert size > 0
    # The whole point: closing the job (process death) kills its members.
    limits = info._obj.BasicLimitInformation
    assert limits.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert fake_k32.calls[2][3] == 4321  # OpenProcess got the child's pid
    assert fake_k32.calls[4][1] == 2000  # only the process handle is closed


def test_second_bind_reuses_the_same_job(fake_k32):
    assert bind(FakeProc(pid=1)) is True
    assert bind(FakeProc(pid=2)) is True

    assert fake_k32.names().count("CreateJobObjectW") == 1
    assert fake_k32.names().count("AssignProcessToJobObject") == 2


# ---------- degradation: always a logged no-op, never an exception ----------


def test_non_windows_is_a_noop(monkeypatch):
    reset_state()
    monkeypatch.setattr(windows_job, "_is_windows", lambda: False)
    # _kernel32 returns None off Windows -> nothing to bind to.
    assert bind(FakeProc()) is False


def test_missing_kernel32_is_a_noop(monkeypatch):
    reset_state()
    monkeypatch.setattr(windows_job, "_kernel32", lambda: None)
    assert bind(FakeProc()) is False


def test_already_exited_child_is_not_bound(fake_k32):
    assert bind(FakeProc(rc=0)) is False
    assert fake_k32.calls == []


def test_process_without_pid_is_not_bound(fake_k32):
    class NoPid:
        pid = None

        def poll(self):
            return None

    assert bind(NoPid()) is False
    assert fake_k32.calls == []


def test_create_job_failure_latches_off(monkeypatch):
    reset_state()
    kernel32 = FakeKernel32(create_job=0)
    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: kernel32)

    assert bind(FakeProc()) is False
    assert bind(FakeProc()) is False
    # Latched: the failing API is not hammered once per child.
    assert kernel32.names().count("CreateJobObjectW") == 1
    reset_state()


def test_set_information_failure_closes_job_and_latches_off(monkeypatch):
    reset_state()
    kernel32 = FakeKernel32(set_info=0)
    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: kernel32)

    assert bind(FakeProc()) is False
    # A job without KILL_ON_JOB_CLOSE would be worse than none: it is closed.
    assert kernel32.names() == ["CreateJobObjectW", "SetInformationJobObject", "CloseHandle"]
    reset_state()


def test_open_process_failure_leaves_child_unguarded(monkeypatch):
    reset_state()
    kernel32 = FakeKernel32(open_process=0)
    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: kernel32)

    assert bind(FakeProc()) is False
    assert "AssignProcessToJobObject" not in kernel32.names()
    reset_state()


def test_assign_failure_still_closes_the_process_handle(monkeypatch):
    reset_state()
    kernel32 = FakeKernel32(assign=0)
    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: kernel32)

    assert bind(FakeProc()) is False
    assert kernel32.names()[-1] == "CloseHandle"  # no handle leak on the error path
    reset_state()


def test_api_exception_is_swallowed(monkeypatch):
    reset_state()

    class BoomKernel32(FakeKernel32):
        def OpenProcess(self, access, inherit, pid):
            raise OSError("access denied")

    monkeypatch.setattr(windows_job, "_is_windows", lambda: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: BoomKernel32())

    assert bind(FakeProc()) is False  # never propagates to the caller
    reset_state()
