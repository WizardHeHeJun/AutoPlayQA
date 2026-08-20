from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from task.task_loader import TaskValidationError, load_task, resolve_task, validate_task


def write_json(path: Path, data: Dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def main_task(**overrides) -> Dict:
    task = {
        "entry": "start",
        "includes": ["common/popups.json"],
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["close_popup"],  # cross-file reference
            },
        },
    }
    task.update(overrides)
    return task


def popups_nodes() -> Dict:
    return {
        "nodes": {
            "close_popup": {
                "recognition": {"type": "ui_text", "expected": "关闭"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }


def test_task_without_includes_passes_through(tmp_path, sample_task):
    path = write_json(tmp_path / "plain.json", sample_task)

    loaded = load_task(path)

    assert loaded == sample_task
    assert "_merge" not in loaded


def test_includes_merge_resolves_cross_file_next(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    path = write_json(tmp_path / "main.json", main_task())

    loaded = load_task(path)

    assert set(loaded["nodes"]) == {"start", "close_popup"}
    assert "includes" not in loaded and "on_conflict" not in loaded
    assert loaded["_merge"]["conflicts"] == []
    assert loaded["_merge"]["includes"] == [str((tmp_path / "common" / "popups.json").resolve())]


def test_include_node_can_reference_main_node(tmp_path):
    popups = popups_nodes()
    popups["nodes"]["close_popup"]["next"] = ["start"]
    write_json(tmp_path / "common" / "popups.json", popups)
    path = write_json(tmp_path / "main.json", main_task())

    loaded = load_task(path)

    assert loaded["nodes"]["close_popup"]["next"] == ["start"]


def test_entry_may_come_from_include(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = {"entry": "close_popup", "includes": ["common/popups.json"]}
    path = write_json(tmp_path / "main.json", task)

    loaded = load_task(path)

    assert loaded["entry"] == "close_popup"
    assert set(loaded["nodes"]) == {"close_popup"}


def test_unresolved_cross_file_reference_fails_atomically(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task()
    task["nodes"]["start"]["next"] = ["missing_node"]
    path = write_json(tmp_path / "main.json", task)

    with pytest.raises(TaskValidationError, match="unknown next node 'missing_node'"):
        load_task(path)


def test_include_map_records_each_node_source(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    path = write_json(tmp_path / "main.json", main_task())

    loaded = load_task(path)

    assert loaded["_merge"]["include_map"] == {
        "close_popup": "common/popups.json",
        "start": "<task>",
    }
    # the origin map lives beside the nodes, never inside them
    assert set(loaded["nodes"]["close_popup"]) == {"recognition", "action", "next"}


def test_include_may_declare_description(tmp_path):
    fragment = popups_nodes()
    fragment["description"] = "通用弹窗处理"
    fragment["_comment"] = "自由注释"
    write_json(tmp_path / "common" / "popups.json", fragment)
    path = write_json(tmp_path / "main.json", main_task())

    loaded = load_task(path)

    assert set(loaded["nodes"]) == {"start", "close_popup"}
    assert "description" not in loaded  # fragment metadata does not leak into the task


def test_include_with_non_string_description_rejected(tmp_path):
    fragment = popups_nodes()
    fragment["description"] = 42
    write_json(tmp_path / "common" / "popups.json", fragment)
    path = write_json(tmp_path / "main.json", main_task())

    with pytest.raises(TaskValidationError, match="'description' must be a string"):
        load_task(path)


@pytest.mark.parametrize("field, value", [
    ("watchdogs", [{"type": "ocr", "expected": "错误"}]),
    ("popups", []),
    ("on_finding", "close_popup"),
    ("defaults", {"timeout_ms": 1000}),
    ("max_steps", 10),
    ("name", "popups"),
])
def test_include_with_task_level_field_rejected(tmp_path, field, value):
    """A fragment is not a runnable task: task-level fields fail loudly instead
    of being silently dropped, so a fragment can't be mistaken for a task."""
    fragment = popups_nodes()
    fragment[field] = value
    write_json(tmp_path / "common" / "popups.json", fragment)
    path = write_json(tmp_path / "main.json", main_task())

    with pytest.raises(TaskValidationError, match=f"task-level field\\(s\\) {field}"):
        load_task(path)


def test_strict_conflict_raises(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task()
    task["nodes"]["close_popup"] = {
        "recognition": {"type": "always"},
        "action": {"type": "none"},
        "next": [],
    }
    path = write_json(tmp_path / "main.json", task)

    with pytest.raises(TaskValidationError, match="conflict 'close_popup'"):
        load_task(path)


def test_strict_conflict_lists_every_colliding_node_with_sources(tmp_path):
    """One error naming every collision (and both files), not one per run."""
    fragment = popups_nodes()
    fragment["nodes"]["back_home"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"}, "next": [],
    }
    write_json(tmp_path / "common" / "popups.json", fragment)
    task = main_task()
    for name in ("close_popup", "back_home"):
        task["nodes"][name] = {
            "recognition": {"type": "always"}, "action": {"type": "none"}, "next": [],
        }
    path = write_json(tmp_path / "main.json", task)

    with pytest.raises(TaskValidationError) as excinfo:
        load_task(path)

    message = str(excinfo.value)
    assert "conflict 'close_popup'" in message and "conflict 'back_home'" in message
    assert "common/popups.json" in message and "<task>" in message


def test_strict_conflict_between_two_includes_reported(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    write_json(tmp_path / "common" / "popups_v2.json", popups_nodes())
    path = write_json(
        tmp_path / "main.json",
        main_task(includes=["common/popups.json", "common/popups_v2.json"]),
    )

    with pytest.raises(TaskValidationError) as excinfo:
        load_task(path)

    message = str(excinfo.value)
    assert "conflict 'close_popup'" in message
    assert "first defined in common/popups.json" in message
    assert "redefined by common/popups_v2.json" in message


def test_conflicting_load_leaves_no_partial_state(tmp_path):
    """Atomicity: the raising load must not hand back (or cache) a half-merged
    node table -- fixing the conflict and reloading gives the full merge."""
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task()
    task["nodes"]["close_popup"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"}, "next": [],
    }
    path = write_json(tmp_path / "main.json", task)

    with pytest.raises(TaskValidationError):
        load_task(path)

    write_json(tmp_path / "main.json", main_task())
    loaded = load_task(path)
    assert set(loaded["nodes"]) == {"start", "close_popup"}
    assert loaded["_merge"]["conflicts"] == []


def test_resolve_task_does_not_mutate_the_input_task(tmp_path):
    """save_task validates the merged view but writes the ORIGINAL dict: if
    resolve_task expanded includes in place, the fragment would get inlined."""
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task()

    resolve_task(task, tmp_path)

    assert task["includes"] == ["common/popups.json"]
    assert set(task["nodes"]) == {"start"}
    assert "_merge" not in task


def test_failed_merge_does_not_mutate_the_input_task(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task()
    task["nodes"]["close_popup"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"}, "next": [],
    }

    with pytest.raises(TaskValidationError):
        resolve_task(task, tmp_path)

    assert set(task["nodes"]) == {"start", "close_popup"}
    assert "_merge" not in task


def test_overwrite_lets_main_node_specialize_shared_node(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    task = main_task(on_conflict="overwrite")
    specialized = {
        "recognition": {"type": "always"},
        "action": {"type": "key", "params": {"keycode": 4}},
        "next": [],
    }
    task["nodes"]["close_popup"] = specialized
    path = write_json(tmp_path / "main.json", task)

    loaded = load_task(path)

    assert loaded["nodes"]["close_popup"] == specialized
    assert loaded["_merge"]["conflicts"] == ["close_popup"]


def test_overwrite_later_include_wins(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    second = popups_nodes()
    second["nodes"]["close_popup"]["recognition"] = {"type": "ocr", "expected": "确定"}
    write_json(tmp_path / "common" / "popups_v2.json", second)
    task = main_task(
        includes=["common/popups.json", "common/popups_v2.json"],
        on_conflict="overwrite",
    )
    path = write_json(tmp_path / "main.json", task)

    loaded = load_task(path)

    assert loaded["nodes"]["close_popup"]["recognition"]["type"] == "ocr"
    # include_map credits the file whose version actually survived the merge
    assert loaded["_merge"]["include_map"]["close_popup"] == "common/popups_v2.json"


def test_include_path_escaping_task_dir_rejected(tmp_path):
    write_json(tmp_path / "escape.json", popups_nodes())
    task_dir = tmp_path / "tasks"
    path = write_json(task_dir / "main.json", main_task(includes=["../escape.json"]))

    with pytest.raises(TaskValidationError, match="escapes the task directory"):
        load_task(path)


def test_absolute_include_path_rejected(tmp_path):
    abs_include = write_json(tmp_path / "abs.json", popups_nodes())
    path = write_json(tmp_path / "main.json", main_task(includes=[str(abs_include)]))

    with pytest.raises(TaskValidationError, match="must be relative"):
        load_task(path)


def test_missing_include_rejected(tmp_path):
    path = write_json(tmp_path / "main.json", main_task())

    with pytest.raises(TaskValidationError, match="Include file not found"):
        load_task(path)


def test_duplicate_include_rejected(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    path = write_json(
        tmp_path / "main.json",
        main_task(includes=["common/popups.json", "common/popups.json"]),
    )

    with pytest.raises(TaskValidationError, match="Duplicate include"):
        load_task(path)


def test_include_defining_entry_rejected(tmp_path):
    nested = popups_nodes()
    nested["entry"] = "close_popup"
    write_json(tmp_path / "common" / "popups.json", nested)
    path = write_json(tmp_path / "main.json", main_task())

    with pytest.raises(TaskValidationError, match="single-level"):
        load_task(path)


def test_empty_include_nodes_rejected(tmp_path):
    write_json(tmp_path / "common" / "popups.json", {"nodes": {}})
    path = write_json(tmp_path / "main.json", main_task())

    with pytest.raises(TaskValidationError, match="non-empty 'nodes'"):
        load_task(path)


def test_invalid_on_conflict_rejected(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    path = write_json(tmp_path / "main.json", main_task(on_conflict="merge"))

    with pytest.raises(TaskValidationError, match="'on_conflict' must be one of"):
        load_task(path)


def test_validate_task_rejects_unresolved_includes():
    with pytest.raises(TaskValidationError, match="unresolved 'includes'"):
        validate_task(main_task())


# ---------- custom action validation ----------


def custom_node_task(action: Dict) -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": action,
                "next": [],
            },
        },
    }


def test_custom_action_requires_name():
    with pytest.raises(TaskValidationError, match="requires non-empty 'name'"):
        validate_task(custom_node_task({"type": "custom"}))


def test_custom_action_unregistered_name_rejected():
    with pytest.raises(TaskValidationError, match="unregistered custom action 'nope'"):
        validate_task(custom_node_task({"type": "custom", "name": "nope"}))


def test_custom_action_builtin_validates():
    validate_task(custom_node_task({"type": "custom", "name": "swipe_until"}))


# ---------- popups (known-benign popup whitelist) validation ----------


def popup_task(popups) -> Dict:
    return {
        "entry": "start",
        "popups": popups,
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
            },
        },
    }


def test_valid_popups_pass():
    validate_task(popup_task([
        {"name": "user_agreement",
         "recognition": {"type": "ui_text", "expected": "用户协议"},
         "action": {"type": "click", "target": "recognized"}},
        {"name": "server_warning",
         "recognition": {"type": "ocr", "expected": "维护", "roi": [0, 0, 100, 100]},
         "action": {"type": "key", "params": {"keycode": 4}}},
    ]))


def test_popups_must_be_a_list():
    with pytest.raises(TaskValidationError, match="'popups' must be a list"):
        validate_task(popup_task({"name": "x"}))


def test_popup_recognition_type_must_be_supported():
    with pytest.raises(TaskValidationError, match="unsupported recognition type"):
        validate_task(popup_task([
            {"recognition": {"type": "always"},  # 'always' would fire every sweep
             "action": {"type": "key", "params": {"keycode": 4}}},
        ]))


def test_popup_ocr_requires_expected():
    with pytest.raises(TaskValidationError, match="requires non-empty 'expected'"):
        validate_task(popup_task([
            {"recognition": {"type": "ocr"},
             "action": {"type": "key", "params": {"keycode": 4}}},
        ]))


def test_popup_action_must_be_dismiss_capable():
    with pytest.raises(TaskValidationError, match="unsupported action type 'agent'"):
        validate_task(popup_task([
            {"recognition": {"type": "ui_text", "expected": "用户协议"},
             "action": {"type": "agent", "text": "do it"}},
        ]))


def test_popup_click_needs_target_or_coords():
    with pytest.raises(TaskValidationError, match="needs target='recognized' or params x/y"):
        validate_task(popup_task([
            {"recognition": {"type": "ui_text", "expected": "用户协议"},
             "action": {"type": "click", "params": {}}},
        ]))


# ---------- popups: the optional `confirm` second gate ----------


def close_x_popup(**extra) -> Dict:
    """The shape that misfired on 2026-08-11: a shared close-X template."""
    popup = {
        "name": "促销弹窗关闭X",
        "recognition": {"type": "template", "template": "popup_close_x", "threshold": 0.82},
        "action": {"type": "click", "target": "recognized"},
    }
    popup.update(extra)
    return popup


def test_popup_confirm_is_optional():
    validate_task(popup_task([close_x_popup()]))


def test_popup_accepts_a_confirm_gate():
    validate_task(popup_task([close_x_popup(
        confirm={"type": "ocr", "expected": "限时礼包", "roi": [100, 450, 1000, 1800]},
    )]))


def test_popup_confirm_accepts_a_combination():
    validate_task(popup_task([close_x_popup(confirm={
        "type": "or",
        "any_of": [
            {"type": "ocr", "expected": "限时"},
            {"type": "ocr", "expected": "礼包"},
        ],
    })]))


def test_popup_confirm_must_be_an_object():
    with pytest.raises(TaskValidationError, match="'confirm' must be a recognition object"):
        validate_task(popup_task([close_x_popup(confirm="限时")]))


def test_popup_confirm_is_validated_like_a_recognition():
    """A typo'd confirm must fail loudly — a silently ignored one degrades the
    gate back to 'always confirmed', which is the bug it exists to prevent."""
    with pytest.raises(TaskValidationError, match="unsupported confirm type"):
        validate_task(popup_task([close_x_popup(confirm={"type": "orr", "any_of": []})]))
    with pytest.raises(TaskValidationError, match="confirm 'ocr' requires non-empty 'expected'"):
        validate_task(popup_task([close_x_popup(confirm={"type": "ocr"})]))
    with pytest.raises(TaskValidationError, match="confirm roi must be"):
        validate_task(popup_task([close_x_popup(
            confirm={"type": "ocr", "expected": "限时", "roi": [1, 2, 3]},
        )]))


# ---------- back_fallback (unknown-popup BACK escape opt-out) ----------


def back_fallback_task(value) -> Dict:
    task = popup_task(None)
    task.pop("popups")
    task["back_fallback"] = value
    return task


def test_back_fallback_accepts_booleans():
    validate_task(back_fallback_task(False))
    validate_task(back_fallback_task(True))


def test_back_fallback_is_optional(sample_task):
    validate_task(sample_task)  # absent = engine config decides


def test_back_fallback_must_be_boolean():
    with pytest.raises(TaskValidationError, match="'back_fallback' must be a boolean"):
        validate_task(back_fallback_task("no"))


# ---------- max_steps (per-task step budget override) ----------


def max_steps_task(value) -> Dict:
    task = back_fallback_task(True)
    task.pop("back_fallback")
    task["max_steps"] = value
    return task


def test_max_steps_accepts_positive_int():
    validate_task(max_steps_task(200))


def test_max_steps_is_optional(sample_task):
    validate_task(sample_task)  # absent = engine config / default decides


def test_max_steps_rejects_non_positive():
    with pytest.raises(TaskValidationError, match="'max_steps' must be a positive integer"):
        validate_task(max_steps_task(0))
    with pytest.raises(TaskValidationError, match="'max_steps' must be a positive integer"):
        validate_task(max_steps_task(-5))


def test_max_steps_rejects_non_int():
    with pytest.raises(TaskValidationError, match="'max_steps' must be a positive integer"):
        validate_task(max_steps_task("50"))
    with pytest.raises(TaskValidationError, match="'max_steps' must be a positive integer"):
        validate_task(max_steps_task(True))


# ---------- wait_still (settle-on-still-frame window) ----------


def wait_still_task(value) -> Dict:
    return {
        "entry": "n",
        "nodes": {
            "n": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "wait_still": value,
            },
        },
    }


def test_wait_still_accepts_full_and_partial_specs():
    validate_task(wait_still_task({"timeout_ms": 8000, "interval_ms": 100, "threshold": 0.02}))
    validate_task(wait_still_task({}))  # all fields default
    validate_task(wait_still_task({"threshold": 0}))


def test_wait_still_is_optional(sample_task):
    validate_task(sample_task)


def test_wait_still_must_be_an_object():
    with pytest.raises(TaskValidationError, match="'wait_still' must be an object"):
        validate_task(wait_still_task(True))


@pytest.mark.parametrize("field", ["timeout_ms", "interval_ms"])
@pytest.mark.parametrize("bad", [-1, 1.5, "500", True])
def test_wait_still_int_fields_rejected(field, bad):
    with pytest.raises(TaskValidationError, match="must be a non-negative integer"):
        validate_task(wait_still_task({field: bad}))


@pytest.mark.parametrize("bad", [-0.1, 1.5, "0.01", True])
def test_wait_still_threshold_range(bad):
    with pytest.raises(TaskValidationError, match="threshold"):
        validate_task(wait_still_task({"threshold": bad}))


# ---------- action-level repeat (params.repeat & friends) ----------

def repeat_task(action: Dict) -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": action,
                "next": [],
            },
        },
    }


@pytest.mark.parametrize("action", [
    {"type": "click", "target": "recognized", "params": {"repeat": 8}},
    {"type": "click", "params": {"x": 1, "y": 2, "repeat": 2, "repeat_delay_ms": 0}},
    {"type": "key", "params": {"keycode": 4, "repeat": 3, "repeat_wait_freezes_ms": 800}},
    {"type": "drag", "params": {"x1": 0, "y1": 0, "x2": 9, "y2": 9, "repeat": 2}},
    {"type": "input_text", "params": {"text": "a", "repeat": 2}},
    {"type": "gesture", "params": {"frames": [], "repeat": 2}},
])
def test_repeat_accepted_on_instantaneous_executor_actions(action):
    validate_task(repeat_task(action))


@pytest.mark.parametrize("action", [
    {"type": "wait", "params": {"duration_ms": 100, "repeat": 3}},
    {"type": "agent", "text": "do it", "params": {"repeat": 3}},
    {"type": "none", "params": {"repeat": 3}},
    {"type": "custom", "name": "swipe_until", "params": {"repeat": 3}},
])
def test_repeat_rejected_where_it_would_do_nothing(action):
    with pytest.raises(TaskValidationError, match="does not support"):
        validate_task(repeat_task(action))


@pytest.mark.parametrize("bad", [0, -1, 1.5, "3", True])
def test_repeat_must_be_a_positive_integer(bad):
    with pytest.raises(TaskValidationError, match="'repeat' must be an integer >= 1"):
        validate_task(repeat_task({"type": "click", "params": {"x": 1, "y": 2, "repeat": bad}}))


@pytest.mark.parametrize("field", ["repeat_delay_ms", "repeat_wait_freezes_ms"])
@pytest.mark.parametrize("bad", [-1, 1.5, "100", True])
def test_repeat_timing_fields_must_be_non_negative_integers(field, bad):
    action = {"type": "click", "params": {"x": 1, "y": 2, field: bad}}
    with pytest.raises(TaskValidationError, match="must be a non-negative integer"):
        validate_task(repeat_task(action))


# ---------- task-level defaults block ----------

def defaults_task(defaults, node_extra: Dict | None = None) -> Dict:
    node = {
        "recognition": {"type": "always"},
        "action": {"type": "none"},
        "next": [],
    }
    node.update(node_extra or {})
    return {"entry": "start", "defaults": defaults, "nodes": {"start": node}}


def test_defaults_fill_nodes_that_omit_the_field(tmp_path):
    path = write_json(tmp_path / "d.json", defaults_task(
        {"timeout_ms": 15000, "poll_interval_ms": 500, "post_delay_ms": 300,
         "wait_still": {"timeout_ms": 3000}}
    ))

    node = load_task(path)["nodes"]["start"]

    assert node["timeout_ms"] == 15000
    assert node["poll_interval_ms"] == 500
    assert node["post_delay_ms"] == 300
    assert node["wait_still"] == {"timeout_ms": 3000}


def test_node_field_wins_over_defaults(tmp_path):
    path = write_json(tmp_path / "d.json", defaults_task(
        {"timeout_ms": 15000, "post_delay_ms": 300}, {"timeout_ms": 1000}
    ))

    node = load_task(path)["nodes"]["start"]

    assert node["timeout_ms"] == 1000   # node field wins
    assert node["post_delay_ms"] == 300  # default fills the rest


def test_node_may_opt_out_of_a_default_with_an_explicit_null(tmp_path):
    path = write_json(tmp_path / "d.json", defaults_task(
        {"wait_still": {"timeout_ms": 3000}}, {"wait_still": None}
    ))

    # Explicit null = "engine default, not the task default": the loader DROPS
    # the key entirely so the engine's own `.get(field, DEFAULT)` reaches its
    # built-in default. Leaving a literal None behind would make `.get()` return
    # None (not the default) and crash the engine's `timeout_ms / 1000` math.
    node = load_task(path)["nodes"]["start"]
    assert "wait_still" not in node


def test_node_null_int_default_is_dropped_not_kept_as_none(tmp_path):
    # Same opt-out for the integer knobs: a null'd timeout_ms must vanish, never
    # survive as None (which the engine's deadline arithmetic cannot divide).
    path = write_json(tmp_path / "d.json", defaults_task(
        {"timeout_ms": 15000}, {"timeout_ms": None}
    ))

    node = load_task(path)["nodes"]["start"]
    assert "timeout_ms" not in node


def test_defaults_do_not_mutate_the_input_task(tmp_path):
    task = defaults_task({"timeout_ms": 15000})
    write_json(tmp_path / "d.json", task)

    resolve_task(task, tmp_path)

    # save_task writes the dict it was given: the compact form must survive.
    assert "timeout_ms" not in task["nodes"]["start"]


def test_defaults_also_cover_included_nodes(tmp_path):
    write_json(tmp_path / "common" / "popups.json", popups_nodes())
    path = write_json(tmp_path / "main.json", main_task(defaults={"post_delay_ms": 250}))

    nodes = load_task(path)["nodes"]

    assert nodes["start"]["post_delay_ms"] == 250
    assert nodes["close_popup"]["post_delay_ms"] == 250


def test_defaults_wait_still_is_copied_per_node(tmp_path):
    task = defaults_task({"wait_still": {"timeout_ms": 3000}})
    task["nodes"]["other"] = {
        "recognition": {"type": "always"}, "action": {"type": "none"}, "next": [],
    }
    path = write_json(tmp_path / "d.json", task)

    nodes = load_task(path)["nodes"]

    assert nodes["start"]["wait_still"] is not nodes["other"]["wait_still"]


def test_defaults_rejects_keys_outside_the_whitelist():
    with pytest.raises(TaskValidationError, match="unsupported key"):
        validate_task(defaults_task({"post_delay_ms": 100, "psot_delay_ms": 100}))


@pytest.mark.parametrize("bad_defaults", [
    {"recognition": {"type": "always"}},   # control flow is not defaultable
    {"on_timeout": "start"},
    {"finding": "boom"},
    {"next": []},
])
def test_defaults_does_not_accept_control_flow_fields(bad_defaults):
    with pytest.raises(TaskValidationError, match="unsupported key"):
        validate_task(defaults_task(bad_defaults))


def test_defaults_must_be_an_object():
    with pytest.raises(TaskValidationError, match="'defaults' must be an object"):
        validate_task(defaults_task([1, 2]))


@pytest.mark.parametrize("field", ["timeout_ms", "poll_interval_ms", "post_delay_ms"])
@pytest.mark.parametrize("bad", [-1, 1.5, "500", True])
def test_defaults_int_fields_rejected(field, bad):
    with pytest.raises(TaskValidationError, match="must be a non-negative integer"):
        validate_task(defaults_task({field: bad}))


def test_defaults_wait_still_is_validated_like_a_node_one():
    with pytest.raises(TaskValidationError, match="'wait_still' must be an object"):
        validate_task(defaults_task({"wait_still": True}))
    with pytest.raises(TaskValidationError, match="threshold"):
        validate_task(defaults_task({"wait_still": {"threshold": 2}}))


def test_defaults_is_optional(sample_task):
    validate_task(sample_task)


