"""Unit tests for MotionEventBackend -- pointer-diffing protocol generation.

No device required. Verifies that pointer frames translate into the correct
injector line protocol (the same protocol shape validated on-device), plus that
the executor routes the "gesture" action type into the backend.
"""
from __future__ import annotations

from unittest.mock import patch

from action.action_executor import ActionExecutor
from action.backends.motionevent_backend import MotionEventBackend


def _proto(gesture):
    return MotionEventBackend.frames_to_protocol(gesture)


def test_single_tap_down_then_up():
    gesture = {"frames": [
        {"delay_ms": 0, "pointers": [{"id": 0, "x": 540, "y": 1200}]},
        {"delay_ms": 50, "pointers": []},
    ]}
    assert _proto(gesture) == [
        "0 DOWN 0 1 0 540 1200",
        "50 UP 0 1 0 540 1200",
    ]


def test_two_finger_down_move_up_sequence():
    gesture = {"frames": [
        {"delay_ms": 0, "pointers": [{"id": 0, "x": 360, "y": 1200}, {"id": 1, "x": 720, "y": 1200}]},
        {"delay_ms": 16, "pointers": [{"id": 0, "x": 360, "y": 1100}, {"id": 1, "x": 720, "y": 1300}]},
        {"delay_ms": 16, "pointers": []},
    ]}
    proto = _proto(gesture)
    assert proto == [
        # frame 0: first contact down, second contact pointer-down (index 1)
        "0 DOWN 0 1 0 360 1200",
        "0 POINTER_DOWN 1 2 0 360 1200 1 720 1200",
        # frame 1: both move
        "16 MOVE -1 2 0 360 1100 1 720 1300",
        # frame 2: lift both -- pointer-up (index 0) then final up
        "16 POINTER_UP 0 2 0 360 1100 1 720 1300",
        "0 UP 1 1 1 720 1300",
    ]


def test_delay_only_on_first_event_of_frame():
    gesture = {"frames": [
        {"delay_ms": 100, "pointers": [{"id": 0, "x": 10, "y": 10}, {"id": 1, "x": 20, "y": 20}]},
    ]}
    proto = _proto(gesture)
    assert proto[0].startswith("100 DOWN")
    assert proto[1].startswith("0 POINTER_DOWN")  # second event in same frame: no delay


def test_no_move_event_when_positions_unchanged():
    gesture = {"frames": [
        {"delay_ms": 0, "pointers": [{"id": 0, "x": 5, "y": 5}]},
        {"delay_ms": 30, "pointers": [{"id": 0, "x": 5, "y": 5}]},  # same position
        {"delay_ms": 30, "pointers": []},
    ]}
    proto = _proto(gesture)
    # No MOVE emitted; the 30ms delay carries onto the UP of the last frame.
    assert proto == [
        "0 DOWN 0 1 0 5 5",
        "30 UP 0 1 0 5 5",
    ]


def test_synthesize_pinch_frame_count_and_release():
    g = MotionEventBackend.synthesize_pinch(cx=540, cy=1200, start_gap=200, end_gap=600, steps=10)
    assert len(g["frames"]) == 12  # steps+1 move frames + final release
    assert g["frames"][-1]["pointers"] == []
    assert g["frames"][0]["pointers"][0]["id"] == 0
    assert g["frames"][0]["pointers"][1]["id"] == 1


def test_synthesize_pinch_round_trips_through_protocol():
    g = MotionEventBackend.synthesize_pinch(cx=540, cy=1200, start_gap=200, end_gap=600, steps=5)
    proto = _proto(g)
    assert proto[0].startswith("0 DOWN 0 1")
    assert proto[1].startswith("0 POINTER_DOWN 1 2")
    assert proto[-1].startswith("0 UP")
    assert any(line.split()[1] == "MOVE" for line in proto)


# -- Executor routing --------------------------------------------------------------

def test_executor_routes_explicit_frames(fake_logger):
    executor = ActionExecutor(fake_logger, {})
    frames = [{"delay_ms": 0, "pointers": [{"id": 0, "x": 1, "y": 2}]},
              {"delay_ms": 10, "pointers": []}]
    with patch.object(executor.motion, "replay", return_value={"ok": "True"}) as mock_replay:
        result = executor.execute("dev1", {"type": "gesture", "params": {"frames": frames}})
    mock_replay.assert_called_once_with("dev1", {"frames": frames})
    assert result["ok"] == "True"


def test_executor_routes_pinch_spec(fake_logger):
    executor = ActionExecutor(fake_logger, {})
    with patch.object(executor.motion, "replay", return_value={"ok": "True"}) as mock_replay:
        executor.execute("dev1", {"type": "gesture", "params": {
            "pinch": {"cx": 540, "cy": 1200, "start_gap": 200, "end_gap": 600, "steps": 4}}})
    device, gesture = mock_replay.call_args.args
    assert device == "dev1"
    assert gesture["frames"][0]["pointers"][0]["id"] == 0
    assert gesture["frames"][-1]["pointers"] == []


def test_executor_gesture_missing_params_errors(fake_logger):
    executor = ActionExecutor(fake_logger, {})
    result = executor.execute("dev1", {"type": "gesture", "params": {}})
    assert result["ok"] == "False"
    assert "frames" in result["stderr"]
