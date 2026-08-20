"""request_stop: the cooperative clean stop ends a run THROUGH _finish.

Two check points are covered: the node boundary (stop asked while a node is
executing) and the recognition poll (stop asked mid-poll must not wait out the
timeout budget, and must not fall through to recovery / popup sweep / BACK).
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Dict, List, Optional

from task.task_engine import TaskEngine


class FakeHub:
    """Scripted recognizer: expected-text -> hit dict (None = miss)."""

    def __init__(self, hits: Dict[str, Optional[Dict]], on_recognize=None):
        self.hits = hits
        self.calls: List[str] = []
        self.on_recognize = on_recognize

    def recognize(self, device_id: str, spec: Dict) -> Optional[Dict]:
        if self.on_recognize:
            self.on_recognize(spec)
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        expected = spec["expected"]
        self.calls.append(expected)
        return copy.deepcopy(self.hits.get(expected))


class FakeExecutor:
    def __init__(self):
        self.executed: List[Dict] = []

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        return {"ok": "True", "stdout": "", "stderr": ""}


def make_engine(hub, executor=None):
    return TaskEngine(hub, executor or FakeExecutor(), logging.getLogger("test"))


def chain_task() -> Dict:
    """start -> mid -> finish, everything recognizable instantly."""
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["mid"],
                "timeout_ms": 0,
            },
            "mid": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["finish"],
                "timeout_ms": 0,
            },
            "finish": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_stop_at_node_boundary_finishes_cleanly():
    hub = FakeHub({})
    engine = make_engine(hub)

    def on_step(node: str) -> None:
        if node == "start":
            engine.request_stop()

    result = engine.run("dev1", chain_task(), on_step=on_step)

    # Went through _finish: a full result dict, not an exception.
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == TaskEngine.STOP_ERROR
    # Stopped after the node that asked: the chain never reached "finish".
    executed = [s["node"] for s in result["steps"]]
    assert "start" in executed
    assert "finish" not in executed


def test_stop_mid_poll_skips_timeout_budget_and_recovery():
    task = chain_task()
    # "mid" can never be recognized and has a big poll budget plus an authored
    # recovery; the stop must win over both.
    task["nodes"]["start"]["next"] = ["blocked"]
    task["nodes"]["blocked"] = {
        "recognition": {"type": "ui_text", "expected": "永不出现"},
        "action": {"type": "none"},
        "next": [],
        "timeout_ms": 60000,
        "poll_interval_ms": 10,
        "on_timeout": "start",
    }

    engine_ref = {}

    def stop_on_miss(spec: Dict) -> None:
        if spec.get("expected") == "永不出现":
            engine_ref["engine"].request_stop()

    hub = FakeHub({"永不出现": None}, on_recognize=stop_on_miss)
    engine = make_engine(hub)
    engine_ref["engine"] = engine

    started = time.monotonic()
    result = engine.run("dev1", task)
    elapsed = time.monotonic() - started

    assert result["status"] == "failed"
    assert result["error"] == TaskEngine.STOP_ERROR
    # Not the timeout error, and nowhere near the 60s budget.
    assert "timeout" not in result["error"].lower()
    assert elapsed < 5


def test_stop_flag_resets_between_runs():
    hub = FakeHub({})
    engine = make_engine(hub)

    def on_step(node: str) -> None:
        if node == "start":
            engine.request_stop()

    stopped = engine.run("dev1", chain_task(), on_step=on_step)
    assert stopped["error"] == TaskEngine.STOP_ERROR

    # A later run must not inherit the stale stop flag.
    clean = engine.run("dev1", chain_task())
    assert clean["ok"] is True
    assert clean["status"] == "completed"
