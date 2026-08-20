"""Engine-level stall escapes: known-benign popup sweep + unknown-popup BACK fallback.

A whitelisted popup (user agreement / in-game warning) that stalls recognition
is dismissed automatically WITHOUT recording a finding. What the whitelist
cannot clear is an UNKNOWN blocker: the engine reports it
(`unknown_popup_backoff`, evidence pinned to the blocking frame), presses BACK
once, and only retries recognition if the screen actually changed — an
unresolved stall still lands in the normal timeout path.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from task.task_engine import TaskEngine


def hit(x: int = 100, y: int = 200, text: str = "t") -> Dict:
    return {"center": (x, y), "text": text, "score": 0.9, "channel": "ui_text"}


class StatefulHub:
    """`state["popup"]` gates whether the popup trigger or the main screen shows."""

    def __init__(self, state: Dict):
        self.state = state
        self.calls: List[str] = []

    def recognize(self, device_id: str, spec: Dict, image=None, cache_key=None) -> Optional[Dict]:
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        exp = spec.get("expected")
        self.calls.append(exp)
        if exp == "用户协议":
            return hit(text="用户协议") if self.state["popup"] else None
        if exp == "主页":
            return None if self.state["popup"] else hit(500, 600, "主页")
        return None


class DismissingExecutor:
    """Executing the dismiss (BACK) action clears the popup."""

    def __init__(self, state: Dict):
        self.state = state
        self.executed: List[Dict] = []

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        if action["type"] == "key":
            self.state["popup"] = False
        return {"ok": "True", "stdout": "", "stderr": ""}


class RecordingRecorder:
    """Captures findings vs timeline so a test can prove a dismissal is NOT a finding."""

    def __init__(self, context_fails: bool = False):
        self.findings: List[Dict] = []
        self.timeline: List[Dict] = []
        self.context_images: List[Dict] = []
        self.context_fails = context_fails

    def open_run(self, *a, **k):
        pass

    def save_context_image(self, tag, image):
        # Mirrors FindingsRecorder: returns a run-relative path, or None when
        # the write failed (never raises).
        self.context_images.append({"tag": tag, "image": image})
        if self.context_fails:
            return None
        return f"{tag}.png"

    def record(self, finding_type, severity, message, **kwargs):
        self.findings.append(
            {"type": finding_type, "severity": severity, "message": message, **kwargs}
        )

    def add_timeline(self, event, **detail):
        self.timeline.append({"event": event, **detail})

    def snapshot_history(self):
        pass

    def finalize(self, status, error=None, node_stats=None):
        self.node_stats = node_stats
        return self.findings, {"report_path": None}


def _popup_task() -> Dict:
    return {
        "entry": "start",
        "popups": [{
            "name": "user_agreement",
            "recognition": {"type": "ui_text", "expected": "用户协议"},
            "action": {"type": "key", "params": {"keycode": 4}},
        }],
        "nodes": {
            "start": {"recognition": {"type": "always"}, "action": {"type": "none"},
                      "next": ["main"], "timeout_ms": 0, "poll_interval_ms": 0},
            "main": {"recognition": {"type": "ui_text", "expected": "主页"}, "action": {"type": "none"},
                     "next": [], "timeout_ms": 0, "poll_interval_ms": 0},
        },
    }


def _engine(hub, executor, recorder=None, **kwargs):
    eng = TaskEngine(hub, executor, logging.getLogger("test"), findings_recorder=recorder, **kwargs)
    eng._popup_settle_ms = lambda: 0  # don't sleep in tests
    return eng


def test_known_popup_is_dismissed_and_flow_continues():
    state = {"popup": True}
    hub = StatefulHub(state)
    executor = DismissingExecutor(state)
    engine = _engine(hub, executor)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "completed"
    assert result["popups_dismissed"] == ["user_agreement"]
    # The dismiss action ran exactly once (BACK), clearing the popup.
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]


def test_dismissal_is_not_recorded_as_a_finding():
    state = {"popup": True}
    hub = StatefulHub(state)
    recorder = RecordingRecorder()
    engine = _engine(hub, DismissingExecutor(state), recorder=recorder)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "completed"
    # No finding for a known-benign popup, but it IS visible on the timeline.
    assert recorder.findings == []
    assert any(e["event"] == "popup_dismissed" and e["name"] == "user_agreement"
               for e in recorder.timeline)


def test_dismissal_is_logged_even_with_a_recorder_attached(caplog):
    """Timeline AND log: with a recorder (the default) the timeline is invisible
    to whoever is watching the terminal, and an auto-click needs explaining."""
    caplog.set_level(logging.INFO, logger="test")
    state = {"popup": True}
    engine = _engine(StatefulHub(state), DismissingExecutor(state), recorder=RecordingRecorder())

    engine.run("dev1", _popup_task(), task_name="t")

    messages = [r.getMessage() for r in caplog.records]
    assert any("popup 'user_agreement' dismissed" in m for m in messages)


def test_sweep_only_runs_on_stall_not_on_happy_path():
    # Popup already gone: the main screen is recognized immediately, so the
    # sweep never fires (no dismiss action, nothing dismissed).
    state = {"popup": False}
    hub = StatefulHub(state)
    executor = DismissingExecutor(state)
    engine = _engine(hub, executor)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "completed"
    assert result["popups_dismissed"] == []
    assert executor.executed == []


def test_unknown_blocking_screen_still_times_out():
    # The whitelist trigger never matches (popup stays True so 主页 never shows,
    # but 用户协议 is replaced by an unknown text) -> nothing dismissed. The BACK
    # fallback tries one press, but the screen stays blocked -> timeout.
    state = {"popup": True}

    class OnlyMainHub(StatefulHub):
        def recognize(self, device_id, spec, image=None, cache_key=None):
            if spec.get("type") == "always":
                return {"center": None, "text": "", "score": 1.0, "channel": "always"}
            # Neither the main screen nor the whitelisted popup is ever present.
            return None

    hub = OnlyMainHub(state)
    executor = DismissingExecutor(state)
    engine = _engine(hub, executor)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "failed"
    assert result["popups_dismissed"] == []
    # Nothing was dismissed by the whitelist; the only action is the single
    # BACK fallback press, which did not unblock anything either.
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]


# ---------- `confirm`: the second gate before an unattended click ----------


class ConfirmHub(StatefulHub):
    """Adds a `确认文案` anchor the popup entry's `confirm` looks for.

    `state["confirm"]` decides whether that second anchor is on screen, so a
    test can present a popup whose trigger matches but whose identity does not.
    """

    def __init__(self, state: Dict, capturer=None):
        super().__init__(state)
        self.capturer = capturer
        self.frames: List = []

    def recognize(self, device_id: str, spec: Dict, image=None, cache_key=None):
        if spec.get("expected") == "确认文案":
            self.frames.append(image)
            return hit(text="确认文案") if self.state.get("confirm") else None
        if spec.get("expected") == "用户协议":
            self.frames.append(image)
        return super().recognize(device_id, spec, image=image, cache_key=cache_key)


def _confirm_task() -> Dict:
    task = _popup_task()
    task["popups"][0]["confirm"] = {"type": "ocr", "expected": "确认文案"}
    return task


def test_confirm_gate_passes_and_the_popup_is_dismissed():
    state = {"popup": True, "confirm": True}
    executor = DismissingExecutor(state)
    engine = _engine(ConfirmHub(state), executor)

    result = engine.run("dev1", _confirm_task(), task_name="t")

    assert result["status"] == "completed"
    assert result["popups_dismissed"] == ["user_agreement"]
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]


def test_confirm_miss_does_not_click_and_does_not_count():
    # The trigger matches but the confirm anchor is absent — exactly the
    # 2026-08-11 shape, where a shared close-X template matched the panel under
    # test. Nothing may be clicked; the stall stays a stall and gets reported.
    state = {"popup": True, "confirm": False}
    executor = DismissingExecutor(state)
    recorder = RecordingRecorder()
    engine = _engine(ConfirmHub(state), executor, recorder=recorder)

    result = engine.run("dev1", _confirm_task(), task_name="t")

    assert result["popups_dismissed"] == []
    # The only action is the BACK fallback's single press, not the dismiss.
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]
    assert all(e["event"] != "popup_dismissed" for e in recorder.timeline)
    # Missing the sweep is not silent: the blocker is reported as an unknown
    # one and the node is only reached via the generic escape.
    assert [f["type"] for f in recorder.findings] == ["unknown_popup_backoff"]
    assert result["node_stats"]["main"]["back_assisted_hits"] == 1
    assert result["node_stats"]["main"]["popup_assisted_hits"] == 0


def test_confirm_failure_is_treated_as_a_miss():
    """A confirm that blows up must not degrade to 'confirmed' — no click."""
    state = {"popup": True, "confirm": True}

    class ExplodingConfirmHub(ConfirmHub):
        def recognize(self, device_id, spec, image=None, cache_key=None):
            if spec.get("expected") == "确认文案":
                raise RuntimeError("ocr engine died")
            return super().recognize(device_id, spec, image=image, cache_key=cache_key)

    executor = DismissingExecutor(state)
    engine = _engine(ExplodingConfirmHub(state), executor)

    result = engine.run("dev1", _confirm_task(), task_name="t")

    assert result["popups_dismissed"] == []
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]  # BACK only


def test_confirm_reads_the_same_frame_as_the_trigger():
    # Both gates must judge one moment of an animating screen, not two.
    state = {"popup": True, "confirm": True}
    capturer = FrameCapturer({"blocked": False})
    hub = ConfirmHub(state, capturer=capturer)
    engine = _engine(hub, DismissingExecutor(state))

    engine.run("dev1", _confirm_task(), task_name="t")

    assert len(hub.frames) >= 2
    trigger_frame, confirm_frame = hub.frames[0], hub.frames[1]
    assert trigger_frame is not None
    assert confirm_frame is trigger_frame


def test_entry_without_confirm_keeps_the_old_behaviour():
    state = {"popup": True}
    executor = DismissingExecutor(state)
    engine = _engine(ConfirmHub(state), executor)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["popups_dismissed"] == ["user_agreement"]


# ---------- popup evidence: the frame the click was decided on ----------


def test_popup_evidence_is_pinned_and_linked_from_the_timeline():
    state = {"popup": True}
    capturer = FrameCapturer({"blocked": False})
    hub = ConfirmHub(state, capturer=capturer)
    recorder = RecordingRecorder()
    engine = _engine(hub, DismissingExecutor(state), recorder=recorder)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "completed"
    # Saved once, tagged with a serial number so repeat sweeps of the same
    # popup cannot overwrite each other.
    assert [c["tag"] for c in recorder.context_images] == ["popup_01_user_agreement"]
    # The saved frame IS the frame the match was made on, not a re-capture.
    assert recorder.context_images[0]["image"] is hub.frames[0]

    dismissed = [e for e in recorder.timeline if e["event"] == "popup_dismissed"]
    assert len(dismissed) == 1
    assert dismissed[0]["evidence"] == "popup_01_user_agreement.png"
    assert dismissed[0]["score"] == 0.9
    assert dismissed[0]["center"] == [100, 200]
    # Still not a finding: evidence only, no anomaly recorded.
    assert recorder.findings == []


def test_popup_evidence_is_logged_next_to_the_dismissal(caplog):
    caplog.set_level(logging.INFO, logger="test")
    state = {"popup": True}
    hub = ConfirmHub(state, capturer=FrameCapturer({"blocked": False}))
    engine = _engine(hub, DismissingExecutor(state), recorder=RecordingRecorder())

    engine.run("dev1", _popup_task(), task_name="t")

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "popup 'user_agreement' dismissed (evidence popup_01_user_agreement.png)" in m
        for m in messages
    )


def test_popup_evidence_failure_does_not_block_the_sweep():
    state = {"popup": True}
    hub = ConfirmHub(state, capturer=FrameCapturer({"blocked": False}))
    recorder = RecordingRecorder(context_fails=True)
    engine = _engine(hub, DismissingExecutor(state), recorder=recorder)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["status"] == "completed"
    assert result["popups_dismissed"] == ["user_agreement"]
    dismissed = [e for e in recorder.timeline if e["event"] == "popup_dismissed"]
    assert dismissed[0]["evidence"] is None


def test_popup_evidence_skipped_without_a_frame():
    # No capturer reachable -> no frame -> nothing to pin, and the sweep still
    # works (the entry's recognition grabs its own frame per channel).
    state = {"popup": True}
    recorder = RecordingRecorder()
    engine = _engine(StatefulHub(state), DismissingExecutor(state), recorder=recorder)

    result = engine.run("dev1", _popup_task(), task_name="t")

    assert result["popups_dismissed"] == ["user_agreement"]
    assert recorder.context_images == []


# ---------- unknown-popup BACK fallback ----------


class BlockedHub:
    """`state["blocked"]` gates the main screen; nothing on the whitelist shows.

    Models an UNKNOWN overlay: the task's popup whitelist can't see it, so the
    sweep is powerless and only the BACK fallback can move things along.
    """

    def __init__(self, state: Dict, capturer=None):
        self.state = state
        self.capturer = capturer  # the engine grabs frames through the hub
        self.calls: List[str] = []

    def recognize(self, device_id: str, spec: Dict, image=None, cache_key=None) -> Optional[Dict]:
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        exp = spec.get("expected")
        self.calls.append(exp)
        if exp == "主页" and not self.state["blocked"]:
            return hit(500, 600, "主页")
        return None


class BackExecutor:
    """Records actions; a BACK press clears the blocker when `unblocks`."""

    def __init__(self, state: Dict, unblocks: bool = True):
        self.state = state
        self.unblocks = unblocks
        self.executed: List[Dict] = []

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        if action["type"] == "key" and self.unblocks:
            self.state["blocked"] = False
        return {"ok": "True", "stdout": "", "stderr": ""}


class FrameCapturer:
    """Frames for the pixel diff: black while blocked, white once unblocked.

    `frozen=True` keeps returning the same frame, i.e. the BACK press changed
    nothing on screen.
    """

    def __init__(self, state: Dict, frozen: bool = False):
        self.state = state
        self.frozen = frozen
        self.stream_enabled = False
        self.calls = 0

    def capture_image(self, device_id: str):
        from PIL import Image

        self.calls += 1
        blocked = True if self.frozen else self.state["blocked"]
        return Image.new("RGB", (32, 32), (0, 0, 0) if blocked else (255, 255, 255))


def _blocked_task() -> Dict:
    """Same shape as _popup_task, but the whitelist entry never matches."""
    task = _popup_task()
    task["nodes"]["main"]["timeout_ms"] = 0
    return task


def test_back_fallback_reports_unknown_popup_then_escapes():
    state = {"blocked": True}
    capturer = FrameCapturer(state)
    hub = BlockedHub(state, capturer=capturer)
    executor = BackExecutor(state)
    recorder = RecordingRecorder()
    engine = _engine(hub, executor, recorder=recorder)

    result = engine.run("dev1", _blocked_task(), task_name="t")

    # BACK unblocked the screen, so the extra recognition round found 主页.
    assert result["status"] == "completed"
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]
    backoffs = [f for f in recorder.findings if f["type"] == "unknown_popup_backoff"]
    assert len(backoffs) == 1
    assert backoffs[0]["severity"] == "warning"
    # Evidence is pinned to the frame that showed the blocker, captured before
    # the press that may have wiped it.
    assert backoffs[0]["image"] is not None
    assert any(e["event"] == "back_fallback" for e in recorder.timeline)
    # The node was reached thanks to the fallback, and node_stats says so.
    assert result["node_stats"]["main"]["back_assisted_hits"] == 1
    assert result["node_stats"]["main"]["direct_hits"] == 0


def test_back_fallback_without_screen_change_does_not_retry():
    state = {"blocked": True}
    capturer = FrameCapturer(state, frozen=True)  # BACK changes nothing visible
    hub = BlockedHub(state, capturer=capturer)
    executor = BackExecutor(state, unblocks=False)
    recorder = RecordingRecorder()
    engine = _engine(hub, executor, recorder=recorder)

    result = engine.run("dev1", _blocked_task(), task_name="t")

    assert result["status"] == "failed"
    # One press, and no recognition round wasted on an unchanged screen.
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]
    assert hub.calls.count("主页") == 1
    assert [f["type"] for f in recorder.findings] == ["unknown_popup_backoff"]


def test_back_fallback_presses_back_once_per_stall():
    # The screen keeps changing (so the retry is granted) but never unblocks:
    # still exactly one press — chained BACKs could walk out of the game.
    state = {"blocked": True}

    class FlickeringCapturer(FrameCapturer):
        def capture_image(self, device_id: str):
            from PIL import Image

            self.calls += 1
            shade = 0 if self.calls % 2 else 255
            return Image.new("RGB", (32, 32), (shade, shade, shade))

    capturer = FlickeringCapturer(state)
    hub = BlockedHub(state, capturer=capturer)
    executor = BackExecutor(state, unblocks=False)
    engine = _engine(hub, executor)

    result = engine.run("dev1", _blocked_task(), task_name="t")

    assert result["status"] == "failed"
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]
    # The granted retry ran exactly one extra recognition round.
    assert hub.calls.count("主页") == 2


def test_back_fallback_skipped_when_node_defines_on_timeout():
    # An authored on_timeout is a designed recovery and outranks the generic
    # BACK: pressing it first could push the screen off the state that branch
    # expects. So: no press, no backoff finding, straight to the recovery node.
    state = {"blocked": True}
    task = _blocked_task()
    task["nodes"]["main"]["on_timeout"] = "rescue"
    task["nodes"]["rescue"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"},
        "next": [], "timeout_ms": 0, "poll_interval_ms": 0,
    }
    hub = BlockedHub(state, capturer=FrameCapturer(state))
    executor = BackExecutor(state)
    recorder = RecordingRecorder()
    engine = _engine(hub, executor, recorder=recorder)

    result = engine.run("dev1", task, task_name="t")

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "rescue"]
    assert executor.executed == []
    assert all(f["type"] != "unknown_popup_backoff" for f in recorder.findings)
    assert [f["type"] for f in recorder.findings] == ["timeout_recovery"]


def test_back_fallback_covers_dead_end_without_on_timeout():
    # Mirror image of the test above: with no authored escape the stall would
    # just fail the run, so the fallback is exactly what steps in.
    state = {"blocked": True}
    hub = BlockedHub(state, capturer=FrameCapturer(state))
    executor = BackExecutor(state)
    recorder = RecordingRecorder()
    engine = _engine(hub, executor, recorder=recorder)

    result = engine.run("dev1", _blocked_task(), task_name="t")

    assert result["status"] == "completed"
    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}]
    assert [f["type"] for f in recorder.findings] == ["unknown_popup_backoff"]


def test_back_fallback_disabled_by_config():
    state = {"blocked": True}
    hub = BlockedHub(state, capturer=FrameCapturer(state))
    executor = BackExecutor(state)
    recorder = RecordingRecorder()
    engine = _engine(hub, executor, recorder=recorder, engine_config={"back_fallback": False})

    result = engine.run("dev1", _blocked_task(), task_name="t")

    assert result["status"] == "failed"
    assert executor.executed == []
    assert recorder.findings == []


def test_back_fallback_disabled_by_task_json():
    state = {"blocked": True}
    hub = BlockedHub(state, capturer=FrameCapturer(state))
    executor = BackExecutor(state)
    task = _blocked_task()
    task["back_fallback"] = False  # e.g. a battle we must not exit
    engine = _engine(hub, executor)

    result = engine.run("dev1", task, task_name="t")

    assert result["status"] == "failed"
    assert executor.executed == []


def test_back_fallback_is_not_bug_skip():
    # on_finding routes REPORTED bugs (watchdog / crash). The backoff finding
    # must not trigger it: a stall is not a bug-skip trigger.
    state = {"blocked": True}
    task = _blocked_task()
    task["on_finding"] = "rescue"
    task["nodes"]["rescue"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"},
        "next": [], "timeout_ms": 0, "poll_interval_ms": 0,
    }
    hub = BlockedHub(state, capturer=FrameCapturer(state, frozen=True))
    engine = _engine(hub, BackExecutor(state, unblocks=False))

    result = engine.run("dev1", task, task_name="t")

    assert result["status"] == "failed"
    assert [s["node"] for s in result["steps"]] == ["start"]
