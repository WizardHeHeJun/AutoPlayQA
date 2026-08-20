"""Agent-handoff statistics: an offline, read-only aggregation over the action
logs recorded while an external agent was doing a task's `agent` step.

Every time the engine hits an `agent` node it suspends and an outside agent
performs the step by hand (see task_engine's handoff). The recording side writes
one `outputs/agent_sessions/<dir>/session.json` per such interaction, tagged
`context.kind == "handoff"` with the task and node it belongs to.

What this module answers: **is that handoff still worth a human round-trip?**
An `agent` node the agent solves the exact same way every time is not a
"can't be made deterministic" step any more — it is a node nobody has written
yet. Sessions are reduced to an action *signature* (the sequence of action
types + the element texts they touched); when one signature dominates a node's
history, the node is flagged as a solidify candidate and its dominant signature
is the recipe (feed the session through `task_editor.action_log_to_draft` to get
the nodes).

This never changes replay behaviour or QA verdicts — it only reads finished
logs. Malformed / half-written session files are skipped (debug log), never
fatal: a crashed recording must not break the report.

Library module: no `print` here (see CLI `user_interface/cli_handler.py`, which
prints `format_handoff_report`'s string).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from core.logger import LOGGER_NAME

DEFAULT_SESSIONS_DIR = "outputs/agent_sessions"

#: A handoff has to have happened a few times before "it is always the same"
#: means anything — two identical sessions is a coincidence, not a pattern.
SOLIDIFY_MIN_SESSIONS = 3

#: ...and the sessions have to actually agree: below this share of identical
#: signatures the node is genuinely variable and belongs to the agent.
SOLIDIFY_MIN_RATIO = 0.8

#: Bucket label for a handoff session that recorded no node name.
UNKNOWN_NODE = "<unknown node>"

Signature = Tuple[Tuple[str, Optional[str]], ...]


def scan_handoffs(
    sessions_dir: Union[str, Path] = DEFAULT_SESSIONS_DIR,
    task_name: Optional[str] = None,
    days: Optional[int] = None,
    logger=None,
) -> Dict[str, Dict]:
    """Aggregate handoff action logs under `sessions_dir` per (task, node).

    task_name: restrict to sessions whose `context.task` matches.
    days: only sessions started within the last N days (by `started_at`);
    None/<=0 means no cutoff. A session with an unparseable `started_at` is
    kept — a missing timestamp is not evidence of being old.

    Returns {task: {node: {
        "sessions": int,                 # handoff sessions seen for this node
        "signatures": int,               # distinct action signatures among them
        "dominant_ratio": float,         # share of the most common signature
        "dominant_signature": [[action_type, element_text|None], ...],
        "solidify_candidate": bool,      # same thing every time -> node it
    }}}
    """
    logger = _fallback_logger(logger)
    cutoff = None
    if days is not None and days > 0:
        cutoff = datetime.now() - timedelta(days=days)

    collected: Dict[str, Dict[str, List[Signature]]] = {}
    for path in _iter_session_files(sessions_dir):
        session = _load_session(path, logger)
        if session is None:
            continue
        context = session.get("context")
        if not isinstance(context, dict) or context.get("kind") != "handoff":
            continue
        task = context.get("task")
        if not isinstance(task, str) or not task:
            _debug(logger, "handoff_stats: %s has kind=handoff but no task, skipped", path)
            continue
        if task_name is not None and task != task_name:
            continue
        if cutoff is not None and _is_older_than(session.get("started_at"), cutoff):
            continue
        node = context.get("node")
        node_key = node if isinstance(node, str) and node else UNKNOWN_NODE
        signature = _signature(session.get("steps"), path, logger)
        if signature is None:
            continue
        collected.setdefault(task, {}).setdefault(node_key, []).append(signature)

    return {
        task: {node: _summarize(signatures) for node, signatures in nodes.items()}
        for task, nodes in collected.items()
    }


def _summarize(signatures: List[Signature]) -> Dict:
    counter = Counter(signatures)
    dominant, dominant_count = counter.most_common(1)[0]
    sessions = len(signatures)
    ratio = round(dominant_count / sessions, 3)
    return {
        "sessions": sessions,
        "signatures": len(counter),
        "dominant_ratio": ratio,
        "dominant_signature": [[action_type, text] for action_type, text in dominant],
        "solidify_candidate": sessions >= SOLIDIFY_MIN_SESSIONS and ratio >= SOLIDIFY_MIN_RATIO,
    }


def _signature(steps, path: Path, logger) -> Optional[Signature]:
    """(action type, element text) per step — what the agent actually did.

    The element text (not its coordinates) is the identity that survives a
    re-layout, which is the same reason task anchors are written as text.
    """
    if steps is None:
        return ()
    if not isinstance(steps, list):
        _debug(logger, "handoff_stats: %s has a non-list 'steps', skipped", path)
        return None
    signature: List[Tuple[str, Optional[str]]] = []
    for step in steps:
        if not isinstance(step, dict):
            _debug(logger, "handoff_stats: %s has a non-object step, skipped", path)
            return None
        action = step.get("action")
        action_type = action.get("type") if isinstance(action, dict) else None
        if not isinstance(action_type, str) or not action_type:
            _debug(logger, "handoff_stats: %s has a step without action type, skipped", path)
            return None
        element = step.get("element")
        text = element.get("text") if isinstance(element, dict) else None
        signature.append((action_type, text if isinstance(text, str) and text else None))
    return tuple(signature)


def _iter_session_files(sessions_dir: Union[str, Path]):
    base = Path(sessions_dir)
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        path = entry / "session.json"
        if path.is_file():
            yield path


def _load_session(path: Path, logger) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _debug(logger, "handoff_stats: failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        _debug(logger, "handoff_stats: %s is not a JSON object, skipped", path)
        return None
    return data


def _is_older_than(started_at, cutoff: datetime) -> bool:
    if not isinstance(started_at, str) or not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)
    return started < cutoff


def _fallback_logger(logger):
    """The caller's logger, or the project one.

    Skipped sessions (unreadable JSON, missing task) are the only trace of a
    corrupt log folder; defaulting to None used to throw them away whenever the
    caller forgot to pass a logger. The parameter stays for injection.
    """
    return logger if logger is not None else logging.getLogger(LOGGER_NAME)


def _debug(logger, message: str, *args) -> None:
    _fallback_logger(logger).debug(message, *args)


def format_handoff_report(data: Dict[str, Dict]) -> str:
    """Render `scan_handoffs`'s return value as a plain-text console table.

    Kept here (not in the CLI) so the CLI stays a thin print-the-string
    wrapper, matching `anchor_health.format_health_report`.
    """
    if not data:
        return "No agent handoff sessions found."

    lines: List[str] = []
    for task in sorted(data):
        nodes = data[task]
        total = sum(agg.get("sessions", 0) for agg in nodes.values())
        lines.append(f"== {task} ({total} handoff session(s)) ==")
        header = f"  {'':<2}{'node':<28} {'sessions':>8} {'distinct':>8} {'dominant':>9}"
        lines.append(header)
        for node in sorted(nodes, key=lambda n: -nodes[n].get("dominant_ratio", 0)):
            agg = nodes[node]
            mark = "->" if agg.get("solidify_candidate") else "  "
            lines.append(
                f"  {mark}{node[:28]:<28} {agg.get('sessions', 0):>8} "
                f"{agg.get('signatures', 0):>8} {agg.get('dominant_ratio', 0.0):>9.1%}"
            )
            if agg.get("solidify_candidate"):
                lines.append(f"       actions: {_format_signature(agg.get('dominant_signature'))}")
                lines.append(
                    "       → 建议固化为确定性节点"
                    "（用 task_editor.action_log_to_draft 出草稿，再补 QA 断言）"
                )
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def _format_signature(signature) -> str:
    if not signature:
        return "(no steps recorded)"
    parts = []
    for item in signature:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            action_type, text = item
            parts.append(f"{action_type}('{text}')" if text else f"{action_type}(?)")
        else:
            parts.append(str(item))
    return " -> ".join(parts)
