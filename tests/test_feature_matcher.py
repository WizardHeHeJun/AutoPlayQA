from __future__ import annotations

import logging

import numpy as np
import pytest
from PIL import Image

from perception.feature_matcher import FeatureMatcher
from task.recognizers import RecognizerHub
from task.task_loader import TaskValidationError, validate_task

LOGGER = logging.getLogger("test")


# --- synthetic texture helpers ----------------------------------------------
# ORB needs corners/blobs, so the "icon" is deterministic noise plus hard edges
# (a flat two-color block would yield almost no keypoints — which is exactly the
# case the docs steer to the template channel).

def _sprite(size: int = 64, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)
    # Blocky high-contrast structure on top of the noise -> stable keypoints.
    arr[8:24, 8:24] = 255
    arr[32:56, 12:28] = 0
    arr[10:20, 40:60] = 30
    arr[40:60, 40:60] = 220
    return arr


def _scene(placements, w=320, h=320, seed=5) -> Image.Image:
    """Low-contrast noise background with sprite copies pasted at (x, y)."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 40, size=(h, w, 3), dtype=np.uint8)
    sprite = _sprite()
    for x, y in placements:
        arr[y:y + sprite.shape[0], x:x + sprite.shape[1]] = sprite
    return Image.fromarray(arr, "RGB")


def _sprite_image() -> Image.Image:
    return Image.fromarray(_sprite(), "RGB")


@pytest.fixture
def fmatcher(tmp_path):
    return FeatureMatcher(LOGGER, template_dir=tmp_path)


def _save_png(tmp_path, name: str, arr: np.ndarray, mode: str = "RGB"):
    path = tmp_path / f"{name}.png"
    Image.fromarray(arr, mode).save(path, format="PNG")
    return path


# --- core matching -----------------------------------------------------------

def test_match_locates_textured_sprite(fmatcher):
    scene = _scene([(100, 120)])

    hit = fmatcher.match(scene, _sprite_image())

    assert hit is not None
    assert hit["matches"] >= 4
    # Center of the 64x64 sprite pasted at (100, 120), within keypoint slop.
    assert abs(hit["center"][0] - 132) <= 8
    assert abs(hit["center"][1] - 152) <= 8
    assert 0.0 < hit["score"] <= 1.0


def test_match_absent_sprite_returns_none(fmatcher):
    assert fmatcher.match(_scene([]), _sprite_image()) is None


def test_min_matches_gate_rejects_weak_evidence(fmatcher):
    scene = _scene([(100, 120)])
    # An unreachable evidence bar turns a real hit into a miss: min_matches is
    # the gate, not a hint.
    assert fmatcher.match(scene, _sprite_image(), min_matches=10_000) is None


def test_roi_limits_search_but_reports_full_coords(fmatcher):
    # Two copies; the ROI covers only the lower-right one.
    scene = _scene([(20, 20), (200, 200)])

    hit = fmatcher.match(scene, _sprite_image(), roi=[180, 180, 320, 320])

    assert hit is not None
    assert hit["center"][0] > 180 and hit["center"][1] > 180  # full-screen coords


def test_scaled_sprite_still_matches(fmatcher):
    # ORB's selling point: the art got rescaled between builds and the anchor
    # still resolves, where matchTemplate would need an explicit scale sweep.
    import cv2

    sprite = _sprite()
    big = cv2.resize(sprite, (96, 96), interpolation=cv2.INTER_LINEAR)
    arr = np.array(_scene([]))
    arr[100:196, 120:216] = big
    scene = Image.fromarray(arr, "RGB")

    hit = fmatcher.match(scene, _sprite_image(), min_matches=4)

    assert hit is not None
    assert abs(hit["center"][0] - 168) <= 20
    assert abs(hit["center"][1] - 148) <= 20


def test_alpha_transparent_region_grows_no_keypoints(fmatcher, tmp_path):
    # Same crop twice: opaque core + a transparent border filled with garbage
    # art. The mask must keep the garbage out, so the template still locates the
    # clean sprite in the scene.
    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[..., :3] = _sprite()
    rgba[..., 3] = 255
    rgba[:16, :, :3] = np.random.default_rng(99).integers(0, 256, size=(16, 64, 3), dtype=np.uint8)
    rgba[:16, :, 3] = 0  # ...and that garbage band is transparent
    _save_png(tmp_path, "masked", rgba, mode="RGBA")

    hit = fmatcher.match(_scene([(100, 120)]), "masked")

    assert hit is not None
    assert abs(hit["center"][1] - 152) <= 12


def test_flat_template_reports_no_keypoints(fmatcher, tmp_path, caplog):
    flat = np.full((40, 40, 3), 128, np.uint8)  # perfectly uniform: no corners
    _save_png(tmp_path, "flat", flat)

    with caplog.at_level(logging.WARNING):
        assert fmatcher.match(_scene([(100, 120)]), "flat") is None
    assert any("no ORB keypoints" in r.getMessage() for r in caplog.records)


# --- template store conventions ---------------------------------------------

def test_template_loaded_by_bare_name_and_cached(fmatcher, tmp_path):
    _save_png(tmp_path, "banner", _sprite())
    scene = _scene([(30, 40)])

    first = fmatcher.match(scene, "banner")
    second = fmatcher.match(scene, "banner")

    assert first is not None and second is not None
    assert first["name"] == "banner"
    assert len(fmatcher._cache) == 1  # descriptors computed once, reused after
    assert fmatcher.list_templates() == ["banner"]


def test_missing_template_raises(fmatcher):
    with pytest.raises(FileNotFoundError):
        fmatcher.match(_scene([]), "does_not_exist")


def test_unavailable_matcher_is_inert(fmatcher, monkeypatch):
    monkeypatch.setattr(fmatcher, "available", lambda: False)
    assert fmatcher.match(_scene([(10, 10)]), _sprite_image()) is None


# --- RecognizerHub integration ----------------------------------------------

class _StubCapturer:
    def __init__(self, image):
        self._image = image

    def capture_image(self, device_id):
        return self._image


def _hub(image, matcher):
    return RecognizerHub(
        dump_matcher=None, ocr_engine=None, screenshot_capturer=_StubCapturer(image),
        logger=LOGGER, feature_matcher=matcher,
    )


def test_recognizer_feature_hit(fmatcher, tmp_path):
    _save_png(tmp_path, "logo", _sprite())
    scene = _scene([(150, 60)])

    hit = _hub(scene, fmatcher).recognize("dev", {"type": "feature", "template": "logo"})

    assert hit is not None
    assert hit["channel"] == "feature"
    assert hit["text"] == "logo"
    assert hit["matches"] >= 4
    assert isinstance(hit["center"], tuple)
    assert abs(hit["center"][0] - 182) <= 10


def test_recognizer_feature_miss_returns_none(fmatcher, tmp_path):
    _save_png(tmp_path, "logo", _sprite())
    assert _hub(_scene([]), fmatcher).recognize("dev", {"type": "feature", "template": "logo"}) is None


def test_recognizer_feature_without_matcher_is_none():
    hub = RecognizerHub(None, None, _StubCapturer(_scene([])), LOGGER)
    assert hub.recognize("dev", {"type": "feature", "template": "logo"}) is None


def test_recognizer_feature_missing_template_field(fmatcher):
    assert _hub(_scene([(10, 10)]), fmatcher).recognize("dev", {"type": "feature"}) is None


def test_recognizer_feature_missing_file_is_a_miss_not_a_crash(fmatcher):
    # An authoring typo must not blow up a run: logged + treated as a miss.
    assert _hub(_scene([]), fmatcher).recognize("dev", {"type": "feature", "template": "ghost"}) is None


def test_recognizer_feature_uses_passed_frame(fmatcher, tmp_path):
    # image= is supplied (two-shot watchdog pinning); the capturer must not run.
    _save_png(tmp_path, "logo", _sprite())
    scene = _scene([(150, 60)])

    class _Boom:
        def capture_image(self, device_id):
            raise AssertionError("should not capture when image= is given")

    hub = RecognizerHub(None, None, _Boom(), LOGGER, feature_matcher=fmatcher)
    hit = hub.recognize("dev", {"type": "feature", "template": "logo"}, image=scene)

    assert hit is not None and hit["channel"] == "feature"


# --- feature as a watchdog channel -------------------------------------------

def test_feature_works_as_a_watchdog_assertion(fmatcher, tmp_path):
    """WATCHDOG_TYPES is derived from RECOGNITION_TYPES, so `feature` is a legal
    negative assertion: a re-skinned error banner can be forbidden by texture."""
    import copy

    from task.recognizers import WATCHDOG_TYPES
    from task.task_engine import TaskEngine

    assert "feature" in WATCHDOG_TYPES

    _save_png(tmp_path, "error_banner", _sprite())
    scene = _scene([(80, 90)])
    hub = _hub(scene, fmatcher)

    task = {
        "entry": "start",
        "nodes": {
            "start": {"recognition": {"type": "always"}, "action": {"type": "none"},
                      "next": [], "timeout_ms": 0},
        },
        "watchdogs": [{
            "type": "feature", "template": "error_banner", "min_matches": 4,
            "severity": "error", "message": "报错横幅出现", "fail_task": True,
        }],
    }
    validate_task(copy.deepcopy(task))  # the schema accepts it too

    class _Executor:
        def execute(self, device_id, action, tracer=None):
            return {"ok": "True", "stdout": "", "stderr": ""}

    engine = TaskEngine(hub, _Executor(), LOGGER)
    result = engine.run("dev", task)

    assert result["status"] == "failed"
    assert "报错横幅出现" in result["error"]


# --- task_loader validation --------------------------------------------------

def _task_with_recognition(recognition):
    return {
        "entry": "n",
        "nodes": {"n": {"recognition": recognition, "action": {"type": "none"}, "next": []}},
    }


def test_validate_feature_recognition_ok():
    validate_task(_task_with_recognition({
        "type": "feature", "template": "banner", "min_matches": 8, "ratio": 0.7,
        "roi": [0, 0, 100, 100],
    }))


def test_validate_feature_requires_template():
    with pytest.raises(TaskValidationError, match="requires non-empty 'template'"):
        validate_task(_task_with_recognition({"type": "feature"}))


@pytest.mark.parametrize("bad", [0, -1, 2.5, "4", True])
def test_validate_feature_min_matches_must_be_positive_int(bad):
    with pytest.raises(TaskValidationError, match="min_matches"):
        validate_task(_task_with_recognition(
            {"type": "feature", "template": "b", "min_matches": bad}
        ))


@pytest.mark.parametrize("bad", [0, 1.5, -0.2, "0.7"])
def test_validate_feature_ratio_range(bad):
    with pytest.raises(TaskValidationError, match="ratio"):
        validate_task(_task_with_recognition({"type": "feature", "template": "b", "ratio": bad}))


def test_validate_feature_watchdog_requires_template():
    task = _task_with_recognition({"type": "always"})
    task["watchdogs"] = [{"type": "feature"}]
    with pytest.raises(TaskValidationError, match="requires non-empty 'template'"):
        validate_task(task)


def test_validate_feature_popup_entry_ok():
    task = _task_with_recognition({"type": "always"})
    task["popups"] = [{
        "name": "reskinned_agreement",
        "recognition": {"type": "feature", "template": "agree_btn", "min_matches": 6},
        "action": {"type": "click", "target": "recognized"},
    }]
    validate_task(task)
