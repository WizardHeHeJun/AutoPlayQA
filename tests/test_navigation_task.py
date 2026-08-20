from __future__ import annotations

import json

import pytest

from task.task_loader import TaskValidationError, list_tasks, load_task, validate_task


def test_valid_task_passes(sample_task):
    validate_task(sample_task)  # should not raise


def test_missing_entry(sample_task):
    del sample_task["entry"]
    with pytest.raises(TaskValidationError, match="entry"):
        validate_task(sample_task)


def test_entry_not_in_nodes(sample_task):
    sample_task["entry"] = "ghost"
    with pytest.raises(TaskValidationError, match="ghost"):
        validate_task(sample_task)


def test_unknown_next_reference(sample_task):
    sample_task["nodes"]["start"]["next"] = ["ghost"]
    with pytest.raises(TaskValidationError, match="ghost"):
        validate_task(sample_task)


def test_unknown_on_timeout_reference(sample_task):
    sample_task["nodes"]["start"]["on_timeout"] = "ghost"
    with pytest.raises(TaskValidationError, match="on_timeout"):
        validate_task(sample_task)


def test_bad_recognition_type(sample_task):
    sample_task["nodes"]["start"]["recognition"]["type"] = "magic"
    with pytest.raises(TaskValidationError, match="recognition type"):
        validate_task(sample_task)


def test_ui_text_requires_expected(sample_task):
    del sample_task["nodes"]["start"]["recognition"]["expected"]
    with pytest.raises(TaskValidationError, match="expected"):
        validate_task(sample_task)


def test_bad_action_type(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "teleport"}
    with pytest.raises(TaskValidationError, match="action type"):
        validate_task(sample_task)


def test_click_without_target_or_coords(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "click"}
    with pytest.raises(TaskValidationError, match="click"):
        validate_task(sample_task)


def test_agent_action_requires_text(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "agent"}
    with pytest.raises(TaskValidationError, match="agent"):
        validate_task(sample_task)


def test_llm_alias_accepted_with_text(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "llm", "text": "do something"}
    validate_task(sample_task)  # should not raise


def test_agent_action_with_text_valid(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "agent", "text": "处理验证码"}
    validate_task(sample_task)  # should not raise


def test_key_action_requires_keycode(sample_task):
    sample_task["nodes"]["start"]["action"] = {"type": "key"}
    with pytest.raises(TaskValidationError, match="keycode"):
        validate_task(sample_task)


def test_bad_roi(sample_task):
    sample_task["nodes"]["start"]["recognition"]["roi"] = [1, 2, 3]
    with pytest.raises(TaskValidationError, match="roi"):
        validate_task(sample_task)


def test_negative_timeout(sample_task):
    sample_task["nodes"]["start"]["timeout_ms"] = -1
    with pytest.raises(TaskValidationError, match="timeout_ms"):
        validate_task(sample_task)


def test_load_task_from_file(tmp_path, sample_task):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(sample_task, ensure_ascii=False), encoding="utf-8")
    task = load_task(path)
    assert task["entry"] == "start"


def test_load_task_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TaskValidationError, match="Invalid JSON"):
        load_task(path)


def test_load_task_missing_file(tmp_path):
    with pytest.raises(TaskValidationError, match="not found"):
        load_task(tmp_path / "nope.json")


def test_list_tasks(tmp_path, sample_task):
    (tmp_path / "b_task.json").write_text(json.dumps(sample_task), encoding="utf-8")
    (tmp_path / "a_task.json").write_text(json.dumps(sample_task), encoding="utf-8")
    assert list_tasks(tmp_path) == ["a_task", "b_task"]


def test_list_tasks_missing_dir(tmp_path):
    assert list_tasks(tmp_path / "ghost") == []


def test_shipped_sample_task_is_valid():
    task = load_task("task/task_definitions/open_settings.json")
    assert task["entry"] in task["nodes"]
