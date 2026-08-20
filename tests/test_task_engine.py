from __future__ import annotations

import copy
import logging
from typing import Dict, List, Optional

from task.replay_cache import center_distance
from task.task_engine import TaskEngine
from task.task_loader import resolve_task


class FakeHub:
    """Scripted recognizer: maps expected-text -> hit dict (None = miss)."""

    def __init__(self, hits: Dict[str, Optional[Dict]]):
        self.hits = hits
        self.calls: List[str] = []

    def recognize(self, device_id: str, spec: Dict) -> Optional[Dict]:
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        expected = spec["expected"]
        self.calls.append(expected)
        return copy.deepcopy(self.hits.get(expected))


class FakeExecutor:
    def __init__(self, fail_types: Optional[set] = None):
        self.executed: List[Dict] = []
        self.fail_types = fail_types or set()

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        if action["type"] in self.fail_types:
            return {"ok": "False", "stdout": "", "stderr": "boom"}
        return {"ok": "True", "stdout": "", "stderr": ""}


def hit(x: int = 100, y: int = 200, text: str = "t") -> Dict:
    return {"center": (x, y), "text": text, "score": 0.9, "channel": "ui_text"}


def make_engine(hub, executor=None, **kwargs):
    return TaskEngine(hub, executor or FakeExecutor(), logging.getLogger("test"), **kwargs)


def test_linear_task_success(sample_task):
    hub = FakeHub({"设置": hit(300, 400)})
    executor = FakeExecutor()
    engine = make_engine(hub, executor)

    result = engine.run("dev1", sample_task)

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "finish"]
    # click at the recognized center, none action executes nothing
    assert executor.executed == [{"type": "click", "params": {"x": 300, "y": 400}}]


def test_on_step_called_per_recognized_node(sample_task):
    hub = FakeHub({"设置": hit(300, 400)})
    engine = make_engine(hub)
    seen: List[str] = []

    result = engine.run("dev1", sample_task, on_step=lambda node: seen.append(node))

    assert result["status"] == "completed"
    # one callback per node boundary, in order
    assert seen == ["start", "finish"]


def test_next_candidates_first_hit_wins():
    task = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["popup", "main"],
                "timeout_ms": 0,
            },
            "popup": {
                "recognition": {"type": "ui_text", "expected": "弹窗"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
            "main": {
                "recognition": {"type": "ui_text", "expected": "主页"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    # popup misses, main hits -> branch to main
    hub = FakeHub({"弹窗": None, "主页": hit()})
    engine = make_engine(hub)

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "main"]


def test_recognition_timeout_fails_task(sample_task):
    hub = FakeHub({"设置": None})
    engine = make_engine(hub)

    result = engine.run("dev1", sample_task)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "timeout" in result["error"].lower()
    assert result["steps"] == []


def test_on_timeout_recovery():
    task = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "目标"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
                "on_timeout": "recover",
            },
            "recover": {
                "recognition": {"type": "always"},
                "action": {"type": "click", "params": {"x": 1, "y": 2}},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    hub = FakeHub({"目标": None})
    executor = FakeExecutor()
    engine = make_engine(hub, executor)

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["recover"]
    # The authored on_timeout owns this stall: the BACK fallback stays out of
    # the way entirely (no key press), only the recovery node's click runs.
    assert executor.executed == [{"type": "click", "params": {"x": 1, "y": 2}}]
    # The timed-out node's recovery is counted; the recovery node's hit is not
    # a "direct" one.
    assert result["node_stats"]["start"]["timeout_recoveries"] == 1
    assert result["node_stats"]["recover"]["recovery_hits"] == 1
    assert result["node_stats"]["recover"]["direct_hits"] == 0


def agent_task() -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "click", "params": {"x": 1, "y": 1}},
                "next": ["smart_step"],
                "timeout_ms": 0,
            },
            "smart_step": {
                "recognition": {"type": "always"},
                "action": {"type": "agent", "text": "完成滑块验证码"},
                "next": ["finish"],
                "timeout_ms": 0,
            },
            "finish": {
                "recognition": {"type": "ui_text", "expected": "完成"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_agent_action_suspends_with_handoff():
    executor = FakeExecutor()
    engine = make_engine(FakeHub({"完成": hit()}), executor)

    result = engine.run("dev1", agent_task())

    assert result["ok"] is False
    assert result["status"] == "agent_required"
    assert result["error"] is None
    assert result["handoff"] == {"node": "smart_step", "instruction": "完成滑块验证码"}
    # the agent step itself was NOT executed
    assert executor.executed == [{"type": "click", "params": {"x": 1, "y": 1}}]
    assert [s["node"] for s in result["steps"]] == ["start", "smart_step"]


def test_resume_with_start_after():
    engine = make_engine(FakeHub({"完成": hit()}))

    result = engine.run("dev1", agent_task(), start_after="smart_step")

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["finish"]


class _TimelineRecorder:
    """Findings recorder stub that only remembers timeline events."""

    def __init__(self):
        self.events: List[Dict] = []

    def open_run(self, device_id, task_name=None):
        self.events.append({"event": "open_run", "device": device_id})

    def add_timeline(self, event, **detail):
        self.events.append({"event": event, **detail})

    def record(self, finding_type, severity, message, **kwargs):
        pass

    def snapshot_history(self):
        pass

    def finalize(self, status, error=None, node_stats=None):
        return [], {"counts": {}, "report_path": None}


def test_handoff_and_resume_are_both_on_the_timeline():
    """The suspend side logs agent_handoff; the resume side must be symmetric."""
    recorder = _TimelineRecorder()
    engine = make_engine(FakeHub({"完成": hit()}), findings_recorder=recorder, run_log=False)

    engine.run("dev1", agent_task())
    handoff = [e for e in recorder.events if e["event"] == "agent_handoff"]
    assert handoff == [{"event": "agent_handoff", "node": "smart_step"}]

    recorder.events.clear()
    result = engine.run("dev1", agent_task(), start_after="smart_step")

    assert result["status"] == "completed"
    names = [e["event"] for e in recorder.events]
    resume = [e for e in recorder.events if e["event"] == "agent_resume"]
    assert resume == [{"event": "agent_resume", "node": "smart_step"}]
    # recorded after the run was opened (the run folder must exist first)
    assert names.index("open_run") < names.index("agent_resume")


def test_resume_says_so_on_the_console_too(caplog):
    """A resumed run must not look like a fresh one in the terminal."""
    engine = make_engine(FakeHub({"完成": hit()}))

    with caplog.at_level(logging.INFO, logger="test"):
        engine.run("dev1", agent_task(), start_after="smart_step")

    assert any("Task resumed after node 'smart_step'" in r.getMessage() for r in caplog.records)


def test_every_run_ends_with_one_summary_line(sample_task, caplog):
    """A clean run used to finish in silence; now it states its own numbers."""
    engine = make_engine(FakeHub({"设置": hit(300, 400)}))

    with caplog.at_level(logging.DEBUG, logger="test"):
        engine.run("dev1", sample_task, task_name="冒烟")

    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Task '冒烟'")]
    assert len(summary) == 1
    assert "completed: steps=2" in summary[0] and "findings=0" in summary[0]
    # Per-node timing rides along at DEBUG (run.log only, never the console).
    node_done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("EVT node_done")]
    assert len(node_done) == 2  # one per executed node, terminal one included
    assert "node=start" in node_done[0] and "via=ui_text" in node_done[0]
    assert "node=finish" in node_done[1] and "via=always" in node_done[1]


def test_run_lines_name_their_device(sample_task, caplog):
    """Two devices replaying in parallel interleave in one log; every line says
    which phone it belongs to."""
    engine = make_engine(FakeHub({"设置": hit(300, 400)}))

    with caplog.at_level(logging.DEBUG, logger="test"):
        engine.run("dev-A", sample_task, task_name="冒烟")

    lines = _messages(caplog)
    assert any(m.startswith("[step 1] node 'start' recognized") and m.endswith("on dev-A")
               for m in lines)
    assert any(m.startswith("Task '冒烟'") and "device=dev-A" in m for m in lines)
    assert all("device=dev-A" in m for m in lines if m.startswith("EVT node_done"))


class _CountingCapturer:
    """Capturer stand-in exposing the per-backend counters the engine reports."""

    def __init__(self):
        self.reset_calls = 0
        self._stats = {"scrcpy": {"n": 4, "ms": 52, "avg_ms": 13.0}}

    def stats(self):
        return dict(self._stats)

    def reset_stats(self):
        self.reset_calls += 1
        self._stats = {"scrcpy": {"n": 4, "ms": 52, "avg_ms": 13.0}}


def test_finish_reports_the_capture_backend_totals(sample_task, caplog):
    """Slow replays are usually slow at *looking*; the run says so in one line."""
    hub = FakeHub({"设置": hit(300, 400)})
    hub.capturer = _CountingCapturer()
    engine = make_engine(hub)

    with caplog.at_level(logging.DEBUG, logger="test"):
        engine.run("dev1", sample_task)

    stats_lines = [m for m in _messages(caplog) if m.startswith("EVT capture_stats")]
    assert stats_lines == ["EVT capture_stats device=dev1 scrcpy_n=4 scrcpy_avg_ms=13.000"]
    # Counters are scoped to the run, so the next one does not inherit them.
    assert hub.capturer.reset_calls == 1


def test_a_capturer_without_counters_is_simply_skipped(sample_task, caplog):
    engine = make_engine(FakeHub({"设置": hit(300, 400)}))  # hub has no capturer at all

    with caplog.at_level(logging.DEBUG, logger="test"):
        result = engine.run("dev1", sample_task)

    assert result["status"] == "completed"
    assert not [m for m in _messages(caplog) if m.startswith("EVT capture_stats")]


def test_no_resume_event_on_a_normal_run():
    recorder = _TimelineRecorder()
    engine = make_engine(FakeHub({"完成": hit()}), findings_recorder=recorder, run_log=False)

    engine.run("dev1", agent_task())

    assert not [e for e in recorder.events if e["event"] == "agent_resume"]


def test_resume_after_terminal_node_completes():
    engine = make_engine(FakeHub({}))
    task = agent_task()

    result = engine.run("dev1", task, start_after="finish")

    assert result["status"] == "completed"
    assert result["steps"] == []


def test_resume_with_unknown_node_fails():
    engine = make_engine(FakeHub({}))

    result = engine.run("dev1", agent_task(), start_after="ghost")

    assert result["status"] == "failed"
    assert "ghost" in result["error"]


def test_llm_alias_behaves_as_agent():
    task = agent_task()
    task["nodes"]["smart_step"]["action"] = {"type": "llm", "text": "旧格式指令"}
    engine = make_engine(FakeHub({"完成": hit()}))

    result = engine.run("dev1", task)

    assert result["status"] == "agent_required"
    assert result["handoff"]["instruction"] == "旧格式指令"


def test_action_failure_stops_task(sample_task):
    hub = FakeHub({"设置": hit()})
    executor = FakeExecutor(fail_types={"click"})
    engine = make_engine(hub, executor)

    result = engine.run("dev1", sample_task)

    assert result["status"] == "failed"
    assert "start" in result["error"]
    assert [s["node"] for s in result["steps"]] == ["start"]


def test_node_stats_counts_direct_hits_and_poll_rounds(sample_task):
    hub = FakeHub({"设置": hit(300, 400)})
    engine = make_engine(hub)

    result = engine.run("dev1", sample_task)

    stats = result["node_stats"]
    assert stats["start"]["direct_hits"] == 1
    assert stats["finish"]["direct_hits"] == 1
    # One recognition attempt per node, none of it assisted.
    assert stats["start"]["poll_rounds"] == 1
    assert stats["start"]["popup_assisted_hits"] == 0
    assert stats["start"]["back_assisted_hits"] == 0
    assert stats["start"]["drift_count"] == 0


def test_node_stats_are_read_only_telemetry():
    # A candidate that misses is still counted, and counting it changes nothing
    # about which branch is taken.
    task = {
        "entry": "start",
        "nodes": {
            "start": {"recognition": {"type": "always"}, "action": {"type": "none"},
                      "next": ["popup", "main"], "timeout_ms": 0},
            "popup": {"recognition": {"type": "ui_text", "expected": "弹窗"},
                      "action": {"type": "none"}, "next": [], "timeout_ms": 0},
            "main": {"recognition": {"type": "ui_text", "expected": "主页"},
                     "action": {"type": "none"}, "next": [], "timeout_ms": 0},
        },
    }
    engine = make_engine(FakeHub({"弹窗": None, "主页": hit()}))

    result = engine.run("dev1", task)

    assert [s["node"] for s in result["steps"]] == ["start", "main"]
    stats = result["node_stats"]
    assert stats["popup"]["poll_rounds"] == 1 and stats["popup"]["direct_hits"] == 0
    assert stats["main"]["direct_hits"] == 1


# --- wait_still: settle on a still frame before polling `next` ---------------

class _FrameCapturer:
    """Scripted frame source; the last frame repeats once the script runs out."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def capture_image(self, device_id):
        self.calls += 1
        index = min(self.calls - 1, len(self.frames) - 1)
        return self.frames[index]


def _frame(value: int):
    from PIL import Image

    return Image.new("L", (20, 20), color=value)


def _wait_still_task(wait_still=None) -> Dict:
    node = {
        "recognition": {"type": "always"},
        "action": {"type": "click", "params": {"x": 1, "y": 1}},
        "next": ["done"],
        "timeout_ms": 0,
    }
    if wait_still is not None:
        node["wait_still"] = wait_still
    return {
        "entry": "start",
        "nodes": {
            "start": node,
            "done": {"recognition": {"type": "always"}, "action": {"type": "none"},
                     "next": [], "timeout_ms": 0},
        },
    }


def _engine_with_frames(frames, **kwargs):
    hub = FakeHub({})
    hub.capturer = _FrameCapturer(frames)
    return make_engine(hub, **kwargs), hub.capturer


def test_wait_still_releases_once_two_frames_match():
    # Animating (0 -> 200), then two identical frames = settled.
    engine, capturer = _engine_with_frames([_frame(0), _frame(200), _frame(200), _frame(200)])

    result = engine.run(
        "dev1", _wait_still_task({"timeout_ms": 5000, "interval_ms": 0, "threshold": 0.01})
    )

    assert result["status"] == "completed"
    # Round 1 saw the animation still moving, round 2 found the screen still.
    assert result["node_stats"]["start"]["wait_still_rounds"] == 2


def test_wait_still_timeout_continues_without_failing_or_reporting():
    # A screen that never settles: alternating frames forever.
    engine, capturer = _engine_with_frames([_frame(0), _frame(255), _frame(0), _frame(255)])

    result = engine.run(
        "dev1", _wait_still_task({"timeout_ms": 0, "interval_ms": 0, "threshold": 0.01})
    )

    # Giving up waiting is not a failure and records no finding — recognition
    # (which still runs) is what judges a genuine stall.
    assert result["status"] == "completed"
    assert result["findings"] == []
    assert result["node_stats"]["start"]["wait_still_rounds"] == 1


def test_wait_still_absent_costs_nothing():
    engine, capturer = _engine_with_frames([_frame(0)])

    result = engine.run("dev1", _wait_still_task())

    assert result["status"] == "completed"
    assert capturer.calls == 0  # no wait_still field -> not a single extra capture
    assert result["node_stats"]["start"]["wait_still_rounds"] == 0


def test_wait_still_without_capturer_degrades_to_no_wait():
    engine = make_engine(FakeHub({}))  # FakeHub exposes no capturer

    result = engine.run("dev1", _wait_still_task({"interval_ms": 0}))

    assert result["status"] == "completed"
    assert result["node_stats"]["start"]["wait_still_rounds"] == 0


def test_max_steps_guard_breaks_loops():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["a"],
                "timeout_ms": 0,
            },
        },
    }
    engine = make_engine(FakeHub({}), max_steps=5)

    result = engine.run("dev1", task)

    assert result["status"] == "failed"
    assert "Max steps" in result["error"]
    assert len(result["steps"]) == 5


def _looping_task(**overrides) -> Dict:
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["a"],
                "timeout_ms": 0,
            },
        },
    }
    task.update(overrides)
    return task


def test_max_steps_defaults_to_50_when_unconfigured():
    engine = make_engine(FakeHub({}))

    result = engine.run("dev1", _looping_task())

    assert result["status"] == "failed"
    assert "Max steps (50)" in result["error"]
    assert len(result["steps"]) == 50


def test_max_steps_engine_config_overrides_default():
    engine = make_engine(FakeHub({}), engine_config={"max_steps": 3})

    result = engine.run("dev1", _looping_task())

    assert "Max steps (3)" in result["error"]
    assert len(result["steps"]) == 3


def test_max_steps_task_json_overrides_engine_config():
    engine = make_engine(FakeHub({}), engine_config={"max_steps": 20})

    result = engine.run("dev1", _looping_task(max_steps=4))

    assert "Max steps (4)" in result["error"]
    assert len(result["steps"]) == 4


def test_max_steps_invalid_engine_config_falls_back_to_default(caplog):
    with caplog.at_level(logging.WARNING):
        engine = make_engine(FakeHub({}), engine_config={"max_steps": -1})

    assert engine.max_steps == 50
    assert any("Invalid max_steps" in r.message for r in caplog.records)


def test_max_steps_invalid_task_json_falls_back_to_engine_value(caplog):
    engine = make_engine(FakeHub({}), max_steps=6)

    with caplog.at_level(logging.WARNING):
        result = engine.run("dev1", _looping_task(max_steps=0))

    assert "Max steps (6)" in result["error"]
    assert any("Invalid max_steps" in r.message for r in caplog.records)


# --- action-level repeat (params.repeat / repeat_delay_ms / repeat_wait_freezes_ms) ---

class _CountingHub(FakeHub):
    """FakeHub that also tolerates the `image=` kwarg watchdog checks pass."""

    def recognize(self, device_id: str, spec: Dict, image=None, **kwargs) -> Optional[Dict]:
        return super().recognize(device_id, spec)


class _SequencedExecutor:
    """Executor whose ok/failure verdict is scripted per call (last entry repeats)."""

    def __init__(self, oks):
        self.oks = list(oks)
        self.executed: List[Dict] = []

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        ok = self.oks[min(len(self.executed), len(self.oks) - 1)]
        self.executed.append(action)
        return {"ok": "True" if ok else "False", "stdout": "", "stderr": "" if ok else "boom"}


def _repeat_task(params: Dict, action_type: str = "click") -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": action_type, "params": params},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_repeat_fires_the_action_n_times_and_strips_its_own_params():
    executor = FakeExecutor()
    engine = make_engine(FakeHub({}), executor)

    result = engine.run("dev1", _repeat_task({"x": 5, "y": 6, "repeat": 4}))

    assert result["status"] == "completed"
    # Four identical shots, and the repeat knobs never reach the executor.
    assert executor.executed == [{"type": "click", "params": {"x": 5, "y": 6}}] * 4


def test_repeat_absent_or_one_executes_exactly_once():
    executor = FakeExecutor()
    engine = make_engine(FakeHub({}), executor)

    engine.run("dev1", _repeat_task({"keycode": 4, "repeat": 1}, action_type="key"))
    engine.run("dev1", _repeat_task({"keycode": 4}, action_type="key"))

    assert executor.executed == [{"type": "key", "params": {"keycode": 4}}] * 2


def test_repeat_delay_sleeps_only_between_shots(monkeypatch):
    slept: List[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: slept.append(seconds))
    executor = FakeExecutor()
    engine = make_engine(FakeHub({}), executor)

    result = engine.run(
        "dev1", _repeat_task({"x": 1, "y": 2, "repeat": 3, "repeat_delay_ms": 50})
    )

    assert result["status"] == "completed"
    assert len(executor.executed) == 3
    assert slept == [0.05, 0.05]  # gaps only, none after the last shot


def test_repeat_wait_freezes_settles_between_shots(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    executor = FakeExecutor()
    # Still-frame script: moving (0 -> 200), then two identical frames.
    engine, capturer = _engine_with_frames(
        [_frame(0), _frame(200), _frame(200)], executor=executor
    )

    result = engine.run(
        "dev1",
        _repeat_task({"x": 1, "y": 2, "repeat": 2, "repeat_wait_freezes_ms": 5000}),
    )

    assert result["status"] == "completed"
    assert len(executor.executed) == 2
    # One settle window between the two shots: baseline frame + 2 sampling rounds.
    assert capturer.calls == 3
    # The node's own wait_still telemetry stays about wait_still, not repeats.
    assert result["node_stats"]["start"]["wait_still_rounds"] == 0


def test_repeat_wait_freezes_timeout_is_not_a_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    executor = FakeExecutor()
    # A screen that never settles + a zero budget: the wait gives up immediately.
    engine, capturer = _engine_with_frames(
        [_frame(0), _frame(255), _frame(0), _frame(255)], executor=executor
    )

    result = engine.run(
        "dev1", _repeat_task({"x": 1, "y": 2, "repeat": 2, "repeat_wait_freezes_ms": 0})
    )

    # Same semantics as wait_still: stop waiting, never fail, never report.
    assert result["status"] == "completed"
    assert result["findings"] == []
    assert len(executor.executed) == 2


def test_repeat_wait_freezes_without_capturer_still_fires_every_shot(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    executor = FakeExecutor()
    engine = make_engine(FakeHub({}), executor)  # FakeHub exposes no capturer

    result = engine.run(
        "dev1", _repeat_task({"x": 1, "y": 2, "repeat": 3, "repeat_wait_freezes_ms": 5000})
    )

    assert result["status"] == "completed"
    assert len(executor.executed) == 3


def test_repeat_failed_shot_does_not_abort_the_burst(caplog):
    executor = _SequencedExecutor([False, False, True])
    engine = make_engine(FakeHub({}), executor)

    with caplog.at_level(logging.WARNING):
        result = engine.run("dev1", _repeat_task({"x": 1, "y": 2, "repeat": 3}))

    # Two failed shots, last one ok -> the last shot decides: node passes.
    assert result["status"] == "completed"
    assert len(executor.executed) == 3
    assert result["steps"][0]["results"] == [{"ok": "True", "stdout": "", "stderr": ""}]
    assert sum("Repeat shot" in r.message for r in caplog.records) == 2


def test_repeat_last_shot_failure_fails_the_node():
    executor = _SequencedExecutor([True, True, False])
    engine = make_engine(FakeHub({}), executor)

    result = engine.run("dev1", _repeat_task({"x": 1, "y": 2, "repeat": 3}))

    assert result["status"] == "failed"
    assert "boom" in result["error"]
    assert len(executor.executed) == 3  # still no early abort


def test_repeat_does_not_multiply_watchdog_sampling():
    hub = _CountingHub({"错误": None})
    task = _repeat_task({"x": 1, "y": 2, "repeat": 6})
    task["watchdogs"] = [{"type": "ui_text", "expected": "错误"}]
    engine = make_engine(hub)

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    # Still exactly the two-shot sampling per step (action time + settled),
    # regardless of how many shots the burst fired.
    assert hub.calls.count("错误") == 2


# ---------- defaults block: node null opts back to the engine default ----------


class _RecordingRecorder:
    """Findings recorder stub that keeps every record() call for assertions."""

    def __init__(self):
        self.records: List[Dict] = []

    def open_run(self, device_id, task_name=None):
        pass

    def record(self, finding_type, severity, message, **kwargs):
        self.records.append(
            {"type": finding_type, "severity": severity, "message": message, **kwargs}
        )

    def add_timeline(self, event, **detail):
        pass

    def snapshot_history(self):
        pass

    def finalize(self, status, error=None, node_stats=None):
        return list(self.records), {"counts": {}, "report_path": None}


def test_node_null_defaults_fall_back_to_engine_default_and_run_completes():
    """A node that spells a defaults key out as `null` opts OUT of the default.

    The loader must DROP the key so the engine's `.get(field, DEFAULT)` reaches
    its built-in default — not leave a literal None behind, which would make the
    engine's `timeout_ms / 1000` arithmetic raise TypeError (crashing outside
    `_finish` and leaking the run's recorders). This is a real engine.run(), not
    just a loader-dict check.
    """
    raw = {
        "entry": "opt_out",
        "defaults": {
            "timeout_ms": 200,
            "poll_interval_ms": 500,
            "post_delay_ms": 300,
            "wait_still": {"timeout_ms": 3000},
        },
        "nodes": {
            "opt_out": {
                "recognition": {"type": "ui_text", "expected": "设置"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["inherits"],
                # every whitelist key spelled out as null = back to engine default
                "timeout_ms": None,
                "poll_interval_ms": None,
                "post_delay_ms": None,
                "wait_still": None,
            },
            "inherits": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
            },
        },
    }

    resolved = resolve_task(raw, ".")
    opt_out = resolved["nodes"]["opt_out"]
    # The null'd keys are GONE (so `.get(key, DEFAULT)` yields the engine
    # default, neither the defaults value 200/500/300 nor a literal None).
    for key in ("timeout_ms", "poll_interval_ms", "post_delay_ms", "wait_still"):
        assert key not in opt_out
    # A node that stays silent still inherits the defaults block.
    assert resolved["nodes"]["inherits"]["timeout_ms"] == 200

    # And it actually runs: the deadline arithmetic uses the engine default,
    # never crashing on None.
    hub = FakeHub({"设置": hit(300, 400)})
    engine = make_engine(hub)
    result = engine.run("dev1", resolved)

    assert result["ok"] is True
    assert result["status"] == "completed"


def test_node_null_timeout_would_crash_without_the_strip(monkeypatch):
    """Direct proof the engine reads the ENGINE default, not None nor 200.

    With the engine default patched to 0, the opted-out node polls exactly one
    round before its (zero) deadline lapses — deterministic, and only reachable
    if the null'd timeout resolved to the engine default rather than staying 200.
    """
    monkeypatch.setattr("task.task_engine.DEFAULT_TIMEOUT_MS", 0)
    raw = {
        "entry": "opt_out",
        "defaults": {"timeout_ms": 200},
        # BACK fallback off so the stall doesn't add its own recognition round.
        "back_fallback": False,
        "nodes": {
            "opt_out": {
                "recognition": {"type": "ui_text", "expected": "缺失"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": None,
            },
        },
    }
    resolved = resolve_task(raw, ".")

    hub = FakeHub({"缺失": None})  # never hits -> times out on the entry node
    engine = make_engine(hub)
    result = engine.run("dev1", resolved)

    assert result["ok"] is False
    assert "timeout" in result["error"].lower()
    # timeout_ms == engine default (patched 0) -> a single poll round, not the
    # many rounds a 200ms budget would spin through.
    assert result["node_stats"]["opt_out"]["poll_rounds"] == 1


# ---------- combined recognition: non-box_index sub-anchor drift is reported ---


class _ComboDriftHub:
    """Hub whose `and` combo hits with a drifted NON-box_index sub-anchor.

    box_index defaults to 0, so `_combo_hit` copies sub[0] (clean) to the top
    level; sub[1]'s drift metadata lives only in sub_hits. The engine must walk
    the sub_hits to notice it.
    """

    def __init__(self):
        self.replay_cache = object()  # truthy: engine passes cache_key + checks drift

    def recognize(self, device_id: str, spec: Dict, cache_key=None) -> Optional[Dict]:
        rec_type = spec.get("type")
        if rec_type == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        if rec_type == "and":
            sub0 = {"center": (100, 200), "text": "商店", "score": 0.9, "channel": "ocr"}
            sub1 = {
                "center": (900, 1600), "text": "金币", "score": 0.9, "channel": "ocr",
                "cache": "drift", "prev_center": [300, 400],
                "drift_px": round(center_distance([300, 400], (900, 1600)), 1),
            }
            top = dict(sub0)  # box_index 0 -> clean sub copied to the top level
            top.update(channel="and", sub_channel="ocr", sub_index=0,
                       sub_hits=[sub0, sub1])
            return top
        return None


def _combo_drift_task() -> Dict:
    return {
        "entry": "combo",
        "nodes": {
            "combo": {
                "recognition": {
                    "type": "and",
                    "all_of": [
                        {"type": "ocr", "expected": "商店"},
                        {"type": "ocr", "expected": "金币"},
                    ],
                    "box_index": 0,
                },
                "action": {"type": "none"},
                "next": ["done"],
                "timeout_ms": 0,
                "post_delay_ms": 0,
            },
            "done": {"recognition": {"type": "always"}, "action": {"type": "none"},
                     "next": [], "timeout_ms": 0},
        },
    }


def test_combo_reports_drift_of_non_box_index_sub_anchor():
    hub = _ComboDriftHub()
    recorder = _RecordingRecorder()
    engine = make_engine(hub, findings_recorder=recorder)

    result = engine.run("dev1", _combo_drift_task(), task_name="t")

    assert result["status"] == "completed"
    drifts = [r for r in recorder.records if r["type"] == "anchor_drift"]
    # The drifted anchor is sub[1] (金币), NOT the box_index anchor sub[0].
    assert len(drifts) == 1
    assert drifts[0]["node"] == "combo"
    assert drifts[0]["extra"]["prev_center"] == [300, 400]
    assert drifts[0]["extra"]["new_center"] == [900, 1600]
    assert drifts[0]["extra"]["drift_px"] > 0
    # Telemetry increments too — the move is not silently healed.
    assert result["node_stats"]["combo"]["drift_count"] == 1


# ---------- step-by-step logging (what the terminal shows during a run) ----------


class _MissThenHitHub(FakeHub):
    """Misses the first `misses` recognitions, then behaves like FakeHub."""

    def __init__(self, hits: Dict[str, Optional[Dict]], misses: int = 1):
        super().__init__(hits)
        self.misses = misses

    def recognize(self, device_id: str, spec: Dict) -> Optional[Dict]:
        if spec.get("type") != "always" and self.misses > 0:
            self.misses -= 1
            self.calls.append(spec.get("expected"))
            return None
        return super().recognize(device_id, spec)


def _messages(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records]


def test_node_recognition_log_carries_the_step_number(sample_task, caplog):
    caplog.set_level(logging.INFO, logger="test")
    engine = make_engine(FakeHub({"设置": hit(300, 400)}))

    engine.run("dev1", sample_task)

    lines = _messages(caplog)
    assert any(m.startswith("[step 1] node 'start' recognized via ui_text") for m in lines)
    assert any(m.startswith("[step 2] node 'finish' recognized via always") for m in lines)


def test_action_logged_with_resolved_coordinates_and_duration(sample_task, caplog):
    """A `target: recognized` click must log where the tap ACTUALLY landed."""
    caplog.set_level(logging.INFO, logger="test")
    engine = make_engine(FakeHub({"设置": hit(612, 388)}))

    engine.run("dev1", sample_task)

    action_lines = [m for m in _messages(caplog) if " action " in m]
    assert len(action_lines) == 1
    assert action_lines[0].startswith("[step 1] action click (612, 388) ok ")
    assert action_lines[0].endswith("ms")


def test_failed_action_logs_a_warning(sample_task, caplog):
    caplog.set_level(logging.INFO, logger="test")
    engine = make_engine(FakeHub({"设置": hit()}), FakeExecutor(fail_types={"click"}))

    engine.run("dev1", sample_task)

    failed = [r for r in caplog.records if " action " in r.getMessage()]
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert "FAILED" in failed[0].getMessage() and "boom" in failed[0].getMessage()


def test_repeat_burst_logs_one_summary_line_not_one_per_shot(caplog):
    caplog.set_level(logging.INFO, logger="test")
    task = {
        "entry": "mash",
        "nodes": {
            "mash": {
                "recognition": {"type": "ui_text", "expected": "连打"},
                "action": {"type": "click", "params": {"x": 10, "y": 20, "repeat": 4}},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    executor = FakeExecutor()
    engine = make_engine(FakeHub({"连打": hit()}), executor)

    engine.run("dev1", task)

    assert len(executor.executed) == 4  # every shot fired ...
    action_lines = [m for m in _messages(caplog) if " action " in m]
    assert len(action_lines) == 1  # ... but only one line about them
    assert action_lines[0].startswith("[step 1] action click (10, 20) x4 ok ")


def test_poll_misses_go_to_debug_and_heartbeat_to_info(monkeypatch, caplog):
    """A long wait must not leave the terminal silent — nor spam it per poll."""
    monkeypatch.setattr("task.task_engine.POLL_HEARTBEAT_S", 0.0)
    caplog.set_level(logging.DEBUG, logger="test")
    task = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "设置"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 5000,
                "poll_interval_ms": 0,
            },
        },
    }
    engine = make_engine(_MissThenHitHub({"设置": hit()}, misses=1))

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("poll miss #1 on [start]" in m for m in debug)
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(m.startswith("waiting for [start] (") and "1 polls)" in m for m in info)


def test_no_heartbeat_when_recognition_hits_immediately(sample_task, caplog):
    caplog.set_level(logging.DEBUG, logger="test")
    engine = make_engine(FakeHub({"设置": hit()}))

    engine.run("dev1", sample_task)

    assert not [m for m in _messages(caplog) if m.startswith("waiting for")]


def test_post_delay_wait_is_debug_only(caplog):
    caplog.set_level(logging.DEBUG, logger="test")
    task = copy.deepcopy(
        {
            "entry": "start",
            "nodes": {
                "start": {
                    "recognition": {"type": "always"},
                    "action": {"type": "none"},
                    "next": [],
                    "timeout_ms": 0,
                    "post_delay_ms": 1,
                },
            },
        }
    )
    engine = make_engine(FakeHub({}))

    engine.run("dev1", task)

    delay = [r for r in caplog.records if "post_delay" in r.getMessage()]
    assert len(delay) == 1
    assert delay[0].levelno == logging.DEBUG


def test_recent_events_is_empty_without_a_recorder(sample_task):
    engine = make_engine(FakeHub({"设置": hit()}))
    assert engine.recent_events() == []


# ---------- is_running (read by out-of-band observers, e.g. task.sentinel) ----------

def test_is_running_is_set_during_a_run_and_cleared_after(sample_task):
    hub = FakeHub({"设置": hit(300, 400)})
    engine = make_engine(hub)
    seen: List[bool] = []

    assert engine.is_running is False
    result = engine.run("dev1", sample_task, on_step=lambda node: seen.append(engine.is_running))

    assert result["status"] == "completed"
    assert seen and all(seen), "is_running must be true for the whole run"
    assert engine.is_running is False


def test_is_running_is_cleared_when_a_run_raises(sample_task):
    class ExplodingHub(FakeHub):
        def recognize(self, device_id, spec):
            raise RuntimeError("hub is broken")

    engine = make_engine(ExplodingHub({}))
    # The engine turns a recognition blow-up into a failed run, not an
    # exception; either way the flag must not stay set.
    try:
        engine.run("dev1", sample_task)
    except RuntimeError:
        pass
    assert engine.is_running is False
    assert engine.running_device is None


def test_running_device_names_the_phone_of_the_run_in_flight(sample_task):
    # The engine is a singleton across devices, so an observer (task.sentinel)
    # needs the device to tell "busy with me" from "busy with someone else".
    hub = FakeHub({"设置": hit(300, 400)})
    engine = make_engine(hub)
    seen: List[Optional[str]] = []

    assert engine.running_device is None
    engine.run("dev-A", sample_task, on_step=lambda node: seen.append(engine.running_device))

    assert seen and all(d == "dev-A" for d in seen)
    assert engine.running_device is None, "no stale device once the run is over"
