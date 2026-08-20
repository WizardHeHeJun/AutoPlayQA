"""Custom action for picking one row out of a list of identical labels.

WHY THIS EXISTS
---------------
`RecognizerHub._ocr_match` returns the single best-scoring OCR item. When a
list repeats the same label on every row — a level list where each unlocked
stage carries its own "前往" button, say — every candidate scores 1.0 and the
winner is simply whichever item the OCR backend emitted first. That order is
detection order, not top-to-bottom, so "tap the first row" cannot be expressed
as a recognition spec at all: it needs the full item list, ranked by geometry.

Positional fallbacks were rejected on purpose. A fixed ROI around row N pins
the task to today's account progress (the topmost playable stage moves down as
stages get cleared), and a hardcoded coordinate breaks the "anchor, never
pixels" rule. This handler keeps the anchor — it just ranks the anchors it
found instead of taking OCR's word for which one is "best".
"""
from __future__ import annotations

from typing import Dict, List

from task.custom_actions import CustomActionContext, register

DEFAULT_THRESHOLD = 0.65


@register("click_topmost_text")
def click_topmost_text(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Click the highest (smallest-y) on-screen occurrence of a text.

    params:
      expected: text to look for (required)
      roi: [x1, y1, x2, y2] search region (optional)
      threshold: similarity gate, same scale as an ocr recognition (default 0.65)
      order: "top" (default) or "bottom" — which end of the column to take
    """
    expected = params.get("expected")
    if not isinstance(expected, str) or not expected:
        raise ValueError("click_topmost_text requires a non-empty 'expected' param")
    roi = params.get("roi")
    threshold = float(params.get("threshold", DEFAULT_THRESHOLD))
    order = params.get("order", "top")
    if order not in ("top", "bottom"):
        raise ValueError("click_topmost_text 'order' must be 'top' or 'bottom'")

    hub = ctx.hub
    if not hub.ocr_engine or not hub.ocr_engine.available():
        raise ValueError("click_topmost_text needs the OCR engine, which is unavailable")

    image = hub.capturer.capture_image(ctx.device_id)
    items = hub.ocr_engine.recognize(image, roi=roi)
    matches = [
        item for item in items
        if hub.matcher.text_similarity(expected, item["text"]) >= threshold
    ]
    if not matches:
        # Not raising: the caller gates this node on the same text, so an empty
        # result here means the screen changed under us mid-step. Fail the node
        # and let its on_timeout / next candidates deal with it.
        return [{"ok": "False", "stdout": "",
                 "stderr": f"click_topmost_text: no '{expected}' on screen"}]

    matches.sort(key=lambda it: (it["center"][1], it["center"][0]),
                 reverse=(order == "bottom"))
    cx, cy = matches[0]["center"]
    ctx.logger.info("click_topmost_text: '%s' x%d, %s pick at (%d, %d)",
                    expected, len(matches), order, cx, cy)
    return [ctx.executor.execute(
        ctx.device_id,
        {"type": "click", "params": {"x": int(cx), "y": int(cy)}},
        ctx.tracer,
    )]
