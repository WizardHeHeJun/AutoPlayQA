"""Combined recognition (`and` / `or`) — hit logic, one-frame reuse, validation.

No device: the dump matcher / OCR engine / capturer are stubs and the frames
are plain PIL images, so "did this read one frame or three" is observable by
counting capture calls and comparing image identity.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pytest
from PIL import Image

from task.recognizers import RecognizerHub
from task.task_engine import TaskEngine
from task.task_loader import TaskValidationError, validate_task

LOGGER = logging.getLogger("test-combo")


class StubMatcher:
    """uiautomator dump stand-in: expected text -> hit center."""

    def __init__(self, nodes: Dict[str, tuple]):
        self.nodes = nodes
        self.calls: List[str] = []

    def match_text(self, device_id: str, expected: str):
        self.calls.append(expected)
        center = self.nodes.get(expected)
        if center is None:
            return None, 0.0
        return {"center": center, "text": expected, "desc": ""}, 1.0

    @staticmethod
    def text_similarity(expected: str, text: str) -> float:
        return 1.0 if expected == text else 0.0


class StubOcr:
    """OCR stand-in that records which frame object it was handed."""

    def __init__(self, items: List[Dict]):
        self.items = items
        self.frames: List = []

    @staticmethod
    def available() -> bool:
        return True

    def recognize(self, image, roi=None) -> List[Dict]:
        self.frames.append(image)
        return list(self.items)


class CountingCapturer:
    def __init__(self, image=None):
        self.image = image if image is not None else Image.new("RGB", (40, 40), (0, 0, 0))
        self.calls = 0

    def capture_image(self, device_id: str):
        self.calls += 1
        return self.image


def ocr_item(text: str, x: int, y: int) -> Dict:
    return {"text": text, "center": (x, y), "bbox": [x - 5, y - 5, x + 5, y + 5]}


def make_hub(nodes=None, ocr_items=None, capturer=None) -> RecognizerHub:
    return RecognizerHub(
        dump_matcher=StubMatcher(nodes or {}),
        ocr_engine=StubOcr(ocr_items or []),
        screenshot_capturer=capturer or CountingCapturer(),
        logger=LOGGER,
    )


# --- `and` -------------------------------------------------------------------

AND_SPEC = {
    "type": "and",
    "all_of": [
        {"type": "ui_text", "expected": "商店"},
        {"type": "ui_text", "expected": "金币"},
    ],
}


def test_and_hits_only_when_every_sub_hits():
    hub = make_hub({"商店": (100, 200), "金币": (300, 400)})

    hit = hub.recognize("dev", AND_SPEC)

    assert hit is not None
    assert hit["channel"] == "and"
    assert hit["sub_channel"] == "ui_text"
    assert hit["center"] == (100, 200)  # box_index defaults to the first sub
    assert hit["sub_index"] == 0
    assert [h["text"] for h in hit["sub_hits"]] == ["商店", "金币"]


def test_and_misses_when_one_sub_misses_and_stops_early():
    """A miss is the whole combination's miss; later subs need not be evaluated."""
    matcher = StubMatcher({"金币": (300, 400)})  # 商店 absent
    hub = RecognizerHub(matcher, StubOcr([]), CountingCapturer(), LOGGER)

    assert hub.recognize("dev", AND_SPEC) is None
    assert matcher.calls == ["商店"]  # short-circuited before 金币


def test_and_box_index_picks_which_sub_supplies_the_hit_box():
    hub = make_hub({"商店": (100, 200), "金币": (300, 400)})

    hit = hub.recognize("dev", dict(AND_SPEC, box_index=1))

    assert hit["center"] == (300, 400)
    assert hit["sub_index"] == 1
    assert hit["text"] == "金币"


def test_and_out_of_range_box_index_falls_back_to_first(caplog):
    hub = make_hub({"商店": (100, 200), "金币": (300, 400)})

    with caplog.at_level(logging.WARNING, logger="test-combo"):
        hit = hub.recognize("dev", dict(AND_SPEC, box_index=7))

    assert hit["center"] == (100, 200)
    assert "box_index" in caplog.text


def test_and_empty_list_is_a_miss_not_a_hit(caplog):
    """Degenerate spec must never become a constant-true gate."""
    with caplog.at_level(logging.WARNING, logger="test-combo"):
        assert make_hub().recognize("dev", {"type": "and", "all_of": []}) is None
    assert "all_of" in caplog.text


# --- `or` --------------------------------------------------------------------

OR_SPEC = {
    "type": "or",
    "any_of": [
        {"type": "ui_text", "expected": "确认"},
        {"type": "ui_text", "expected": "确定"},
    ],
}


def test_or_returns_the_first_hit_and_stops():
    matcher = StubMatcher({"确认": (10, 20), "确定": (30, 40)})
    hub = RecognizerHub(matcher, StubOcr([]), CountingCapturer(), LOGGER)

    hit = hub.recognize("dev", OR_SPEC)

    assert hit["channel"] == "or"
    assert hit["center"] == (10, 20)
    assert hit["sub_index"] == 0
    assert matcher.calls == ["确认"]


def test_or_falls_through_to_the_later_alternative():
    hub = make_hub({"确定": (30, 40)})

    hit = hub.recognize("dev", OR_SPEC)

    assert hit["center"] == (30, 40)
    assert hit["sub_index"] == 1
    assert hit["sub_channel"] == "ui_text"


def test_or_misses_when_nothing_hits():
    assert make_hub().recognize("dev", OR_SPEC) is None


# --- one frame per evaluation ------------------------------------------------

def test_pixel_subs_share_one_captured_frame():
    """Two OCR subs must judge the SAME frame, not two screenshots."""
    ocr = StubOcr([ocr_item("商店", 100, 200), ocr_item("金币", 300, 400)])
    capturer = CountingCapturer()
    hub = RecognizerHub(StubMatcher({}), ocr, capturer, LOGGER)

    hit = hub.recognize("dev", {
        "type": "and",
        "all_of": [
            {"type": "ocr", "expected": "商店"},
            {"type": "ocr", "expected": "金币"},
        ],
        "box_index": 1,
    })

    assert hit["center"] == (300, 400)
    assert capturer.calls == 1
    assert len(ocr.frames) == 2
    assert ocr.frames[0] is ocr.frames[1] is capturer.image


def test_passed_in_frame_is_reused_by_every_sub():
    """image= (the engine's two-shot watchdog) must reach the subs untouched."""
    ocr = StubOcr([ocr_item("商店", 100, 200)])

    class Boom:
        def capture_image(self, device_id):
            raise AssertionError("must not capture when image= is given")

    hub = RecognizerHub(StubMatcher({"金币": (1, 2)}), ocr, Boom(), LOGGER)
    frame = Image.new("RGB", (20, 20), (5, 5, 5))

    hit = hub.recognize("dev", {
        "type": "and",
        "all_of": [
            {"type": "ui_text", "expected": "金币"},
            {"type": "ocr", "expected": "商店"},
        ],
    }, image=frame)

    assert hit is not None
    assert ocr.frames == [frame]


def test_dump_only_combination_captures_nothing():
    capturer = CountingCapturer()
    hub = RecognizerHub(StubMatcher({"商店": (1, 2), "金币": (3, 4)}), StubOcr([]), capturer, LOGGER)

    assert hub.recognize("dev", AND_SPEC) is not None
    assert capturer.calls == 0


def test_nested_combination_shares_the_outer_frame():
    ocr = StubOcr([ocr_item("商店", 100, 200)])
    capturer = CountingCapturer()
    hub = RecognizerHub(StubMatcher({}), ocr, capturer, LOGGER)

    hit = hub.recognize("dev", {
        "type": "or",
        "any_of": [
            {"type": "ocr", "expected": "不存在"},
            {"type": "and", "all_of": [
                {"type": "ocr", "expected": "商店"},
                {"type": "blank_screen"},          # uniform stub frame -> stddev 0
            ]},
        ],
    })

    assert hit is not None
    assert hit["channel"] == "or"
    assert hit["sub_channel"] == "and"
    assert hit["center"] == (100, 200)
    assert capturer.calls == 1
    assert all(f is capturer.image for f in ocr.frames)


# --- gate integrity ----------------------------------------------------------

def test_always_inside_a_combination_is_rejected_at_runtime():
    hub = make_hub({"商店": (1, 2)})
    with pytest.raises(ValueError, match="always"):
        hub.recognize("dev", {"type": "or", "any_of": [{"type": "always"}]})


def test_nesting_deeper_than_two_levels_is_rejected_at_runtime():
    hub = make_hub({"商店": (1, 2)})
    spec = {"type": "and", "all_of": [
        {"type": "and", "all_of": [
            {"type": "and", "all_of": [{"type": "ui_text", "expected": "商店"}]},
        ]},
    ]}
    with pytest.raises(ValueError, match="nested deeper"):
        hub.recognize("dev", spec)


def test_sub_recognitions_get_distinct_replay_cache_keys():
    """Two OCR subs must not overwrite each other's cached anchor box."""
    seen: List[Optional[str]] = []

    class RecordingHub(RecognizerHub):
        def _recognize_ocr(self, device_id, spec, cache_key=None, image=None):
            seen.append(cache_key)
            return {"center": (1, 2), "text": spec["expected"], "score": 1.0,
                    "channel": "ocr", "bbox": [0, 0, 2, 4]}

    hub = RecordingHub(StubMatcher({}), StubOcr([]), CountingCapturer(), LOGGER)
    hub.recognize("dev", {
        "type": "and",
        "all_of": [{"type": "ocr", "expected": "a"}, {"type": "ocr", "expected": "b"}],
    }, cache_key="dev|task|node")

    assert seen == ["dev|task|node#0", "dev|task|node#1"]


# --- engine integration ------------------------------------------------------

def combo_task(box_index: int) -> Dict:
    return {
        "entry": "shop",
        "nodes": {
            "shop": {
                "recognition": dict(AND_SPEC, box_index=box_index),
                "action": {"type": "click", "target": "recognized"},
                "next": [],
                "timeout_ms": 0,
                "post_delay_ms": 0,
            },
        },
    }


class FakeExecutor:
    def __init__(self):
        self.executed: List[Dict] = []

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        return {"ok": "True", "stdout": "", "stderr": ""}


def test_engine_clicks_the_box_index_sub_hit():
    hub = make_hub({"商店": (100, 200), "金币": (300, 400)})
    executor = FakeExecutor()
    engine = TaskEngine(hub, executor, LOGGER)

    result = engine.run("dev1", combo_task(box_index=1))

    assert result["status"] == "completed"
    assert executor.executed == [{"type": "click", "params": {"x": 300, "y": 400}}]


def test_engine_treats_a_combination_miss_as_a_plain_recognition_miss():
    hub = make_hub({"商店": (100, 200)})  # 金币 missing -> AND misses
    executor = FakeExecutor()
    engine = TaskEngine(hub, executor, LOGGER)

    result = engine.run("dev1", combo_task(box_index=0))

    assert result["ok"] is False
    assert "timeout" in result["error"].lower()
    # Nothing was clicked: a half-satisfied combination is a miss, and the only
    # action left is the engine's own BACK fallback for the stall.
    assert [a["type"] for a in executor.executed] == ["key"]


# --- task_loader validation --------------------------------------------------

def task_with_recognition(recognition: Dict) -> Dict:
    return {
        "entry": "n",
        "nodes": {"n": {"recognition": recognition, "action": {"type": "none"}, "next": []}},
    }


def test_validate_accepts_and_or_recognition():
    validate_task(task_with_recognition({
        "type": "and",
        "all_of": [
            {"type": "template", "template": "shop_icon", "threshold": 0.85},
            {"type": "ocr", "expected": "商店", "roi": [0, 0, 1080, 300]},
        ],
        "box_index": 1,
    }))
    validate_task(task_with_recognition({
        "type": "or",
        "any_of": [
            {"type": "ui_text", "expected": "确认"},
            {"type": "yolo", "label": "button", "conf": 0.4},
        ],
    }))


@pytest.mark.parametrize("recognition, match", [
    ({"type": "and"}, "non-empty 'all_of' list"),
    ({"type": "and", "all_of": []}, "non-empty 'all_of' list"),
    ({"type": "or", "any_of": []}, "non-empty 'any_of' list"),
    ({"type": "and", "all_of": [{"type": "always"}]}, "unsupported recognition type"),
    ({"type": "and", "all_of": [{"type": "magic"}]}, "unsupported recognition type"),
    ({"type": "and", "all_of": ["商店"]}, "must be a recognition object"),
    ({"type": "and", "all_of": [{"type": "ocr"}]}, "requires non-empty 'expected'"),
    ({"type": "and", "all_of": [{"type": "template"}]}, "requires non-empty 'template'"),
    ({"type": "and", "all_of": [{"type": "feature", "template": "t", "ratio": 2}]},
     "'ratio' must be a number"),
    ({"type": "and", "all_of": [{"type": "ocr", "expected": "x", "roi": [1, 2]}]},
     r"roi must be \[x1, y1, x2, y2\]"),
    ({"type": "and", "all_of": [{"type": "ocr", "expected": "x"}], "box_index": 1},
     r"'box_index' must be an integer in \[0, 0\]"),
    ({"type": "and", "all_of": [{"type": "ocr", "expected": "x"}], "box_index": True},
     "'box_index' must be an integer"),
    ({"type": "or", "any_of": [{"type": "ocr", "expected": "x"}], "box_index": 0},
     "'or' has no 'box_index'"),
])
def test_validate_rejects_malformed_combinations(recognition, match):
    with pytest.raises(TaskValidationError, match=match):
        validate_task(task_with_recognition(recognition))


def test_validate_allows_one_level_of_nesting():
    validate_task(task_with_recognition({
        "type": "and",
        "all_of": [
            {"type": "ui_text", "expected": "商店"},
            {"type": "or", "any_of": [
                {"type": "ocr", "expected": "金币"},
                {"type": "ocr", "expected": "钻石"},
            ]},
        ],
    }))


def test_validate_rejects_nesting_deeper_than_two_levels():
    with pytest.raises(TaskValidationError, match="deeper than 2 levels"):
        validate_task(task_with_recognition({
            "type": "and",
            "all_of": [{"type": "or", "any_of": [
                {"type": "and", "all_of": [{"type": "ui_text", "expected": "商店"}]},
            ]}],
        }))


def test_validate_accepts_combination_watchdog():
    task = task_with_recognition({"type": "always"})
    task["watchdogs"] = [{
        "type": "and",
        "all_of": [
            {"type": "ocr", "expected": "网络错误"},
            {"type": "blank_screen", "threshold": 10},
        ],
        "severity": "error",
    }]
    validate_task(task)


def test_validate_rejects_malformed_combination_watchdog():
    task = task_with_recognition({"type": "always"})
    task["watchdogs"] = [{"type": "or", "any_of": [{"type": "ocr"}]}]
    with pytest.raises(TaskValidationError, match="requires non-empty 'expected'"):
        validate_task(task)


def test_validate_accepts_combination_popup():
    task = task_with_recognition({"type": "always"})
    task["popups"] = [{
        "name": "agreement",
        "recognition": {"type": "and", "all_of": [
            {"type": "ui_text", "expected": "用户协议"},
            {"type": "ui_text", "expected": "同意"},
        ], "box_index": 1},
        "action": {"type": "click", "target": "recognized"},
    }]
    validate_task(task)


def test_validate_rejects_malformed_combination_popup():
    task = task_with_recognition({"type": "always"})
    task["popups"] = [{
        "recognition": {"type": "and", "all_of": []},
        "action": {"type": "key", "params": {"keycode": 4}},
    }]
    with pytest.raises(TaskValidationError, match="non-empty 'all_of' list"):
        validate_task(task)


def test_watchdog_combination_spec_reaches_the_hub():
    """WATCHDOG_SPEC_KEYS must forward all_of/any_of, or a combination watchdog
    would arrive at the hub as a bare {"type": "and"} and never fire."""
    from task.task_engine import WATCHDOG_SPEC_KEYS

    for key in ("all_of", "any_of", "box_index"):
        assert key in WATCHDOG_SPEC_KEYS
