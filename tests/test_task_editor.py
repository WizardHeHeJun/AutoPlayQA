from __future__ import annotations

import pytest

from task.task_editor import BLIND_CLICK_COMMENT, action_log_to_draft, records_to_draft_task
from task.task_loader import validate_task


def record(user_text, actions, ok=True):
    return {"device_id": "dev1", "user_text": user_text, "actions": actions, "results_ok": ok}


def test_single_action_records_chain():
    records = [
        record("点击设置", [{"type": "click", "params": {"x": 1, "y": 2}}]),
        record("返回", [{"type": "key", "params": {"keycode": 4}}]),
    ]
    task = records_to_draft_task(records)

    names = list(task["nodes"].keys())
    assert len(names) == 2
    assert task["entry"] == names[0]
    assert task["nodes"][names[0]]["next"] == [names[1]]
    assert task["nodes"][names[1]]["next"] == []
    assert task["nodes"][names[0]]["action"] == {"type": "click", "params": {"x": 1, "y": 2}}
    assert all(n["recognition"] == {"type": "always"} for n in task["nodes"].values())


def test_multi_action_record_expands_to_sub_nodes():
    records = [
        record(
            "在账号框输入123",
            [
                {"type": "click", "params": {"x": 10, "y": 20}},
                {"type": "input_text", "params": {"text": "123"}},
            ],
        ),
        record("点确定", [{"type": "click", "params": {"x": 5, "y": 5}}]),
    ]
    task = records_to_draft_task(records)

    names = list(task["nodes"].keys())
    assert len(names) == 3
    # sub-nodes chain through to the next record
    assert task["nodes"][names[0]]["next"] == [names[1]]
    assert task["nodes"][names[1]]["next"] == [names[2]]
    assert task["nodes"][names[1]]["action"]["type"] == "input_text"


def test_failed_records_are_dropped():
    records = [
        record("失败的", [{"type": "click", "params": {"x": 1, "y": 1}}], ok=False),
        record("成功的", [{"type": "click", "params": {"x": 2, "y": 2}}]),
    ]
    task = records_to_draft_task(records)
    assert len(task["nodes"]) == 1


def test_no_successful_records_raises():
    with pytest.raises(ValueError, match="record on"):
        records_to_draft_task([record("失败", [{"type": "click", "params": {"x": 1, "y": 1}}], ok=False)])


# ---------- action log (outputs/agent_sessions/**/session.json) -> draft ----------


def element(text="商店", source="dump", bounds=(80, 180, 120, 220), index=5):
    return {
        "index": index,
        "source": source,
        "text": text,
        "desc": "",
        "center": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
        "bounds": list(bounds),
        "clickable": True,
    }


def step(action, element=None, index=1, tool="click_index"):
    return {
        "index": index,
        "t_offset_ms": index * 1000,
        "tool": tool,
        "action": action,
        "element": element,
        "screenshot": f"s{index:03d}_before.png",
    }


def click(x=100, y=200):
    return {"type": "click", "params": {"x": x, "y": y}}


def session(steps, kind="explore", **context):
    return {
        "device_id": "dev1",
        "started_at": "2026-08-17T10:00:00",
        "ended_at": None,
        "context": {"kind": kind, "task": None, "node": None, "run_id": None, "label": None,
                    **context},
        "steps": steps,
    }


def test_dump_element_becomes_ui_text_anchor():
    task = action_log_to_draft(session([step(click(), element(text="商店", source="dump"))]))

    names = list(task["nodes"])
    assert names == ["step_01_商店"]
    node = task["nodes"][names[0]]
    assert node["recognition"] == {"type": "ui_text", "expected": "商店"}
    # anchored click, not the recorded coordinates
    assert node["action"] == {"type": "click", "target": "recognized"}
    assert node["post_delay_ms"] == 800
    assert node["next"] == []
    assert "comment" not in node


def test_ocr_element_gets_expanded_and_clamped_roi():
    # 40% of a 40x40 box = 16px on each side; x1/y1 would go negative -> clamp
    task = action_log_to_draft(
        session([step(click(), element(text="领取", source="ocr", bounds=(10, 5, 50, 45)))])
    )

    node = task["nodes"]["step_01_领取"]
    assert node["recognition"]["type"] == "ocr"
    assert node["recognition"]["expected"] == "领取"
    assert node["recognition"]["roi"] == [0, 0, 66, 61]
    assert node["action"] == {"type": "click", "target": "recognized"}


def test_click_without_element_keeps_coordinates_and_is_marked():
    task = action_log_to_draft(session([step(click(640, 360), element=None)]))

    node = task["nodes"]["step_01"]
    assert node["recognition"] == {"type": "always"}
    assert node["action"] == {"type": "click", "params": {"x": 640, "y": 360}}
    assert node["comment"] == BLIND_CLICK_COMMENT


def test_non_click_actions_pass_through_untouched():
    drag = {"type": "drag", "params": {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 300}}
    text = {"type": "input_text", "params": {"text": "abc"}}
    task = action_log_to_draft(session([step(drag, index=1), step(text, index=2)]))

    nodes = task["nodes"]
    assert nodes["step_01"]["action"] == drag
    assert nodes["step_02"]["action"] == text
    # a passthrough action is not a blind click; nothing to flag
    assert all("comment" not in n for n in nodes.values())
    assert all(n["recognition"] == {"type": "always"} for n in nodes.values())


def test_steps_chain_in_order_and_validate():
    task = action_log_to_draft(
        session([
            step(click(), element(text="商店", source="dump"), index=1),
            step(click(), element(text="购买", source="ocr"), index=2),
            step({"type": "key", "params": {"keycode": 4}}, index=3),
        ]),
        name_prefix="shop",
    )

    names = list(task["nodes"])
    assert names == ["shop_01_商店", "shop_02_购买", "shop_03"]
    assert task["entry"] == "shop_01_商店"
    assert task["nodes"]["shop_01_商店"]["next"] == ["shop_02_购买"]
    assert task["nodes"]["shop_02_购买"]["next"] == ["shop_03"]
    assert task["nodes"]["shop_03"]["next"] == []
    validate_task(task)  # already called inside, asserted here as the contract


def test_element_without_text_falls_back_to_coordinates():
    task = action_log_to_draft(session([step(click(7, 8), element(text="", source="dump"))]))

    node = task["nodes"]["step_01"]
    assert node["recognition"] == {"type": "always"}
    assert node["action"] == {"type": "click", "params": {"x": 7, "y": 8}}
    assert node["comment"] == BLIND_CLICK_COMMENT


def test_empty_session_raises():
    with pytest.raises(ValueError, match="no steps"):
        action_log_to_draft(session([]))
    with pytest.raises(ValueError, match="no steps"):
        action_log_to_draft({"device_id": "dev1"})


def test_step_without_action_raises():
    with pytest.raises(ValueError, match="action"):
        action_log_to_draft(session([{"index": 1, "element": None}]))
