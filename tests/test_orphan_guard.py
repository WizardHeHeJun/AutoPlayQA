"""Orphan guard wiring at the five long-lived Popen sites.

Each site must, in this order: pre-warm the adb daemon from an *unbound* process
(`core.adb_daemon`), spawn its long-lived adb client, then bind that client to
the kill-on-close job (`core.windows_job`). Order is the whole point — a client
that auto-starts the daemon and is then bound would drag the machine-wide adb
daemon into the job with it.

The two atexit registries (frame stream / gesture recorder) are checked here
too: registered while running, de-registered by stop(), and drained by the hook.

No device, no real process: subprocess is mocked everywhere, and the daemon/job
helpers are replaced by recorders.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core import adb_daemon, windows_job
from record import frame_stream as frame_stream_mod
from record import gesture_recorder as gesture_mod
from record.frame_stream import ScrcpyFrameStream
from record.gesture_recorder import GestureRecorder, TouchCalibration
from perception.pcap_recorder import RollingPcapRecorder
from perception.screen_recorder import RollingScreenRecorder
from perception.scrcpy_stream import _DeviceStream

LOGGER = logging.getLogger("test")


class FakePopen:
    """Long-lived adb client stand-in (pid so a real bind() could find it)."""

    def __init__(self, pid=9999):
        self.pid = pid
        self._rc = None
        self.stdout = SimpleNamespace(readline=lambda: "")

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9


@pytest.fixture
def guard(monkeypatch):
    """Record the daemon/spawn/bind sequence instead of performing it."""
    state = {"order": [], "bound": [], "procs": []}

    def fake_daemon(adb_path: str = "adb") -> bool:
        state["order"].append("daemon")
        return True

    def fake_bind(proc) -> bool:
        state["order"].append("bind")
        state["bound"].append(proc)
        return True

    monkeypatch.setattr(adb_daemon, "ensure_adb_daemon", fake_daemon)
    monkeypatch.setattr(windows_job, "bind", fake_bind)

    def make_popen(module_path: str):
        def fake_popen(cmd, **kwargs):
            state["order"].append("popen")
            proc = FakePopen()
            state["procs"].append(proc)
            return proc

        monkeypatch.setattr(f"{module_path}.subprocess.Popen", fake_popen)

    state["make_popen"] = make_popen
    return state


def _ok_run(stdout=""):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return fake_run


# ---------- perception/screen_recorder.py ----------


def test_screen_recorder_warms_daemon_then_binds_every_segment(guard, monkeypatch):
    guard["make_popen"]("perception.screen_recorder")
    monkeypatch.setattr("perception.screen_recorder.subprocess.run", _ok_run())
    monkeypatch.setattr("perception.screen_recorder.STOP_SETTLE_S", 0)

    rec = RollingScreenRecorder(LOGGER, segment_s=60)
    rec.start("dev1")

    assert guard["order"] == ["daemon", "popen", "bind"]
    assert guard["bound"] == [rec._proc]

    rec._segment_started -= 61
    rec.tick()  # rotation spawns a fresh client -> it needs its own bind

    assert guard["order"][-3:] == ["daemon", "popen", "bind"]
    assert guard["bound"][-1] is rec._proc


# ---------- perception/pcap_recorder.py ----------


def test_pcap_recorder_warms_daemon_then_binds_every_segment(guard, monkeypatch):
    guard["make_popen"]("perception.pcap_recorder")

    def probe_run(cmd, **kwargs):
        shell = cmd[-1] if "shell" in cmd else ""
        if "echo ok" in shell:
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        if "--version" in shell:
            return SimpleNamespace(returncode=0, stdout="libpcap 1.10.1", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("perception.pcap_recorder.subprocess.run", probe_run)
    monkeypatch.setattr("perception.pcap_recorder.STOP_SETTLE_S", 0)

    rec = RollingPcapRecorder(LOGGER, segment_s=60)
    rec.start("dev1")

    assert guard["order"] == ["daemon", "popen", "bind"]
    assert guard["bound"] == [rec._proc]

    rec._segment_started -= 61
    rec.tick()

    assert guard["order"][-3:] == ["daemon", "popen", "bind"]


# ---------- perception/scrcpy_stream.py ----------


def test_scrcpy_stream_warms_daemon_then_binds_server_client(guard, monkeypatch, tmp_path):
    jar = tmp_path / "scrcpy-server-v3.1"
    jar.write_bytes(b"not-a-real-jar")
    guard["make_popen"]("perception.scrcpy_stream")
    # `adb forward tcp:0` prints the chosen local port.
    monkeypatch.setattr("perception.scrcpy_stream.subprocess.run", _ok_run("41234\n"))
    monkeypatch.setattr(_DeviceStream, "_connect_when_ready", lambda self: SimpleNamespace())
    monkeypatch.setattr(_DeviceStream, "_decode_loop", lambda self: None)

    stream = _DeviceStream(LOGGER, "dev1", str(jar), 30, 8_000_000, 0)
    stream.start()

    assert guard["order"] == ["daemon", "popen", "bind"]
    assert guard["bound"] == [stream._proc]


# ---------- record/gesture_recorder.py ----------


def test_gesture_recorder_binds_getevent_client_and_registers_for_atexit(guard, monkeypatch):
    guard["make_popen"]("record.gesture_recorder")
    monkeypatch.setattr(
        "record.gesture_recorder.calibrate",
        lambda device_id: TouchCalibration("/dev/input/event7", 143999, 319999, 1080, 2400),
    )

    recorder = GestureRecorder("dev1", screenshot_capturer=None, logger=LOGGER)
    recorder.start(on_gesture=lambda event, images: None)

    assert guard["order"] == ["daemon", "popen", "bind"]
    assert guard["bound"] == [recorder._proc]
    assert recorder in gesture_mod._active_recorders

    recorder.stop()

    assert recorder not in gesture_mod._active_recorders  # nothing left to clean


def test_gesture_atexit_hook_stops_registered_recorders(monkeypatch):
    stopped = []
    recorder = GestureRecorder("dev1", screenshot_capturer=None, logger=LOGGER)
    monkeypatch.setattr(recorder, "stop", lambda: stopped.append("stopped"))
    gesture_mod._active_recorders.add(recorder)

    gesture_mod._stop_active_recorders()

    assert stopped == ["stopped"]
    gesture_mod._active_recorders.discard(recorder)


# ---------- record/frame_stream.py ----------


def test_frame_stream_stop_deregisters_from_the_atexit_registry():
    stream = ScrcpyFrameStream("dev1", logger=LOGGER)
    frame_stream_mod._active_streams.add(stream)

    stream.stop()  # nothing started: must still de-register, not raise

    assert stream not in frame_stream_mod._active_streams


def test_frame_stream_atexit_hook_stops_registered_streams(monkeypatch):
    stopped = []
    stream = ScrcpyFrameStream("dev1", logger=LOGGER)
    monkeypatch.setattr(stream, "stop", lambda: stopped.append("stopped"))
    frame_stream_mod._active_streams.add(stream)

    frame_stream_mod._stop_active_streams()

    assert stopped == ["stopped"]
    frame_stream_mod._active_streams.discard(stream)
