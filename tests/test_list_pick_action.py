from __future__ import annotations

import logging

import pytest
from PIL import Image

from task.custom_actions import CustomActionContext, get_handler
import task.custom_actions.list_pick  # noqa: F401  (registers "click_topmost_text")


# ---------- fakes ----------

class FakeOcr:
    def __init__(self, items, is_available=True):
        self.items = items
        self.is_available = is_available
        self.roi_seen = []

    def available(self):
        return self.is_available

    def recognize(self, image, roi=None):
        self.roi_seen.append(roi)
        return list(self.items)


class FakeMatcher:
    @staticmethod
    def text_similarity(target, field):
        return 1.0 if target in field else 0.0


class FakeCapturer:
    def capture_image(self, device_id):
        return Image.new("RGB", (1080, 2448), (0, 0, 0))


class FakeHub:
    def __init__(self, ocr):
        self.ocr_engine = ocr
        self.matcher = FakeMatcher()
        self.capturer = FakeCapturer()


class FakeExecutor:
    def __init__(self):
        self.actions = []

    def execute(self, device_id, action, tracer=None):
        self.actions.append(action)
        return {"ok": "True", "stdout": "", "stderr": ""}


def item(text, cx, cy):
    return {"text": text, "center": (cx, cy), "bbox": [cx - 40, cy - 20, cx + 40, cy + 20],
            "score": 1.0}


def make_ctx(items, is_available=True):
    ocr = FakeOcr(items, is_available=is_available)
    hub = FakeHub(ocr)
    executor = FakeExecutor()
    ctx = CustomActionContext(device_id="dev", executor=executor, hub=hub, hit={},
                              logger=logging.getLogger("test"), tracer=None)
    return ctx, executor, ocr


HANDLER = get_handler("click_topmost_text")


# ---------- tests ----------

def test_registered():
    assert HANDLER is not None


def test_picks_smallest_y_regardless_of_ocr_order():
    """The whole point: OCR order is detection order, not top-to-bottom."""
    ctx, executor, _ = make_ctx([
        item("前往", 895, 1733),   # emitted first, but it is the LOWER row
        item("关卡", 108, 1461),
        item("前往", 895, 1521),
    ])
    results = HANDLER(ctx, {"expected": "前往"})
    assert results[0]["ok"] == "True"
    assert executor.actions == [{"type": "click", "params": {"x": 895, "y": 1521}}]


def test_order_bottom_picks_largest_y():
    ctx, executor, _ = make_ctx([item("前往", 895, 1521), item("前往", 895, 1733)])
    HANDLER(ctx, {"expected": "前往", "order": "bottom"})
    assert executor.actions == [{"type": "click", "params": {"x": 895, "y": 1733}}]


def test_roi_is_forwarded_to_ocr():
    ctx, _, ocr = make_ctx([item("前往", 895, 1521)])
    HANDLER(ctx, {"expected": "前往", "roi": [700, 900, 1080, 1900]})
    assert ocr.roi_seen == [[700, 900, 1080, 1900]]


def test_no_match_fails_the_node_without_clicking():
    ctx, executor, _ = make_ctx([item("挑战", 540, 1670)])
    results = HANDLER(ctx, {"expected": "前往"})
    assert results[0]["ok"] == "False"
    assert "前往" in results[0]["stderr"]
    assert executor.actions == []


def test_threshold_filters_weak_matches():
    ctx, executor, _ = make_ctx([item("挑战", 540, 1670)])
    # FakeMatcher scores non-substring matches 0.0; a 0.0 gate must accept it.
    HANDLER(ctx, {"expected": "前往", "threshold": 0.0})
    assert executor.actions == [{"type": "click", "params": {"x": 540, "y": 1670}}]


def test_missing_expected_raises():
    ctx, _, _ = make_ctx([])
    with pytest.raises(ValueError, match="expected"):
        HANDLER(ctx, {})


def test_bad_order_raises():
    ctx, _, _ = make_ctx([item("前往", 895, 1521)])
    with pytest.raises(ValueError, match="order"):
        HANDLER(ctx, {"expected": "前往", "order": "sideways"})


def test_unavailable_ocr_raises():
    ctx, _, _ = make_ctx([item("前往", 895, 1521)], is_available=False)
    with pytest.raises(ValueError, match="OCR"):
        HANDLER(ctx, {"expected": "前往"})
