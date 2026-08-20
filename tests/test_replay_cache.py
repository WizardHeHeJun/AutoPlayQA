from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from task.recognizers import RecognizerHub
from task.replay_cache import ReplayCache, center_distance
from task.task_engine import TaskEngine


@pytest.fixture
def cache(fake_logger, tmp_path):
    return ReplayCache(fake_logger, path=str(tmp_path / "replay_cache.json"))


def make_image(width=1080, height=1920):
    from PIL import Image

    return Image.new("RGB", (width, height))


# ---------- ReplayCache ----------

def test_put_get_roundtrip_persists(fake_logger, cache, tmp_path):
    key = ReplayCache.make_key("dev1", "login", "start")
    cache.put(key, bbox=[100, 200, 220, 240], center=[160, 220], text="设置", screen=(1080, 1920))

    fresh = ReplayCache(fake_logger, path=str(tmp_path / "replay_cache.json"))
    entry = fresh.get(key)
    assert entry["bbox"] == [100, 200, 220, 240]
    assert entry["center"] == [160, 220]
    assert entry["screen"] == [1080, 1920]


def test_clear_and_size(cache):
    cache.put("k1", [0, 0, 10, 10], [5, 5], "a", (1080, 1920))
    cache.put("k2", [0, 0, 10, 10], [5, 5], "b", (1080, 1920))
    assert cache.size() == 2
    assert cache.clear() == 2
    assert cache.size() == 0


def test_roi_expansion_and_clamping():
    entry = {"bbox": [10, 10, 130, 50], "screen": [1080, 1920]}
    roi = ReplayCache.roi_from(entry, (1080, 1920))
    # margin_x = max(30, 60) = 60; margin_y = max(30, 20) = 30; clamped at 0.
    assert roi == [0, 0, 190, 80]


def test_roi_none_on_resolution_change():
    entry = {"bbox": [10, 10, 130, 50], "screen": [1080, 1920]}
    assert ReplayCache.roi_from(entry, (720, 1280)) is None


def test_corrupt_cache_file_starts_empty(fake_logger, tmp_path):
    path = tmp_path / "replay_cache.json"
    path.write_text("not json", encoding="utf-8")
    cache = ReplayCache(fake_logger, path=str(path))
    assert cache.size() == 0


# ---------- RecognizerHub fast path ----------

class FakeOcrEngine:
    """Returns items only when the queried region contains them (roi=None means full screen)."""

    def __init__(self, items: List[Dict]):
        self.items = items
        self.roi_calls: List[Optional[List[int]]] = []

    def available(self) -> bool:
        return True

    def recognize(self, png_bytes: bytes, roi=None) -> List[Dict]:
        self.roi_calls.append(list(roi) if roi else None)
        if roi is None:
            return self.items
        x1, y1, x2, y2 = roi
        return [
            i for i in self.items
            if x1 <= i["center"][0] <= x2 and y1 <= i["center"][1] <= y2
        ]


class FakeMatcher:
    def text_similarity(self, expected: str, actual: str) -> float:
        return 1.0 if expected == actual else 0.0


class FakeCapturer:
    def __init__(self, image):
        self.image = image

    def capture_image(self, device_id: str):
        return self.image


def make_hub(fake_logger, cache, items):
    ocr = FakeOcrEngine(items)
    hub = RecognizerHub(FakeMatcher(), ocr, FakeCapturer(make_image()), fake_logger, replay_cache=cache)
    return hub, ocr


def test_first_run_populates_cache_without_roi(fake_logger, cache):
    items = [{"text": "设置", "score": 0.9, "bbox": [100, 200, 220, 240], "center": (160, 220)}]
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置"}, cache_key="dev1|t|n")
    assert hit["center"] == (160, 220)
    assert "cache" not in hit
    assert ocr.roi_calls == [None]
    assert cache.get("dev1|t|n")["bbox"] == [100, 200, 220, 240]


def test_second_run_hits_cached_roi(fake_logger, cache):
    items = [{"text": "设置", "score": 0.9, "bbox": [100, 200, 220, 240], "center": (160, 220)}]
    cache.put("dev1|t|n", [100, 200, 220, 240], [160, 220], "设置", (1080, 1920))
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置"}, cache_key="dev1|t|n")
    assert hit["cache"] == "hit"
    assert len(ocr.roi_calls) == 1 and ocr.roi_calls[0] is not None


def test_anchor_drift_flagged_and_cache_updated(fake_logger, cache):
    # Anchor cached at top-left, but the UI moved it far away.
    cache.put("dev1|t|n", [100, 200, 220, 240], [160, 220], "设置", (1080, 1920))
    items = [{"text": "设置", "score": 0.9, "bbox": [800, 1500, 920, 1540], "center": (860, 1520)}]
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置"}, cache_key="dev1|t|n")
    assert hit["cache"] == "drift"
    assert hit["prev_center"] == [160, 220]
    assert hit["center"] == (860, 1520)
    # How far it moved rides along, so the engine can weigh it against the
    # reporting tolerance instead of re-deriving it.
    assert hit["drift_px"] == round(center_distance([160, 220], (860, 1520)), 1)
    # ROI attempt first, then full screen.
    assert ocr.roi_calls[0] is not None and ocr.roi_calls[1] is None
    assert cache.get("dev1|t|n")["center"] == [860, 1520]


def test_explicit_spec_roi_bypasses_cache(fake_logger, cache):
    cache.put("dev1|t|n", [100, 200, 220, 240], [160, 220], "设置", (1080, 1920))
    items = [{"text": "设置", "score": 0.9, "bbox": [100, 200, 220, 240], "center": (160, 220)}]
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置", "roi": [0, 0, 540, 960]}, cache_key="dev1|t|n")
    assert hit is not None
    assert "cache" not in hit
    assert ocr.roi_calls == [[0, 0, 540, 960]]


def test_no_cache_key_keeps_old_behavior(fake_logger, cache):
    items = [{"text": "设置", "score": 0.9, "bbox": [100, 200, 220, 240], "center": (160, 220)}]
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置"})
    assert hit is not None and "cache" not in hit
    assert ocr.roi_calls == [None]
    assert cache.size() == 0


def test_resolution_change_is_plain_miss_not_drift(fake_logger, cache):
    cache.put("dev1|t|n", [100, 200, 220, 240], [160, 220], "设置", (720, 1280))
    items = [{"text": "设置", "score": 0.9, "bbox": [100, 200, 220, 240], "center": (160, 220)}]
    hub, ocr = make_hub(fake_logger, cache, items)

    hit = hub.recognize("dev1", {"type": "ocr", "expected": "设置"}, cache_key="dev1|t|n")
    assert "cache" not in hit
    assert ocr.roi_calls == [None]
    assert cache.get("dev1|t|n")["screen"] == [1080, 1920]


# ---------- TaskEngine drift finding ----------

class DriftingHub:
    """Recognizes the entry node with a drift flag once, then plain hits."""

    def __init__(self, center=(860, 1520)):
        self.replay_cache = object()  # truthy: engine should pass cache_key
        self.keys: List[Optional[str]] = []
        self.drifted = False
        self.center = center

    def recognize(self, device_id: str, spec: Dict, cache_key=None) -> Optional[Dict]:
        self.keys.append(cache_key)
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        hit = {"center": self.center, "text": "设置", "score": 0.9, "channel": "ocr"}
        if not self.drifted:
            self.drifted = True
            hit["cache"] = "drift"
            hit["prev_center"] = [160, 220]
            hit["drift_px"] = round(center_distance([160, 220], self.center), 1)
        return hit


class FakeExecutor:
    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        return {"ok": "True", "stdout": "", "stderr": ""}


class FakeRecorder:
    def __init__(self):
        self.records: List[Dict] = []

    def open_run(self, device_id, task_name=None):
        pass

    def record(self, finding_type, severity, message, **kwargs):
        self.records.append({"type": finding_type, "severity": severity, "message": message, **kwargs})

    def add_timeline(self, event, **detail):
        pass

    def snapshot_history(self):
        pass

    def finalize(self, status, error=None, node_stats=None):
        self.node_stats = node_stats
        return list(self.records), {"counts": {}, "report_path": None}


def test_engine_reports_anchor_drift_finding(fake_logger, sample_task):
    hub = DriftingHub()
    recorder = FakeRecorder()
    engine = TaskEngine(hub, FakeExecutor(), fake_logger, findings_recorder=recorder)

    result = engine.run("dev1", sample_task, task_name="login")
    assert result["status"] == "completed"
    drifts = [r for r in recorder.records if r["type"] == "anchor_drift"]
    assert len(drifts) == 1
    assert drifts[0]["node"] == "start"
    assert drifts[0]["extra"]["prev_center"] == [160, 220]
    assert drifts[0]["extra"]["drift_px"] > 0
    # cache keys carry device|task|node
    assert "dev1|login|start" in hub.keys
    # The move is telemetry too, not only a finding.
    assert result["node_stats"]["start"]["drift_count"] == 1


def test_drift_within_tolerance_is_counted_but_not_reported(fake_logger, sample_task):
    # The anchor slid 5px (a row reflowed): still a cache miss that re-ran full
    # recognition, but not worth a finding.
    hub = DriftingHub(center=(163, 224))
    recorder = FakeRecorder()
    engine = TaskEngine(
        hub, FakeExecutor(), fake_logger, findings_recorder=recorder,
        engine_config={"drift_tolerance_px": 30},
    )

    result = engine.run("dev1", sample_task, task_name="login")

    assert [r["type"] for r in recorder.records if r["type"] == "anchor_drift"] == []
    stats = result["node_stats"]["start"]
    assert stats["drift_count"] == 1
    assert stats["drift_px"] == [5.0]


def test_drift_beyond_tolerance_is_reported(fake_logger, sample_task):
    hub = DriftingHub(center=(163, 224))
    recorder = FakeRecorder()
    engine = TaskEngine(
        hub, FakeExecutor(), fake_logger, findings_recorder=recorder,
        engine_config={"drift_tolerance_px": 2},
    )

    engine.run("dev1", sample_task, task_name="login")

    drifts = [r for r in recorder.records if r["type"] == "anchor_drift"]
    assert len(drifts) == 1
    assert drifts[0]["extra"]["drift_px"] == 5.0


def test_center_distance_is_euclidean():
    assert center_distance([0, 0], (3, 4)) == 5.0
    assert center_distance([10, 10], [10, 10]) == 0.0


def test_engine_without_task_name_skips_cache_key(fake_logger, sample_task):
    hub = DriftingHub()
    engine = TaskEngine(hub, FakeExecutor(), fake_logger)
    engine.run("dev1", sample_task)
    assert all(k is None for k in hub.keys)
