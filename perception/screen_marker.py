from __future__ import annotations

import time
from typing import Dict, List, Optional

from utils.image_annotator import draw_set_of_marks
from utils.image_scale import downscale_short_edge


class ScreenMarker:
    """Produce a Set-of-Marks annotated screenshot + an indexed element table.

    Built for the agent-handoff round: instead of the external brain eyeballing
    pixel coordinates off a raw screenshot, it gets a frame with numbered badges
    plus a table mapping each index to its tap point, then taps by index. Both
    free locating channels feed it — uiautomator dump first (real controls with
    bounds + clickable), local OCR as the fallback for game surfaces the dump
    can't see — mirroring UIDetector's two-level approach.

    Indices are reading-order (top-to-bottom, left-to-right) so the agent and a
    human reading the same frame agree on what "#3" means. Coordinates stay in
    absolute device pixels, the same space the ``click`` action uses.

    **Coordinate systems** (one rule, two spaces): recognition and the returned
    element table are *always* native device pixels; only the saved preview
    image may be downscaled, and it is annotated after the resize so the badges
    stay crisp (see ``draw_set_of_marks``'s scale contract). ``click_index``
    therefore resolves against untouched device coordinates regardless of what
    the picture was scaled to.
    """

    def __init__(self, logger, capturer, matcher, ocr_engine=None):
        self.logger = logger
        self.capturer = capturer
        self.matcher = matcher
        self.ocr_engine = ocr_engine

    def mark(
        self,
        device_id: str,
        source: str = "auto",
        min_dump_nodes: int = 3,
        save: bool = True,
        full_resolution: bool = False,
    ) -> Dict:
        """Capture, locate, annotate, and (optionally) persist a marked frame.

        source: "auto" (dump, OCR only when the dump is too sparse), "dump",
        "ocr", or "both" (dump + OCR, de-duplicated). Returns
        {path, width, height, image_width, image_height, scale, source,
        elements}, where each element is
        {index, source, text, desc, center, bounds, clickable}. This runs a
        dump (~4s) and/or OCR (~0.8s) so it's a handoff convenience, not for the
        hot replay loop.

        full_resolution=False (the default) saves the annotated frame downscaled
        to a 720px short edge to keep the agent's image-token bill down;
        width/height stay the device's native size and the element table stays
        in device pixels either way — only the picture shrinks. Detection always
        runs on the native capture, so accuracy is unaffected.
        """
        started = time.perf_counter()
        image = self.capturer.capture_image(device_id)
        # Locate on the native frame: downscaling before OCR/dump would cost
        # recognition accuracy, and the table must stay in device pixels.
        elements, used_source = self._collect(device_id, image, source, min_dump_nodes)

        path: Optional[str] = None
        canvas, scale = (image, 1.0) if full_resolution else downscale_short_edge(image)
        if save:
            # Resize first, annotate second: badges drawn at the canvas's own
            # scale stay crisp, where annotating then resampling would blur the
            # digits. Element coordinates are device pixels, so pass `scale`.
            annotated = draw_set_of_marks(canvas, elements, scale=scale)
            path = self.capturer.save_image(annotated, device_id, "marked")

        width, height = image.size
        canvas_width, canvas_height = canvas.size
        clickable = sum(1 for el in elements if el.get("clickable"))
        # A handoff round costs a dump (~4s) and/or an OCR pass, so the caller
        # gets one INFO line saying what it bought: which channel answered, how
        # many tappable vs. text-only marks, and what it cost.
        self.logger.info(
            "marked screen via %s: %d element(s) (%d clickable, %d text) in %dms",
            used_source, len(elements), clickable, len(elements) - clickable,
            int((time.perf_counter() - started) * 1000),
        )
        return {
            "path": path,
            "width": width,
            "height": height,
            "image_width": canvas_width,
            "image_height": canvas_height,
            "scale": round(scale, 4),
            "source": used_source,
            "elements": elements,
        }

    def _collect(self, device_id, image, source, min_dump_nodes):
        dump_els: List[Dict] = []
        if source in ("auto", "dump", "both"):
            dump_els = self._dump_elements(device_id)

        # auto: only pay for OCR when the dump came back too sparse (game surface).
        want_ocr = source in ("ocr", "both") or (source == "auto" and len(dump_els) < min_dump_nodes)
        ocr_els: List[Dict] = self._ocr_elements(image) if want_ocr else []

        if source == "dump":
            merged, used = dump_els, "dump"
        elif source == "ocr":
            merged, used = ocr_els, "ocr"
        elif source == "both":
            merged, used = dump_els + self._dedup_ocr(ocr_els, dump_els), "both"
        else:  # auto
            if dump_els and len(dump_els) >= min_dump_nodes:
                merged, used = dump_els, "dump"
            elif dump_els or ocr_els:
                merged = dump_els + self._dedup_ocr(ocr_els, dump_els)
                used = "both" if dump_els and ocr_els else ("ocr" if ocr_els else "dump")
            else:
                merged, used = [], "none"

        merged.sort(key=lambda e: (e["center"][1], e["center"][0]))  # reading order
        for i, el in enumerate(merged, start=1):
            el["index"] = i
        return merged, used

    def _dump_elements(self, device_id) -> List[Dict]:
        xml_text = self.matcher.dump_ui_xml(device_id)
        nodes = self.matcher.extract_nodes(xml_text) if xml_text else []
        out = []
        for n in nodes:
            if not (n["text"] or n["desc"] or n["clickable"]):
                continue
            out.append(
                {
                    "source": "dump",
                    "text": n["text"],
                    "desc": n["desc"],
                    "center": list(n["center"]),
                    "bounds": list(n["bounds"]),
                    "clickable": n["clickable"],
                }
            )
        return out

    def _ocr_elements(self, image) -> List[Dict]:
        if not self.ocr_engine or not self.ocr_engine.available():
            return []
        try:
            items = self.ocr_engine.recognize(image)
        except Exception as exc:  # noqa: BLE001 - OCR is best-effort here
            self.logger.warning("ScreenMarker OCR pass failed: %s", exc)
            return []
        return [
            {
                "source": "ocr",
                "text": it["text"],
                "desc": "",
                "center": list(it["center"]),
                "bounds": list(it["bbox"]),
                "clickable": False,  # OCR can't tell a control from a label
            }
            for it in items
            if it["text"]
        ]

    @staticmethod
    def _dedup_ocr(ocr_els: List[Dict], dump_els: List[Dict]) -> List[Dict]:
        """Drop OCR items whose center sits inside a dump element's bounds.

        The dump node is richer (it knows clickability and exact bounds), so when
        both channels see the same on-screen text we keep the dump one.
        """
        kept = []
        for o in ocr_els:
            ox, oy = o["center"]
            inside = any(
                d["bounds"][0] <= ox <= d["bounds"][2] and d["bounds"][1] <= oy <= d["bounds"][3]
                for d in dump_els
            )
            if not inside:
                kept.append(o)
        return kept
