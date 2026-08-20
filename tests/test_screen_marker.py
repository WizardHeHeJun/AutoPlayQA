from __future__ import annotations

from unittest.mock import patch

from PIL import Image

import mcp_server
from perception.screen_marker import ScreenMarker
from utils.image_annotator import draw_set_of_marks


# --- stubs -------------------------------------------------------------------

class _Capturer:
    def __init__(self, image):
        self.image = image
        self.saved = []

    def capture_image(self, device_id):
        return self.image

    def save_image(self, image, device_id, prefix):
        self.saved.append((prefix, image))
        return f"/out/{prefix}_{device_id}.png"


class _Matcher:
    def __init__(self, nodes, xml="<xml/>"):
        self._nodes = nodes
        self._xml = xml

    def dump_ui_xml(self, device_id):
        return self._xml

    def extract_nodes(self, xml):
        return self._nodes


class _Ocr:
    def __init__(self, items, avail=True):
        self._items = items
        self._avail = avail
        self.called = False

    def available(self):
        return self._avail

    def recognize(self, image):
        self.called = True
        return self._items


def _node(text, center, bounds, clickable=True, desc=""):
    return {
        "text": text, "desc": desc, "resource_id": "", "class_name": "android.widget.Button",
        "clickable": clickable, "focusable": False, "enabled": True,
        "bounds": list(bounds), "center": tuple(center),
    }


def _ocr_item(text, center, bbox):
    return {"text": text, "score": 0.95, "bbox": list(bbox), "center": tuple(center)}


def _marker(nodes, ocr_items=None, ocr_avail=True, image=None):
    image = image or Image.new("RGB", (120, 240), "black")
    return ScreenMarker(
        mcp_server._logger,
        _Capturer(image),
        _Matcher(nodes),
        _Ocr(ocr_items or [], avail=ocr_avail),
    )


# --- ScreenMarker.collect ----------------------------------------------------

def test_dump_elements_indexed_in_reading_order():
    # Provided out of order; expect sort by (y, x) and 1-based indices.
    nodes = [
        _node("B", center=(60, 100), bounds=(50, 80, 70, 120)),
        _node("A", center=(20, 20), bounds=(10, 10, 30, 30)),
        _node("C", center=(10, 100), bounds=(0, 80, 20, 120)),
    ]
    result = _marker(nodes).mark("dev", save=False)
    assert result["source"] == "dump"
    assert [(e["index"], e["text"]) for e in result["elements"]] == [(1, "A"), (2, "C"), (3, "B")]


def test_auto_keeps_dump_when_rich_and_skips_ocr():
    nodes = [_node(f"n{i}", center=(10, 10 * i), bounds=(0, 10 * i, 20, 10 * i + 8)) for i in range(1, 4)]
    m = _marker(nodes, ocr_items=[_ocr_item("ghost", (99, 99), (90, 90, 108, 108))])
    result = m.mark("dev", save=False)
    assert result["source"] == "dump"
    assert len(result["elements"]) == 3
    assert m.ocr_engine.called is False  # rich dump → OCR never paid for


def test_auto_falls_back_to_ocr_when_dump_sparse():
    nodes = [_node("only", center=(10, 10), bounds=(0, 0, 20, 20))]
    ocr = [_ocr_item("登录", (60, 200), (40, 190, 80, 210))]
    result = _marker(nodes, ocr_items=ocr).mark("dev", save=False)
    assert result["source"] == "both"
    texts = {e["text"] for e in result["elements"]}
    assert texts == {"only", "登录"}
    # OCR-sourced element is non-clickable and carries its bbox as bounds.
    ocr_el = next(e for e in result["elements"] if e["source"] == "ocr")
    assert ocr_el["clickable"] is False
    assert ocr_el["bounds"] == [40, 190, 80, 210]


def test_auto_ocr_only_when_dump_empty():
    ocr = [_ocr_item("开始", (60, 200), (40, 190, 80, 210))]
    result = _marker([], ocr_items=ocr).mark("dev", save=False)
    assert result["source"] == "ocr"
    assert result["elements"][0]["text"] == "开始"


def test_both_dedups_ocr_inside_dump_bounds():
    nodes = [_node("设置", center=(25, 25), bounds=(0, 0, 50, 50))]
    ocr = [
        _ocr_item("设置", (25, 25), (5, 5, 45, 45)),    # inside dump bounds → dropped
        _ocr_item("外面", (200, 200), (190, 190, 210, 210)),  # outside → kept
    ]
    result = _marker(nodes, ocr_items=ocr).mark("dev", source="both", save=False)
    texts = sorted(e["text"] for e in result["elements"])
    assert texts == ["外面", "设置"]


def test_mark_saves_annotated_image():
    nodes = [_node("X", center=(20, 20), bounds=(10, 10, 30, 30))]
    m = _marker(nodes)
    result = m.mark("dev", save=True)
    assert result["path"] == "/out/marked_dev.png"
    assert m.capturer.saved[0][0] == "marked"


# --- preview downscaling -----------------------------------------------------

def test_marked_preview_downscales_but_keeps_device_coordinates():
    """Hard invariant: the picture may shrink, the element table may not.

    click_index resolves a badge number against the cached table, so those
    centers/bounds must stay in native device pixels no matter what the saved
    frame was scaled to.
    """
    nodes = [_node("登录", center=(900, 2000), bounds=(800, 1900, 1000, 2100))]
    m = _marker(nodes, image=Image.new("RGB", (1080, 2400), "black"))
    result = m.mark("dev", save=True)

    el = result["elements"][0]
    assert el["center"] == [900, 2000]
    assert el["bounds"] == [800, 1900, 1000, 2100]
    assert (result["width"], result["height"]) == (1080, 2400)  # device space
    assert (result["image_width"], result["image_height"]) == (720, 1600)
    assert result["scale"] == round(720 / 1080, 4)
    assert m.capturer.saved[0][1].size == (720, 1600)  # the annotated PNG shrank


def test_marked_full_resolution_bypasses_downscaling():
    nodes = [_node("登录", center=(900, 2000), bounds=(800, 1900, 1000, 2100))]
    m = _marker(nodes, image=Image.new("RGB", (1080, 2400), "black"))
    result = m.mark("dev", save=True, full_resolution=True)
    assert result["scale"] == 1.0
    assert (result["image_width"], result["image_height"]) == (1080, 2400)
    assert m.capturer.saved[0][1].size == (1080, 2400)


def test_marked_small_screen_is_not_upscaled():
    nodes = [_node("A", center=(60, 120), bounds=(50, 110, 70, 130))]
    m = _marker(nodes, image=Image.new("RGB", (480, 800), "black"))
    result = m.mark("dev", save=True)
    assert result["scale"] == 1.0
    assert m.capturer.saved[0][1].size == (480, 800)


# --- annotator ---------------------------------------------------------------

def test_draw_set_of_marks_scales_device_coordinates_onto_the_canvas():
    """Badges are drawn after the resize, at scale * device coordinates."""
    canvas = Image.new("RGB", (100, 100), "black")
    elements = [{"index": 1, "center": [160, 160], "bounds": [140, 140, 180, 180],
                 "clickable": True}]
    out = draw_set_of_marks(canvas, elements, scale=0.5)
    assert out.getpixel((80, 80)) != (0, 0, 0)   # badge landed at 160*0.5
    assert out.getpixel((2, 2)) == (0, 0, 0)     # and nowhere near the origin


def test_draw_set_of_marks_returns_new_image():
    img = Image.new("RGB", (100, 100), "black")
    elements = [
        {"index": 1, "center": [50, 50], "bounds": [40, 40, 60, 60], "clickable": True},
        {"index": 2, "center": [50, 52], "clickable": False},  # near #1 → nudged, no bounds
    ]
    out = draw_set_of_marks(img, elements)
    assert out.size == img.size
    assert out is not img  # input left untouched
    assert out.getbbox() is not None  # something was drawn over the black frame


# --- MCP tools ---------------------------------------------------------------

def test_screenshot_marked_stores_last_marks():
    elements = [{"index": 1, "source": "dump", "text": "设置", "desc": "",
                 "center": [10, 20], "bounds": [0, 0, 20, 40], "clickable": True}]
    marked = {"path": "/x.png", "width": 100, "height": 200, "source": "dump", "elements": elements}
    with patch.object(mcp_server._marker, "mark", return_value=marked):
        result = mcp_server.screenshot_marked("dev1")
    assert result["source"] == "dump"
    assert mcp_server._last_marks["dev1"] == elements


def test_click_index_taps_stored_center():
    mcp_server._last_marks["dev2"] = [{"index": 1, "text": "登录", "desc": "", "center": [30, 40]}]
    with patch.object(mcp_server._executor, "execute", return_value={"ok": True}) as mock_exec:
        result = mcp_server.click_index("dev2", 1)
    assert result["ok"] is True
    assert result["tapped"] == {"index": 1, "text": "登录", "center": [30, 40]}
    mock_exec.assert_called_once_with("dev2", {"type": "click", "params": {"x": 30, "y": 40}})


def test_click_index_without_marks_errors():
    mcp_server._last_marks.pop("devX", None)
    result = mcp_server.click_index("devX", 1)
    assert result["ok"] is False
    assert "screenshot_marked" in result["error"]


def test_click_index_out_of_range_errors():
    mcp_server._last_marks["dev3"] = [{"index": 1, "text": "a", "desc": "", "center": [1, 2]}]
    result = mcp_server.click_index("dev3", 9)
    assert result["ok"] is False
    assert "not in last marked" in result["error"]
