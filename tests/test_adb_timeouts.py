"""Every blocking adb call must be bounded.

An adb command without `timeout=` waits forever when the adb server wedges or a
device stops answering, which is how a QA run (or an MCP tool call) ends up
hanging silently instead of failing. These tests pin the contract per call site:
the timeout is passed, and when it fires the caller either follows its existing
fallback chain or returns a clear error — never blocks.
"""
from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

import pytest

from action.backends.adb_backend import AdbBackend
from action.backends.motionevent_backend import MotionEventBackend
from core.adb_timeout import (
    DEFAULT_ADB_TIMEOUT_S,
    AdbTimeout,
    adb_timeout_s,
    configure_adb_timeout,
    reset_adb_timeout,
)
from core.device_manager import DeviceManager
from perception.screenshot_capturer import ScreenshotCapturer
from perception.ui_dump_matcher import UiDumpMatcher

LOG = logging.getLogger("test")


def timeout_expired(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=args[0] if args else "adb", timeout=30)


@pytest.fixture(autouse=True)
def _default_timeout():
    reset_adb_timeout()
    yield
    reset_adb_timeout()


# -- the knob ---------------------------------------------------------------

def test_default_timeout_is_active_without_config():
    assert adb_timeout_s() == DEFAULT_ADB_TIMEOUT_S


def test_config_overrides_timeout():
    assert configure_adb_timeout({"timeout_s": 12}) == 12
    assert adb_timeout_s() == 12


@pytest.mark.parametrize("bad", [{}, {"timeout_s": None}, {"timeout_s": "abc"}, {"timeout_s": 0},
                                 {"timeout_s": -5}, None])
def test_invalid_timeout_config_keeps_the_default(bad):
    """A bad value must never disable the timeout — that is the failure mode."""
    configure_adb_timeout(bad)
    assert adb_timeout_s() == DEFAULT_ADB_TIMEOUT_S


# -- screenshots (the chain a latched-off scrcpy stream falls back to) ------

def screencap_capturer(tmp_path):
    return ScreenshotCapturer(
        LOG, output_dir=str(tmp_path), capture_config={"backend": "screencap"},
    )


def test_screencap_passes_the_timeout(tmp_path):
    cap = screencap_capturer(tmp_path)
    configure_adb_timeout({"timeout_s": 7})
    with patch("perception.screenshot_capturer.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = b"\x00" * 32
        try:
            cap.capture_png_bytes("dev1")
        except Exception:  # noqa: BLE001 - payload is deliberately bogus
            pass
    assert run.call_args_list[0].kwargs["timeout"] == 7


def test_capture_image_raises_instead_of_hanging(tmp_path):
    """Wedged transport: both chain levels time out, so the caller gets an error."""
    cap = screencap_capturer(tmp_path)
    with patch("perception.screenshot_capturer.subprocess.run", side_effect=timeout_expired) as run:
        with pytest.raises(AdbTimeout):
            cap.capture_image("dev1")
    assert run.call_count == 2  # raw, then the `-p` fallback: the chain still ran
    # The transport was stuck, not the raw payload: keep the fast path armed.
    assert cap._raw_capture_ok is True


def test_raw_timeout_still_falls_back_to_device_png(tmp_path):
    """A slow link can blow the timeout on the ~10MB raw buffer while the
    device-encoded PNG (~1MB) still arrives — the `-p` level must stay usable."""
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (3, 2), (1, 2, 3)).save(buf, format="PNG")

    class Png:
        returncode = 0
        stdout = buf.getvalue()
        stderr = b""

    cap = screencap_capturer(tmp_path)
    with patch("perception.screenshot_capturer.subprocess.run",
               side_effect=[subprocess.TimeoutExpired(cmd="adb", timeout=30), Png()]):
        img = cap.capture_image("dev1")
    assert img.size == (3, 2)
    assert cap._raw_capture_ok is False  # raw is too heavy for this link


# -- ui dump (two-level fallback chain must survive a timeout) --------------

def test_dump_timeout_returns_empty_and_keeps_the_file_fallback():
    matcher = UiDumpMatcher(LOG)
    with patch("perception.ui_dump_matcher.subprocess.run", side_effect=timeout_expired) as run:
        assert matcher.dump_ui_xml("dev1") == ""
    # tty dump, then the file dump fallback: the chain still ran, bounded.
    assert run.call_count == 2
    assert all(call.kwargs["timeout"] == DEFAULT_ADB_TIMEOUT_S for call in run.call_args_list)


def test_dump_falls_back_to_file_when_tty_times_out():
    matcher = UiDumpMatcher(LOG)
    xml = '<?xml version="1.0"?><hierarchy><node bounds="[0,0][10,10]" text="hi"/></hierarchy>'

    class Ok:
        returncode = 0
        stdout = xml
        stderr = ""

    class Dumped:
        returncode = 0
        stdout = "UI hierarchy dumped to: /sdcard/window_dump.xml"
        stderr = ""

    with patch("perception.ui_dump_matcher.subprocess.run",
               side_effect=[subprocess.TimeoutExpired(cmd="adb", timeout=30), Dumped(), Ok()]):
        out = matcher.dump_ui_xml("dev1")
    assert out.startswith("<?xml")


# -- actions ----------------------------------------------------------------

def test_click_timeout_reports_a_failed_action():
    backend = AdbBackend(LOG)
    with patch("action.backends.adb_backend.subprocess.run", side_effect=timeout_expired):
        result = backend.click("dev1", 1, 2)
    assert result["ok"] == "False"
    assert "timed out" in result["stderr"]


def test_click_passes_the_timeout():
    backend = AdbBackend(LOG)
    configure_adb_timeout({"timeout_s": 9})
    with patch("action.backends.adb_backend.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        backend.press_key("dev1", 4)
    assert run.call_args.kwargs["timeout"] == 9


def test_gesture_budget_adds_the_scripted_duration():
    """A long scripted gesture is legitimate wait time, not a stuck adb."""
    gesture = {"frames": [{"delay_ms": 0, "pointers": []},
                          {"delay_ms": 2000, "pointers": []},
                          {"delay_ms": 3000, "pointers": []}]}
    assert MotionEventBackend._gesture_duration_s(gesture) == 5.0


def test_gesture_injection_timeout_reports_failure():
    backend = MotionEventBackend(LOG)
    backend._installed_on.add("dev1")
    with patch("action.backends.motionevent_backend.subprocess.run", side_effect=timeout_expired):
        result = backend.replay("dev1", {"frames": [{"delay_ms": 0, "pointers": []}]})
    assert result["ok"] == "False"
    assert "timed out" in result["stderr"]


# -- device discovery -------------------------------------------------------

def test_device_manager_uses_the_configured_timeout():
    configure_adb_timeout({"timeout_s": 11})
    with patch("core.device_manager.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "List of devices attached\n"
        run.return_value.stderr = ""
        DeviceManager(LOG).discover_devices()
    assert run.call_args.kwargs["timeout"] == 11
