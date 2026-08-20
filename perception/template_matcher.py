from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from core.logger import log_event

# OpenCV template matching: the deterministic "eye" for graphics the text
# channels can't read. uiautomator sees no nodes on a game surface and OCR only
# reads labels, so a barracks vs. a resource field — pure sprites — are invisible
# to both. matchTemplate locates a known icon image by pixel correlation.
DEFAULT_THRESHOLD = 0.8
# IoU above which two hits are treated as the same object during NMS.
NMS_IOU = 0.3
#: Image extensions accepted as template assets under the template directory.
TEMPLATE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

ImageLike = Union[bytes, bytearray, "object"]  # PIL Image / numpy array / PNG bytes
TemplateLike = Union[str, bytes, bytearray, "object"]


# ---------- template asset conventions (shared with FeatureMatcher) ----------
# Both pixel matchers read the same `task/templates/` store, so name resolution
# and decoding live here once instead of drifting apart in two files.


def list_template_names(template_dir: Union[str, Path]) -> List[str]:
    """Names (stems) of the template images available under template_dir."""
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        return []
    return sorted(p.stem for p in template_dir.iterdir() if p.suffix.lower() in TEMPLATE_EXTS)


def template_label(template: "TemplateLike") -> str:
    """Short name of a template reference for a log field (never its bytes)."""
    if isinstance(template, (str, Path)):
        return Path(template).stem
    return f"<{type(template).__name__}>"


def resolve_template_path(template_dir: Union[str, Path], name: str) -> Path:
    """Map a template reference to a file: explicit path, or bare name + ext sweep."""
    template_dir = Path(template_dir)
    p = Path(name)
    # An explicit path (absolute or with a directory part) is used as given.
    if p.is_absolute() or p.parent != Path("."):
        if p.is_file():
            return p
        raise FileNotFoundError(f"Template not found: {name}")
    # Bare name: look under template_dir, trying common extensions.
    if p.suffix:
        cand = template_dir / name
        if cand.is_file():
            return cand
    else:
        for ext in TEMPLATE_EXTS:
            cand = template_dir / f"{name}{ext}"
            if cand.is_file():
                return cand
    raise FileNotFoundError(
        f"Template '{name}' not found in {template_dir} "
        f"(available: {list_template_names(template_dir)})"
    )


def decode_image_file(path: Union[str, Path]):
    """Decode an image file to a cv2 array, alpha preserved (BGRA when present).

    cv2.imread mangles non-ASCII Windows paths, so the bytes are read by Python
    and handed to imdecode instead.
    """
    import cv2
    import numpy as np

    data = np.fromfile(os.fspath(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Could not decode template image: {path}")
    return img


class TemplateMatcher:
    """Locate icon/building templates on a screenshot via OpenCV matchTemplate.

    cv2 is lazy-imported so the dependency stays optional, mirroring OcrEngine:
    available() reports False when opencv isn't installed and callers fall back
    to the text channels. Templates are PNG/JPG files under template_dir,
    referenced by name; a PNG's alpha channel becomes a match mask so a
    non-rectangular icon doesn't drag its background into the correlation.

    matchTemplate is translation-invariant but not scale/rotation invariant, so
    a template captured at one resolution won't hit at another. Pass `scales`
    (e.g. [0.8, 0.9, 1.0, 1.1, 1.2]) to sweep sizes; the best-scoring scale wins.

    Match results mirror the OCR item shape so downstream code is uniform:
      {name, score, bbox: [x1, y1, x2, y2], center: [x, y], scale}
    score is TM_CCOEFF_NORMED correlation in [-1, 1] (1 = identical).
    """

    def __init__(self, logger, template_dir: Union[str, Path] = "task/templates"):
        self.logger = logger
        self.template_dir = Path(template_dir)
        self._import_failed = False
        # Decoded templates keyed by resolved file path, so the hot loop doesn't
        # re-read/decode the same PNG every frame. Inline (non-file) templates
        # are not cached.
        self._cache: Dict[str, tuple] = {}

    # ---------- availability ----------

    def available(self) -> bool:
        if self._import_failed:
            return False
        try:
            import cv2  # noqa: F401
        except ImportError:
            self._import_failed = True
            self.logger.warning("opencv-python not installed; template matching disabled")
            return False
        return True

    # ---------- public API ----------

    def list_templates(self) -> List[str]:
        """Names (stems) of the template images available under template_dir."""
        return list_template_names(self.template_dir)

    def match(
        self,
        image: ImageLike,
        template: TemplateLike,
        threshold: float = DEFAULT_THRESHOLD,
        scales: Optional[Sequence[float]] = None,
        roi: Optional[Sequence[int]] = None,
        grayscale: bool = False,
    ) -> Optional[Dict]:
        """Best single match of `template` in `image`, or None below threshold."""
        matches = self.match_all(
            image, template, threshold=threshold, max_results=1,
            scales=scales, roi=roi, grayscale=grayscale,
        )
        return matches[0] if matches else None

    def match_all(
        self,
        image: ImageLike,
        template: TemplateLike,
        threshold: float = DEFAULT_THRESHOLD,
        max_results: int = 20,
        scales: Optional[Sequence[float]] = None,
        roi: Optional[Sequence[int]] = None,
        grayscale: bool = False,
    ) -> List[Dict]:
        """All matches at/above threshold, NMS-deduplicated, best score first.

        roi: optional [x1, y1, x2, y2] absolute pixels to search within; results
        are reported in full-screen coordinates regardless. scales defaults to
        [1.0] (single size). Returns at most max_results items.
        """
        if not self.available():
            return []
        import cv2
        import numpy as np

        started = time.perf_counter()
        # Highest correlation seen, threshold or not: on a miss it is the number
        # the threshold should be tuned against.
        best_raw: Optional[float] = None
        haystack = self._to_bgr(image)
        offset_x, offset_y = 0, 0
        if roi:
            x1, y1, x2, y2 = (int(v) for v in roi)
            haystack = haystack[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        if haystack.size == 0:
            return []

        name, tmpl_bgr, mask = self._load_template(template)
        if grayscale:
            haystack = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY)
            tmpl_bgr = cv2.cvtColor(tmpl_bgr, cv2.COLOR_BGR2GRAY)

        raw: List[Dict] = []
        for scale in (scales or (1.0,)):
            tpl, msk = self._scale_template(tmpl_bgr, mask, float(scale))
            th, tw = tpl.shape[:2]
            if th > haystack.shape[0] or tw > haystack.shape[1] or th < 1 or tw < 1:
                continue
            try:
                result = cv2.matchTemplate(haystack, tpl, cv2.TM_CCOEFF_NORMED, mask=msk)
            except cv2.error as exc:  # mask/method combos can throw on odd inputs
                self.logger.warning("matchTemplate failed (scale=%.2f): %s", scale, exc)
                continue
            # A masked correlation can emit nan/inf where the mask is degenerate.
            result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
            if result.size:
                peak = float(result.max())
                best_raw = peak if best_raw is None else max(best_raw, peak)
            ys, xs = np.where(result >= threshold)
            for y, x in zip(ys.tolist(), xs.tolist()):
                raw.append({
                    "score": float(result[y, x]),
                    "x1": x + offset_x,
                    "y1": y + offset_y,
                    "w": tw,
                    "h": th,
                    "scale": round(float(scale), 3),
                })

        kept = self._nms(raw, max_results)
        matches = [
            {
                "name": name,
                "score": round(m["score"], 4),
                "bbox": [m["x1"], m["y1"], m["x1"] + m["w"], m["y1"] + m["h"]],
                "center": [m["x1"] + m["w"] // 2, m["y1"] + m["h"] // 2],
                "scale": m["scale"],
            }
            for m in kept
        ]
        best = matches[0] if matches else None
        log_event(
            self.logger, "template_match",
            template=name, n=len(matches),
            score=best["score"] if best else None,
            center=f"{best['center'][0]},{best['center'][1]}" if best else None,
            best_score=None if best or best_raw is None else round(best_raw, 4),
            ms=int((time.perf_counter() - started) * 1000),
        )
        return matches

    def save_template(
        self, image: ImageLike, name: str, region: Optional[Sequence[int]] = None
    ) -> str:
        """Crop `region` [x1, y1, x2, y2] out of a screenshot and store it as a
        reusable template PNG under template_dir; return the saved path.

        Closes the capture→match loop: grab a screenshot, hand the building's
        bounding box here, then match the saved template on later frames. No
        region keeps the whole frame. Overwrites an existing same-name template.
        """
        from PIL import Image

        if isinstance(image, (bytes, bytearray)):
            pil = Image.open(io.BytesIO(image))
        elif isinstance(image, Image.Image):
            pil = image
        else:  # numpy BGR array
            import numpy as np

            arr = np.asarray(image)
            pil = Image.fromarray(arr[:, :, ::-1] if arr.ndim == 3 else arr)
        pil = pil.convert("RGB")
        if region:
            x1, y1, x2, y2 = (int(v) for v in region)
            pil = pil.crop((x1, y1, x2, y2))

        self.template_dir.mkdir(parents=True, exist_ok=True)
        stem = name[:-4] if name.lower().endswith(".png") else name
        path = self.template_dir / f"{stem}.png"
        pil.save(path, format="PNG")
        self._cache.pop(str(path.resolve()), None)  # invalidate any stale decode
        self.logger.info("Saved template %s (%dx%d)", path, pil.width, pil.height)
        return os.fspath(path)

    # ---------- internals ----------

    def _to_bgr(self, image: ImageLike):
        """Coerce PIL Image / PNG bytes / ndarray to a contiguous BGR uint8 array."""
        import numpy as np
        from PIL import Image

        if isinstance(image, (bytes, bytearray)):
            image = Image.open(io.BytesIO(image))
        if isinstance(image, Image.Image):
            return np.ascontiguousarray(np.array(image.convert("RGB"))[:, :, ::-1])
        arr = np.asarray(image)  # assume already BGR (e.g. straight from cv2)
        return np.ascontiguousarray(arr)

    def _load_template(self, template: TemplateLike):
        """Return (name, bgr_array, mask_or_None) for a name/path/PIL/bytes/array.

        A PNG alpha channel is split off into a single-channel mask; fully/near
        transparent pixels are excluded from the correlation.
        """
        import numpy as np

        if isinstance(template, str):
            path = self._resolve_template_path(template)
            key = str(path.resolve())
            if key in self._cache:
                return self._cache[key]
            bgr, mask = self._split_alpha(decode_image_file(path))
            entry = (path.stem, bgr, mask)
            self._cache[key] = entry
            return entry

        # Inline template (PIL / bytes / ndarray) — not cached.
        from PIL import Image

        if isinstance(template, (bytes, bytearray)):
            template = Image.open(io.BytesIO(template))
        if isinstance(template, Image.Image):
            arr = np.array(template)
            if arr.ndim == 3 and arr.shape[2] == 4:  # RGBA
                bgr = arr[:, :, [2, 1, 0]]
                mask = arr[:, :, 3]
                return ("template", np.ascontiguousarray(bgr), np.ascontiguousarray(mask))
            return ("template", self._to_bgr(template), None)
        return ("template", self._split_alpha(np.asarray(template))[0],
                self._split_alpha(np.asarray(template))[1])

    @staticmethod
    def _split_alpha(img):
        """(bgr, mask) from a possibly-BGRA cv2 array; mask is None without alpha."""
        import numpy as np

        if img.ndim == 3 and img.shape[2] == 4:
            bgr = np.ascontiguousarray(img[:, :, :3])
            mask = np.ascontiguousarray(img[:, :, 3])
            return bgr, mask
        if img.ndim == 2:  # grayscale template
            import cv2

            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), None
        return np.ascontiguousarray(img[:, :, :3]), None

    @staticmethod
    def _scale_template(tmpl, mask, scale: float):
        if scale == 1.0:
            return tmpl, mask
        import cv2

        h, w = tmpl.shape[:2]
        new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        tpl = cv2.resize(tmpl, (new_w, new_h), interpolation=interp)
        msk = cv2.resize(mask, (new_w, new_h), interpolation=interp) if mask is not None else None
        return tpl, msk

    def _resolve_template_path(self, name: str) -> Path:
        return resolve_template_path(self.template_dir, name)

    @staticmethod
    def _nms(boxes: List[Dict], max_results: int) -> List[Dict]:
        """Greedy non-max suppression: keep the highest score, drop overlaps."""
        if not boxes:
            return []
        ordered = sorted(boxes, key=lambda b: b["score"], reverse=True)
        kept: List[Dict] = []
        for box in ordered:
            if all(TemplateMatcher._iou(box, k) <= NMS_IOU for k in kept):
                kept.append(box)
                if len(kept) >= max_results:
                    break
        return kept

    @staticmethod
    def _iou(a: Dict, b: Dict) -> float:
        ax1, ay1, ax2, ay2 = a["x1"], a["y1"], a["x1"] + a["w"], a["y1"] + a["h"]
        bx1, by1, bx2, by2 = b["x1"], b["y1"], b["x1"] + b["w"], b["y1"] + b["h"]
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter == 0:
            return 0.0
        union = a["w"] * a["h"] + b["w"] * b["h"] - inter
        return inter / union if union else 0.0
