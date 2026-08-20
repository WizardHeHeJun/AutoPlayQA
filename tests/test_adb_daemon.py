"""core.adb_daemon: pre-warm the shared adb daemon outside the job object.

subprocess is mocked throughout (no real `adb start-server`). What matters here
is the caching contract — exactly one attempt per adb path per process, so a
per-segment call site cannot stall a run — and that failures degrade to a
logged False instead of raising.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from core import adb_daemon
from core.adb_daemon import ensure_adb_daemon


@pytest.fixture
def calls(monkeypatch):
    """Empty daemon cache + a recording fake `adb start-server`."""
    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="daemon started", stderr="")

    monkeypatch.setattr(adb_daemon, "_ensured", {})
    monkeypatch.setattr("core.adb_daemon.subprocess.run", fake_run)
    return recorded


def test_starts_the_server_once_per_process(calls):
    assert ensure_adb_daemon() is True
    assert ensure_adb_daemon() is True
    assert ensure_adb_daemon() is True

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["adb", "start-server"]
    # Bounded: a wedged adb server must not hang the caller forever.
    assert kwargs["timeout"] == adb_daemon.START_SERVER_TIMEOUT_S


def test_each_adb_path_is_warmed_separately(calls):
    ensure_adb_daemon("adb")
    ensure_adb_daemon(r"C:\sdk\platform-tools\adb.exe")
    ensure_adb_daemon("adb")

    assert [c[0][0] for c in calls] == ["adb", r"C:\sdk\platform-tools\adb.exe"]


def test_missing_adb_binary_degrades_to_false(monkeypatch):
    monkeypatch.setattr(adb_daemon, "_ensured", {})

    def boom(cmd, **kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr("core.adb_daemon.subprocess.run", boom)

    assert ensure_adb_daemon() is False  # warned, not raised


def test_timeout_degrades_to_false_and_is_not_retried(monkeypatch):
    monkeypatch.setattr(adb_daemon, "_ensured", {})
    attempts = []

    def timing_out(cmd, **kwargs):
        attempts.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr("core.adb_daemon.subprocess.run", timing_out)

    assert ensure_adb_daemon() is False
    assert ensure_adb_daemon() is False
    # A per-segment call site must not pay the timeout again and again.
    assert len(attempts) == 1


def test_nonzero_exit_reports_failure(monkeypatch):
    monkeypatch.setattr(adb_daemon, "_ensured", {})
    monkeypatch.setattr(
        "core.adb_daemon.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="cannot bind"),
    )

    assert ensure_adb_daemon() is False


def test_reset_state_clears_the_cache(calls):
    ensure_adb_daemon()
    adb_daemon.reset_state()
    ensure_adb_daemon()

    assert len(calls) == 2
