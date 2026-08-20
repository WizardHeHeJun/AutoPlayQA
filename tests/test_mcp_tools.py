from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import patch

from PIL import Image

import mcp_server


def _wait_terminal(run_id, timeout_s=5.0):
    """Poll get_run_status until the background run leaves the running state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = mcp_server.get_run_status(run_id)
        if status.get("status") != "running":
            return status
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not finish within {timeout_s}s")


def test_find_text_via_dump():
    node = {"center": (10, 20), "text": "设置", "desc": ""}
    with patch.object(mcp_server._matcher, "match_text", return_value=(node, 0.9)):
        result = mcp_server.find_text("dev1", "设置")
    assert result["found"] is True
    assert result["center"] == [10, 20]
    assert result["channel"] == "ui_text"


def test_find_text_falls_back_to_ocr():
    items = [{"text": "登录", "score": 0.99, "bbox": [0, 0, 10, 10], "center": (5, 5)}]
    with patch.object(mcp_server._matcher, "match_text", return_value=(None, 0.0)), \
            patch.object(mcp_server._ocr, "available", return_value=True), \
            patch.object(mcp_server._ocr, "recognize", return_value=items), \
            patch.object(mcp_server._capturer, "capture_image", return_value=object()):
        result = mcp_server.find_text("dev1", "登录")
    assert result["found"] is True
    assert result["channel"] == "ocr"
    assert result["center"] == [5, 5]


def test_find_text_not_found():
    with patch.object(mcp_server._matcher, "match_text", return_value=(None, 0.0)), \
            patch.object(mcp_server._ocr, "available", return_value=False):
        result = mcp_server.find_text("dev1", "不存在")
    assert result["found"] is False


# --- screenshot preview downscaling ------------------------------------------
#
# The agent pays image tokens per look, so the *returned* frame is normalised to
# a 720px short edge. Device coordinates and the recognition channels must stay
# on the native capture.

def _screenshot_with(image):
    """Run the screenshot tool over a fake capture; returns (result, saved_image)."""
    saved = {}

    def fake_save(img, device_id, prefix):
        saved["image"] = img
        saved["prefix"] = prefix
        return f"/out/{prefix}_{device_id}.png"

    with patch.object(mcp_server._capturer, "capture_image", return_value=image), \
            patch.object(mcp_server._capturer, "save_image", side_effect=fake_save):
        result = mcp_server.screenshot("dev1")
    return result, saved["image"]


def test_screenshot_downscales_short_edge_to_720():
    result, saved = _screenshot_with(Image.new("RGB", (1080, 2400), "black"))
    assert saved.size == (720, 1600)  # short edge capped, aspect ratio kept
    assert (result["image_width"], result["image_height"]) == (720, 1600)
    # Device space is unchanged: click() coordinates still mean native pixels.
    assert (result["width"], result["height"]) == (1080, 2400)
    assert result["scale"] == round(720 / 1080, 4)


def test_screenshot_leaves_small_frames_alone():
    image = Image.new("RGB", (480, 800), "black")
    result, saved = _screenshot_with(image)
    assert saved is image  # no resample, not even a copy
    assert (result["image_width"], result["image_height"]) == (480, 800)
    assert result["scale"] == 1.0


def test_screenshot_full_resolution_bypasses_downscaling():
    image = Image.new("RGB", (1080, 2400), "black")
    saved = {}

    def fake_save(img, device_id, prefix):
        saved["image"] = img
        return "/out/mcp.png"

    with patch.object(mcp_server._capturer, "capture_image", return_value=image), \
            patch.object(mcp_server._capturer, "save_image", side_effect=fake_save):
        result = mcp_server.screenshot("dev1", full_resolution=True)
    assert saved["image"] is image
    assert (result["image_width"], result["image_height"]) == (1080, 2400)
    assert result["scale"] == 1.0


def test_recognition_tools_still_see_the_native_capture():
    """Only the return face shrinks — ocr/find_text keep full-resolution input."""
    image = Image.new("RGB", (1080, 2400), "black")
    seen = []

    def fake_recognize(img, roi=None):
        seen.append(img)
        return []

    with patch.object(mcp_server._capturer, "capture_image", return_value=image), \
            patch.object(mcp_server._ocr, "recognize", side_effect=fake_recognize), \
            patch.object(mcp_server._ocr, "available", return_value=True), \
            patch.object(mcp_server._matcher, "match_text", return_value=(None, 0.0)):
        mcp_server.ocr("dev1")
        mcp_server.find_text("dev1", "登录")

    assert len(seen) == 2
    assert all(img is image for img in seen)


def test_click_routes_to_executor():
    with patch.object(mcp_server._executor, "execute", return_value={"ok": "True"}) as mock_exec:
        mcp_server.click("dev1", 3, 4)
    mock_exec.assert_called_once_with("dev1", {"type": "click", "params": {"x": 3, "y": 4}})


def test_press_key_routes_to_executor():
    with patch.object(mcp_server._executor, "execute", return_value={"ok": "True"}) as mock_exec:
        mcp_server.press_key("dev1", 4)
    mock_exec.assert_called_once_with("dev1", {"type": "key", "params": {"keycode": 4}})


def test_save_task_rejects_bad_json(tmp_path):
    result = mcp_server.save_task("bad", "{not json")
    assert result["ok"] is False
    assert "Invalid JSON" in result["error"]


def test_save_task_rejects_invalid_task():
    result = mcp_server.save_task("bad", json.dumps({"entry": "x"}))
    assert result["ok"] is False
    assert "nodes" in result["error"]


def test_save_task_writes_valid_task(tmp_path, sample_task):
    with patch.object(mcp_server, "DEFAULT_TASK_DIR", tmp_path), \
            patch.object(mcp_server, "get_task_path", lambda name: tmp_path / f"{name}.json"):
        result = mcp_server.save_task("demo", json.dumps(sample_task))
    assert result["ok"] is True
    saved = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))
    assert saved["entry"] == "start"


def test_run_task_passes_start_after(sample_task):
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", return_value={"status": "completed"}) as mock_run:
        result = mcp_server.run_task("dev1", "demo", start_after="start")
    assert result == {"status": "completed"}
    mock_run.assert_called_once_with("dev1", sample_task, start_after="start", task_name="demo")


def test_get_task_schema_mentions_agent():
    schema = mcp_server.get_task_schema()
    assert "agent" in schema and "start_after" in schema


def test_get_run_status_unknown_run():
    result = mcp_server.get_run_status("nope")
    assert result["ok"] is False
    assert "Unknown run_id" in result["error"]


def test_status_poll_answers_while_a_device_tool_is_wedged():
    """A stuck device tool must not freeze the rest of the server.

    FastMCP awaits a *synchronous* tool body inline on its event loop, so a
    blocking adb round trip inside screenshot() used to stop the server reading
    stdin at all: unrelated cheap calls (a get_run_status poll) then hung until
    the client's 1800s idle timeout. Blocking tools now run in a worker thread,
    so this poll must come back immediately while screenshot is still stuck.
    """
    stuck = threading.Event()
    entered = threading.Event()

    def wedged_capture(device_id):
        entered.set()
        stuck.wait(10)  # stands in for an adb round trip that never returns
        raise RuntimeError("released")

    async def scenario():
        started = time.monotonic()
        blocked = asyncio.create_task(
            mcp_server.mcp.call_tool("screenshot", {"device_id": "dev1"})
        )
        # Yielding is the measurement: if the tool body ran on the event loop we
        # would not get control back here until it released, so the clock starts
        # before the yield, not after it.
        while not entered.is_set() and time.monotonic() - started < 5:
            await asyncio.sleep(0.01)
        result = await mcp_server.mcp.call_tool("get_run_status", {"run_id": "nope"})
        elapsed = time.monotonic() - started
        still_wedged = not blocked.done()
        stuck.set()
        try:
            await blocked
        except Exception:  # noqa: BLE001 - the wedged call fails once released
            pass
        return result, elapsed, still_wedged

    with patch.object(mcp_server._capturer, "capture_image", side_effect=wedged_capture):
        result, elapsed, still_wedged = asyncio.run(scenario())

    assert still_wedged, "the device tool finished; it never exercised the wedge"
    assert elapsed < 1.0, f"status poll waited {elapsed:.1f}s behind a wedged tool"
    payload = result[1] if isinstance(result, tuple) else result
    assert "Unknown run_id" in json.dumps(payload, default=str)


def test_blocking_tools_are_registered_off_the_event_loop():
    """Guard the wiring: only pure in-memory readers may stay synchronous."""
    manager = mcp_server.mcp._tool_manager
    pure = {"get_run_status", "get_task_schema"}
    for tool in manager.list_tools():
        if tool.name in pure:
            assert not tool.is_async, f"{tool.name} should stay on the loop"
        else:
            assert tool.is_async, f"{tool.name} can block; it must run in a worker thread"


def test_offloaded_tools_keep_their_schema():
    """The thread-offload wrapper must not blur a tool's parameters."""
    schema = mcp_server.mcp._tool_manager.get_tool("run_task").parameters
    assert set(schema["properties"]) == {"device_id", "name", "start_after", "export_to"}
    assert "recognition-gated replay" in mcp_server.mcp._tool_manager.get_tool("run_task").description


def test_start_task_runs_in_background_and_reports_done(sample_task):
    final = {"status": "completed", "findings": [], "report": None}

    def fake_run(device_id, task, start_after=None, task_name=None, on_step=None):
        if on_step:
            on_step("start")
            on_step("finish")
        return final

    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", side_effect=fake_run):
        started = mcp_server.start_task("dev1", "demo")
        assert started["ok"] is True
        run_id = started["run_id"]
        status = _wait_terminal(run_id)

    assert status["status"] == "done"
    assert status["steps"] == 2
    assert status["current_node"] == "finish"
    assert status["result"] == final
    assert "elapsed_s" in status


def test_get_run_status_surfaces_recent_events_while_running(sample_task):
    """A polling caller needs to tell "slow but moving" from "stuck"."""
    release = threading.Event()
    events = [{"time": "12:00:00", "event": "node_recognized", "detail": {"node": "start"}}]

    def blocking_run(device_id, task, start_after=None, task_name=None, on_step=None):
        release.wait(2.0)
        return {"status": "completed", "findings": [], "report": None}

    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", side_effect=blocking_run), \
            patch.object(mcp_server._engine, "recent_events", return_value=events):
        run_id = mcp_server.start_task("dev1", "demo")["run_id"]
        running = mcp_server.get_run_status(run_id)
        release.set()
        finished = _wait_terminal(run_id)

    assert running["status"] == "running"
    assert running["recent_events"] == events
    # Finished runs carry the full result instead; no stale live feed.
    assert "recent_events" not in finished
    assert finished["result"]["status"] == "completed"


def test_start_task_maps_agent_required(sample_task):
    final = {"status": "agent_required", "handoff": {"node": "gm", "instruction": "tap"}}
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", return_value=final):
        mcp_server._runs.clear()
        run_id = mcp_server.start_task("dev1", "demo")["run_id"]
        status = _wait_terminal(run_id)
    assert status["status"] == "agent_required"
    assert status["result"]["handoff"]["node"] == "gm"


def test_start_task_failed_status_maps_to_error(sample_task):
    final = {"status": "failed", "error": "boom", "findings": [], "report": None}
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", return_value=final):
        mcp_server._runs.clear()
        run_id = mcp_server.start_task("dev1", "demo")["run_id"]
        status = _wait_terminal(run_id)
    assert status["status"] == "error"
    assert status["result"] == final


def test_start_task_exception_reports_error(sample_task):
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", side_effect=RuntimeError("kaboom")):
        mcp_server._runs.clear()
        run_id = mcp_server.start_task("dev1", "demo")["run_id"]
        status = _wait_terminal(run_id)
    assert status["status"] == "error"
    assert "kaboom" in status["error"]


def test_start_task_rejects_second_active_run(sample_task):
    import threading

    release = threading.Event()

    def blocking_run(device_id, task, start_after=None, task_name=None, on_step=None):
        release.wait(2.0)
        return {"status": "completed", "findings": [], "report": None}

    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_task", return_value=sample_task), \
            patch.object(mcp_server._engine, "run", side_effect=blocking_run):
        first = mcp_server.start_task("dev1", "demo")
        assert first["ok"] is True
        second = mcp_server.start_task("dev1", "demo")
        assert second["ok"] is False
        assert first["run_id"] in second["error"]
        release.set()
        _wait_terminal(first["run_id"])


def test_run_suite_reports_case_progress_in_the_background():
    suite = {"name": "mini", "cases": ["case_a", "case_b"]}
    final = {
        "ok": True, "suite": "mini", "cases": [], "duration_s": 1.0,
        "summary": {"cases": 2, "cases_passed": 2},
    }

    def fake_run(device_id, suite_def, export_to=None, on_progress=None):
        on_progress({"event": "case_start", "case": "case_a", "index": 1,
                     "total": 2, "attempt": 1, "boot": "full"})
        on_progress({"event": "node", "case": "case_a", "node": "用例开始"})
        on_progress({"event": "case_end", "case": "case_a", "index": 1, "total": 2,
                     "status": "completed", "duration_s": 1.0, "landed": True,
                     "findings": 0, "error": None, "will_retry": False})
        return final

    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_suite", return_value=suite), \
            patch.object(mcp_server._suite_runner, "run", side_effect=fake_run):
        started = mcp_server.run_suite("mini", "dev1")
        assert started["ok"] is True and started["cases"] == ["case_a", "case_b"]
        status = _wait_terminal(started["run_id"])

    assert status["status"] == "done"
    assert status["kind"] == "suite"
    assert status["case"] == "case_a"
    assert status["cases_total"] == 2 and status["cases_done"] == 1
    assert status["current_node"] == "用例开始"
    assert status["result"] == final


def test_run_suite_rejects_an_unknown_suite():
    from task.task_loader import SuiteValidationError

    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_suite",
                      side_effect=SuiteValidationError("Suite file not found: x")):
        result = mcp_server.run_suite("nope", "dev1")
    assert result["ok"] is False and "not found" in result["error"]
    assert mcp_server._runs == {}


def test_run_suite_with_failing_cases_still_reports_done():
    """Findings/failed cases are the tool working — not a broken MCP call."""
    suite = {"name": "mini", "cases": ["case_a"]}
    final = {"ok": False, "suite": "mini", "cases": [], "duration_s": 1.0,
             "aborted_at": None, "summary": {"cases": 1, "cases_passed": 0}}
    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_suite", return_value=suite), \
            patch.object(mcp_server._suite_runner, "run", return_value=final):
        run_id = mcp_server.run_suite("mini", "dev1")["run_id"]
        status = _wait_terminal(run_id)
    assert status["status"] == "done"
    assert status["result"] == final


def test_run_suite_aborted_maps_to_error_status():
    """Only an orchestration that stopped early (cases left unrun) is an error."""
    suite = {"name": "mini", "cases": ["case_a", "case_b"]}
    final = {"ok": False, "suite": "mini", "cases": [], "duration_s": 1.0,
             "aborted_at": "case_a", "summary": {"cases": 2, "cases_passed": 0}}
    mcp_server._runs.clear()
    with patch.object(mcp_server, "load_suite", return_value=suite), \
            patch.object(mcp_server._suite_runner, "run", return_value=final):
        run_id = mcp_server.run_suite("mini", "dev1")["run_id"]
        status = _wait_terminal(run_id)
    assert status["status"] == "error"


# ---------- classify_scene ----------

def test_classify_scene_returns_the_reading_plus_the_taxonomy():
    """The tool's contract: never a bare label — evidence, checked and the
    label vocabulary come back with every answer."""
    from perception.scene_classifier import SceneReading

    reading = SceneReading(
        scene="popup", confidence=0.87,
        evidence={"signal": "popup", "title": "提示"},
        checked=["blank", "popup"], elapsed_ms={"blank": 1, "popup": 320, "total": 321},
    )
    image = Image.new("RGB", (10, 10))
    with patch.object(mcp_server._capturer, "capture_image", return_value=image), \
            patch.object(mcp_server._scene_classifier, "classify", return_value=reading) as classify:
        result = mcp_server.classify_scene("dev1")
    classify.assert_called_once_with(image, device_id="dev1")
    assert result["scene"] == "popup"
    assert result["confidence"] == 0.87
    assert result["evidence"]["title"] == "提示"
    assert result["checked"] == ["blank", "popup"]
    assert result["elapsed_ms"]["total"] == 321


def test_classify_scene_taxonomy_reports_the_built_in_label_set():
    """The label vocabulary is pluggable, but the *shape* is a contract.

    The framework itself ships exactly one scene — `blank` — and expects the
    integrating project to register the rest; asserting on that (rather than on
    some game's screen names) is what keeps this test game-agnostic.
    """
    from perception.scene_classifier import SCENE_UNKNOWN, SceneReading

    with patch.object(mcp_server._capturer, "capture_image", return_value=Image.new("RGB", (10, 10))), \
            patch.object(mcp_server._scene_classifier, "classify", return_value=SceneReading()):
        result = mcp_server.classify_scene("dev1")

    taxonomy = result["taxonomy"]
    assert set(taxonomy) >= {"labels", "implemented", "planned", "unknown", "signals"}
    assert "blank" in taxonomy["labels"], "the built-in taxonomy always ships `blank`"
    assert "blank" in taxonomy["implemented"], "`blank` is a pixel probe, always available"
    assert taxonomy["unknown"] == SCENE_UNKNOWN
    assert "other_app" in taxonomy["signals"], "`other_app` is a signal, not a scene"


def test_classify_scene_reports_unknown_verbatim():
    """`unknown` is passed through with its probe list, never smoothed over."""
    from perception.scene_classifier import SCENE_UNKNOWN, SceneReading

    reading = SceneReading(checked=["blank", "popup"], elapsed_ms={"total": 9})
    with patch.object(mcp_server._capturer, "capture_image", return_value=Image.new("RGB", (10, 10))), \
            patch.object(mcp_server._scene_classifier, "classify", return_value=reading):
        result = mcp_server.classify_scene("dev1")
    assert result["scene"] == SCENE_UNKNOWN
    assert result["confidence"] == 0.0
    assert result["checked"] == ["blank", "popup"]


# --- stderr on the event loop -------------------------------------------------
#
# FastMCP's run() calls logging.basicConfig(handlers=[RichHandler(stderr)]), and
# mcp's lowlevel server writes one INFO line per request from the event loop
# thread. stderr is a pipe to the client, so an unfiltered firehose into it turns
# the next write into an unbounded block on the loop: requests stop being read
# and a tool call hangs until the client's idle timeout fires, leaving no trace.

def test_root_guard_keeps_fastmcp_from_installing_its_rich_stderr_handler():
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        mcp_server._guard_root_stderr_logging()
        assert root.handlers, "root must be claimed before basicConfig runs"

        # What FastMCP.run() does; basicConfig is a no-op once handlers exist.
        from mcp.server.fastmcp.utilities.logging import configure_logging
        configure_logging("INFO")

        assert [type(h).__name__ for h in root.handlers] == ["StreamHandler"]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)


def test_root_guard_drops_our_own_records_and_the_per_request_line():
    import logging

    from core.logger import LOGGER_NAME

    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        mcp_server._guard_root_stderr_logging()
        guard = root.handlers[0]
        seen = []
        guard.emit = lambda record: seen.append(record.getMessage())  # type: ignore[method-assign]

        # The project's own firehose: already printed by its console handler.
        project = logging.getLogger(LOGGER_NAME)
        project.debug("EVT capture backend=scrcpy ms=11 device=dev1")
        project.warning("scrcpy frame grab failed")
        assert seen == []

        # mcp's per-request line, logged from the event loop thread.
        lowlevel = logging.getLogger("mcp.server.lowlevel.server")
        lowlevel.info("Processing request of type CallToolRequest")
        assert seen == []

        # A genuine third-party warning must still reach stderr.
        logging.getLogger("some_dependency").warning("deprecated thing")
        assert seen == ["deprecated thing"]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
