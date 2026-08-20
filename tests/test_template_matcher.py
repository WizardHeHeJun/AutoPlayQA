from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

import mcp_server
from perception.template_matcher import TemplateMatcher
from task.recognizers import RecognizerHub
from task.task_loader import TaskValidationError, validate_task


# --- synthetic scene helpers -------------------------------------------------

def _tile() -> np.ndarray:
    """A distinctive 24x24 four-color block (high variance → well-defined corr)."""
    t = np.zeros((24, 24, 3), np.uint8)
    t[:12, :12] = (255, 0, 0)
    t[:12, 12:] = (0, 255, 0)
    t[12:, :12] = (0, 0, 255)
    t[12:, 12:] = (255, 255, 0)
    return t


def _scene(placements, w=240, h=240) -> Image.Image:
    """Dark deterministic-noise background with `_tile()` copies at (x, y)."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 50, size=(h, w, 3), dtype=np.uint8)
    tile = _tile()
    for x, y in placements:
        arr[y:y + 24, x:x + 24] = tile
    return Image.fromarray(arr, "RGB")


def _tile_image() -> Image.Image:
    return Image.fromarray(_tile(), "RGB")


@pytest.fixture
def matcher(tmp_path):
    return TemplateMatcher(mcp_server._logger, template_dir=tmp_path)


# --- core matching -----------------------------------------------------------

def test_match_locates_inline_template_center(matcher):
    scene = _scene([(50, 60)])
    hit = matcher.match(scene, _tile_image(), threshold=0.8)
    assert hit is not None
    assert hit["center"] == [50 + 12, 60 + 12]
    assert hit["bbox"] == [50, 60, 74, 84]
    assert hit["score"] >= 0.95  # exact crop → near-perfect correlation


def test_match_returns_none_below_threshold(matcher):
    scene = _scene([])  # tile absent; only noise
    assert matcher.match(scene, _tile_image(), threshold=0.8) is None


def test_match_all_finds_every_instance_with_nms(matcher):
    scene = _scene([(40, 40), (150, 160)])
    hits = matcher.match_all(scene, _tile_image(), threshold=0.8, max_results=20)
    # NMS collapses each correlation peak to one box → exactly two objects.
    centers = sorted(h["center"] for h in hits)
    assert centers == [[52, 52], [162, 172]]


def test_roi_limits_search_but_reports_full_coords(matcher):
    scene = _scene([(40, 40), (150, 160)])
    hits = matcher.match_all(scene, _tile_image(), threshold=0.8, roi=[140, 150, 200, 200])
    assert len(hits) == 1
    assert hits[0]["center"] == [162, 172]  # full-screen coordinates, not roi-relative


def test_grayscale_match_still_locates(matcher):
    scene = _scene([(70, 30)])
    hit = matcher.match(scene, _tile_image(), threshold=0.8, grayscale=True)
    assert hit is not None
    assert hit["center"] == [82, 42]


def test_scaled_template_matches_with_scale_sweep(matcher):
    # A noisy (non-self-similar) sprite placed at 1.5x. Native-size search can't
    # correlate with the enlarged art; the scale sweep resizes the template and
    # locks on. Build the enlarged block with the same interpolation the matcher
    # uses for upscaling so the 1.5 match is near-exact.
    import cv2

    rng = np.random.default_rng(3)
    tile = rng.integers(0, 256, size=(24, 24, 3), dtype=np.uint8)
    template = Image.fromarray(tile, "RGB")
    arr = np.array(_scene([]))
    arr[60:96, 60:96] = cv2.resize(tile, (36, 36), interpolation=cv2.INTER_LINEAR)
    scene = Image.fromarray(arr, "RGB")

    assert matcher.match(scene, template, threshold=0.9) is None  # native size misses
    hit = matcher.match(scene, template, threshold=0.9, scales=[1.0, 1.5])
    assert hit is not None
    assert hit["scale"] == 1.5
    assert hit["bbox"] == [60, 60, 96, 96]
    assert hit["center"] == [78, 78]


# --- alpha mask --------------------------------------------------------------

def test_alpha_mask_ignores_transparent_background(matcher):
    # Template: the four-color tile with an opaque core and a transparent border
    # whose RGB is wrong on purpose. Without the mask the border would drag the
    # score down; with it, only the core counts.
    rgba = np.zeros((24, 24, 4), np.uint8)
    rgba[..., :3] = _tile()
    rgba[..., 3] = 0
    rgba[6:18, 6:18, 3] = 255  # only the center is opaque
    rgba[:6, :, :3] = (123, 45, 200)  # garbage in the transparent region
    template = Image.fromarray(rgba, "RGBA")

    scene = _scene([(80, 80)])
    hit = matcher.match(scene, template, threshold=0.9)
    assert hit is not None
    assert hit["center"] == [92, 92]


# --- template store: save / list / resolve -----------------------------------

def test_save_template_crops_and_matches(matcher, tmp_path):
    scene = _scene([(100, 50)])
    path = matcher.save_template(scene, "barracks", region=[100, 50, 124, 74])
    assert path.endswith("barracks.png")
    assert "barracks" in matcher.list_templates()
    # The saved crop should locate itself back in the same scene.
    hit = matcher.match(scene, "barracks", threshold=0.95)
    assert hit["center"] == [112, 62]


def test_save_template_strips_png_suffix(matcher):
    scene = _scene([])
    path = matcher.save_template(scene, "tower.png")
    assert path.endswith("tower.png")
    assert matcher.list_templates() == ["tower"]


def test_missing_template_raises(matcher):
    with pytest.raises(FileNotFoundError):
        matcher.match(_scene([]), "does_not_exist", threshold=0.8)


def test_png_bytes_template(matcher):
    scene = _scene([(30, 30)])
    buf = io.BytesIO()
    _tile_image().save(buf, format="PNG")
    hit = matcher.match(scene, buf.getvalue(), threshold=0.9)
    assert hit["center"] == [42, 42]


# --- RecognizerHub integration ----------------------------------------------

class _StubCapturer:
    def __init__(self, image):
        self._image = image

    def capture_image(self, device_id):
        return self._image


def test_recognizer_template_hit(matcher):
    scene = _scene([(20, 140)])
    matcher.save_template(scene, "icon", region=[20, 140, 44, 164])
    hub = RecognizerHub(
        dump_matcher=None, ocr_engine=None,
        screenshot_capturer=_StubCapturer(scene),
        logger=mcp_server._logger, template_matcher=matcher,
    )
    hit = hub.recognize("dev", {"type": "template", "template": "icon", "threshold": 0.9})
    assert hit is not None
    assert hit["channel"] == "template"
    assert hit["center"] == (32, 152)


def test_recognizer_template_miss_returns_none(matcher):
    matcher.save_template(_scene([(10, 10)]), "icon", region=[10, 10, 34, 34])
    hub = RecognizerHub(
        dump_matcher=None, ocr_engine=None,
        screenshot_capturer=_StubCapturer(_scene([])),  # tile absent now
        logger=mcp_server._logger, template_matcher=matcher,
    )
    assert hub.recognize("dev", {"type": "template", "template": "icon"}) is None


def test_recognizer_template_uses_passed_frame(matcher):
    # image= is supplied; the capturer must not be consulted (two-shot watchdog).
    scene = _scene([(20, 140)])
    matcher.save_template(scene, "icon", region=[20, 140, 44, 164])

    class _Boom:
        def capture_image(self, device_id):
            raise AssertionError("should not capture when image= is given")

    hub = RecognizerHub(
        dump_matcher=None, ocr_engine=None, screenshot_capturer=_Boom(),
        logger=mcp_server._logger, template_matcher=matcher,
    )
    hit = hub.recognize("dev", {"type": "template", "template": "icon"}, image=scene)
    assert hit["center"] == (32, 152)


def test_recognizer_passes_scales_and_alpha_mask_from_task_json(matcher, tmp_path):
    """A node's recognition dict, verbatim from task JSON, must reach the matcher.

    The template is a PNG whose volatile band is transparent (alpha = mask) and
    the on-screen art is drawn 1.5x larger, so the hit only happens if BOTH the
    mask and the `scales` sweep survive the recognizers -> matcher hop.
    """
    import cv2

    rng = np.random.default_rng(4)
    core = rng.integers(0, 256, size=(24, 24, 3), dtype=np.uint8)
    rgba = np.zeros((24, 24, 4), np.uint8)
    rgba[..., :3] = core
    rgba[..., 3] = 255
    rgba[:6, :, :3] = (250, 0, 250)  # volatile band: wrong art on purpose...
    rgba[:6, :, 3] = 0               # ...knocked out by alpha
    Image.fromarray(rgba, "RGBA").save(tmp_path / "panel.png", format="PNG")

    arr = np.array(_scene([]))
    arr[80:116, 40:76] = cv2.resize(core, (36, 36), interpolation=cv2.INTER_LINEAR)
    scene = Image.fromarray(arr, "RGB")

    hub = RecognizerHub(
        dump_matcher=None, ocr_engine=None, screenshot_capturer=_StubCapturer(scene),
        logger=mcp_server._logger, template_matcher=matcher,
    )
    node_recognition = {
        "type": "template", "template": "panel", "threshold": 0.9,
        "scales": [1.0, 1.5], "roi": [0, 0, 240, 240],
    }

    # Without the sweep the enlarged art is unreachable -> proves scales matters.
    assert hub.recognize("dev", dict(node_recognition, scales=None)) is None
    hit = hub.recognize("dev", node_recognition)
    assert hit is not None
    assert hit["channel"] == "template"
    assert hit["center"] == (58, 98)


# --- task_loader validation --------------------------------------------------

def _task_with_recognition(recognition):
    return {
        "entry": "n",
        "nodes": {"n": {"recognition": recognition, "action": {"type": "none"}, "next": []}},
    }


def test_validate_template_recognition_ok():
    validate_task(_task_with_recognition({"type": "template", "template": "barracks"}))


def test_validate_template_recognition_requires_template_field():
    with pytest.raises(TaskValidationError, match="requires non-empty 'template'"):
        validate_task(_task_with_recognition({"type": "template"}))


def test_validate_template_watchdog_requires_template_field():
    task = _task_with_recognition({"type": "always"})
    task["watchdogs"] = [{"type": "template"}]
    with pytest.raises(TaskValidationError, match="requires non-empty 'template'"):
        validate_task(task)


# --- MCP tools ---------------------------------------------------------------

def test_find_template_tool(monkeypatch, matcher, tmp_path):
    scene = _scene([(60, 70)])
    matcher.save_template(scene, "build", region=[60, 70, 84, 94])
    monkeypatch.setattr(mcp_server, "_template_matcher", matcher)
    monkeypatch.setattr(mcp_server._capturer, "capture_image", lambda dev: scene)

    result = mcp_server.find_template("dev", "build", threshold=0.9)
    assert result["found"] is True
    assert result["center"] == [72, 82]
    assert result["count"] == 1


def test_find_template_tool_not_found(monkeypatch, matcher):
    monkeypatch.setattr(mcp_server, "_template_matcher", matcher)
    monkeypatch.setattr(mcp_server._capturer, "capture_image", lambda dev: _scene([]))
    result = mcp_server.find_template("dev", "ghost", threshold=0.9)
    assert result["found"] is False
    assert "not found" in result["error"]


def test_capture_template_tool(monkeypatch, matcher, tmp_path):
    scene = _scene([(100, 100)])
    monkeypatch.setattr(mcp_server, "_template_matcher", matcher)
    monkeypatch.setattr(mcp_server._capturer, "capture_image", lambda dev: scene)
    result = mcp_server.capture_template("dev", "hq", region=[100, 100, 124, 124])
    assert result["ok"] is True
    assert result["name"] == "hq"
    assert "hq" in matcher.list_templates()


def test_list_templates_tool(monkeypatch, matcher):
    matcher.save_template(_scene([]), "a")
    matcher.save_template(_scene([]), "b")
    monkeypatch.setattr(mcp_server, "_template_matcher", matcher)
    assert mcp_server.list_templates() == ["a", "b"]
