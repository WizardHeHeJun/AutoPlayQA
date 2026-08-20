from __future__ import annotations

import json
from pathlib import Path

from task.step_numbering import (
    compute_step_labels,
    format_task_outline,
    write_step_labels,
)


def _node(next_=None, on_timeout=None, finding=None, action=None, rec=None):
    node = {
        "recognition": rec or {"type": "always"},
        "action": action or {"type": "none"},
        "next": list(next_ or []),
    }
    if on_timeout is not None:
        node["on_timeout"] = on_timeout
    if finding is not None:
        node["finding"] = finding
    return node


def test_spine_gets_consecutive_integers():
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b"]),
            "b": _node(["c"]),
            "c": _node([]),
        },
    }
    assert compute_step_labels(task) == {"a": "1", "b": "2", "c": "3"}


def test_timeout_fallback_is_dotted_under_its_step():
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b"], on_timeout="a_recover"),
            "a_recover": _node(["b"], finding="oops"),
            "b": _node([]),
        },
    }
    labels = compute_step_labels(task)
    assert labels["a"] == "1"
    assert labels["b"] == "2"
    assert labels["a_recover"] == "1.1"


def test_alt_next_branch_and_nested_chain():
    # a -> [b (spine), alt]; alt -> alt2 continues the branch chain.
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b", "alt"]),
            "b": _node([]),
            "alt": _node(["alt2"]),
            "alt2": _node([]),
        },
    }
    labels = compute_step_labels(task)
    assert labels["a"] == "1"
    assert labels["b"] == "2"
    assert labels["alt"] == "1.1"
    assert labels["alt2"] == "1.1.1"


def test_closest_spine_ancestor_wins_for_shared_target():
    # Both step 1 (alt-next) and step 2 (timeout) point at "shared". Step 1 is
    # processed first, so "shared" lands under 1, not 2.
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b", "shared"]),
            "b": _node([], on_timeout="shared"),
            "shared": _node([]),
        },
    }
    labels = compute_step_labels(task)
    assert labels["a"] == "1"
    assert labels["b"] == "2"
    assert labels["shared"] == "1.1"


def test_loop_back_to_spine_does_not_renumber():
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b"]),
            "b": _node([], on_timeout="a"),  # loops back to the entry
        },
    }
    labels = compute_step_labels(task)
    assert labels == {"a": "1", "b": "2"}


def test_unreachable_node_marked_question():
    task = {
        "entry": "a",
        "nodes": {
            "a": _node([]),
            "orphan": _node([]),
        },
    }
    labels = compute_step_labels(task)
    assert labels["a"] == "1"
    assert labels["orphan"] == "?"


def test_outline_orders_branches_under_parent_and_flags_findings():
    task = {
        "entry": "a",
        "nodes": {
            "a": _node(["b"], on_timeout="a_recover"),
            "a_recover": _node(["b"], finding="popup"),
            "b": _node([]),
        },
    }
    outline = format_task_outline(task)
    lines = [ln for ln in outline.splitlines() if "]" in ln and "入口" not in ln]
    # Order: 1, then its branch 1.1, then 2.
    assert lines[0].lstrip().startswith("[1] a")
    assert lines[1].lstrip().startswith("└ [1.1] a_recover")
    assert "[finding]" in lines[1]
    assert lines[2].lstrip().startswith("[2] b")


def test_write_step_labels_writes_and_reorders(tmp_path: Path):
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "entry": "a",
                "nodes": {
                    "a": _node(["b"], on_timeout="a_recover"),
                    "a_recover": _node(["b"]),
                    "b": _node([]),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = write_step_labels(path)
    assert result["count"] == 3

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["nodes"]["a"]["step"] == "1"
    assert data["nodes"]["a_recover"]["step"] == "1.1"
    assert data["nodes"]["b"]["step"] == "2"
    # step is written first in each node.
    assert list(data["nodes"]["a"].keys())[0] == "step"


def test_write_step_labels_is_idempotent(tmp_path: Path):
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {"entry": "a", "nodes": {"a": _node(["b"]), "b": _node([])}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_step_labels(path)
    first = path.read_text(encoding="utf-8")
    write_step_labels(path)
    second = path.read_text(encoding="utf-8")
    assert first == second
