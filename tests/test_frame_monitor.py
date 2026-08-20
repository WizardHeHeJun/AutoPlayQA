"""Background frame monitor: ring, cursor, latch-off and the MCP tool face.

No device anywhere: the capturer is a fake that hands back PIL frames (and, in
the failure tests, raises), which is exactly the seam the real monitor uses.
Timing-sensitive tests drive `_capture_once` directly so the assertions are
deterministic; the few that need the real thread shorten the interval instead of
sleeping for a second.
"""
from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest
from PIL import Image

import mcp_server
from perception import frame_monitor as monitor_mod
from perception.frame_monitor import FrameMonitor, FrameMonitorRegistry

LOGGER = logging.getLogger("test")


class FakeCapturer:
    """Stands in for the shared ScreenshotCapturer (capture_image is all we use)."""

    def __init__(self, size=(1080, 2400), fail_with=None):
        self.size = size
        self.fail_with = fail_with
        self.calls = 0
        self.devices = []

    def capture_image(self, device_id):
        self.calls += 1
        self.devices.append(device_id)
        if self.fail_with is not None:
            raise self.fail_with
        return Image.new("RGB", self.size, "black")


def _monitor(tmp_path, capturer, **kwargs):
    """A monitor with its output dir made, ready for direct _capture_once calls."""
    mon = FrameMonitor(LOGGER, capturer, "dev1", output_root=str(tmp_path), **kwargs)
    mon.monitor_dir.mkdir(parents=True, exist_ok=True)
    return mon


def _wait_for(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- capture, naming, downscaling ----------

def test_frames_are_written_in_capture_order_and_downscaled(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer())
    for _ in range(3):
        mon._capture_once()

    files = sorted(p.name for p in mon.monitor_dir.glob("*.png"))
    assert len(files) == 3
    # Zero-padded sequence leads the name, so lexical order == capture order.
    assert [name.split("_")[1] for name in files] == ["000001", "000002", "000003"]
    with Image.open(mon.monitor_dir / files[0]) as img:
        assert img.size == (720, 1600)  # short edge normalised, aspect kept

    status = mon.status()
    assert status["frames_total"] == 3
    assert status["frames_on_disk"] == 3
    assert status["failures"] == 0 and status["dropped"] == 0


def test_full_resolution_keeps_native_pixels(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer(), full_resolution=True)
    mon._capture_once()
    frame = next(iter(mon.monitor_dir.glob("*.png")))
    with Image.open(frame) as img:
        assert img.size == (1080, 2400)


def test_it_never_builds_its_own_capturer(tmp_path):
    """The shared capturer is the contract (scrcpy pool + OCR warmup live there)."""
    capturer = FakeCapturer()
    mon = _monitor(tmp_path, capturer)
    mon._capture_once()
    assert capturer.calls == 1 and capturer.devices == ["dev1"]
    assert mon.capturer is capturer


# ---------- cursor semantics ----------

def test_get_new_frames_returns_only_what_is_new(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer())
    mon._capture_once()
    mon._capture_once()

    first = mon.take_new()
    assert first["new_count"] == 2
    assert [f["index"] for f in first["frames"]] == [1, 2]
    assert all(f["ts_ms"] > 0 for f in first["frames"])

    # Nothing captured in between: a second look is empty, not a re-delivery.
    assert mon.take_new()["frames"] == []

    mon._capture_once()
    third = mon.take_new()
    assert [f["index"] for f in third["frames"]] == [3]
    assert third["frames_total"] == 3


def test_frames_carry_paths_only(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer())
    mon._capture_once()
    frame = mon.take_new()["frames"][0]
    assert set(frame) == {"index", "path", "ts_ms", "width", "height"}
    assert frame["path"].endswith(".png")


# ---------- ring cleanup ----------

def test_ring_keeps_only_the_newest_frames_on_disk(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer(), max_frames=2)
    for _ in range(5):
        mon._capture_once()

    names = sorted(p.name for p in mon.monitor_dir.glob("*.png"))
    assert [n.split("_")[1] for n in names] == ["000004", "000005"]
    status = mon.status()
    assert status["frames_on_disk"] == 2
    assert status["frames_total"] == 5
    # Three frames were evicted before anyone read them.
    assert status["dropped"] == 3
    assert mon.take_new()["new_count"] == 2


def test_dropped_counts_only_unread_frames(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer(), max_frames=2)
    mon._capture_once()
    mon._capture_once()
    mon.take_new()  # both read; evicting them later is not a drop
    mon._capture_once()
    mon._capture_once()
    assert mon.status()["dropped"] == 0


def test_ring_only_deletes_its_own_frames(tmp_path):
    stranger = tmp_path / "someone_elses.png"
    stranger.write_bytes(b"keep me")
    mon = _monitor(tmp_path, FakeCapturer(), max_frames=1)
    for _ in range(3):
        mon._capture_once()
    assert stranger.exists()
    assert len(list(mon.monitor_dir.glob("*.png"))) == 1


# ---------- failures ----------

def test_latches_off_after_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MAX_CONSECUTIVE_FAILURES", 3)
    mon = _monitor(tmp_path, FakeCapturer(fail_with=RuntimeError("device gone")))

    for _ in range(3):
        mon._capture_once()

    status = mon.status()
    assert status["failures"] == 3
    assert status["latched_off"] and "device gone" in status["latched_off"]


def test_a_good_frame_resets_the_failure_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MAX_CONSECUTIVE_FAILURES", 3)
    capturer = FakeCapturer(fail_with=RuntimeError("flaky"))
    mon = _monitor(tmp_path, capturer)

    mon._capture_once()
    mon._capture_once()
    capturer.fail_with = None
    mon._capture_once()
    capturer.fail_with = RuntimeError("flaky")
    mon._capture_once()

    status = mon.status()
    assert status["failures"] == 3          # counted
    assert status["latched_off"] is None    # but never 3 *in a row*
    assert status["frames_total"] == 1


def test_the_loop_stops_itself_when_it_latches_off(tmp_path, monkeypatch):
    """A monitor that cannot capture must not spin forever."""
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    monkeypatch.setattr(monitor_mod, "MAX_CONSECUTIVE_FAILURES", 3)
    mon = FrameMonitor(LOGGER, FakeCapturer(fail_with=RuntimeError("boom")), "dev1",
                       interval_ms=1, output_root=str(tmp_path))
    mon.start()

    assert _wait_for(lambda: not mon.running), "loop kept running after latch off"
    assert mon.status()["latched_off"]
    summary = mon.stop()  # still stoppable, and still idempotent afterwards
    assert summary["ok"] is True and summary["running"] is False


# ---------- frame sink (the sentinel's seam) ----------

def test_sink_receives_every_frame_with_its_image_and_meta(tmp_path):
    seen = []
    mon = _monitor(tmp_path, FakeCapturer(),
                   frame_sink=lambda dev, img, meta: seen.append((dev, img, meta)))
    mon._capture_once()
    mon._capture_once()

    assert [s[0] for s in seen] == ["dev1", "dev1"]
    # The image is the frame that was written, not bytes or a path.
    assert all(isinstance(s[1], Image.Image) for s in seen)
    assert seen[0][1].size == (720, 1600)  # the downscaled frame that hit disk
    assert [s[2]["index"] for s in seen] == [1, 2]
    assert seen[0][2]["path"].endswith(".png")
    assert mon.status()["sink_errors"] == 0


def test_a_failing_sink_is_counted_and_never_breaks_capture(tmp_path):
    def boom(device_id, image, meta):
        raise RuntimeError("sentinel exploded")

    mon = _monitor(tmp_path, FakeCapturer(), frame_sink=boom)
    for _ in range(3):
        mon._capture_once()

    status = mon.status()
    assert status["sink_errors"] == 3
    # Frames still captured, written and countable — the sink is not the job.
    assert status["frames_total"] == 3
    assert status["failures"] == 0
    assert status["latched_off"] is None
    assert len(list(mon.monitor_dir.glob("*.png"))) == 3


def test_no_sink_means_no_behaviour_change(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer())
    mon._capture_once()
    status = mon.status()
    assert mon.frame_sink is None
    assert status["frames_total"] == 1 and status["sink_errors"] == 0
    assert mon.take_new()["new_count"] == 1


def test_registry_passes_the_sink_through(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    seen = []
    reg = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    try:
        reg.start("dev1", interval_ms=1,
                  frame_sink=lambda dev, img, meta: seen.append(meta["index"]))
        assert _wait_for(lambda: len(seen) >= 2)
    finally:
        reg.stop_all()
    assert seen[:2] == [1, 2]


# ---------- loop health metrics ----------

def test_status_reports_capture_timing(tmp_path):
    mon = _monitor(tmp_path, FakeCapturer())
    assert mon.status()["last_capture_ms"] is None  # nothing measured yet
    mon._capture_once()

    status = mon.status()
    for key in ("overruns", "sink_errors", "last_capture_ms", "avg_capture_ms"):
        assert key in status, f"{key} missing from status()"
    assert status["last_capture_ms"] >= 0
    assert status["avg_capture_ms"] >= 0
    assert status["overruns"] == 0


def test_a_slow_capture_counts_as_an_overrun(tmp_path, monkeypatch):
    """Frames arriving slower than interval_ms is a metric, not a failure."""
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)

    class SlowCapturer(FakeCapturer):
        def capture_image(self, device_id):
            time.sleep(0.05)
            return super().capture_image(device_id)

    mon = FrameMonitor(LOGGER, SlowCapturer(size=(100, 200)), "dev1",
                       interval_ms=1, output_root=str(tmp_path))
    mon.start()
    try:
        assert _wait_for(lambda: mon.status()["overruns"] >= 2)
    finally:
        mon.stop()
    status = mon.status()
    assert status["failures"] == 0          # slow is not broken
    assert status["latched_off"] is None
    assert status["last_capture_ms"] >= 40  # the sleep shows up in the timing


# ---------- lifecycle through the registry ----------

@pytest.fixture
def registry(tmp_path):
    reg = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    yield reg
    reg.stop_all()


def test_start_captures_in_the_background_and_stop_reports_totals(registry, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    started = registry.start("dev1", interval_ms=1, max_frames=50)
    assert started["ok"] is True and started["running"] is True
    assert started["restarted"] is False

    assert _wait_for(lambda: registry.status("dev1")["frames_total"] >= 3)
    stopped = registry.stop("dev1")

    assert stopped["running"] is False
    assert stopped["frames_total"] >= 3
    assert stopped["monitor_dir"] == started["monitor_dir"]
    # Frames survive the stop, so the tail can still be drained.
    assert registry.new_frames("dev1")["new_count"] >= 3


def test_restarting_replaces_the_previous_monitor(registry, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    first = registry.start("dev1", interval_ms=1)
    old = registry._monitors["dev1"]
    second = registry.start("dev1", interval_ms=1)

    assert second["restarted"] is True
    assert old.running is False           # the old loop was stopped, not orphaned
    assert registry._monitors["dev1"] is not old
    # A fresh ring in its own directory; the old run's frames are not mixed in.
    assert second["monitor_dir"] != first["monitor_dir"]
    assert registry._monitors["dev1"]._cursor == 0
    registry.stop("dev1")


def test_stop_is_idempotent(registry, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    registry.start("dev1", interval_ms=1)
    first = registry.stop("dev1")
    second = registry.stop("dev1")

    assert first["ok"] is True and first["already_stopped"] is False
    assert second["ok"] is True and second["already_stopped"] is True
    assert second["running"] is False
    assert second["frames_total"] == first["frames_total"]


def test_stop_joins_the_loop_thread(registry, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    registry.start("dev1", interval_ms=1000)  # sleeping between captures
    thread = registry._monitors["dev1"]._thread
    registry.stop("dev1")
    # Stop wakes the sleep instead of waiting out the interval.
    assert not thread.is_alive()


def test_unknown_device_is_an_error_not_a_crash(registry):
    for result in (registry.new_frames("nope"), registry.stop("nope"),
                   registry.status("nope")):
        assert result["ok"] is False
        assert "start_monitor" in result["error"]


def test_a_monitor_only_sees_its_own_device(registry, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    registry.start("dev1", interval_ms=1)
    registry.start("dev2", interval_ms=1)
    assert _wait_for(lambda: registry.status("dev2")["frames_total"] >= 1)
    a = registry.status("dev1")["monitor_dir"]
    b = registry.status("dev2")["monitor_dir"]
    assert a != b
    registry.stop("dev1")
    registry.stop("dev2")


# ---------- interpreter-exit safety net ----------

def test_atexit_hook_stops_registered_monitors(tmp_path, monkeypatch):
    reg = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    mon = FrameMonitor(LOGGER, FakeCapturer(), "dev1", output_root=str(tmp_path))
    stopped = []
    monkeypatch.setattr(mon, "stop", lambda: stopped.append("stopped"))
    reg._monitors["dev1"] = mon
    assert reg in monitor_mod._active_registries

    monitor_mod.stop_all_monitors()

    assert stopped == ["stopped"]


def test_a_started_monitor_deregisters_on_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    mon = FrameMonitor(LOGGER, FakeCapturer(), "dev1", interval_ms=1,
                       output_root=str(tmp_path))
    mon.start()
    assert mon in monitor_mod._active_monitors
    mon.stop()
    assert mon not in monitor_mod._active_monitors


# ---------- MCP tool face ----------

@pytest.fixture
def no_sentinel(monkeypatch):
    """Frame-supply tests must not spin up the sentinel's real findings chain.

    start_monitor attaches a sentinel by default, and its recorder points at the
    configured outputs/findings tree — a fake capturer's all-black frames read as
    a blank screen, so an unguarded test would write junk findings into the real
    delivery folder. The sentinel's own behaviour is covered in test_sentinel.py
    and by the round trip below, which redirects the output dir.
    """
    monkeypatch.setattr(mcp_server, "_sentinel_config", {"enabled": False})


def test_mcp_start_monitor_attaches_a_sentinel(tmp_path, monkeypatch):
    """The sentinel rides the monitor's frames and reports through the tools."""
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    # Point the sentinel's recorder at tmp_path, never the real findings tree.
    monkeypatch.setattr(mcp_server, "_config",
                        {**mcp_server._config, "findings": {"output_dir": str(tmp_path / "f")}})
    monkeypatch.setattr(mcp_server, "_sentinel_config",
                        {"enabled": True, "blank_min_frames": 2, "logcat_poll_interval_s": 3600})
    registry = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))

    with patch.object(mcp_server, "_monitors", registry):
        started = mcp_server.start_monitor("dev1", interval_ms=1, max_frames=5)
        assert started["sentinel"]["findings_count"] == 0
        # FakeCapturer paints solid black: to the sentinel that is a blank screen.
        assert _wait_for(
            lambda: mcp_server.get_new_frames("dev1")["sentinel"]["findings_count"] >= 1
        ), "the sentinel never saw the blank screen"
        stopped = mcp_server.stop_monitor("dev1")

    sentinel = stopped["sentinel"]
    assert sentinel["finalized"] is True
    # One finding for the whole episode, not one per frame.
    assert sentinel["findings_count"] == 1
    assert sentinel["report"]["report_path"].endswith("report.json")
    # It wrote into the redirected tree, never the real outputs/findings.
    assert str(tmp_path) in sentinel["report"]["run_dir"]


def test_mcp_start_monitor_can_opt_out_of_the_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    registry = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    with patch.object(mcp_server, "_monitors", registry):
        started = mcp_server.start_monitor("dev1", interval_ms=1, sentinel=False)
        assert started["sentinel"] == {"enabled": False}
        assert mcp_server.get_new_frames("dev1")["sentinel"] == {"enabled": False}
        stopped = mcp_server.stop_monitor("dev1")
    assert stopped["sentinel"] == {"enabled": False}
    assert stopped["running"] is False


def test_mcp_monitor_tools_round_trip(tmp_path, monkeypatch, no_sentinel):
    monkeypatch.setattr(monitor_mod, "MIN_INTERVAL_MS", 1)
    registry = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    with patch.object(mcp_server, "_monitors", registry):
        started = mcp_server.start_monitor("dev1", interval_ms=1, max_frames=5)
        assert started["ok"] is True and started["running"] is True
        assert _wait_for(lambda: mcp_server.get_new_frames("dev1")["frames_total"] >= 1)
        stopped = mcp_server.stop_monitor("dev1")
    assert stopped["running"] is False
    assert stopped["monitor_dir"] == started["monitor_dir"]


def test_mcp_monitor_resolves_a_single_device(tmp_path, no_sentinel):
    from core.device_manager import DeviceProfile

    registry = FrameMonitorRegistry(LOGGER, FakeCapturer(), output_root=str(tmp_path))
    with patch.object(mcp_server, "_monitors", registry), \
            patch.object(mcp_server._device_manager, "discover_devices",
                         return_value=[DeviceProfile("only-dev", "physical")]):
        started = mcp_server.start_monitor(interval_ms=1000)
        assert started["device_id"] == "only-dev"
        assert mcp_server.get_new_frames()["device_id"] == "only-dev"
        assert mcp_server.stop_monitor()["running"] is False


def test_mcp_monitor_refuses_to_guess_between_devices(no_sentinel):
    from core.device_manager import DeviceProfile

    devices = [DeviceProfile("a", "physical"), DeviceProfile("b", "wireless")]
    with patch.object(mcp_server._device_manager, "discover_devices", return_value=devices):
        result = mcp_server.start_monitor()
    assert result["ok"] is False
    assert "2 devices connected" in result["error"]

    with patch.object(mcp_server._device_manager, "discover_devices", return_value=[]):
        empty = mcp_server.start_monitor()
    assert empty["ok"] is False and "No device connected" in empty["error"]


def test_monitor_tools_run_off_the_event_loop():
    """They touch the device and the disk, so they must not block the loop."""
    manager = mcp_server.mcp._tool_manager
    for name in ("start_monitor", "get_new_frames", "stop_monitor"):
        assert manager.get_tool(name).is_async, f"{name} must run in a worker thread"
