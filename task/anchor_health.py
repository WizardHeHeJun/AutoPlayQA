"""Cross-run anchor-health inspection: an offline, read-only aggregation over
`outputs/findings/<date>/<device>/<run_id>/report.json`'s `node_stats` field.

`node_stats` is written per run by the engine (a parallel change, not this
module) as `{node_name: {direct_hits, timeout_recoveries,
popup_assisted_hits, drift_count, ...}}` — a single run's observation of how
each node got recognized. This module never touches replay decisions or QA
verdicts; it only rolls many runs' `node_stats` up into per-task trends so an
author can spot "anchor rot" — a node whose direct-hit rate keeps dropping,
or whose drift count keeps climbing — across a task's history.

Report files that predate `node_stats` (or that are simply unreadable/
corrupt) are tolerated: they still count toward the run total but contribute
nothing to the node-level aggregates.

Library module: no `print` here (see CLI `user_interface/cli_handler.py`
for the console table, which reuses `format_health_report`).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

DEFAULT_FINDINGS_DIR = "outputs/findings"

#: node_stats sub-fields whose per-run values are meaningful to sum for a
#: fallback-rate view; anything else present in a stats dict is still summed
#: generically (see `_merge_node_stats`) but isn't part of this ratio.
_HIT_KEYS = ("direct_hits", "timeout_recoveries", "popup_assisted_hits")


def scan_health(
    findings_dir: Union[str, Path] = DEFAULT_FINDINGS_DIR,
    task_name: Optional[str] = None,
    days: Optional[int] = None,
    logger=None,
) -> Dict[str, Dict]:
    """Aggregate `node_stats` across historical runs under `findings_dir`.

    task_name: restrict aggregation to runs whose report["task"] matches.
    days: only consider runs from the last N days (by the `<YYYYMMDD>` date
    folder); None/<=0 means no cutoff.

    Returns {task_name: {
        "runs": int,                     # runs matched (node_stats or not)
        "nodes": {node_name: {           # summed counters + derived rates
            "runs_seen": int, "direct_hits": int, "timeout_recoveries": int,
            "popup_assisted_hits": int, "drift_count": int,
            "fallback_rate": float,      # (timeout+popup) / total hits, 0 if none
            ...any other numeric/list node_stats keys, summed/collected...
        }},
        "timeline": [{"date": "YYYYMMDD", "runs": int, "direct_hits": int,
                       "timeout_recoveries": int, "popup_assisted_hits": int,
                       "drift_count": int}, ...],   # sorted by date
    }}
    """
    tasks: Dict[str, Dict] = {}
    for date_str, _device, _run_id, report_path in _iter_reports(findings_dir, days):
        report = _load_report(report_path, logger)
        if report is None:
            continue
        name = report.get("task") or "<unnamed>"
        if task_name is not None and name != task_name:
            continue

        bucket = tasks.setdefault(name, {"runs": 0, "nodes": {}, "_daily": {}})
        bucket["runs"] += 1

        node_stats = report.get("node_stats")
        if not isinstance(node_stats, dict):
            continue

        day_totals = bucket["_daily"].setdefault(
            date_str, {"runs": 0, "direct_hits": 0, "timeout_recoveries": 0,
                       "popup_assisted_hits": 0, "drift_count": 0}
        )
        day_totals["runs"] += 1

        for node, stats in node_stats.items():
            if not isinstance(stats, dict):
                continue
            agg = bucket["nodes"].setdefault(node, {"runs_seen": 0})
            _merge_node_stats(agg, stats)
            for key in ("direct_hits", "timeout_recoveries", "popup_assisted_hits", "drift_count"):
                value = stats.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    day_totals[key] += value

    for bucket in tasks.values():
        for agg in bucket["nodes"].values():
            _finalize_node_agg(agg)
        bucket["timeline"] = [
            {"date": day, **totals} for day, totals in sorted(bucket["_daily"].items())
        ]
        del bucket["_daily"]

    return tasks


def _merge_node_stats(agg: Dict, stats: Dict) -> None:
    agg["runs_seen"] += 1
    for key, value in stats.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            agg[key] = agg.get(key, 0) + value
        elif isinstance(value, list):
            samples = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if samples:
                agg.setdefault(f"{key}_samples", []).extend(samples)


def _finalize_node_agg(agg: Dict) -> None:
    for key in _HIT_KEYS + ("drift_count",):
        agg.setdefault(key, 0)
    total_hits = sum(agg.get(k, 0) for k in _HIT_KEYS)
    fallback_hits = agg.get("timeout_recoveries", 0) + agg.get("popup_assisted_hits", 0)
    agg["fallback_rate"] = round(fallback_hits / total_hits, 3) if total_hits else 0.0


def _iter_reports(findings_dir, days: Optional[int]):
    base = Path(findings_dir)
    if not base.is_dir():
        return
    cutoff = None
    if days is not None and days > 0:
        cutoff = date.today() - timedelta(days=days)
    for date_dir in sorted(base.iterdir()):
        if not date_dir.is_dir():
            continue
        run_date = _parse_date(date_dir.name)
        if cutoff is not None and run_date is not None and run_date < cutoff:
            continue
        for device_dir in sorted(date_dir.iterdir()):
            if not device_dir.is_dir():
                continue
            for run_dir in sorted(device_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                report_path = run_dir / "report.json"
                if report_path.is_file():
                    yield date_dir.name, device_dir.name, run_dir.name, report_path


def _parse_date(name: str):
    try:
        return datetime.strptime(name, "%Y%m%d").date()
    except ValueError:
        return None


def _load_report(path: Path, logger) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if logger:
            logger.warning("anchor_health: failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        if logger:
            logger.warning("anchor_health: %s is not a JSON object, skipped", path)
        return None
    return data


def format_health_report(tasks: Dict[str, Dict]) -> str:
    """Render `scan_health`'s return value as a plain-text console table.

    Kept here (not in the CLI) so the CLI stays a thin print-the-string
    wrapper, matching the `step_numbering.format_task_outline` pattern.
    """
    if not tasks:
        return "No findings reports with node_stats found."

    lines: List[str] = []
    for task_name in sorted(tasks):
        bucket = tasks[task_name]
        lines.append(f"== {task_name} ({bucket['runs']} run(s)) ==")
        nodes = bucket.get("nodes") or {}
        if not nodes:
            lines.append("  (no node_stats in any matched report)")
        else:
            header = f"  {'node':<28} {'seen':>5} {'direct':>7} {'timeout':>8} {'popup':>6} {'drift':>6} {'fallback':>9}"
            lines.append(header)
            for node in sorted(nodes, key=lambda n: -nodes[n].get("fallback_rate", 0)):
                agg = nodes[node]
                lines.append(
                    f"  {node[:28]:<28} {agg.get('runs_seen', 0):>5} "
                    f"{agg.get('direct_hits', 0):>7} {agg.get('timeout_recoveries', 0):>8} "
                    f"{agg.get('popup_assisted_hits', 0):>6} {agg.get('drift_count', 0):>6} "
                    f"{agg.get('fallback_rate', 0.0):>9.1%}"
                )
        timeline = bucket.get("timeline") or []
        if timeline:
            lines.append("  daily: " + ", ".join(
                f"{d['date']}(runs={d['runs']},timeout={d['timeout_recoveries']},drift={d['drift_count']})"
                for d in timeline
            ))
        lines.append("")
    return "\n".join(lines).rstrip("\n")
