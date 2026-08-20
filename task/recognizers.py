from __future__ import annotations

import time
from typing import Dict, Optional

from core.logger import log_event
from perception.scene_classifier import SCENE_UNKNOWN
from perception.scene_classifier import DEFAULT_MIN_CONFIDENCE as DEFAULT_SCENE_MIN_CONFIDENCE
from task.replay_cache import center_distance

#: Anchors / matched texts are truncated to this many characters in log lines
#: (a log field must stay one short token; the full text is in the hit dict).
MAX_LOG_TEXT = 40

DEFAULT_MATCH_THRESHOLD = 0.65
DEFAULT_BLANK_STDDEV = 8.0
DEFAULT_TEMPLATE_THRESHOLD = 0.8
DEFAULT_YOLO_CONF = 0.25
DEFAULT_FEATURE_MIN_MATCHES = 4
DEFAULT_FEATURE_RATIO = 0.75
DEFAULT_SCENE_MIN_CONF = DEFAULT_SCENE_MIN_CONFIDENCE

RECOGNITION_TYPES = (
    "always", "ui_text", "ocr", "blank_screen", "template", "feature", "yolo", "scene",
    "and", "or",
)

# Channels allowed in task-level watchdogs ("always" would fire on every
# check, so it is deliberately excluded).
WATCHDOG_TYPES = tuple(t for t in RECOGNITION_TYPES if t != "always")

#: Combined recognitions and the spec key holding their sub-recognitions.
COMBO_LIST_KEY = {"and": "all_of", "or": "any_of"}
COMBO_TYPES = tuple(COMBO_LIST_KEY)

#: Channels a combination may nest. "always" is excluded on purpose: an
#: unconditional hit inside an AND is dead weight and inside an OR makes the
#: whole gate constant-true, i.e. a recognition gate that no longer gates.
COMBO_SUB_TYPES = tuple(t for t in RECOGNITION_TYPES if t != "always")

#: How deep and/or may nest (a combination of combinations is fine; deeper is
#: an unreadable spec that belongs in separate nodes).
MAX_COMBO_DEPTH = 2

#: Channels that read the screen. Only these make a combination worth grabbing
#: a shared frame for (ui_text reads the uiautomator dump instead).
_PIXEL_TYPES = ("ocr", "blank_screen", "template", "feature", "yolo", "scene") + COMBO_TYPES


def _brief(value) -> Optional[str]:
    """One short token for a log field (None stays None and is dropped)."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_LOG_TEXT else text[:MAX_LOG_TEXT] + "..."


class RecognizerHub:
    """Evaluates one recognition spec against the current screen.

    Channels:
      always       - unconditional hit (no coordinates), MaaFW's DirectHit.
      ui_text      - uiautomator dump node matched by text similarity.
      ocr          - local OCR text matched by similarity, optional roi.
      blank_screen - hits when the frame is near-uniform (grayscale stddev
                     below `threshold`, default 8.0); for loading/blank-screen
                     waits and watchdog assertions. Its hit `score` carries the
                     measured stddev, not a similarity.
      template     - OpenCV icon/building match (TM_CCOEFF_NORMED >= threshold,
                     default 0.8); for game-surface graphics the text channels
                     can't read. spec: {template: <name|path>, threshold, roi,
                     scales: [..], grayscale}. Hit center is the matched icon's
                     center; score is the correlation. A PNG alpha channel acts
                     as a match mask, so transparent areas of the template are
                     ignored (knock out the volatile bits — counters, avatars).
      feature      - ORB keypoint match (>= min_matches surviving
                     correspondences, default 4); template matching's
                     deformation-tolerant sibling for TEXTURED anchors that get
                     re-skinned or rescaled between builds. spec: {template:
                     <name|path>, min_matches, ratio, roi}. Center comes from the
                     RANSAC homography (or the matched points' centroid); score
                     is the fraction of the template's keypoints found again,
                     with the real gate being the match count. Flat, low-texture
                     art grows no keypoints — keep using `template` for that.
      yolo         - YOLO object detection (score >= conf, default 0.25); robust
                     to scale/occlusion where template matching fails. spec:
                     {label: <class name>, conf, roi}. Hits the highest-scoring
                     detection of `label` (any class if omitted); center is the
                     detected box center, score the detection confidence. Inert
                     until a model is present (see YoloDetector).
      scene        - whole-frame scene classification (SceneClassifier): hits
                     when the classified scene matches `expected` by dotted
                     PREFIX ("level" accepts "level.battle"/"level.puzzle", "popup"
                     accepts "popup.crash") and its confidence is >= `min_conf`
                     (default 0.5). spec: {expected: <label>, min_conf}. Answers
                     "which screen am I on" rather than "is this anchor here",
                     so the hit carries no coordinates (center None, like
                     blank_screen); score is the classifier's confidence and
                     the hit's "evidence"/"checked" carry why. `unknown` never
                     matches anything — an unrecognized screen is a miss that
                     falls through to the node's on_timeout, not a guess.
      and          - every sub-recognition in `all_of` must hit; `box_index`
                     (default 0) picks whose hit box becomes this node's
                     ("target": "recognized" clicks that one).
      or           - the sub-recognitions in `any_of` are tried in order and the
                     first hit wins, carrying its own box.

    Combinations exist for anchors no single channel pins down safely (an icon
    that also appears elsewhere, a label that only means this screen next to
    that icon). One evaluation of a combination reads ONE frame, shared by every
    sub-recognition, so the children cannot disagree about what was on screen.
    They combine hit/miss verdicts only: a miss is still a miss and nothing here
    weakens the recognition gate.

    recognize() returns None when not found, otherwise a hit dict:
      {"center": (x, y) | None, "text": str, "score": float, "channel": str}
    Combination hits copy the chosen sub-hit (center/bbox/text/score) and add
    "channel" = "and"/"or", "sub_channel", "sub_index" and "sub_hits".
    OCR hits additionally carry "bbox", and — when a replay cache is attached
    and cache_key given — "cache": "hit" (found inside the cached region) or
    "drift" (cached region missed, anchor found elsewhere; "prev_center" holds
    the old position and "drift_px" how far it moved). The cache only narrows
    the OCR search region; a cache miss always falls back to full recognition,
    never to blind coordinates.
    """

    def __init__(self, dump_matcher, ocr_engine, screenshot_capturer, logger,
                 replay_cache=None, template_matcher=None, yolo_detector=None,
                 feature_matcher=None, yolo_registry=None, scene_classifier=None):
        self.matcher = dump_matcher
        self.ocr_engine = ocr_engine
        self.capturer = screenshot_capturer
        self.logger = logger
        self.replay_cache = replay_cache
        self.template_matcher = template_matcher
        # yolo_detector is the default model; yolo_registry (optional) additionally
        # serves the named ones a node asks for via recognition {"model": "<name>"}.
        self.yolo_detector = yolo_detector
        self.yolo_registry = yolo_registry
        self.feature_matcher = feature_matcher
        # scene_classifier backs the `scene` channel above AND is read directly
        # by the MCP classify_scene tool. Rule-based and lazy (it only needs the
        # shared OcrEngine), so an absent one just makes `scene` a permanent
        # miss rather than an error.
        self.scene_classifier = scene_classifier

    def recognize(self, device_id: str, spec: Dict, cache_key: Optional[str] = None,
                  image=None) -> Optional[Dict]:
        """Evaluate one recognition spec against the current screen.

        image: an optional pre-captured PIL frame. When given, the pixel
        channels (ocr / blank_screen) recognize against that exact frame instead
        of grabbing a fresh one — used by the engine's two-shot watchdog so the
        same frame can serve as both the detection and the evidence screenshot.
        ui_text reads the uiautomator dump and ignores it.
        """
        return self._dispatch(device_id, spec, cache_key=cache_key, image=image, depth=0)

    def _dispatch(self, device_id: str, spec: Dict, cache_key: Optional[str],
                  image, depth: int) -> Optional[Dict]:
        """Route one spec to its channel. `depth` counts enclosing combinations."""
        rec_type = spec.get("type", "always")
        if rec_type == "always":
            hit = {"center": None, "text": "", "score": 1.0, "channel": "always"}
            self._log_recognize("always", None, time.perf_counter(), device_id, hit=hit)
            return hit
        if rec_type == "ui_text":
            return self._recognize_ui_text(device_id, spec)
        if rec_type == "ocr":
            return self._recognize_ocr(device_id, spec, cache_key=cache_key, image=image)
        if rec_type == "blank_screen":
            return self._recognize_blank(device_id, spec, image=image)
        if rec_type == "template":
            return self._recognize_template(device_id, spec, image=image)
        if rec_type == "feature":
            return self._recognize_feature(device_id, spec, image=image)
        if rec_type == "yolo":
            return self._recognize_yolo(device_id, spec, image=image)
        if rec_type == "scene":
            return self._recognize_scene(device_id, spec, image=image)
        if rec_type in COMBO_TYPES:
            return self._recognize_combo(device_id, spec, cache_key=cache_key,
                                         image=image, depth=depth)
        raise ValueError(f"Unsupported recognition type: {rec_type}")

    # ---------- instrumentation ----------

    def _log_recognize(self, channel: str, target, started: float,
                       device_id: Optional[str] = None,
                       hit: Optional[Dict] = None, best_score=None,
                       cache: Optional[str] = None) -> None:
        """One DEBUG line per recognition attempt (hit AND miss).

        `EVT recognize channel=ocr hit=0 device=dev1 target=开始 best_score=0.41
        ms=730` — the per-poll trace that makes a threshold tunable after the
        fact (`best_score` is the highest score that stayed below the gate, when
        the channel can produce one cheaply), and `device` keeps two phones
        replaying in parallel apart in one log.

        `cache` reports what the replay-anchor cache did for this attempt
        (`hit` = found inside the cached ROI, `drift` = found elsewhere and
        reported as anchor_drift, `miss` = the cached ROI came up empty and the
        full-screen search had to run). The cache only ever narrows the search,
        so this field explains latency — never the verdict.

        This sits in the polling loop, so it does string work only: no image is
        touched and nothing is re-measured — `started` is a perf_counter stamp
        the caller already had.
        """
        log_event(
            self.logger, "recognize",
            channel=channel,
            hit=1 if hit else 0,
            device=device_id,
            target=_brief(target),
            score=hit.get("score") if hit else None,
            matched=_brief(hit.get("text")) if hit else None,
            best_score=None if hit else best_score,
            cache=cache,
            ms=int((time.perf_counter() - started) * 1000),
        )

    # ---------- combined recognition (and / or) ----------

    def _recognize_combo(self, device_id: str, spec: Dict, cache_key: Optional[str],
                         image, depth: int) -> Optional[Dict]:
        """Evaluate an `and` / `or` combination against ONE frame.

        The shared frame is the point: sub-recognitions that each grabbed their
        own screenshot could vote on different moments of an animating screen
        and "confirm" a state that never existed. When the caller already has a
        frame (the engine's two-shot watchdog) it is reused as-is; otherwise one
        is captured here and handed down. Only pixel channels need it, so a
        combination of ui_text children costs no screenshot at all.
        """
        rec_type = spec.get("type")
        list_key = COMBO_LIST_KEY[rec_type]
        subs = spec.get(list_key)
        if not isinstance(subs, list) or not subs:
            self.logger.warning("'%s' recognition requires a non-empty '%s' list", rec_type, list_key)
            return None
        if depth + 1 > MAX_COMBO_DEPTH:
            raise ValueError(
                f"Combined recognition nested deeper than {MAX_COMBO_DEPTH} levels"
            )

        frame = image
        if frame is None and self._needs_frame(subs):
            try:
                frame = self.capturer.capture_image(device_id)
            except Exception as exc:
                # Leave frame None and let each channel hit its own failure path
                # rather than swallowing the miss here.
                self.logger.warning("'%s' recognition frame capture failed: %s", rec_type, exc)

        hits = []
        for index, sub in enumerate(subs):
            if not isinstance(sub, dict):
                self.logger.warning("'%s' recognition %s[%d] is not an object", rec_type, list_key, index)
                return None
            if sub.get("type", "always") == "always":
                raise ValueError(
                    f"'always' is not allowed inside a combined recognition "
                    f"({list_key}[{index}]): it would stop the combination from gating"
                )
            # Per-child cache keys: the replay cache stores one anchor box per
            # key, and two OCR children sharing a key would overwrite each other.
            sub_key = f"{cache_key}#{index}" if cache_key else None
            hit = self._dispatch(device_id, sub, cache_key=sub_key, image=frame, depth=depth + 1)
            if rec_type == "or":
                if hit is not None:
                    return self._combo_hit(rec_type, hit, index, [hit])
                continue
            if hit is None:
                return None  # AND: one miss is the whole combination's miss
            hits.append(hit)

        if rec_type == "or":
            return None

        box_index = spec.get("box_index", 0)
        if isinstance(box_index, bool) or not isinstance(box_index, int) or not 0 <= box_index < len(hits):
            self.logger.warning(
                "'and' recognition box_index %r out of range (0..%d); using 0",
                box_index, len(hits) - 1,
            )
            box_index = 0
        return self._combo_hit(rec_type, hits[box_index], box_index, hits)

    @staticmethod
    def _needs_frame(subs) -> bool:
        return any(
            isinstance(sub, dict) and sub.get("type", "always") in _PIXEL_TYPES for sub in subs
        )

    @staticmethod
    def _combo_hit(rec_type: str, chosen: Dict, index: int, sub_hits) -> Dict:
        """Wrap the sub-hit that supplies this combination's coordinates.

        The chosen sub-hit is copied verbatim (center / bbox / text / score, and
        any replay-cache drift metadata), so "target": "recognized" and the
        engine's drift reporting keep working unchanged.
        """
        hit = dict(chosen)
        hit["channel"] = rec_type
        hit["sub_channel"] = chosen.get("channel")
        hit["sub_index"] = index
        hit["sub_hits"] = list(sub_hits)
        return hit

    def _recognize_ui_text(self, device_id: str, spec: Dict) -> Optional[Dict]:
        expected = spec["expected"]
        threshold = float(spec.get("threshold", DEFAULT_MATCH_THRESHOLD))
        started = time.perf_counter()
        node, score = self.matcher.match_text(device_id, expected)
        if not node or score < threshold:
            self._log_recognize(
                "ui_text", expected, started, device_id,
                best_score=round(float(score), 3) if score else None,
            )
            return None
        hit = {
            "center": node["center"],
            "text": node["text"] or node["desc"],
            "score": round(score, 3),
            "channel": "ui_text",
        }
        self._log_recognize("ui_text", expected, started, device_id, hit=hit)
        return hit

    def _recognize_blank(self, device_id: str, spec: Dict, image=None) -> Optional[Dict]:
        from utils.helpers import image_grayscale_stddev

        threshold = float(spec.get("threshold", DEFAULT_BLANK_STDDEV))
        started = time.perf_counter()
        try:
            frame = image if image is not None else self.capturer.capture_image(device_id)
            stddev = image_grayscale_stddev(frame)
        except Exception as exc:
            self.logger.warning("blank_screen recognition failed: %s", exc)
            return None
        if stddev >= threshold:
            # "best" here is the measured stddev, i.e. how far the screen was
            # from counting as blank — the number the threshold is tuned against.
            self._log_recognize(
                "blank_screen", f"stddev<{threshold}", started, device_id,
                best_score=round(stddev, 2),
            )
            return None
        hit = {"center": None, "text": "", "score": round(stddev, 2), "channel": "blank_screen"}
        self._log_recognize("blank_screen", f"stddev<{threshold}", started, device_id, hit=hit)
        return hit

    def _recognize_template(self, device_id: str, spec: Dict, image=None) -> Optional[Dict]:
        if not self.template_matcher or not self.template_matcher.available():
            return None
        name = spec.get("template")
        if not name:
            self.logger.warning("template recognition missing 'template' field")
            return None
        threshold = float(spec.get("threshold", DEFAULT_TEMPLATE_THRESHOLD))
        started = time.perf_counter()
        try:
            frame = image if image is not None else self.capturer.capture_image(device_id)
            hit = self.template_matcher.match(
                frame, name, threshold=threshold,
                roi=spec.get("roi"), scales=spec.get("scales"),
                grayscale=bool(spec.get("grayscale", False)),
            )
        except FileNotFoundError as exc:
            # A missing template is an authoring error, not a transient miss.
            self.logger.warning("template recognition: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("template recognition failed: %s", exc)
            return None
        if not hit:
            # match() drops everything below the threshold, so there is no
            # runner-up score to report here.
            self._log_recognize("template", name, started, device_id)
            return None
        result = {
            "center": tuple(hit["center"]),
            "text": hit["name"],
            "score": hit["score"],
            "channel": "template",
            "bbox": hit["bbox"],
        }
        self._log_recognize("template", name, started, device_id, hit=result)
        return result

    def _recognize_feature(self, device_id: str, spec: Dict, image=None) -> Optional[Dict]:
        if not self.feature_matcher or not self.feature_matcher.available():
            return None
        name = spec.get("template")
        if not name:
            self.logger.warning("feature recognition missing 'template' field")
            return None
        min_matches = int(spec.get("min_matches", DEFAULT_FEATURE_MIN_MATCHES))
        ratio = float(spec.get("ratio", DEFAULT_FEATURE_RATIO))
        started = time.perf_counter()
        try:
            frame = image if image is not None else self.capturer.capture_image(device_id)
            hit = self.feature_matcher.match(
                frame, name, min_matches=min_matches, ratio=ratio, roi=spec.get("roi"),
            )
        except FileNotFoundError as exc:
            # A missing template is an authoring error, not a transient miss.
            self.logger.warning("feature recognition: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("feature recognition failed: %s", exc)
            return None
        if not hit:
            self._log_recognize("feature", name, started, device_id)
            return None
        result = {
            "center": tuple(hit["center"]),
            "text": hit["name"],
            "score": hit["score"],
            "channel": "feature",
            "bbox": hit["bbox"],
            "matches": hit["matches"],
        }
        self._log_recognize("feature", name, started, device_id, hit=result)
        return result

    def _resolve_yolo(self, model: Optional[str]):
        """Pick the detector for this spec: named model via the registry, else default."""
        if model:
            return self.yolo_registry.get(model) if self.yolo_registry else None
        return self.yolo_detector

    def _recognize_yolo(self, device_id: str, spec: Dict, image=None) -> Optional[Dict]:
        detector = self._resolve_yolo(spec.get("model"))
        if not detector or not detector.available():
            return None
        label = spec.get("label")
        conf = float(spec.get("conf", DEFAULT_YOLO_CONF))
        started = time.perf_counter()
        try:
            frame = image if image is not None else self.capturer.capture_image(device_id)
            dets = detector.detect(
                frame, conf=conf, classes=[label] if label else None, roi=spec.get("roi"),
            )
        except Exception as exc:
            self.logger.warning("YOLO recognition failed: %s", exc)
            return None
        if not dets:
            # detect() already dropped everything under `conf`, so a miss has no
            # runner-up score either (the detector logs its own box count).
            self._log_recognize("yolo", label or "*", started, device_id)
            return None
        best = dets[0]  # detect() returns best score first
        hit = {
            "center": tuple(best["center"]),
            "text": best["label"],
            "score": best["score"],
            "channel": "yolo",
            "bbox": best["bbox"],
        }
        self._log_recognize("yolo", label or "*", started, device_id, hit=hit)
        return hit

    def _recognize_scene(self, device_id: str, spec: Dict, image=None) -> Optional[Dict]:
        """Gate on *which screen* this is, via the whole-frame SceneClassifier.

        Matching is a dotted PREFIX match, so a node can gate on a family
        ("level") or an exact screen ("level.fps"). `unknown` matches nothing:
        an unrecognized screen must fall through to the node's on_timeout, not
        be talked into the closest label.
        """
        classifier = self.scene_classifier
        if not classifier or not classifier.available():
            return None
        expected = spec.get("expected")
        if not isinstance(expected, str) or not expected:
            self.logger.warning("scene recognition missing 'expected' field")
            return None
        min_conf = float(spec.get("min_conf", DEFAULT_SCENE_MIN_CONF))
        started = time.perf_counter()
        try:
            frame = image if image is not None else self.capturer.capture_image(device_id)
            reading = classifier.classify(frame, device_id=device_id)
        except Exception as exc:
            self.logger.warning("scene recognition failed: %s", exc)
            return None
        # `unknown` can never satisfy a gate — not even `expected: "unknown"`.
        # "I don't recognize this screen" is the absence of evidence, and a
        # recognition gate that opens on absent evidence is not a gate. A task
        # that wants to react to it uses the node's on_timeout branch (with a
        # `finding`), which is where "we ended up somewhere unexpected" belongs.
        unknown = reading.scene == SCENE_UNKNOWN
        if unknown or not reading.matches(expected) or reading.confidence < min_conf:
            # `best_score` doubles as "how sure the classifier was about the
            # scene it *did* see" — with the scene itself in the log line, a
            # miss says whether the gate or the classification was wrong.
            self._log_recognize(
                "scene", f"{expected}<-{reading.scene}", started, device_id,
                best_score=reading.confidence,
            )
            return None
        hit = {
            "center": None,
            "text": reading.scene,
            "score": reading.confidence,
            "channel": "scene",
            "scene": reading.scene,
            "evidence": dict(reading.evidence),
            "checked": list(reading.checked),
        }
        self._log_recognize("scene", expected, started, device_id, hit=hit)
        return hit

    def _recognize_ocr(self, device_id: str, spec: Dict, cache_key: Optional[str] = None,
                       image=None) -> Optional[Dict]:
        if not self.ocr_engine or not self.ocr_engine.available():
            return None
        expected = spec["expected"]
        threshold = float(spec.get("threshold", DEFAULT_MATCH_THRESHOLD))
        roi = spec.get("roi")
        started = time.perf_counter()
        # Filled by _ocr_match with the best sub-threshold similarity, so a miss
        # can be logged with "how close it got".
        stats: Dict = {}

        try:
            image = image if image is not None else self.capturer.capture_image(device_id)
        except Exception as exc:
            self.logger.warning("OCR recognition failed: %s", exc)
            return None

        # Fast path: OCR only the cached anchor region (expanded). An explicit
        # spec roi takes precedence — the task author already narrowed it.
        use_cache = self.replay_cache is not None and cache_key and not roi
        entry = self.replay_cache.get(cache_key) if use_cache else None
        screen = image.size if use_cache else None
        fast_missed = False
        if entry and screen:
            fast_roi = self.replay_cache.roi_from(entry, screen)
            if fast_roi:
                hit = self._ocr_match(image, fast_roi, expected, threshold, stats)
                if hit:
                    hit["cache"] = "hit"
                    self.replay_cache.put(cache_key, hit["bbox"], hit["center"], hit["text"], screen)
                    self._log_recognize("ocr", expected, started, device_id,
                                        hit=hit, cache=hit["cache"])
                    return hit
                fast_missed = True

        hit = self._ocr_match(image, roi, expected, threshold, stats)
        if hit is None:
            # `miss` only when there WAS a cached region to miss — otherwise the
            # field would read as "cache failed" on every uncached node.
            self._log_recognize("ocr", expected, started, device_id,
                                best_score=stats.get("best_score"),
                                cache="miss" if fast_missed else None)
            return None
        if use_cache and screen:
            self.replay_cache.put(cache_key, hit["bbox"], hit["center"], hit["text"], screen)
            if fast_missed:
                # Found, but outside the cached region: the UI moved. Flag it
                # so the engine reports a finding instead of silently healing,
                # and carry how far it moved so the engine can separate a real
                # relocation from a few pixels of layout jitter.
                hit["cache"] = "drift"
                hit["prev_center"] = list(entry["center"])
                hit["drift_px"] = round(center_distance(entry["center"], hit["center"]), 1)
        self._log_recognize("ocr", expected, started, device_id,
                            hit=hit, cache=hit.get("cache"))
        return hit

    def _ocr_match(self, image, roi, expected: str, threshold: float,
                   stats: Optional[Dict] = None) -> Optional[Dict]:
        """Best OCR item matching `expected`, or None below `threshold`.

        `stats`: optional dict the caller passes in to collect `best_score` —
        the highest similarity seen, hit or miss. Instrumentation only; the
        recognition verdict is unaffected.
        """
        try:
            items = self.ocr_engine.recognize(image, roi=roi)
        except Exception as exc:
            self.logger.warning("OCR recognition failed: %s", exc)
            return None

        best = None
        best_score = 0.0
        for item in items:
            score = self.matcher.text_similarity(expected, item["text"])
            if score > best_score:
                best, best_score = item, score
        if stats is not None and best_score:
            stats["best_score"] = round(best_score, 3)

        if not best or best_score < threshold:
            return None
        return {
            "center": best["center"],
            "text": best["text"],
            "score": round(best_score, 3),
            "channel": "ocr",
            "bbox": list(best["bbox"]),
        }
