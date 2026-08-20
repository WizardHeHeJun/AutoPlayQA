from __future__ import annotations

from unittest.mock import patch

from action.action_executor import ActionExecutor


def make_executor(fake_logger):
    return ActionExecutor(fake_logger, {"execution": {"default_swipe_duration_ms": 500}})


def test_click_routes_to_adb(fake_logger):
    executor = make_executor(fake_logger)
    with patch.object(executor.adb, "click", return_value={"ok": "True"}) as mock_click:
        result = executor.execute("dev1", {"type": "click", "params": {"x": 10, "y": 20}})
    mock_click.assert_called_once_with("dev1", 10, 20)
    assert result["ok"] == "True"


def test_drag_uses_default_duration(fake_logger):
    executor = make_executor(fake_logger)
    with patch.object(executor.adb, "drag", return_value={"ok": "True"}) as mock_drag:
        executor.execute("dev1", {"type": "drag", "params": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}})
    mock_drag.assert_called_once_with("dev1", 1, 2, 3, 4, 500)


def test_input_text_routes_to_adb(fake_logger):
    executor = make_executor(fake_logger)
    with patch.object(executor.adb, "input_text", return_value={"ok": "True"}) as mock_input:
        executor.execute("dev1", {"type": "input_text", "params": {"text": "hello"}})
    mock_input.assert_called_once_with("dev1", "hello")


def test_wait_action_sleeps(fake_logger):
    executor = make_executor(fake_logger)
    with patch("action.action_executor.time.sleep") as mock_sleep:
        result = executor.execute("dev1", {"type": "wait", "params": {"duration_ms": 1500}})
    mock_sleep.assert_called_once_with(1.5)
    assert result["ok"] == "True"


def test_key_routes_to_adb(fake_logger):
    executor = make_executor(fake_logger)
    with patch.object(executor.adb, "press_key", return_value={"ok": "True"}) as mock_key:
        executor.execute("dev1", {"type": "key", "params": {"keycode": 4}})
    mock_key.assert_called_once_with("dev1", 4)


def test_unsupported_action_type(fake_logger):
    executor = make_executor(fake_logger)
    result = executor.execute("dev1", {"type": "teleport", "params": {}})
    assert result["ok"] == "False"
    assert "Unsupported" in result["stderr"]
