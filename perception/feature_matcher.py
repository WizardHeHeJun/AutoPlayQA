from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

from core.logger import log_event
from perception.template_matcher import (
    decode_image_file,
    list_template_names,
    resolve_template_path,
    template_label,
)

# ORB feature matching: template matching's deformation-tolerant sibling.
#
# matchTemplate correlates raw pixels, so a redesigned button, a re-skinned
# banner or a slightly rescaled icon drops below threshold and the anchor rots.
# ORB instead describes local keypoints (corners/blobs) and matches them, which
# survives translation, moderate scale/rotation, partial occlusion and small art
# changes — as long as the anchor HAS texture. A flat two-color icon yields
# almost no keypoints, so `template` stays the right channel there.
#
# ORB is chosen over SIFT/SURF deliberately: patent-free, in the base opencv
# package (no opencv-contrib), and fast enough for the replay hot loop.
DEFAULT_MIN_MATCHES = 4  # cv2.findHomography's own minimum
DEFAULT_RATIO = 0.75  # Lowe's ratio test on the two nearest neighbours
DEFAULT_N_FEATURES = 1000
# cv2.ORB_create's default edgeThreshold is 31: a 31px border where no keypoint
# is detected. UI templates are small (an icon is often 48-96px), so the default
# leaves almost nothing detectable and every match silently misses. A narrow
# border keeps small templates usable; descriptors near the edge sample the
# padded border, which costs a little precision and buys the whole channel.
DEFAULT_EDGE_THRESHOLD = 8

ImageLike = Union[bytes, bytearray, "object"]
TemplateLike = Union[str, bytes, bytearray, "object"]


class FeatureMatcher:
    """Locate a textured template on a screenshot via ORB keypoint matching.

    Mirrors TemplateMatcher: cv2 is lazy-imported so the dependency stays
    optional (available() reports False without it and the channel goes inert),
    templates are files under the same `task/templates/` store referenced by
    bare name, and a PNG alpha channel masks the template — transparent pixels
    grow no keypoints, so an icon's background never contributes features.

    match() returns None below `min_matches`, otherwise:
      {name, score, matches, keypoints, center: [x, y], bbox: [x1,y1,x2,y2],
       method: "homography" | "centroid"}
    `matches` is the number of surviving (inlier) correspondences — the real
    confidence signal here — and `score` normalizes it by the template's own
    keypoint count, i.e. "what fraction of the template was found again".
    Position comes from the RANSAC homography's projected template centre when
    one can be estimated, else from the centroid of the matched scene points.
    """

    def __init__(self, logger, template_dir: Union[str, Path] = "task/templates",
                 n_features: int = DEFAULT_N_FEATURES,
                 edge_threshold: int = DEFAULT_EDGE_THRESHOLD):
        self.logger = logger
        self.template_dir = Path(template_dir)
        self.n_features = int(n_features)
        self.edge_threshold = int(edge_threshold)
        self._import_failed = False
        self._orb = None
        self._bf = None
        # Descriptors keyed by resolved file path: detecting keypoints on the
        # template every frame would be pure waste in a replay loop. Inline
        # (non-file) templates are not cached.
        self._cache: Dict[str, Tuple] = {}

    # ---------- availability ----------

    def available(self) -> bool:
        if self._import_failed:
            return False
        try:
            import cv2  # noqa: F401
        except ImportError:
            self._import_failed = True
            self.logger.warning("opencv-python not installed; feature matching disabled")
            return False
        return True

    def list_templates(self):
        """Names (stems) of the template images available under template_dir."""
        return list_template_names(self.template_dir)

    # ---------- public API ----------

    def match(
        self,
        image: ImageLike,
        template: TemplateLike,
        min_matches: int = DEFAULT_MIN_MATCHES,
        ratio: float = DEFAULT_RATIO,
        roi: Optional[Sequence[int]] = None,
    ) -> Optional[Dict]:
        """Best location of `template` in `image`, or None below `min_matches`.

        roi: optional [x1, y1, x2, y2] absolute pixels to search within; the
        result is always reported in full-screen coordinates.

        Every attempt (hit or miss) leaves one DEBUG `EVT feature_match` line,
        so a rotting anchor is visible in run.log without re-running the match.
        """
        started = time.perf_counter()
        hit = self._match(image, template, min_matches, ratio, roi)
        log_event(
            self.logger, "feature_match",
            template=template_label(template),
            hit=1 if hit else 0,
            score=hit["score"] if hit else None,
            matches=hit["matches"] if hit else None,
            center=f"{hit['center'][0]},{hit['center'][1]}" if hit else None,
            ms=int((time.perf_counter() - started) * 1000),
        )
        return hit

    def _match(
        self,
        image: ImageLike,
        template: TemplateLike,
        min_matches: int,
        ratio: float,
        roi: Optional[Sequence[int]],
    ) -> Optional[Dict]:
        if not self.available():
            return None
        import cv2
        import numpy as np

        min_matches = max(int(min_matches), 1)
        ratio = float(ratio)

        scene = self._to_gray(image)
        offset_x, offset_y = 0, 0
        if roi:
            x1, y1, x2, y2 = (int(v) for v in roi)
            scene = scene[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        if scene.size == 0:
            return None

        name, tmpl_kp, tmpl_desc, tmpl_shape = self._load_template(template)
        if tmpl_desc is None or len(tmpl_kp) == 0:
            self.logger.warning(
                "feature template '%s' has no ORB keypoints (too flat/small); "
                "use the template channel for low-texture art", name,
            )
            return None

        scene_kp, scene_desc = self._detect(scene, None)
        if scene_desc is None or len(scene_kp) < 2:
            return None

        good = self._ratio_test(tmpl_desc, scene_desc, ratio)
        if len(good) < min_matches:
            return None

        src = np.float32([tmpl_kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([scene_kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        center, bbox, inliers, method = None, None, len(good), "centroid"
        if len(good) >= 4:
            center, bbox, inliers = self._locate_by_homography(src, dst, tmpl_shape)
            if center is not None:
                method = "homography"
        if center is None:
            center, bbox = self._locate_by_centroid(dst)
            inliers = len(good)
        if inliers < min_matches:
            return None

        return {
            "name": name,
            "score": round(min(1.0, inliers / max(len(tmpl_kp), 1)), 4),
            "matches": int(inliers),
            "keypoints": len(tmpl_kp),
            "center": [int(center[0]) + offset_x, int(center[1]) + offset_y],
            "bbox": [
                bbox[0] + offset_x, bbox[1] + offset_y,
                bbox[2] + offset_x, bbox[3] + offset_y,
            ],
            "method": method,
        }

    # ---------- internals ----------

    def _ensure_orb(self):
        """Build the ORB detector / matcher once (lazy, like OcrEngine)."""
        if self._orb is None:
            import cv2

            self._orb = cv2.ORB_create(
                nfeatures=self.n_features, edgeThreshold=self.edge_threshold
            )
            # Hamming distance for ORB's binary descriptors; crossCheck must be
            # off because knnMatch(k=2) powers the ratio test.
            self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        return self._orb

    def _detect(self, gray, mask):
        orb = self._ensure_orb()
        return orb.detectAndCompute(gray, mask)

    def _ratio_test(self, tmpl_desc, scene_desc, ratio: float):
        """Lowe's ratio test: keep a match only if it is clearly better than #2."""
        try:
            pairs = self._bf.knnMatch(tmpl_desc, scene_desc, k=2)
        except Exception as exc:  # degenerate descriptor sets
            self.logger.warning("ORB knnMatch failed: %s", exc)
            return []
        good = []
        for pair in pairs:
            if len(pair) < 2:
                continue
            best, second = pair[0], pair[1]
            if best.distance < ratio * second.distance:
                good.append(best)
        return good

    def _locate_by_homography(self, src, dst, tmpl_shape):
        """RANSAC homography -> projected template centre + quad bounding box."""
        import cv2
        import numpy as np

        try:
            matrix, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        except cv2.error as exc:
            self.logger.warning("findHomography failed: %s", exc)
            return None, None, 0
        if matrix is None:
            return None, None, 0
        inliers = int(mask.sum()) if mask is not None else len(src)

        h, w = tmpl_shape
        corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
        try:
            projected = cv2.perspectiveTransform(corners, matrix).reshape(-1, 2)
        except cv2.error as exc:
            self.logger.warning("perspectiveTransform failed: %s", exc)
            return None, None, inliers
        if not np.all(np.isfinite(projected)):
            return None, None, inliers
        xs, ys = projected[:, 0], projected[:, 1]
        center = (float(xs.mean()), float(ys.mean()))
        bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        return center, bbox, inliers

    @staticmethod
    def _locate_by_centroid(dst):
        """Fallback position: centre of mass of the matched scene keypoints."""
        import numpy as np

        points = dst.reshape(-1, 2)
        center = (float(np.mean(points[:, 0])), float(np.mean(points[:, 1])))
        bbox = [
            int(points[:, 0].min()), int(points[:, 1].min()),
            int(points[:, 0].max()), int(points[:, 1].max()),
        ]
        return center, bbox

    def _load_template(self, template: TemplateLike):
        """(name, keypoints, descriptors, (h, w)) for a name/path/PIL/bytes/array.

        A PNG alpha channel becomes the ORB detection mask, so a non-rectangular
        icon contributes no background keypoints.
        """
        if isinstance(template, str):
            path = resolve_template_path(self.template_dir, template)
            key = str(path.resolve())
            if key in self._cache:
                return self._cache[key]
            gray, mask = self._split_alpha_gray(decode_image_file(path))
            kp, desc = self._detect(gray, mask)
            entry = (path.stem, kp, desc, gray.shape[:2])
            self._cache[key] = entry
            return entry

        gray, mask = self._inline_gray(template)
        kp, desc = self._detect(gray, mask)
        return ("template", kp, desc, gray.shape[:2])

    def _inline_gray(self, template):
        """(gray, mask) for a PIL / PNG-bytes / ndarray template."""
        import numpy as np
        from PIL import Image

        if isinstance(template, (bytes, bytearray)):
            template = Image.open(io.BytesIO(template))
        if isinstance(template, Image.Image):
            arr = np.array(template)
            if arr.ndim == 3 and arr.shape[2] == 4:  # RGBA -> BGRA
                arr = arr[:, :, [2, 1, 0, 3]]
            elif arr.ndim == 3:
                arr = arr[:, :, ::-1]
            return self._split_alpha_gray(np.ascontiguousarray(arr))
        return self._split_alpha_gray(np.asarray(template))  # assume BGR/BGRA

    @staticmethod
    def _split_alpha_gray(img):
        """(gray_uint8, mask_or_None) from a possibly-BGRA / grayscale array."""
        import cv2
        import numpy as np

        if img.ndim == 2:
            return np.ascontiguousarray(img), None
        if img.shape[2] == 4:
            gray = cv2.cvtColor(np.ascontiguousarray(img[:, :, :3]), cv2.COLOR_BGR2GRAY)
            # Near-transparent pixels are excluded from keypoint detection.
            mask = (np.ascontiguousarray(img[:, :, 3]) > 0).astype(np.uint8) * 255
            return gray, mask
        return cv2.cvtColor(np.ascontiguousarray(img[:, :, :3]), cv2.COLOR_BGR2GRAY), None

    @staticmethod
    def _to_gray(image: ImageLike):
        """PIL Image / PNG bytes / ndarray -> contiguous grayscale uint8 array."""
        import cv2
        import numpy as np
        from PIL import Image

        if isinstance(image, (bytes, bytearray)):
            image = Image.open(io.BytesIO(image))
        if isinstance(image, Image.Image):
            return np.ascontiguousarray(np.array(image.convert("L")))
        arr = np.asarray(image)  # assume already BGR (e.g. straight from cv2)
        if arr.ndim == 2:
            return np.ascontiguousarray(arr)
        return cv2.cvtColor(np.ascontiguousarray(arr[:, :, :3]), cv2.COLOR_BGR2GRAY)
