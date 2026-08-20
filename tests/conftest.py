from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def no_host_process_side_effects(monkeypatch):
    """Keep the orphan guard from touching the host in every other test.

    `ensure_adb_daemon` would shell out to a real `adb start-server` and
    `windows_job.bind` would create a real kernel Job Object — both are host
    side effects the suite must never have (tests mock subprocess, always).
    Priming the daemon cache and stubbing the kernel32 seam makes both a no-op.

    The dedicated tests (`test_windows_job.py` / `test_adb_daemon.py`) import the
    real functions by name at module import time — before this fixture patches
    the module attributes — and re-patch the same seams themselves, so they still
    exercise the real code paths.
    """
    from core import adb_daemon, windows_job
    monkeypatch.setattr(adb_daemon, "_ensured", {"adb": True})
    monkeypatch.setattr(windows_job, "_kernel32", lambda: None)


@pytest.fixture
def fake_logger():
    return logging.getLogger("test")


@pytest.fixture
def sample_task():
    """Minimal valid two-node task used across loader/engine tests."""
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "设置"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["finish"],
                "timeout_ms": 0,
            },
            "finish": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
