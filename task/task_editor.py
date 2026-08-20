from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from task.task_loader import validate_task

#: Fraction of an element's own width/height added on EVERY side when turning a
#: recorded OCR bbox into a node ROI. Same idea as replay_cache's fast-path ROI:
#: the anchor will not sit at exactly the same pixels on replay, so the search
#: window has to be roomier than the box it was captured from -- but still small
#: enough that OCR is not effectively full-screen again.
OCR_ROI_EXPAND = 0.4

#: Marker left on a node the recorder could not anchor (a click with no element
#: behind it): the draft replays the literal coordinates, which is exactly what
#: the project's task rules call a last resort. Not an engine field -- the
#: validator ignores unknown node keys, so it survives save/load as a note to
#: whoever hardens the draft.
BLIND_CLICK_COMMENT = "TODO: 盲点坐标，待补锚点"


def records_to_draft_task(records: List[Dict]) -> Dict:
    """Deterministically convert a recorded session into a replay-draft task.

    Every action of every successful recorded command becomes one node
    (recognition "always", literal action), all chained in order. The draft
    replays blindly — no LLM involved; let an agent rewrite it into a
    recognition-driven task (ui_text/ocr anchors, target="recognized") via the
    MCP save_task tool when robustness matters.
    """
    successful = [r for r in records if r.get("results_ok") and r.get("actions")]
    if not successful:
        raise ValueError("No successful recorded commands; use 'record on' and run some actions first.")

    flat: List[Tuple[str, Dict]] = []
    for i, record in enumerate(successful, start=1):
        base = f"step_{i}_{_slug(record.get('user_text', ''))}"
        actions = record["actions"]
        if len(actions) == 1:
            flat.append((base, actions[0]))
        else:
            for j, action in enumerate(actions, start=1):
                flat.append((f"{base}_{j}", action))

    nodes: Dict[str, Dict] = {}
    for index, (name, action) in enumerate(flat):
        is_last = index == len(flat) - 1
        nodes[name] = {
            "recognition": {"type": "always"},
            "action": action,
            "next": [] if is_last else [flat[index + 1][0]],
            "post_delay_ms": 800,
        }

    task = {"entry": flat[0][0], "nodes": nodes}
    validate_task(task)
    return task


def action_log_to_draft(session: Dict, name_prefix: str = "step") -> Dict:
    """Convert a recorded agent action log into a RECOGNITION-DRIVEN draft task.

    Input is one `outputs/agent_sessions/<dir>/session.json` (see the recording
    side): a list of steps, each carrying the executed action AND — when the
    step went through an indexed element — the element the agent clicked (its
    source channel, text and bounds).

    That element is what makes this different from `records_to_draft_task`,
    which can only replay coordinates blindly: a step whose element carries text
    becomes an anchored node (`ui_text`/`ocr` recognition + `target:
    "recognized"`), i.e. the shape the task rules ask authors to write by hand.
    Steps with no element behind them still fall back to literal coordinates,
    but they are marked (`BLIND_CLICK_COMMENT`) instead of silently pretending
    to be anchored.

    The conversion is deterministic and local — no LLM, no device. The result is
    a *draft*: an author/agent still has to add the QA assertions (watchdogs,
    finding branches, on_timeout recoveries) the engine treats as first-class.

    Raises ValueError when the session has no usable steps.
    """
    steps = session.get("steps") if isinstance(session, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("Session has no steps; record some actions before converting.")

    prepared: List[Tuple[str, Dict]] = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Session step #{position} is not an object")
        action = step.get("action")
        if not isinstance(action, dict) or not action.get("type"):
            raise ValueError(f"Session step #{position} has no action dict")
        element = step.get("element") if isinstance(step.get("element"), dict) else None
        prepared.append((_node_name(name_prefix, position, element), _node_body(action, element)))

    nodes: Dict[str, Dict] = {}
    for index, (name, body) in enumerate(prepared):
        is_last = index == len(prepared) - 1
        body["next"] = [] if is_last else [prepared[index + 1][0]]
        body["post_delay_ms"] = 800
        nodes[name] = body

    task = {"entry": prepared[0][0], "nodes": nodes}
    validate_task(task)
    return task


def _node_name(name_prefix: str, position: int, element: Optional[Dict]) -> str:
    """`<prefix>_<NN>[_<slug of the element text>]` — position keeps it unique."""
    base = f"{name_prefix}_{position:02d}"
    text = (element or {}).get("text") or ""
    slug = _slug(text) if text.strip() else ""
    return f"{base}_{slug}" if slug and slug != "action" else base


def _node_body(action: Dict, element: Optional[Dict]) -> Dict:
    """Recognition + action for one recorded step.

    The action dict comes straight from the log — it is already in the
    `{"type", "params"}` executor format, so it is passed through untouched
    (rewrapping it would produce params-in-params).
    """
    text = (element or {}).get("text") or ""
    source = (element or {}).get("source")
    if action.get("type") == "click" and text.strip() and source in ("dump", "ocr"):
        if source == "dump":
            recognition: Dict = {"type": "ui_text", "expected": text}
        else:
            recognition = {"type": "ocr", "expected": text}
            roi = _expanded_roi(element.get("bounds"))
            if roi is not None:
                recognition["roi"] = roi
        return {"recognition": recognition, "action": {"type": "click", "target": "recognized"}}

    body = {"recognition": {"type": "always"}, "action": dict(action)}
    if action.get("type") == "click":
        # Literal coordinates: replayable, but the first thing to harden.
        body["comment"] = BLIND_CLICK_COMMENT
    return body


def _expanded_roi(bounds) -> Optional[List[int]]:
    """Grow a recorded [x1, y1, x2, y2] box by OCR_ROI_EXPAND on every side.

    Clamped to non-negative only: the screen size is not part of the log, and an
    over-wide right/bottom edge costs nothing (recognizers clip to the frame),
    whereas a negative origin would be an invalid crop.
    """
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    margin_x = int(abs(x2 - x1) * OCR_ROI_EXPAND)
    margin_y = int(abs(y2 - y1) * OCR_ROI_EXPAND)
    return [
        max(0, x1 - margin_x),
        max(0, y1 - margin_y),
        max(0, x2 + margin_x),
        max(0, y2 + margin_y),
    ]


def _slug(text: str, max_len: int = 12) -> str:
    cleaned = "".join(c for c in text.strip() if c.isalnum() or "一" <= c <= "鿿")
    return cleaned[:max_len] if cleaned else "action"
