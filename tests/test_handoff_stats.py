from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from task.handoff_stats import format_handoff_report, scan_handoffs


def _write_session(base: Path, name: str, session, raw: str = None) -> Path:
    session_dir = base / name
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "session.json"
    path.write_text(
        raw if raw is not None else json.dumps(session, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _step(action_type="click", text="商店", index=1):
    element = None
    if text is not None:
        element = {
            "index": index, "source": "dump", "text": text, "desc": "",
            "center": [100, 200], "bounds": [80, 180, 120, 220], "clickable": True,
        }
    return {
        "index": index,
        "t_offset_ms": index * 500,
        "tool": "click_index",
        "action": {"type": action_type, "params": {"x": 100, "y": 200}},
        "element": element,
        "screenshot": f"s{index:03d}_before.png",
    }


def _session(kind="handoff", task="smoke_test", node="滑块验证", steps=None, started_at=None):
    return {
        "device_id": "dev1",
        "started_at": started_at or datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "context": {"kind": kind, "task": task, "node": node, "run_id": None, "label": None},
        "steps": steps if steps is not None else [_step()],
    }


def test_empty_dir_returns_empty_dict(tmp_path):
    assert scan_handoffs(tmp_path / "nope") == {}
    assert scan_handoffs(tmp_path) == {}


def test_explore_sessions_are_filtered_out(tmp_path):
    _write_session(tmp_path, "s1", _session(kind="explore", task=None, node=None))
    _write_session(tmp_path, "s2", _session())

    data = scan_handoffs(tmp_path)

    assert list(data) == ["smoke_test"]
    assert data["smoke_test"]["滑块验证"]["sessions"] == 1


def test_handoff_without_task_is_skipped(tmp_path):
    _write_session(tmp_path, "s1", _session(task=None))

    assert scan_handoffs(tmp_path) == {}


def test_corrupt_and_non_object_sessions_are_skipped(tmp_path):
    _write_session(tmp_path, "s1", None, raw="{not json at all")
    _write_session(tmp_path, "s2", ["a list, not an object"])
    _write_session(tmp_path, "s3", _session())

    data = scan_handoffs(tmp_path)

    assert data["smoke_test"]["滑块验证"]["sessions"] == 1


def test_signature_aggregation_counts_distinct_flows(tmp_path):
    same = [_step("click", "商店", 1), _step("click", "购买", 2)]
    other = [_step("click", "商店", 1), _step("key", None, 2)]
    _write_session(tmp_path, "s1", _session(steps=list(same)))
    _write_session(tmp_path, "s2", _session(steps=list(same)))
    _write_session(tmp_path, "s3", _session(steps=list(other)))

    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]

    assert agg["sessions"] == 3
    assert agg["signatures"] == 2
    assert agg["dominant_ratio"] == round(2 / 3, 3)
    assert agg["dominant_signature"] == [["click", "商店"], ["click", "购买"]]
    # 2/3 < 0.8 -> genuinely variable, stays an agent step
    assert agg["solidify_candidate"] is False


def test_solidify_candidate_needs_three_sessions(tmp_path):
    for i in range(2):
        _write_session(tmp_path, f"s{i}", _session())

    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]

    assert agg["sessions"] == 2
    assert agg["dominant_ratio"] == 1.0
    # identical twice is a coincidence, not a pattern
    assert agg["solidify_candidate"] is False


def test_solidify_candidate_at_the_threshold(tmp_path):
    for i in range(3):
        _write_session(tmp_path, f"s{i}", _session())

    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]

    assert agg["sessions"] == 3
    assert agg["dominant_ratio"] == 1.0
    assert agg["solidify_candidate"] is True


def test_ratio_just_below_threshold_is_not_a_candidate(tmp_path):
    # 4 identical + 1 different = 0.8 exactly -> candidate;
    # 3 identical + 1 different = 0.75 -> not.
    for i in range(3):
        _write_session(tmp_path, f"same{i}", _session())
    _write_session(tmp_path, "odd", _session(steps=[_step("key", None, 1)]))

    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]
    assert agg["dominant_ratio"] == 0.75
    assert agg["solidify_candidate"] is False

    _write_session(tmp_path, "same4", _session())
    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]
    assert agg["dominant_ratio"] == 0.8
    assert agg["solidify_candidate"] is True


def test_nodes_are_bucketed_separately(tmp_path):
    _write_session(tmp_path, "s1", _session(node="滑块验证"))
    _write_session(tmp_path, "s2", _session(node="拖动放置"))
    _write_session(tmp_path, "s3", _session(node=None))

    nodes = scan_handoffs(tmp_path)["smoke_test"]

    assert set(nodes) == {"滑块验证", "拖动放置", "<unknown node>"}


def test_task_name_filter(tmp_path):
    _write_session(tmp_path, "s1", _session(task="smoke_test"))
    _write_session(tmp_path, "s2", _session(task="other_task"))

    data = scan_handoffs(tmp_path, task_name="other_task")

    assert list(data) == ["other_task"]


def test_days_cutoff_drops_old_sessions(tmp_path):
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    _write_session(tmp_path, "old", _session(started_at=old))
    _write_session(tmp_path, "new", _session())

    assert scan_handoffs(tmp_path, days=3)["smoke_test"]["滑块验证"]["sessions"] == 1
    # no cutoff -> both
    assert scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]["sessions"] == 2


def test_unparseable_started_at_is_kept(tmp_path):
    _write_session(tmp_path, "s1", _session(started_at="not-a-timestamp"))

    assert scan_handoffs(tmp_path, days=1)["smoke_test"]["滑块验证"]["sessions"] == 1


def test_empty_steps_still_counts_as_a_session(tmp_path):
    _write_session(tmp_path, "s1", _session(steps=[]))

    agg = scan_handoffs(tmp_path)["smoke_test"]["滑块验证"]

    assert agg["sessions"] == 1
    assert agg["dominant_signature"] == []


def test_format_report_marks_solidify_candidates(tmp_path):
    for i in range(3):
        _write_session(tmp_path, f"s{i}", _session())
    _write_session(tmp_path, "v1", _session(node="多变的", steps=[_step("click", "A")]))
    _write_session(tmp_path, "v2", _session(node="多变的", steps=[_step("click", "B")]))

    text = format_handoff_report(scan_handoffs(tmp_path))

    assert "smoke_test" in text
    assert "滑块验证" in text
    assert "->" in text
    assert "建议固化" in text
    assert "click('商店')" in text
    # the variable node is listed but not recommended for solidification
    assert "多变的" in text
    assert text.count("建议固化") == 1


def test_format_report_empty():
    assert format_handoff_report({}) == "No agent handoff sessions found."
