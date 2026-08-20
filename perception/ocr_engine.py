from __future__ import annotations

import io
import time
from typing import Dict, List, Optional, Sequence

from core.logger import log_event


class OcrEngine:
    """Local OCR channel backed by rapidocr-onnxruntime (no network, no torch).

    The model is lazy-loaded on first recognize() call so startup stays fast and
    the dependency stays optional: available() reports False when the package is
    missing and callers fall back to other recognition channels.
    """

    def __init__(self, logger):
        self.logger = logger
        self._ocr = None
        self._import_failed = False

    def available(self) -> bool:
        if self._ocr is not None:
            return True
        if self._import_failed:
            return False
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            self._import_failed = True
            self.logger.warning("rapidocr-onnxruntime not installed; OCR channel disabled")
            return False
        return True

    def ensure_loaded(self) -> None:
        """Force the one-time rapidocr/onnxruntime init (model load + a tiny
        inference) now instead of on first recognize().

        onnxruntime's process-global first init deadlocks when an av (PyAV)
        decoder thread is already running (observed on Windows, 2026-06-12), so
        the scrcpy capture backend calls this before starting its stream.
        Subsequent session creation and inference coexist with the stream fine.
        """
        if not self.available() or self._ocr is not None:
            return
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()
        self._ocr(np.zeros((32, 32, 3), dtype=np.uint8))
        self.logger.info("OCR engine pre-warmed")

    def recognize(self, image, roi: Optional[Sequence[int]] = None) -> List[Dict]:
        """Run OCR on a screenshot given as PNG bytes or a PIL Image.

        Passing the Image straight from ScreenshotCapturer.capture_image avoids
        a PNG encode/decode round trip on the hot recognition path.
        roi: optional [x1, y1, x2, y2] in absolute pixels; results are reported
        in full-screen coordinates regardless.
        Returns [{text, score, bbox: [x1, y1, x2, y2], center: (x, y)}].
        """
        if not self.available():
            return []
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()

        started = time.perf_counter()
        import numpy as np
        from PIL import Image

        if isinstance(image, (bytes, bytearray)):
            image = Image.open(io.BytesIO(image))
        image = image.convert("RGB")
        offset_x, offset_y = 0, 0
        if roi:
            x1, y1, x2, y2 = (int(v) for v in roi)
            image = image.crop((x1, y1, x2, y2))
            offset_x, offset_y = x1, y1

        # RapidOCR's pipeline is cv2-based and expects BGR channel order.
        bgr = np.array(image)[:, :, ::-1]
        result, _ = self._ocr(bgr)
        if not result:
            self._log_pass(0, roi, started)
            return []

        items: List[Dict] = []
        for box, text, score in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            bx1 = int(min(xs)) + offset_x
            by1 = int(min(ys)) + offset_y
            bx2 = int(max(xs)) + offset_x
            by2 = int(max(ys)) + offset_y
            items.append(
                {
                    "text": str(text).strip(),
                    "score": float(score),
                    "bbox": [bx1, by1, bx2, by2],
                    "center": ((bx1 + bx2) // 2, (by1 + by2) // 2),
                }
            )
        self._log_pass(len(items), roi, started)
        return items

    def _log_pass(self, boxes: int, roi: Optional[Sequence[int]], started: float) -> None:
        """`EVT ocr boxes=12 roi=1 ms=730` — one line per pass, never the texts.

        Full-screen vs. ROI is the single biggest OCR cost factor (0.76s vs.
        ~0.25s measured), so the flag is worth carrying; the recognized strings
        are the caller's business and would flood the log.
        """
        log_event(
            self.logger, "ocr", boxes=boxes, roi=1 if roi else 0,
            ms=int((time.perf_counter() - started) * 1000),
        )
