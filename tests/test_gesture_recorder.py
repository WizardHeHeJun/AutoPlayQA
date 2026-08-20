"""Unit tests for the gesture recorder -- calibration + segmentation/classification.

The segmentation tests run against a *real* ``getevent -lt`` capture from a
goodix_ts / Android 14 device (tests/fixtures/getevent_multi.txt), so the parser
is pinned to actual hardware output. Classification edge cases (long_press) that
the capture happens not to contain are covered with synthetic event streams.
"""
import os
from collections import Counter

from record.gesture_recorder import (
    GestureSegmenter,
    GestureThresholds,
    TouchCalibration,
    _parse_touch_device,
    _parse_wm_size,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# Real panel/display values captured from a physical device.
CALIB = TouchCalibration("/dev/input/event7", 143999, 319999, 1080, 2400)


# -- Calibration parsing ----------------------------------------------------------

_GETEVENT_LP = """\
add device 1: /dev/input/event9
  name:     "kalama-mtp-snd-card Button Jack"
add device 3: /dev/input/event7
  name:     "goodix_ts"
    ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
    ABS_MT_POSITION_X     : value 0, min 0, max 143999, fuzz 0, flat 0, resolution 0
    ABS_MT_POSITION_Y     : value 0, min 0, max 319999, fuzz 0, flat 0, resolution 0
"""


def test_parse_touch_device_picks_node_with_abs_mt():
    path, mx, my = _parse_touch_device(_GETEVENT_LP)
    assert path == "/dev/input/event7"
    assert (mx, my) == (143999, 319999)


def test_parse_wm_size_prefers_override():
    out = "Physical size: 1440x3200\nOverride size: 1080x2400\n"
    assert _parse_wm_size(out) == (1080, 2400)


def test_parse_wm_size_falls_back_to_physical():
    assert _parse_wm_size("Physical size: 1440x3200\n") == (1440, 3200)


def test_to_pixels_maps_panel_to_display():
    # Panel centre maps to display centre (within rounding).
    x, y = CALIB.to_pixels(143999 // 2, 319999 // 2)
    assert abs(x - 540) <= 1
    assert abs(y - 1200) <= 1
    # Corners clamp inside bounds.
    assert CALIB.to_pixels(0, 0) == (0, 0)
    assert CALIB.to_pixels(143999, 319999) == (1079, 2399)


# -- Segmentation against the real capture ----------------------------------------

def _segment_fixture(name):
    got = []
    seg = GestureSegmenter(CALIB, on_gesture=got.append)
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        for line in f:
            seg.feed_line(line)
    return got


def test_real_capture_segments_into_expected_gestures():
    got = _segment_fixture("getevent_multi.txt")
    counts = Counter(g.type for g in got)
    # Verified against the actual hardware capture.
    assert counts["multi_touch"] == 1
    assert counts["tap"] == 6
    assert counts["swipe"] == 3
    assert len(got) == 10


def test_real_multi_touch_has_two_pointers():
    got = _segment_fixture("getevent_multi.txt")
    mt = next(g for g in got if g.type == "multi_touch")
    assert max(len(fr["pointers"]) for fr in mt.frames) == 2
    assert len(mt.frames) > 50  # a real pinch is many frames


def test_real_taps_are_in_display_bounds():
    got = _segment_fixture("getevent_multi.txt")
    for g in (x for x in got if x.type == "tap"):
        x, y = g.params["x"], g.params["y"]
        assert 0 <= x < 1080 and 0 <= y < 2400


# -- Synthetic classification edge cases ------------------------------------------

def _line(ts, code, raw, etype="EV_ABS"):
    return f"[  {ts:.6f}] {etype}  {code}  {raw}"


def _single_finger(frames, up_ts):
    """frames = [(ts, panel_x, panel_y), ...]; build a protocol-B single-touch stream."""
    lines = []
    first = True
    for ts, px, py in frames:
        if first:
            lines.append(_line(ts, "ABS_MT_TRACKING_ID", "00000001"))
            lines.append(_line(ts, "BTN_TOUCH", "DOWN", "EV_KEY"))
            first = False
        lines.append(_line(ts, "ABS_MT_POSITION_X", f"{px:08x}"))
        lines.append(_line(ts, "ABS_MT_POSITION_Y", f"{py:08x}"))
        lines.append(_line(ts, "SYN_REPORT", "00000000", "EV_SYN"))
    lines.append(_line(up_ts, "ABS_MT_TRACKING_ID", "ffffffff"))
    lines.append(_line(up_ts, "BTN_TOUCH", "UP", "EV_KEY"))
    lines.append(_line(up_ts, "SYN_REPORT", "00000000", "EV_SYN"))
    return lines


def _run(lines, thresholds=None):
    got = []
    seg = GestureSegmenter(CALIB, thresholds, on_gesture=got.append)
    for ln in lines:
        seg.feed_line(ln)
    return got


def test_classify_tap_short_no_movement():
    lines = _single_finger([(100.0, 72000, 160000)], up_ts=100.05)
    got = _run(lines)
    assert len(got) == 1 and got[0].type == "tap"


def test_classify_long_press_long_hold_no_movement():
    # Same point held ~800ms across several frames -> long_press.
    frames = [(100.0 + i * 0.1, 72000, 160000) for i in range(8)]
    got = _run(_single_finger(frames, up_ts=100.8))
    assert len(got) == 1 and got[0].type == "long_press"
    assert got[0].params["duration_ms"] >= 500


def test_classify_swipe_with_movement():
    # Move ~8000 panel units in X (~60 display px) -> swipe.
    frames = [(100.0, 60000, 160000), (100.05, 64000, 160000), (100.1, 68000, 160000)]
    got = _run(_single_finger(frames, up_ts=100.12))
    assert len(got) == 1 and got[0].type == "swipe"
    assert "path" in got[0].params


def test_long_press_threshold_is_configurable():
    frames = [(100.0 + i * 0.1, 72000, 160000) for i in range(4)]  # ~300ms hold
    got = _run(_single_finger(frames, up_ts=100.3), GestureThresholds(long_press_ms=200))
    assert got[0].type == "long_press"
