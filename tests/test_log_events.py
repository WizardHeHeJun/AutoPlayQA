"""The `EVT` instrumentation contract: one format, three producers.

Every new probe in the project goes through `core.logger.log_event`, so the
line shape (`EVT <event> k=v ...`) is asserted once here, together with the two
places that must never go dark again: the recognition channels (hit AND miss,
with the sub-threshold score a tuner needs) and the findings timeline mirror.

Everything is at DEBUG on purpose — the singleton logger stays wide open while
the console handler filters, so these lines reach `run.log` without changing
what a terminal shows.
"""
from __future__ import annotations

import logging
from typing import Dict, List
from unittest.mock import patch

import pytest
from PIL import Image

from core.logger import LOGGER_NAME, log_event
from task.findings import FindingsRecorder
from task.recognizers import RecognizerHub

LOGGER = logging.getLogger(LOGGER_NAME)


@pytest.fixture
def events(caplog):
    """Captured `EVT ...` message lines, in order."""
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    def read() -> List[str]:
        return [r.getMessage() for r in caplog.records if r.getMessage().startswith("EVT ")]

    return read


# --- log_event formatting ----------------------------------------------------

def test_fields_are_rendered_as_greppable_key_value_pairs(events):
    log_event(LOGGER, "node_done", node="主界面", ms=1234, via="ocr")

    assert events() == ["EVT node_done node=主界面 ms=1234 via=ocr"]


def test_none_valued_fields_are_dropped(events):
    log_event(LOGGER, "recognize", channel="ocr", score=None, best_score=0.41)

    assert events() == ["EVT recognize channel=ocr best_score=0.410"]


def test_floats_carry_three_decimals(events):
    log_event(LOGGER, "recognize", score=0.87654, ratio=1.0)

    assert events() == ["EVT recognize score=0.877 ratio=1.000"]


def test_values_with_whitespace_stay_one_token(events):
    log_event(LOGGER, "recognize", matched="开始 游戏", empty="")

    assert events() == ['EVT recognize matched="开始 游戏" empty=""']


def test_event_and_level_are_usable_as_field_names(events):
    # positional-only signature: the timeline mirror needs `event=` as a field.
    log_event(LOGGER, "timeline", logging.INFO, event="agent_resume", level="info")

    assert events() == ["EVT timeline event=agent_resume level=info"]


def test_level_defaults_to_debug_and_is_honoured(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    log_event(LOGGER, "quiet")
    log_event(LOGGER, "loud", logging.WARNING)

    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["EVT quiet"] == logging.DEBUG
    assert levels["EVT loud"] == logging.WARNING


# --- recognition channels ----------------------------------------------------

class StubMatcher:
    def __init__(self, node=None, score: float = 0.0):
        self.node, self.score = node, score

    def match_text(self, device_id: str, expected: str):
        return self.node, self.score

    @staticmethod
    def text_similarity(expected: str, text: str) -> float:
        return 1.0 if expected == text else 0.4


class StubOcr:
    def __init__(self, items: List[Dict]):
        self.items = items

    @staticmethod
    def available() -> bool:
        return True

    def recognize(self, image, roi=None) -> List[Dict]:
        return list(self.items)


class StubCapturer:
    @staticmethod
    def capture_image(device_id: str):
        return Image.new("RGB", (40, 40), (0, 0, 0))


def make_hub(matcher=None, ocr_items=None) -> RecognizerHub:
    return RecognizerHub(
        dump_matcher=matcher or StubMatcher(),
        ocr_engine=StubOcr(ocr_items or []),
        screenshot_capturer=StubCapturer(),
        logger=LOGGER,
    )


def test_ui_text_hit_is_logged_with_channel_target_and_score(events):
    node = {"center": (10, 20), "text": "商店", "desc": ""}
    hub = make_hub(StubMatcher(node, 0.91))

    hub.recognize("dev", {"type": "ui_text", "expected": "商店"})

    line = events()[0]
    assert line.startswith("EVT recognize channel=ui_text hit=1 device=dev target=商店 score=0.910")
    assert " ms=" in line


def test_ui_text_miss_reports_the_sub_threshold_best_score(events):
    node = {"center": (10, 20), "text": "商城", "desc": ""}
    hub = make_hub(StubMatcher(node, 0.42))  # below the 0.65 default gate

    assert hub.recognize("dev", {"type": "ui_text", "expected": "商店"}) is None

    line = events()[0]
    assert "channel=ui_text hit=0" in line and "best_score=0.420" in line


def test_ocr_miss_reports_how_close_the_best_text_got(events):
    hub = make_hub(ocr_items=[{"text": "开始", "center": (5, 5), "bbox": [0, 0, 10, 10]}])

    assert hub.recognize("dev", {"type": "ocr", "expected": "结束"}) is None

    line = [e for e in events() if "channel=ocr" in e][0]
    assert "hit=0" in line and "best_score=0.400" in line


def test_blank_screen_miss_carries_the_measured_stddev(events):
    hub = make_hub()

    assert hub.recognize("dev", {"type": "blank_screen", "threshold": 0.0}) is None

    line = [e for e in events() if "channel=blank_screen" in e][0]
    assert "hit=0" in line and "best_score=" in line


# --- findings timeline mirror ------------------------------------------------

def test_every_timeline_event_is_mirrored_to_the_log(events, tmp_path):
    recorder = FindingsRecorder(LOGGER, output_dir=str(tmp_path))
    recorder.open_run("dev-1", "冒烟测试")

    recorder.add_timeline("node_recognized", node="主界面", channel="ocr", score=0.87)

    assert events() == ["EVT timeline event=node_recognized node=主界面 channel=ocr score=0.870"]


def test_mirror_summarizes_non_scalar_detail_and_drops_none(events, tmp_path):
    recorder = FindingsRecorder(LOGGER, output_dir=str(tmp_path))
    recorder.open_run("dev-1")

    recorder.add_timeline("poll", candidates=["a", "b", "c"], recovery=None)

    # A list becomes its length (never its contents) and a None field vanishes.
    assert events() == ["EVT timeline event=poll candidates_len=3"]


def test_mirror_falls_back_to_the_project_logger(events, tmp_path):
    recorder = FindingsRecorder(None, output_dir=str(tmp_path))
    recorder.open_run("dev-1")

    recorder.add_timeline("agent_resume", node="开始")

    assert events() == ["EVT timeline event=agent_resume node=开始"]


# --- MCP tool calls ----------------------------------------------------------
#
# The other half of the flight recorder: an agent driving the device by hand
# never enters TaskEngine.run, so without this every click/swipe/screenshot it
# made was invisible after the fact.

def test_a_real_tool_call_emits_one_mcp_tool_line(events):
    import mcp_server

    with patch.object(mcp_server._device_manager, "discover_devices", return_value=[]):
        mcp_server.list_devices()

    line = [e for e in events() if e.startswith("EVT mcp_tool")][0]
    assert "tool=list_devices" in line
    assert "ok=1" in line and " ms=" in line
    assert "n=0" in line  # list result: length only


def test_only_cheap_scalar_arguments_reach_the_log(events):
    import mcp_server

    @mcp_server._instrument
    def save_task(device_id: str, name: str, task_json: str):
        return {"ok": True}

    save_task("dev1", "冒烟测试", '{"entry": "a", "nodes": {}}' * 200)

    line = events()[0]
    assert "tool=save_task device=dev1 name=冒烟测试" in line
    assert "task_json" not in line and "nodes" not in line  # payload never logged
    assert "result_ok=1" in line


def test_long_text_arguments_are_truncated(events):
    import mcp_server

    @mcp_server._instrument
    def input_text(device_id: str, text: str):
        return {"ok": True}

    input_text("dev1", "x" * 100)

    line = events()[0]
    assert "text=" + "x" * 40 + "..." in line


def test_screenshot_results_are_summarized_by_size_not_content(events):
    import mcp_server

    @mcp_server._instrument
    def screenshot(device_id: str):
        return {"path": "/out/a.png", "width": 1080, "height": 2400,
                "image_width": 720, "image_height": 1600}

    screenshot("dev1")

    line = events()[0]
    assert "img=720x1600" in line
    assert "/out/a.png" not in line


def test_a_failing_tool_warns_with_its_name_and_still_raises(caplog):
    import mcp_server

    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    @mcp_server._instrument
    def click(device_id: str, x: int, y: int):
        raise RuntimeError("adb died")

    with pytest.raises(RuntimeError, match="adb died"):
        click("dev1", 10, 20)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("MCP tool 'click' failed" in r.getMessage()
               and "RuntimeError: adb died" in r.getMessage() for r in warnings)
    # A failed call must not also claim success.
    assert not [r for r in caplog.records if r.getMessage().startswith("EVT mcp_tool")]


# --- verify_steps ------------------------------------------------------------

def test_a_passing_verify_step_is_logged_not_only_traced(events):
    from agent.device_agent import DeviceAgent

    class Profile:
        device_id = "dev-7"

    class Resolver:
        @staticmethod
        def generate_actions(text, device_id=None, tracer=None):
            return [{"type": "click", "params": {"x": 1, "y": 2}},
                    {"type": "click", "params": {"x": 3, "y": 4}}]

    frames = [Image.new("RGB", (4, 4), (0, 0, 0)), Image.new("RGB", (4, 4), (255, 255, 255))]

    class Capturer:
        def capture_image(self, device_id):
            return frames.pop(0) if frames else Image.new("RGB", (4, 4), (255, 255, 255))

    agent = DeviceAgent(
        Profile(), LOGGER, Resolver(),
        {"execution": {"verify_steps": True, "verify_change_threshold": 0.005}},
        screenshot_capturer=Capturer(),
    )
    with patch.object(agent.executor, "execute", return_value={"ok": "True"}):
        agent.execute_text_command("点击开始")

    line = [e for e in events() if e.startswith("EVT verify_step")][0]
    assert "device=dev-7" in line and "index=0" in line and "change_ratio=1.000" in line


# --- replay cache ------------------------------------------------------------
#
# The cache only ever narrows the OCR search region, so `cache=` explains
# latency, never the verdict.

class CountingCache:
    """Minimal ReplayCache stand-in with a pre-seeded anchor."""

    def __init__(self, entry=None):
        self.entry, self.puts = entry, []

    def get(self, key):
        return self.entry

    def put(self, key, bbox, center, text, screen):
        self.puts.append(key)

    @staticmethod
    def roi_from(entry, screen):
        return entry.get("roi")


def ocr_hub(items, cache):
    hub = make_hub(ocr_items=items)
    hub.replay_cache = cache
    return hub


def test_a_fast_path_hit_reports_cache_hit(events):
    items = [{"text": "开始", "center": (5, 5), "bbox": [0, 0, 10, 10]}]
    hub = ocr_hub(items, CountingCache({"roi": [0, 0, 20, 20], "center": [5, 5]}))

    hub.recognize("dev", {"type": "ocr", "expected": "开始"}, cache_key="dev|t|n")

    line = [e for e in events() if "channel=ocr" in e][0]
    assert "hit=1" in line and "cache=hit" in line


def test_an_anchor_found_outside_the_cached_region_reports_drift(events):
    calls = {"n": 0}

    class MovedOcr(StubOcr):
        def recognize(self, image, roi=None):
            # Empty inside the cached ROI, found on the full-screen retry.
            calls["n"] += 1
            return [] if calls["n"] == 1 else list(self.items)

    hub = make_hub()
    hub.ocr_engine = MovedOcr([{"text": "开始", "center": (300, 300), "bbox": [290, 290, 310, 310]}])
    hub.replay_cache = CountingCache({"roi": [0, 0, 20, 20], "center": [5, 5]})

    hub.recognize("dev", {"type": "ocr", "expected": "开始"}, cache_key="dev|t|n")

    line = [e for e in events() if "channel=ocr" in e][0]
    assert "hit=1" in line and "cache=drift" in line


def test_a_full_miss_after_a_cached_region_reports_cache_miss(events):
    hub = ocr_hub([], CountingCache({"roi": [0, 0, 20, 20], "center": [5, 5]}))

    assert hub.recognize("dev", {"type": "ocr", "expected": "开始"}, cache_key="dev|t|n") is None

    line = [e for e in events() if "channel=ocr" in e][0]
    assert "hit=0" in line and "cache=miss" in line


def test_an_uncached_node_gets_no_cache_field(events):
    hub = make_hub(ocr_items=[{"text": "开始", "center": (5, 5), "bbox": [0, 0, 10, 10]}])

    hub.recognize("dev", {"type": "ocr", "expected": "开始"})

    line = [e for e in events() if "channel=ocr" in e][0]
    assert "cache=" not in line  # nothing cached is not a cache miss


def test_cache_writes_and_clears_are_logged(events, tmp_path, caplog):
    from task.replay_cache import ReplayCache

    cache = ReplayCache(LOGGER, path=str(tmp_path / "cache.json"))
    cache.put(ReplayCache.make_key("dev-1", "冒烟测试", "主界面"), [0, 0, 10, 10], [5, 5],
              "开始", (1080, 2400))

    assert events() == ["EVT replay_cache_put device=dev-1 task=冒烟测试 node=主界面 center=5,5"]

    assert cache.clear() == 1
    assert any("Replay cache cleared (1 anchor(s))" == r.getMessage()
               and r.levelno == logging.INFO for r in caplog.records)
