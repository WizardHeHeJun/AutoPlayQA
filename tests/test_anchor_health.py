from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from task.anchor_health import format_health_report, scan_health


def _write_report(base: Path, day: str, device: str, run_id: str, report: dict) -> Path:
    run_dir = base / day / device / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _report(task="smoke_test", node_stats=None, **overrides):
    report = {
        "task": task,
        "device": "dev1",
        "started_at": "2026-08-07T10:00:00",
        "finished_at": "2026-08-07T10:05:00",
        "status": "completed",
        "error": None,
        "counts": {},
        "findings": [],
    }
    if node_stats is not None:
        report["node_stats"] = node_stats
    report.update(overrides)
    return report


def test_scan_health_empty_dir_returns_empty_dict(tmp_path):
    assert scan_health(tmp_path / "does_not_exist") == {}


def test_scan_health_aggregates_across_runs(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(node_stats={
        "选择服务器": {"direct_hits": 3, "timeout_recoveries": 1, "popup_assisted_hits": 0, "drift_count": 0},
    }))
    _write_report(tmp_path, today, "dev1", "run2", _report(node_stats={
        "选择服务器": {"direct_hits": 2, "timeout_recoveries": 0, "popup_assisted_hits": 1, "drift_count": 1},
    }))

    tasks = scan_health(tmp_path)

    assert "smoke_test" in tasks
    bucket = tasks["smoke_test"]
    assert bucket["runs"] == 2
    node = bucket["nodes"]["选择服务器"]
    assert node["runs_seen"] == 2
    assert node["direct_hits"] == 5
    assert node["timeout_recoveries"] == 1
    assert node["popup_assisted_hits"] == 1
    assert node["drift_count"] == 1
    # fallback_rate = (timeout + popup) / total hits = (1+1)/7
    assert node["fallback_rate"] == round(2 / 7, 3)


def test_scan_health_filters_by_task_name(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(task="task_a", node_stats={
        "n1": {"direct_hits": 1},
    }))
    _write_report(tmp_path, today, "dev1", "run2", _report(task="task_b", node_stats={
        "n2": {"direct_hits": 1},
    }))

    tasks = scan_health(tmp_path, task_name="task_a")

    assert set(tasks) == {"task_a"}
    assert "n1" in tasks["task_a"]["nodes"]


def test_scan_health_filters_by_days(tmp_path):
    recent = date.today().strftime("%Y%m%d")
    old = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    _write_report(tmp_path, recent, "dev1", "run_recent", _report(node_stats={
        "n1": {"direct_hits": 1},
    }))
    _write_report(tmp_path, old, "dev1", "run_old", _report(node_stats={
        "n1": {"direct_hits": 100},
    }))

    tasks = scan_health(tmp_path, days=7)

    assert tasks["smoke_test"]["runs"] == 1
    assert tasks["smoke_test"]["nodes"]["n1"]["direct_hits"] == 1


def test_scan_health_tolerates_report_without_node_stats(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(node_stats=None))

    tasks = scan_health(tmp_path)

    assert tasks["smoke_test"]["runs"] == 1
    assert tasks["smoke_test"]["nodes"] == {}


def test_scan_health_tolerates_corrupt_json(tmp_path):
    today = date.today().strftime("%Y%m%d")
    run_dir = tmp_path / today / "dev1" / "run_bad"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text("{not valid json", encoding="utf-8")
    _write_report(tmp_path, today, "dev1", "run_good", _report(node_stats={"n1": {"direct_hits": 1}}))

    tasks = scan_health(tmp_path)

    assert tasks["smoke_test"]["runs"] == 1
    assert tasks["smoke_test"]["nodes"]["n1"]["direct_hits"] == 1


def test_scan_health_ignores_non_dict_node_stats_entries(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(node_stats={
        "n1": {"direct_hits": 1},
        "n2": "not a dict",
    }))

    tasks = scan_health(tmp_path)

    assert set(tasks["smoke_test"]["nodes"]) == {"n1"}


def test_scan_health_builds_daily_timeline(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(node_stats={
        "n1": {"direct_hits": 1, "timeout_recoveries": 2, "drift_count": 3},
    }))

    tasks = scan_health(tmp_path)

    timeline = tasks["smoke_test"]["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["date"] == today
    assert timeline[0]["runs"] == 1
    assert timeline[0]["timeout_recoveries"] == 2
    assert timeline[0]["drift_count"] == 3


def test_format_health_report_no_data():
    assert "No findings reports" in format_health_report({})


def test_format_health_report_contains_node_row(tmp_path):
    today = date.today().strftime("%Y%m%d")
    _write_report(tmp_path, today, "dev1", "run1", _report(node_stats={
        "选择服务器": {"direct_hits": 3, "timeout_recoveries": 1},
    }))

    tasks = scan_health(tmp_path)
    text = format_health_report(tasks)

    assert "smoke_test" in text
    assert "选择服务器" in text
