"""Entry-layer tests for gesture recording (MCP tools + shared session layer).

No real device: the recorder is replaced with a fake that emits gestures on
demand, and the calibration probe / display-size query are patched. Everything
lands under tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp_server
from record import record_session
from record.gesture_recorder import GestureEvent, TouchCalibration
from record.record_session import (
    GestureRecordingRegistry,
    calibrate_device,
    calibration_path,
    read_cached_calibration,
)

CALIB = TouchCalibration("/dev/input/event7", 143999, 319999, 1080, 2400)


class FakeRecorder:
    """Stands in for GestureRecorder: start() hands back the calibration and
    keeps the callback so the test can emit gestures itself."""

    def __init__(self, device_id, calib=CALIB, stop_error=None):
        self.device_id = device_id
        self.calib = calib
        self.stop_error = stop_error
        self.started = False
        self.stopped = False
        self.on_gesture = None

    def start(self, on_gesture):
        self.started = True
        self.on_gesture = on_gesture
        return self.calib

    def stop(self):
        self.stopped = True
        if self.stop_error:
            raise RuntimeError(self.stop_error)

    # -- test helpers -------------------------------------------------------------

    def emit_tap(self, index=1, x=100, y=200):
        event = GestureEvent(
            index, "tap", params={"x": x, "y": y},
            frames=[{"delay_ms": 0, "pointers": [{"id": 0, "x": x, "y": y}]},
                    {"delay_ms": 60, "pointers": []}],
            down_point=(x, y),
        )
        self.on_gesture(event, {"before_png": b"before", "after_png": b"after",
                                "anchor_png": b"anchor"})

    def emit_swipe(self, index=2):
        event = GestureEvent(
            index, "swipe",
            params={"x1": 10, "y1": 20, "x2": 300, "y2": 20, "duration_ms": 180,
                    "path": [(10, 20), (300, 20)]},
            frames=[{"delay_ms": 0, "pointers": [{"id": 0, "x": 10, "y": 20}]},
                    {"delay_ms": 180, "pointers": []}],
            down_point=(10, 20),
        )
        self.on_gesture(event, {"before_png": b"before2", "after_png": None,
                                "anchor_png": b"anchor2"})


class FakeDeviceManager:
    def __init__(self, ids):
        self.ids = ids

    def discover_devices(self):
        return [type("D", (), {"device_id": i})() for i in self.ids]


@pytest.fixture
def registry(tmp_path, fake_logger):
    """A registry wired to tmp dirs; .recorders holds the fakes it handed out."""
    recorders = {}

    def factory(device_id):
        recorders[device_id] = FakeRecorder(device_id)
        return recorders[device_id]

    reg = GestureRecordingRegistry(
        capturer=None, logger=fake_logger,
        output_root=str(tmp_path / "recordings"),
        calibration_dir=str(tmp_path / "touch_calibration"),
        device_manager=FakeDeviceManager(["dev1", "dev2"]),
        recorder_factory=factory,
    )
    reg.recorders = recorders
    return reg


# ---------- calibration cache ----------

def _write_cache(tmp_path, device_id, width, height):
    path = calibration_path(device_id, str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "event_device": "/dev/input/event7", "max_x": 143999, "max_y": 319999,
        "screen_width": width, "screen_height": height,
        "calibrated_at": "2026-06-12T17:56:00+08:00",
    }), encoding="utf-8")
    return path


def test_calibrate_uses_cache_when_display_size_matches(tmp_path):
    _write_cache(tmp_path, "dev1", 1080, 2400)
    with patch.object(record_session, "current_display_size", return_value=(1080, 2400)), \
            patch.object(record_session, "probe_calibration") as probe:
        result = calibrate_device("dev1", cache_dir=str(tmp_path))
    probe.assert_not_called()
    assert result["ok"] is True
    assert result["cached"] is True
    assert result["calibration"]["event_device"] == "/dev/input/event7"


def test_calibrate_reprobes_and_overwrites_on_size_mismatch(tmp_path):
    _write_cache(tmp_path, "dev1", 1440, 3200)
    with patch.object(record_session, "current_display_size", return_value=(1080, 2400)), \
            patch.object(record_session, "probe_calibration", return_value=CALIB) as probe:
        result = calibrate_device("dev1", cache_dir=str(tmp_path))
    probe.assert_called_once_with("dev1")
    assert result["cached"] is False
    assert "display size changed" in result["recalibrated_reason"]
    # Cache overwritten with the fresh probe.
    cached = read_cached_calibration("dev1", str(tmp_path))
    assert (cached["screen_width"], cached["screen_height"]) == (1080, 2400)
    assert "calibrated_at" in cached


def test_calibrate_probes_when_no_cache(tmp_path):
    with patch.object(record_session, "current_display_size", return_value=(1080, 2400)), \
            patch.object(record_session, "probe_calibration", return_value=CALIB) as probe:
        result = calibrate_device("dev9", cache_dir=str(tmp_path))
    probe.assert_called_once()
    assert result["cached"] is False
    assert result["recalibrated_reason"] == "no cached calibration for this device"
    assert calibration_path("dev9", str(tmp_path)).is_file()


def test_calibrate_force_bypasses_a_matching_cache(tmp_path):
    _write_cache(tmp_path, "dev1", 1080, 2400)
    with patch.object(record_session, "current_display_size", return_value=(1080, 2400)), \
            patch.object(record_session, "probe_calibration", return_value=CALIB) as probe:
        result = calibrate_device("dev1", cache_dir=str(tmp_path), force=True)
    probe.assert_called_once()
    assert result["cached"] is False
    assert result["recalibrated_reason"] == "forced re-calibration"


def test_calibrate_reports_probe_failure(tmp_path, fake_logger):
    with patch.object(record_session, "current_display_size", return_value=None), \
            patch.object(record_session, "probe_calibration",
                         side_effect=RuntimeError("No touchscreen with ABS_MT_POSITION")):
        result = calibrate_device("dev1", cache_dir=str(tmp_path), logger=fake_logger)
    assert result["ok"] is False
    assert "ABS_MT_POSITION" in result["error"]


def test_wireless_device_id_is_a_legal_filename(tmp_path):
    path = calibration_path("192.168.1.100:5555", str(tmp_path))
    assert ":" not in path.name
    assert path.name == "192.168.1.100_5555.json"


def test_registry_calibrate_rejects_offline_device(registry):
    result = registry.calibrate("ghost")
    assert result["ok"] is False
    assert "not connected" in result["error"]


# ---------- session lifecycle ----------

def test_start_stop_returns_gestures_and_artifacts(registry, tmp_path):
    started = registry.record_start("dev1")
    assert started["ok"] is True
    assert started["calibration"]["screen_width"] == 1080
    session_dir = Path(started["session_dir"])
    assert session_dir.parent == tmp_path / "recordings"
    assert session_dir.is_dir()

    recorder = registry.recorders["dev1"]
    recorder.emit_tap()
    recorder.emit_swipe()
    result = registry.record_stop("dev1")

    assert result["ok"] is True
    assert recorder.stopped is True
    assert result["gesture_count"] == 2
    tap, swipe = result["gestures"]
    assert tap["type"] == "tap"
    assert tap["params"] == {"x": 100, "y": 200}
    assert tap["down_point"] == [100, 200]
    assert tap["duration_ms"] == 60
    assert tap["t_offset_ms"] >= 0
    assert "recorded_at" in tap
    assert set(tap["images"]) == {"before", "after", "anchor"}
    assert swipe["type"] == "swipe"
    assert swipe["params"]["duration_ms"] == 180
    # Missing frames simply have no path (no placeholder file).
    assert set(swipe["images"]) == {"before", "anchor"}
    # Injector pointer frames stay in the manifest, not in the API payload.
    assert "frames" not in tap

    for gesture in result["gestures"]:
        for path in gesture["images"].values():
            assert Path(path).is_file()
    assert (session_dir / "g001_anchor.png").read_bytes() == b"anchor"
    assert (session_dir / "g002_after.png").exists() is False


def test_manifest_is_self_contained_and_written_per_gesture(registry, tmp_path):
    started = registry.record_start("dev1")
    manifest_path = Path(started["manifest_path"])

    registry.recorders["dev1"].emit_tap()
    # Written before stop -- a crash mid-session still leaves the data on disk.
    mid = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert mid["gesture_count"] == 1
    assert mid["stopped_at"] is None
    assert mid["gestures"][0]["images"]["anchor"] == "g001_anchor.png"  # relative
    assert mid["gestures"][0]["frames"]  # pointer frames kept on disk

    registry.record_stop("dev1")
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final["stopped_at"] is not None
    assert final["device_id"] == "dev1"
    assert final["calibration"]["event_device"] == "/dev/input/event7"


def test_start_refreshes_the_calibration_cache(registry, tmp_path):
    registry.record_start("dev1")
    cached = read_cached_calibration("dev1", str(tmp_path / "touch_calibration"))
    assert cached["max_x"] == 143999


def test_duplicate_start_is_refused(registry):
    first = registry.record_start("dev1")
    second = registry.record_start("dev1")
    assert second["ok"] is False
    assert "already active" in second["error"]
    assert first["session_dir"] == second["session_dir"]
    # The live session survives the rejected call.
    assert registry.record_stop("dev1")["ok"] is True


def test_stop_without_start_is_refused(registry):
    result = registry.record_stop("dev1")
    assert result["ok"] is False
    assert "No gesture recording is active" in result["error"]


def test_second_device_records_independently(registry):
    registry.record_start("dev1")
    registry.record_start("dev2")
    registry.recorders["dev2"].emit_tap()
    assert registry.record_stop("dev2")["gesture_count"] == 1
    assert registry.record_stop("dev1")["gesture_count"] == 0


def test_start_rejects_offline_device(registry):
    result = registry.record_start("ghost")
    assert result["ok"] is False
    assert "not connected" in result["error"]


def test_start_failure_leaves_no_session(tmp_path, fake_logger):
    def boom(device_id):
        raise RuntimeError("adb: device offline")

    reg = GestureRecordingRegistry(
        capturer=None, logger=fake_logger,
        output_root=str(tmp_path / "recordings"),
        calibration_dir=str(tmp_path / "cal"),
        recorder_factory=boom,
    )
    result = reg.record_start("dev1")
    assert result["ok"] is False
    assert "device offline" in result["error"]
    # No half-open session: a following stop reports "nothing running".
    assert reg.record_stop("dev1")["ok"] is False


def test_stop_returns_data_even_if_teardown_fails(tmp_path, fake_logger):
    recorder = FakeRecorder("dev1", stop_error="getevent would not die")
    reg = GestureRecordingRegistry(
        capturer=None, logger=fake_logger,
        output_root=str(tmp_path / "recordings"),
        calibration_dir=str(tmp_path / "cal"),
        recorder_factory=lambda device_id: recorder,
    )
    reg.record_start("dev1")
    recorder.emit_tap()
    result = reg.record_stop("dev1")
    assert result["ok"] is True
    assert result["gesture_count"] == 1
    assert "getevent would not die" in result["warning"]


def test_status_reports_live_session(registry):
    assert registry.status("dev1")["recording"] is False
    registry.record_start("dev1")
    registry.recorders["dev1"].emit_tap()
    status = registry.status("dev1")
    assert status["recording"] is True
    assert status["gesture_count"] == 1
    assert len(registry.status()["recording_devices"]) == 1


# ---------- MCP tool wiring ----------

def test_mcp_tools_delegate_to_the_registry(registry):
    with patch.object(mcp_server, "_gesture_registry", registry):
        cal_result = {"ok": True, "cached": True}
        with patch.object(registry, "calibrate", return_value=cal_result) as calibrate:
            assert mcp_server.calibrate_touch("dev1", force=True) == cal_result
        calibrate.assert_called_once_with("dev1", force=True)

        started = mcp_server.record_gestures_start("dev1")
        assert started["ok"] is True
        registry.recorders["dev1"].emit_tap()
        stopped = mcp_server.record_gestures_stop("dev1")
    assert stopped["gesture_count"] == 1
    assert stopped["gestures"][0]["type"] == "tap"
    assert stopped["gestures"][0]["images"]["anchor"].endswith("g001_anchor.png")


def test_mcp_stop_without_start_returns_error(registry):
    with patch.object(mcp_server, "_gesture_registry", registry):
        result = mcp_server.record_gestures_stop("dev1")
    assert result["ok"] is False
    assert "record_gestures_start" in result["error"]


def test_cli_parses_record_gestures_subcommands():
    from user_interface.command_parser import parse_command

    assert parse_command("record gestures start") == {
        "type": "record_gestures", "sub": "start", "device_id": None}
    assert parse_command("record gestures stop dev1") == {
        "type": "record_gestures", "sub": "stop", "device_id": "dev1"}
    assert parse_command("record gestures status")["sub"] == "status"
    assert parse_command("record gestures")["type"] == "unknown"
    # The pre-existing NL-command recorder switches are untouched.
    assert parse_command("record on") == {"type": "record_control", "sub": "on"}


def test_mcp_duplicate_start_returns_error(registry):
    with patch.object(mcp_server, "_gesture_registry", registry):
        mcp_server.record_gestures_start("dev1")
        result = mcp_server.record_gestures_start("dev1")
        mcp_server.record_gestures_stop("dev1")
    assert result["ok"] is False
    assert "already active" in result["error"]
