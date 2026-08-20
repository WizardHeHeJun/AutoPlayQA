"""Derive a human-readable step number for every node of a task state machine.

Tasks are graphs, not lists: `nodes` is a dict and edges are name references
(`next`/`on_timeout`) starting from `entry`. Reading the JSON top-to-bottom does
not tell you the execution order, and recovery branches (the `*未找到`/`*异常`
fallback nodes) interleave with the main flow. These helpers label each node by
its real execution order so a reader does not have to trace the graph by hand.

Labelling rules:
  - Main spine (follow next[0] from entry) gets integer labels 1, 2, 3, …
  - A branch node (reached via on_timeout or next[1:]) gets a dotted label under
    the step that referenced it, e.g. step 2's timeout fallback is 2.1.
  - A branch's own follow-on chain nests further (2.1.1).
  - Labels are recomputed from the graph, so they never go stale after an edit.
  - Unreachable nodes get the label "?".
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional


def compute_step_labels(task: Dict) -> Dict[str, str]:
    """Return a mapping of node name -> step label for a (resolved) task."""
    nodes = task.get("nodes") or {}
    entry = task.get("entry")
    labels: Dict[str, str] = {}

    # Pass 1: the integer spine, following next[0] from entry until it ends or
    # loops back onto an already-numbered node.
    cur = entry
    idx = 1
    while isinstance(cur, str) and cur in nodes and cur not in labels:
        labels[cur] = str(idx)
        idx += 1
        nxt = _next_list(nodes[cur])
        cur = nxt[0] if nxt else None

    # Pass 2: branches. Process labeled nodes in step order so that when a
    # branch target is referenced from several places the closest spine
    # ancestor wins (and produces a stable, low number).
    child_count: Dict[str, int] = defaultdict(int)
    queue = deque(sorted(labels, key=lambda n: _sort_key(labels[n])))
    while queue:
        name = queue.popleft()
        node = nodes.get(name, {})
        my = labels[name]
        is_spine = "." not in my
        nxt = _next_list(node)
        targets: List[str] = []
        on_timeout = node.get("on_timeout")
        if isinstance(on_timeout, str):
            targets.append(on_timeout)
        # A spine node's next[0] is already its spine successor; only next[1:]
        # are branches. A branch node's whole next chain continues the branch.
        targets.extend(nxt[1:] if is_spine else nxt)
        for tgt in targets:
            if tgt in nodes and tgt not in labels:
                child_count[my] += 1
                labels[tgt] = f"{my}.{child_count[my]}"
                queue.append(tgt)

    # Anything never reached from entry.
    for name in nodes:
        labels.setdefault(name, "?")
    return labels


def format_task_outline(task: Dict, labels: Optional[Dict[str, str]] = None) -> str:
    """Render the task as an indented, step-numbered flow listing."""
    nodes = task.get("nodes") or {}
    entry = task.get("entry")
    if labels is None:
        labels = compute_step_labels(task)

    ordered = sorted(nodes, key=lambda n: _sort_key(labels[n]))
    lines: List[str] = []
    for name in ordered:
        node = nodes[name]
        label = labels[name]
        depth = 0 if label == "?" else label.count(".")
        indent = "  " + "  " * depth
        marker = "└ " if depth else ""
        rec = _rec_summary(node.get("recognition"))
        act = _act_summary(node.get("action"))
        nxt = _next_list(node)
        flow = ", ".join(labels.get(n, "?") for n in nxt) if nxt else "—(终)"
        on_timeout = node.get("on_timeout")
        ot = f"  超时→{labels.get(on_timeout, '?')}" if isinstance(on_timeout, str) else ""
        finding = "  [finding]" if node.get("finding") else ""
        lines.append(
            f"{indent}{marker}[{label}] {name}  ({rec} / {act})  →{flow}{ot}{finding}"
        )

    header = (
        f"任务流程  入口={entry}  共 {len(nodes)} 个节点"
        "（按执行顺序；[n] 为步号，→ 后是下一步步号，└ 为兜底分支）"
    )
    return header + "\n" + "\n".join(lines)


def write_step_labels(path: Path | str) -> Dict:
    """Write a `step` field into each node of the task file on disk.

    The label is computed from the fully-resolved graph (validating the task
    and any includes), but only nodes physically defined in this file get a
    `step` written — included shared nodes are reused across tasks and have no
    single step. `step` is placed first in each node for visibility. Returns
    {"path", "count"}.
    """
    from task.task_loader import load_task  # local import avoids an import cycle

    path = Path(path)
    resolved = load_task(path)  # validates + resolves includes
    labels = compute_step_labels(resolved)

    raw = json.loads(path.read_text(encoding="utf-8"))
    nodes = raw.get("nodes") or {}
    count = 0
    for name, node in list(nodes.items()):
        if not isinstance(node, dict):
            continue
        label = labels.get(name)
        if label and label != "?":
            reordered = {"step": label}
            reordered.update({k: v for k, v in node.items() if k != "step"})
            nodes[name] = reordered
            count += 1
        else:
            node.pop("step", None)
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"path": str(path), "count": count}


def _next_list(node: Dict) -> List[str]:
    nxt = node.get("next") if isinstance(node, dict) else None
    return [n for n in nxt if isinstance(n, str)] if isinstance(nxt, list) else []


def _sort_key(label: str):
    if label == "?":
        return (float("inf"),)
    try:
        return tuple(int(p) for p in label.split("."))
    except ValueError:
        return (float("inf"),)


def _rec_summary(rec) -> str:
    if not isinstance(rec, dict):
        return "?"
    rtype = rec.get("type", "?")
    for key in ("expected", "template", "label"):
        value = rec.get(key)
        if value:
            return f"{rtype}:{value}"
    return rtype


def _act_summary(act) -> str:
    if not isinstance(act, dict):
        return "?"
    atype = act.get("type", "?")
    if atype == "custom":
        return f"custom:{act.get('name', '?')}"
    if atype == "click":
        if act.get("target") == "recognized":
            return "click:命中处"
        params = act.get("params", {}) or {}
        return f"click:({params.get('x', '?')},{params.get('y', '?')})"
    if atype in ("agent", "llm"):
        return "agent:交接"
    if atype == "key":
        return f"key:{(act.get('params', {}) or {}).get('keycode', '?')}"
    return atype
