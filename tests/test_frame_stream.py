"""Frame-pick logic for the gesture recorder (pure, no device / no PyAV).

Validates the host-time frame selection that replaces per-gesture screencap:
before = pre-press frame, after = post-settle frame bounded by the next press.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from record import frame_stream as frame_stream_mod
from record.frame_stream import ScrcpyFrameStream, pick_after, pick_before

LOGGER = logging.getLogger("test")


def _buf(*ts):
    """Buffer of (ts, label) where the label echoes the ts for easy assertions."""
    return [(t, f"f{t}") for t in ts]


# -- pick_before ------------------------------------------------------------------

def test_before_picks_last_frame_at_or_before_down():
    buf = _buf(0.0, 0.1, 0.2, 0.3, 0.4)
    # Press at 0.25 -> the 0.2 frame is the last pre-press one (no glow).
    assert pick_before(buf, 0.25) == "f0.2"


def test_before_includes_exact_match():
    buf = _buf(0.0, 0.1, 0.2)
    assert pick_before(buf, 0.2) == "f0.2"


def test_before_falls_back_to_earliest_when_all_newer():
    # Buffer rotated past the down moment -> earliest available is best effort.
    buf = _buf(0.5, 0.6, 0.7)
    assert pick_before(buf, 0.1) == "f0.5"


def test_before_empty_buffer_returns_none():
    assert pick_before([], 1.0) is None


# -- pick_after -------------------------------------------------------------------

def test_after_picks_latest_settled_before_next_down():
    buf = _buf(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    # lift=0.1, settle=0.2 -> target 0.3; next press at 0.55 -> frames 0.3..0.5
    # qualify, take the latest (most settled) = 0.5.
    assert pick_after(buf, up_ts=0.1, settle_s=0.2, next_down_ts=0.55) == "f0.5"


def test_after_excludes_contaminated_frames_after_next_down():
    buf = _buf(0.0, 0.2, 0.4, 0.6, 0.8)
    # next press at 0.5 -> 0.6/0.8 are the NEXT step, must be excluded.
    res = pick_after(buf, up_ts=0.0, settle_s=0.2, next_down_ts=0.5)
    assert res == "f0.4"


def test_after_fast_next_press_returns_best_available_before_next():
    # Next press (0.15) arrives before settle target (0.0+0.3=0.3): no settled
    # frame exists -> return the last frame before the next down (best effort).
    buf = _buf(0.0, 0.05, 0.1, 0.2, 0.3)
    res = pick_after(buf, up_ts=0.0, settle_s=0.3, next_down_ts=0.15)
    assert res == "f0.1"


def test_after_no_next_down_takes_latest_settled():
    buf = _buf(0.0, 0.2, 0.4, 0.6)
    # settle target = 0.0 + 0.1 = 0.1; no next press -> latest settled = 0.6.
    assert pick_after(buf, up_ts=0.0, settle_s=0.1) == "f0.6"


# -- start()/stop() must only ever touch this stream's own port forward ----------
#
# `forward --remove-all` wipes every forward on the shared adb server, which
# would drop other concurrent sessions'/tools' forwards too (e.g. a task run's
# own scrcpy frame stream). tcp:0 always allocates a fresh free local port, so
# start() has nothing of its own to pre-clean; only stop() should ever remove
# a forward, and only its own (tcp:{port}).

class _FakePopen:
    """Long-lived adb client stand-in; still "running" until stop() reaps it."""

    def __init__(self, pid=4242):
        self.pid = pid
        self._rc = None

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9


def test_start_never_wipes_all_port_forwards_and_stop_removes_only_its_own(
    monkeypatch, tmp_path,
):
    jar = tmp_path / "scrcpy-server-v3.1"
    jar.write_bytes(b"not-a-real-jar")
    monkeypatch.setattr(frame_stream_mod, "DEFAULT_SERVER_JAR", str(jar))

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # cmd == ["adb", "-s", device_id, *args]
        if cmd[3:5] == ["forward", "tcp:0"]:
            return SimpleNamespace(returncode=0, stdout="41234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(frame_stream_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(frame_stream_mod.subprocess, "Popen", lambda *a, **k: _FakePopen())
    monkeypatch.setattr(frame_stream_mod.adb_daemon, "ensure_adb_daemon", lambda *a, **k: True)
    monkeypatch.setattr(frame_stream_mod.windows_job, "bind", lambda proc: True)
    monkeypatch.setattr(
        ScrcpyFrameStream, "_connect_when_ready",
        lambda self: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(ScrcpyFrameStream, "_wait_first_frame", lambda self, timeout: True)

    stream = ScrcpyFrameStream("dev1", logger=LOGGER)
    try:
        assert stream.start() is True
        assert not any("--remove-all" in c for c in calls), (
            "start() must not blanket-clear all adb forwards"
        )
        # It should still have picked a fresh port the normal way.
        assert any(c[3:5] == ["forward", "tcp:0"] for c in calls)
    finally:
        calls.clear()
        stream.stop()

    assert any(c[3:] == ["forward", "--remove", "tcp:41234"] for c in calls), (
        "stop() should remove only this stream's own forwarded port"
    )
    assert not any("--remove-all" in c for c in calls)
