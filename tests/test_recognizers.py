"""RecognizerHub dispatch for the `scene` channel.

Device-free: the classifier is a stub returning a canned SceneReading, so what
is under test is the *gate* — prefix matching, the confidence threshold, frame
reuse, and the shape of the hit dict — not the classification itself (that is
tests/test_scene_classifier.py's job).
"""
from __future__ import annotations

import logging
from typing import List, Optional

import pytest
from PIL import Image

from perception.scene_classifier import SceneReading
from task.recognizers import DEFAULT_SCENE_MIN_CONF, RECOGNITION_TYPES, RecognizerHub
from task.task_loader import TaskValidationError, validate_task

LOGGER = logging.getLogger("test-recognizers")


class StubSceneClassifier:
    """Answers with a fixed reading and records the frames it was handed."""

    def __init__(self, scene="home_main", confidence=0.9, available=True):
        self.reading = SceneReading(
            scene=scene, confidence=confidence,
            evidence={"signal": scene, "matched": 3}, checked=["blank", scene],
        )
        self._available = available
        self.frames: List = []
        self.device_ids: List[Optional[str]] = []

    def available(self) -> bool:
        return self._available

    def classify(self, image, *, device_id=None) -> SceneReading:
        self.frames.append(image)
        self.device_ids.append(device_id)
        return self.reading


class CountingCapturer:
    def __init__(self, image=None):
        self.image = image if image is not None else Image.new("RGB", (40, 40))
        self.calls = 0

    def capture_image(self, device_id: str):
        self.calls += 1
        return self.image


def make_hub(classifier=None, capturer=None) -> RecognizerHub:
    return RecognizerHub(
        dump_matcher=None, ocr_engine=None,
        screenshot_capturer=capturer or CountingCapturer(), logger=LOGGER,
        scene_classifier=classifier,
    )


# --- dispatch ---------------------------------------------------------------

def test_scene_is_a_registered_recognition_type():
    assert "scene" in RECOGNITION_TYPES


def test_exact_label_hits():
    classifier = StubSceneClassifier("home_main", 0.92)
    hit = make_hub(classifier).recognize("dev1", {"type": "scene", "expected": "home_main"})
    assert hit is not None
    assert hit["channel"] == "scene"
    assert hit["scene"] == "home_main"
    assert hit["text"] == "home_main"
    assert hit["score"] == 0.92
    # No coordinates: "which screen is this" has no click anchor, same contract
    # as blank_screen.
    assert hit["center"] is None
    assert hit["evidence"]["signal"] == "home_main"
    assert hit["checked"] == ["blank", "home_main"]


def test_prefix_expected_matches_a_dotted_family():
    classifier = StubSceneClassifier("level.battle", 0.8)
    hub = make_hub(classifier)
    assert hub.recognize("dev1", {"type": "scene", "expected": "level"}) is not None
    assert hub.recognize("dev1", {"type": "scene", "expected": "level.battle"}) is not None
    assert hub.recognize("dev1", {"type": "scene", "expected": "level.puzzle"}) is None
    assert hub.recognize("dev1", {"type": "scene", "expected": "lev"}) is None


def test_unknown_never_matches_anything():
    """An unrecognized screen is a miss that falls through to on_timeout —
    including for a node that literally asks for "unknown". Absence of evidence
    must not open a recognition gate."""
    hub = make_hub(StubSceneClassifier("unknown", 0.0))
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is None
    assert hub.recognize("dev1", {"type": "scene", "expected": "level"}) is None
    hub = make_hub(StubSceneClassifier("unknown", 1.0))
    assert hub.recognize("dev1", {"type": "scene", "expected": "unknown"}) is None


def test_confidence_below_min_conf_is_a_miss():
    hub = make_hub(StubSceneClassifier("home_main", 0.30))
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is None
    spec = {"type": "scene", "expected": "home_main", "min_conf": 0.25}
    assert hub.recognize("dev1", spec) is not None


def test_default_min_conf_is_the_classifier_default():
    hub = make_hub(StubSceneClassifier("home_main", DEFAULT_SCENE_MIN_CONF))
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is not None
    hub = make_hub(StubSceneClassifier("home_main", DEFAULT_SCENE_MIN_CONF - 0.01))
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is None


def test_absent_or_unavailable_classifier_is_a_miss_not_an_error():
    assert make_hub(None).recognize("dev1", {"type": "scene", "expected": "home_main"}) is None
    hub = make_hub(StubSceneClassifier("home_main", 1.0, available=False))
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is None


def test_missing_expected_is_a_miss():
    hub = make_hub(StubSceneClassifier("home_main", 1.0))
    assert hub.recognize("dev1", {"type": "scene"}) is None


def test_a_failing_classifier_degrades_to_a_miss():
    class BoomClassifier(StubSceneClassifier):
        def classify(self, image, *, device_id=None):
            raise RuntimeError("classifier exploded")

    hub = make_hub(BoomClassifier())
    assert hub.recognize("dev1", {"type": "scene", "expected": "home_main"}) is None


# --- frame reuse ------------------------------------------------------------

def test_a_supplied_frame_is_reused_instead_of_capturing_a_new_one():
    """The two-shot watchdog / findings contract: evidence pins the frame the
    detection ran on, so recognize(image=) must not grab a fresh screenshot."""
    classifier = StubSceneClassifier("popup", 0.9)
    capturer = CountingCapturer()
    frame = Image.new("RGB", (10, 10))
    hub = make_hub(classifier, capturer)
    hub.recognize("dev1", {"type": "scene", "expected": "popup"}, image=frame)
    assert capturer.calls == 0
    assert classifier.frames == [frame]


def test_without_a_frame_one_is_captured():
    capturer = CountingCapturer()
    hub = make_hub(StubSceneClassifier("popup", 0.9), capturer)
    hub.recognize("dev1", {"type": "scene", "expected": "popup"})
    assert capturer.calls == 1


def test_device_id_is_forwarded_to_the_classifier():
    classifier = StubSceneClassifier("popup", 0.9)
    make_hub(classifier).recognize("dev7", {"type": "scene", "expected": "popup"})
    assert classifier.device_ids == ["dev7"]


# --- combined recognition ---------------------------------------------------

def test_scene_inside_a_combination_shares_the_one_frame():
    classifier = StubSceneClassifier("level.battle", 0.9)
    capturer = CountingCapturer()
    hub = make_hub(classifier, capturer)
    spec = {
        "type": "or",
        "any_of": [
            {"type": "scene", "expected": "level.puzzle"},
            {"type": "scene", "expected": "level.battle"},
        ],
    }
    hit = hub.recognize("dev1", spec)
    assert hit is not None
    assert hit["sub_channel"] == "scene"
    assert capturer.calls == 1              # one frame for both children
    assert classifier.frames[0] is classifier.frames[1] is capturer.image


# --- task schema validation -------------------------------------------------

def test_scene_node_validates_and_requires_expected():
    task = {
        "entry": "where",
        "nodes": {
            "where": {
                "recognition": {"type": "scene", "expected": "level"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            }
        },
    }
    validate_task(task)
    task["nodes"]["where"]["recognition"] = {"type": "scene"}
    with pytest.raises(TaskValidationError):
        validate_task(task)


def test_scene_watchdog_validates():
    task = {
        "entry": "start",
        "watchdogs": [{"type": "scene", "expected": "popup.crash", "severity": "critical"}],
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            }
        },
    }
    validate_task(task)
