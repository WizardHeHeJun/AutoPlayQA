"""Tests for the agent action log (record/action_log.py + its MCP wiring).

No real device: screenshot bytes are handed in by the caller, and the MCP tests
patch the executor/capturer. Everything lands under tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp_server
from record.action_log import (
    ActionLogError,
    ActionLogRegistry,
    ActionLogSession,
    action_succeeded,
    find_element_at,
)

MARKS = [
    {"index": 1, "source": "dump", "text": "面板", "desc": "",
     "center": [100, 200], "bounds": [0, 0, 400, 400], "clickable": False},
    {"index": 2, "source": "dump", "text": "商店", "desc": "",
     "center": [100, 200], "bounds": [80, 180, 120, 220], "clickable": True},
    {"index": 3, "source": "ocr", "text": "别处", "desc": "",
     "center": [900, 900], "bounds": [880, 880, 920, 920], "clickable": False},
]


@pytest.fixture
def registry(tmp_path, fake_logger):
    return ActionLogRegistry(fake_logger, output_root=str(tmp_path / "agent_sessions"))


def _manifest(session) -> dict:
    return json.loads(Path(session.manifest_path).read_text(encoding="utf-8"))


# ---------- element reverse lookup ----------

def test_find_element_at_hits_the_element_under_the_point():
    assert find_element_at(MARKS, 900, 900)["index"] == 3


def test_find_element_at_prefers_the_smallest_overlapping_element():
    # (100, 200) is inside both the panel (#1) and the button (#2).
    assert find_element_at(MARKS, 100, 200)["index"] == 2


def test_find_element_at_misses_and_empty_tables_return_none():
    assert find_element_at(MARKS, 600, 600) is None
    assert find_element_at([], 100, 200) is None
    assert find_element_at(None, 100, 200) is None


def test_find_element_at_skips_malformed_bounds():
    marks = [{"index": 1, "bounds": None}, {"index": 2, "bounds": [0, 0, "x", 5]},
             {"index": 3, "bounds": [0, 0, 10, 10]}]
    assert find_element_at(marks, 5, 5)["index"] == 3


def test_find_element_at_returns_a_copy():
    element = find_element_at(MARKS, 900, 900)
    element["text"] = "mutated"
    assert MARKS[2]["text"] == "别处"


def test_action_succeeded_reads_the_string_ok_flag():
    assert action_succeeded({"ok": "True"}) is True
    assert action_succeeded({"ok": "False"}) is False
    assert action_succeeded({"ok": True}) is True
    assert action_succeeded({"stdout": ""}) is True  # no flag = assume success


# ---------- session lifecycle ----------

def test_start_creates_the_session_and_manifest(registry, tmp_path):
    session = registry.start("dev1", kind="explore", label="shop-run")
    summary = session.summary()
    session_dir = Path(summary["session_dir"])

    assert session_dir.parent == tmp_path / "agent_sessions"
    assert session_dir.name.endswith("_shop-run")
    assert session_dir.is_dir()
    assert registry.active("dev1") is session

    manifest = _manifest(session)
    assert manifest["device_id"] == "dev1"
    assert manifest["ended_at"] is None
    assert manifest["steps"] == []
    assert manifest["context"] == {"kind": "explore", "task": None, "node": None,
                                   "run_id": None, "label": "shop-run"}


def test_handoff_context_carries_task_node_and_run_id(registry):
    session = registry.start("dev1", kind="handoff", task="daily", node="open_settings",
                             run_id="run-7")
    assert _manifest(session)["context"] == {
        "kind": "handoff", "task": "daily", "node": "open_settings",
        "run_id": "run-7", "label": None}
    # No label -> the folder is named after the kind.
    assert Path(session.session_dir).name.endswith("_handoff")


def test_duplicate_start_is_refused_and_keeps_the_live_session(registry):
    first = registry.start("dev1")
    with pytest.raises(ActionLogError) as exc:
        registry.start("dev1")
    assert "already active" in str(exc.value)
    assert registry.active("dev1") is first
    assert registry.stop("dev1")["ok"] is True


def test_second_device_logs_independently(registry):
    a = registry.start("dev1")
    b = registry.start("dev2")
    assert a.session_dir != b.session_dir
    a.log_step("click", {"type": "click", "params": {"x": 1, "y": 2}})
    assert registry.stop("dev2")["step_count"] == 0
    assert registry.stop("dev1")["step_count"] == 1


def test_stop_without_start_is_refused(registry):
    result = registry.stop("dev1")
    assert result["ok"] is False
    assert "record_actions_start" in result["error"]


def test_colliding_session_dirs_get_a_numeric_suffix(registry):
    # Two sessions inside the same second (the folder name's resolution).
    with patch("record.action_log.time.strftime", return_value="20260101_010101"):
        first = registry.start("dev1", label="explore")
        registry.stop("dev1")
        second = registry.start("dev1", label="explore")
        registry.stop("dev1")
        third = registry.start("dev1", label="explore")
    assert Path(first.session_dir).name == "20260101_010101_explore"
    assert Path(second.session_dir).name == "20260101_010101-1_explore"
    assert Path(third.session_dir).name == "20260101_010101-2_explore"


def test_unsafe_labels_become_legal_folder_names(registry):
    session = registry.start("192.168.1.100:5555", label="商店 / run:1")
    name = Path(session.session_dir).name
    assert ":" not in name and "/" not in name and " " not in name


# ---------- steps ----------

def test_log_step_writes_the_manifest_after_every_step(registry):
    session = registry.start("dev1", kind="explore")

    step = session.log_step(
        "click_index", {"type": "click", "params": {"x": 100, "y": 200}},
        element=MARKS[1], screenshot_png=b"\x89PNG-1",
    )
    assert step["index"] == 1
    assert step["screenshot"] == "s001_before.png"
    assert step["t_offset_ms"] >= 0

    # Readable on disk before stop -- an interrupted session keeps its data.
    mid = _manifest(session)
    assert len(mid["steps"]) == 1
    logged = mid["steps"][0]
    assert logged["tool"] == "click_index"
    assert logged["action"] == {"type": "click", "params": {"x": 100, "y": 200}}
    assert logged["element"]["text"] == "商店"
    assert logged["element"]["bounds"] == [80, 180, 120, 220]
    assert logged["screenshot"] == "s001_before.png"  # relative: folder is portable
    assert (Path(session.session_dir) / "s001_before.png").read_bytes() == b"\x89PNG-1"

    session.log_step("swipe", {"type": "drag", "params": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}})
    steps = _manifest(session)["steps"]
    assert [s["index"] for s in steps] == [1, 2]
    assert steps[1]["element"] is None
    assert steps[1]["screenshot"] is None
    assert not (Path(session.session_dir) / "s002_before.png").exists()


def test_log_step_copies_the_element_so_later_edits_do_not_leak(registry):
    session = registry.start("dev1")
    element = dict(MARKS[1])
    session.log_step("click_index", {"type": "click", "params": {}}, element=element)
    element["text"] = "mutated"
    assert _manifest(session)["steps"][0]["element"]["text"] == "商店"


def test_finish_stamps_ended_at_and_returns_the_summary(registry):
    session = registry.start("dev1", kind="explore", label="run")
    session.log_step("press_key", {"type": "key", "params": {"keycode": 4}})
    result = registry.stop("dev1")

    assert result["ok"] is True
    assert result["device_id"] == "dev1"
    assert result["session_dir"] == Path(session.session_dir).as_posix()
    assert result["step_count"] == 1
    assert result["context"]["kind"] == "explore"
    assert result["ended_at"] is not None
    assert result["steps"][0]["tool"] == "press_key"

    final = _manifest(session)
    assert final["ended_at"] == result["ended_at"]
    assert registry.active("dev1") is None


def test_status_reports_the_live_session(registry):
    assert registry.status("dev1")["logging"] is False
    registry.start("dev1")
    assert registry.status("dev1")["logging"] is True
    assert len(registry.status()["logging_devices"]) == 1


def test_unwritable_screenshot_does_not_lose_the_step(tmp_path, fake_logger):
    session = ActionLogSession("dev1", tmp_path / "s", context=None, logger=fake_logger)
    session.start()
    with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
        step = session.log_step("click", {"type": "click", "params": {}},
                                screenshot_png=b"png")
    assert step["screenshot"] is None
    assert len(_manifest(session)["steps"]) == 1


# ---------- MCP wiring ----------

@pytest.fixture
def mcp_registry(tmp_path, fake_logger):
    """Patch the server's registry onto tmp_path, with a fake device backend."""
    reg = ActionLogRegistry(fake_logger, output_root=str(tmp_path / "agent_sessions"))
    with patch.object(mcp_server, "_action_log", reg), \
            patch.object(mcp_server._executor, "execute",
                         return_value={"ok": "True", "stdout": "", "stderr": ""}), \
            patch.object(mcp_server._capturer, "capture_png_bytes", return_value=b"frame"):
        yield reg


def test_actions_are_untouched_without_a_session(mcp_registry):
    result = mcp_server.click("dev1", 3, 4)
    assert result == {"ok": "True", "stdout": "", "stderr": ""}
    mcp_server._executor.execute.assert_called_once_with(
        "dev1", {"type": "click", "params": {"x": 3, "y": 4}})
    # No session -> no screenshot cost at all.
    mcp_server._capturer.capture_png_bytes.assert_not_called()


def test_mcp_start_stop_logs_every_action_tool(mcp_registry):
    started = mcp_server.record_actions_start("dev1", kind="explore", label="demo")
    assert started["ok"] is True
    session_dir = Path(started["session_dir"])

    with patch.dict(mcp_server._last_marks, {"dev1": MARKS}, clear=False):
        mcp_server.click_index("dev1", 2)
        mcp_server.click("dev1", 900, 900)     # bare click over a marked element
        mcp_server.click("dev1", 600, 600)     # bare click over nothing
        mcp_server.swipe("dev1", 1, 2, 3, 4, duration_ms=200)
        mcp_server.input_text("dev1", "hello")
        mcp_server.press_key("dev1", 4)

    stopped = mcp_server.record_actions_stop("dev1")
    assert stopped["ok"] is True
    assert stopped["step_count"] == 6

    manifest = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    steps = manifest["steps"]
    assert [s["tool"] for s in steps] == [
        "click_index", "click", "click", "swipe", "input_text", "press_key"]
    assert steps[0]["element"]["index"] == 2
    assert steps[0]["action"] == {"type": "click", "params": {"x": 100, "y": 200}}
    assert steps[1]["element"]["index"] == 3       # reverse-looked-up
    assert steps[2]["element"] is None
    assert steps[3]["action"]["type"] == "drag"
    assert steps[3]["action"]["params"]["duration_ms"] == 200
    assert steps[4]["action"] == {"type": "input_text", "params": {"text": "hello"}}
    assert steps[5]["action"] == {"type": "key", "params": {"keycode": 4}}
    for step in steps:
        assert (session_dir / step["screenshot"]).read_bytes() == b"frame"


def test_failed_actions_are_not_logged(mcp_registry):
    mcp_server.record_actions_start("dev1")
    with patch.object(mcp_server._executor, "execute",
                      return_value={"ok": "False", "stderr": "device offline"}):
        mcp_server.click("dev1", 3, 4)
    assert mcp_server.record_actions_stop("dev1")["step_count"] == 0


def test_screenshot_failure_still_logs_the_step(mcp_registry):
    mcp_server.record_actions_start("dev1")
    with patch.object(mcp_server._capturer, "capture_png_bytes",
                      side_effect=RuntimeError("adb timed out")):
        mcp_server.click("dev1", 3, 4)
    stopped = mcp_server.record_actions_stop("dev1")
    assert stopped["step_count"] == 1
    assert stopped["steps"][0]["screenshot"] is None


def test_mcp_duplicate_start_returns_an_error(mcp_registry):
    first = mcp_server.record_actions_start("dev1")
    second = mcp_server.record_actions_start("dev1")
    assert second["ok"] is False
    assert "already active" in second["error"]
    assert second["session_dir"] == first["session_dir"]
    assert mcp_server.record_actions_stop("dev1")["ok"] is True


def test_mcp_stop_without_start_returns_an_error(mcp_registry):
    result = mcp_server.record_actions_stop("dev1")
    assert result["ok"] is False
    assert "record_actions_start" in result["error"]
