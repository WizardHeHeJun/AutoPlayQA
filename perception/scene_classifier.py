"""Scene classifier: *which functional screen* is the app showing right now.

The other perception channels answer "is this anchor on screen"; this one
answers the question above them — main menu? inside a level? a modal popup? —
so an agent (or a task node) can branch on where it is instead of probing
anchor after anchor. It is deliberately **rule-based**, not a model.

**The taxonomy is not part of the framework.** Which screens exist, and what
they look like, belongs to the game you are testing — so this module ships the
*mechanism* plus exactly one built-in probe (`blank`, a near-black frame, the
one screen state every Android app shares). Everything else is registered by
the integrating project::

    from perception.scene_classifier import (
        SceneFrame, find_words, mean_score, register_scene_probe, scene_hit,
    )

    NAV_ROI = (0.0, 0.92, 1.0, 1.0)          # relative: 0..1 of width/height
    NAV_WORDS = ("主界面", "背包", "设置")

    def probe_home(frame: SceneFrame):
        matched = find_words(frame.texts(NAV_ROI), NAV_WORDS)
        if len(matched) < 2:                  # two of three: one misread is fine
            return None
        items = [item for _, item in matched]
        return scene_hit("home", mean_score(items),
                         nav_words=[word for word, _ in matched],
                         roi=frame.abs_roi(NAV_ROI))

    register_scene_probe("home", probe_home,
                         description="主界面（底部导航栏）", order=50.0)

The mechanism this module does own, and which a probe set inherits for free:

  * Cost gradient. Probes run cheapest-first (ascending `order`) and
    short-circuit on the first hit: pixel statistics (free) -> narrow-band OCR
    (0.2-1s per band, depending on how much text is in it). Nothing here ever
    OCRs the whole screen, and probes that deliberately share ROI constants get
    the second read free from `SceneFrame`'s memo — sharing bands is how a dozen
    probes end up costing about as much as three.
  * Overlays outrank scenes. A popup covering the main menu is a popup, not a
    main menu, so overlay probes must register with a *lower* `order` than the
    scenes they cover. `order` is the only thing that decides this.
  * `unknown` is a result, not a failure. When no probe fires the reading says
    so and carries `checked` — the probes that ran — as evidence. Guessing the
    most likely scene would be exactly the silent-heal behaviour this project's
    QA discipline forbids.
  * Where order is not enough, probes should share a *predicate* rather than
    rely on being sequenced. `blank` and a splash screen are both near-black
    frames; instead of ordering them, `blank` asks `SceneFrame.looks_like_splash()`
    and stands down. Two probes cannot disagree about one predicate.
  * Thresholds are measured, not intuited. Calibrate a probe against real
    captures (keep them under `tests/fixtures/scenes/`, which is git-ignored)
    and re-measure when you re-tune.

ROIs are **relative** (0..1 fractions of width/height) and converted to pixels
per frame, because the same layout arrives at device-native resolution from a
screencap and downscaled from the scrcpy stream; absolute ROIs would only ever
be right for one of them.

Layering: perception may not import task/, so the registry lives here and the
`scene` recognition channel (task/recognizers.py) merely consumes it.

**Label strings are a contract.** Task JSON (`{"type": "scene", "expected":
"popup"}`) and the `classify_scene` MCP tool are written against them, and
matching is a dotted *prefix* match — `expected: "popup"` accepts
`"popup.error"`, `expected: "menu"` accepts `"menu.settings"`. Add new labels;
do not rename old ones.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.adb_timeout import adb_timeout_s
from core.logger import log_event

# --------------------------------------------------------------------------
# taxonomy — the built-in part of it, which is deliberately tiny
# --------------------------------------------------------------------------

#: Returned when no probe fired. Never a guess.
SCENE_UNKNOWN = "unknown"

#: Not a scene: an extra signal saying the app under test is not in the
#: foreground at all. Only ever produced when the caller passes a device_id AND
#: `scene.game_packages` is configured (see SceneClassifier.__init__).
SIGNAL_OTHER_APP = "other_app"

#: label -> what it means, for the labels the framework itself implements.
#: One entry on purpose: a blank screen is the only "scene" that means the same
#: thing in every game. `taxonomy()` merges this with the registered probes.
BUILTIN_TAXONOMY: Dict[str, str] = {
    "blank": "纯黑 / 息屏 / 无内容帧",
}

#: Backwards-compatible alias. Callers that imported TAXONOMY get the built-in
#: labels; the live, registration-aware view is `SceneClassifier.taxonomy()`.
TAXONOMY: Dict[str, str] = BUILTIN_TAXONOMY

#: Labels with a built-in probe in this version.
IMPLEMENTED_SCENES: Tuple[str, ...] = ("blank",)

#: Declared in the built-in taxonomy with no probe behind them. Empty, and kept
#: as a contract slot: a label declared here answers `unknown` on purpose,
#: because a probe calibrated against zero frames is worse than no probe at all.
PLANNED_SCENES: Tuple[str, ...] = ()

# --------------------------------------------------------------------------
# thresholds owned by the built-in probes
# --------------------------------------------------------------------------

#: Grayscale stddev below which a frame counts as blank. The same 8.0 gate as
#: the `blank_screen` recognizer and the monitor sentinel, deliberately: one
#: number for "the screen has nothing on it" across the project. Sits well clear
#: of both ends — a dead-black frame measures ~0, and real UI measures >=27, so
#: H.264 compression noise from the scrcpy stream cannot cross it.
DEFAULT_BLANK_STDDEV = 8.0

#: Default gate for the `scene` recognition channel and the reported reading.
DEFAULT_MIN_CONFIDENCE = 0.5

#: Splash detection — "near-black frame with a bright centred logo". Generic
#: enough to live in the framework because it is what stops `blank` from
#: reporting a healthy boot as a dead screen; a game's own `loading` probe is
#: expected to pick up whatever `blank` stands down for.
#: Whole-frame luma mean below which the frame counts as "essentially dark".
SPLASH_MAX_MEAN_LUMINANCE = 18.0
#: The centred logo band, and how bright it has to be to count as a logo.
LOGO_BAND_ROI = (0.20, 0.40, 0.80, 0.66)
LOGO_MIN_P99 = 150.0
LOGO_BRIGHT_LEVEL = 60
LOGO_MIN_BRIGHT_FRACTION = 0.03

#: Any word character that is neither a digit nor an underscore — Python's \w is
#: Unicode-aware, so this covers CJK as well as Latin. Used by
#: `looks_like_title` to reject a countdown or a resource amount that OCR read
#: perfectly well but which is not a title.
_WORDLIKE = re.compile(r"[^\W\d_]")

#: Glyphs OCR reports for a panel's close button. Offered as a shared vocabulary
#: for popup probes; note OCR reads these at 0.53-0.65 confidence and sometimes
#: not at all, so treat a close glyph as evidence, never as a gate.
CLOSE_GLYPHS = frozenset({"x", "×", "✕", "✖", "╳"})

#: Foreground-package probe (only with a device_id + configured packages).
_FOREGROUND_PATTERNS = (
    re.compile(r"mResumedActivity[^\n]*?\su0\s+([A-Za-z0-9_.]+)/"),
    re.compile(r"mCurrentFocus[^\n]*?\su0\s+([A-Za-z0-9_.]+)/"),
    re.compile(r"topResumedActivity[^\n]*?\su0\s+([A-Za-z0-9_.]+)/"),
)


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------

#: What a probe returns on a hit: (label, confidence, evidence). Build one with
#: `scene_hit()` rather than by hand — it clamps and rounds the confidence.
SceneHit = Tuple[str, float, Dict[str, Any]]

#: A probe is any callable taking the frame and returning a hit or None.
ProbeFn = Callable[["SceneFrame"], Optional[SceneHit]]


@dataclass
class SceneReading:
    """One classification, with the evidence that produced it.

    A bare label is not actionable when it is wrong; `evidence` (what matched,
    where, how strongly) and `checked` (which probes ran, in order) are what
    make a misclassification diagnosable from a log line or an MCP reply.
    """

    scene: str = SCENE_UNKNOWN
    confidence: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    checked: List[str] = field(default_factory=list)
    elapsed_ms: Dict[str, int] = field(default_factory=dict)

    def matches(self, expected: str) -> bool:
        """Prefix match on the dotted label ("menu" accepts "menu.settings")."""
        if not expected:
            return False
        return self.scene == expected or self.scene.startswith(expected + ".")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene": self.scene,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "checked": list(self.checked),
            "elapsed_ms": dict(self.elapsed_ms),
        }


@dataclass(frozen=True)
class SceneProbe:
    """One registered probe: the label it can produce, and how to look for it.

    `order` is the cost gradient *and* the overlay priority — lower runs first.
    The built-in `blank` probe sits at 0.0 (pure pixel statistics, free), so an
    overlay probe belongs somewhere in 1..99 and the scenes it can cover above
    it. Ties keep registration order.
    """

    label: str
    fn: ProbeFn
    description: str = ""
    order: float = 100.0


# --------------------------------------------------------------------------
# probe registry (the extension point)
# --------------------------------------------------------------------------

#: label -> probe. Insertion-ordered, so ties in `order` resolve to whoever
#: registered first. Populated at import/startup time by the integrating
#: project; not designed for concurrent mutation while classify() is running.
_REGISTRY: Dict[str, SceneProbe] = {}


def register_scene_probe(
    label: str,
    fn: ProbeFn,
    *,
    description: str = "",
    order: float = 100.0,
) -> SceneProbe:
    """Register a probe for `label`; returns the SceneProbe that was stored.

    Registering an existing label replaces it — including the built-in `blank`,
    which is the supported way to swap in a different blank-screen rule.

    `fn` takes the `SceneFrame` and returns `scene_hit(...)` or None. It may
    return a *sub-label* of the one it registered under (a probe registered as
    "popup" is free to answer "popup.error"), which is how one probe covers a
    dotted family.
    """
    if not label:
        raise ValueError("scene probe needs a non-empty label")
    if not callable(fn):
        raise TypeError(f"scene probe '{label}' must be callable")
    probe = SceneProbe(label=label, fn=fn, description=description, order=float(order))
    _REGISTRY[label] = probe
    return probe


def unregister_scene_probe(label: str) -> bool:
    """Drop a registered probe. True when there was one to drop."""
    return _REGISTRY.pop(label, None) is not None


def clear_scene_probes() -> None:
    """Drop every registered probe, leaving only the built-ins.

    Mostly for tests, which must not leak a probe set into the next test.
    """
    _REGISTRY.clear()


def registered_scene_probes() -> Tuple[SceneProbe, ...]:
    """Registered probes in run order (ascending `order`, then registration)."""
    return tuple(sorted(_REGISTRY.values(), key=lambda p: p.order))


# --------------------------------------------------------------------------
# the frame a probe reads
# --------------------------------------------------------------------------

@dataclass
class SceneText:
    """One OCR item in *relative* coordinates."""

    text: str          # normalized: whitespace stripped, lower-cased
    raw: str
    score: float
    bbox: Tuple[float, float, float, float]
    center: Tuple[float, float]


def normalize_text(text: str) -> str:
    """Whitespace-stripped, lower-cased — how probes compare OCR output."""
    return re.sub(r"\s+", "", str(text)).lower()


class SceneFrame:
    """One frame plus a per-classification OCR memo — what probes are handed.

    Probes that read overlapping bands cost nothing extra: `texts()` memoizes by
    ROI, so the second reader of a band pays zero. This is why probe sets should
    share ROI constants rather than each defining their own near-identical crop.
    The memo lives for one classify() call only — it must never outlive the frame.

    `device_id` and `config` are here so a probe gets everything it needs from
    its single argument: `config` is the `scene:` config section, which is where
    a probe's own tuning knobs belong (`frame.config.get("home_nav_min_words", 2)`).
    """

    def __init__(self, image, ocr_engine, logger, *, device_id=None, config=None):
        self.image = image
        self.width, self.height = image.size
        self.device_id: Optional[str] = device_id
        self.config: Dict[str, Any] = config or {}
        self._ocr = ocr_engine
        self._logger = logger
        self._cache: Dict[Tuple[float, float, float, float], List[SceneText]] = {}
        self._gray = None
        self._logo: Optional[Dict[str, float]] = None

    def abs_roi(self, rel: Sequence[float]) -> List[int]:
        """Relative [x1,y1,x2,y2] in 0..1 -> absolute pixels for this frame."""
        x1, y1, x2, y2 = rel
        return [
            max(0, min(self.width, int(round(x1 * self.width)))),
            max(0, min(self.height, int(round(y1 * self.height)))),
            max(0, min(self.width, int(round(x2 * self.width)))),
            max(0, min(self.height, int(round(y2 * self.height)))),
        ]

    def texts(self, rel: Sequence[float]) -> List[SceneText]:
        """OCR one relative band; results are relative too, and memoized."""
        key = tuple(round(float(v), 4) for v in rel)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        items: List[SceneText] = []
        if self._ocr is not None and self._ocr.available():
            x1, y1, x2, y2 = self.abs_roi(rel)
            if x2 > x1 and y2 > y1:
                try:
                    raw_items = self._ocr.recognize(self.image, roi=[x1, y1, x2, y2])
                except Exception as exc:  # noqa: BLE001 - a failed read is a miss, not a crash
                    self._logger.warning("scene classifier OCR failed on roi %s: %s", key, exc)
                    raw_items = []
                for item in raw_items:
                    bx1, by1, bx2, by2 = (float(v) for v in item["bbox"])
                    rb = (bx1 / self.width, by1 / self.height,
                          bx2 / self.width, by2 / self.height)
                    items.append(SceneText(
                        text=normalize_text(item.get("text", "")),
                        raw=str(item.get("text", "")),
                        score=float(item.get("score", 0.0)),
                        bbox=rb,
                        center=((rb[0] + rb[2]) / 2.0, (rb[1] + rb[3]) / 2.0),
                    ))
        self._cache[key] = items
        return items

    def ocr_available(self) -> bool:
        """False when only the pixel helpers below can be used."""
        return bool(self._ocr is not None and self._ocr.available())

    def grayscale_stddev(self) -> float:
        from utils.helpers import image_grayscale_stddev

        return image_grayscale_stddev(self.image)

    def _gray_array(self):
        """Whole-frame luma as a float array, memoized (PIL "L", same as the
        stddev helper, so both talk about the same numbers)."""
        if self._gray is None:
            import numpy as np

            self._gray = np.asarray(self.image.convert("L"), dtype=np.float32)
        return self._gray

    def mean_luminance(self) -> float:
        return float(self._gray_array().mean())

    def center_logo_stats(self) -> Dict[str, float]:
        """Brightness of the centred logo band — how a splash screen differs
        from a dead-black frame. Memoized; only the probes that need it pay."""
        if self._logo is None:
            import numpy as np

            x1, y1, x2, y2 = self.abs_roi(LOGO_BAND_ROI)
            crop = self._gray_array()[y1:y2, x1:x2]
            if crop.size == 0:
                self._logo = {"p99": 0.0, "bright_fraction": 0.0}
            else:
                self._logo = {
                    "p99": float(np.percentile(crop, 99)),
                    "bright_fraction": float((crop > LOGO_BRIGHT_LEVEL).mean()),
                }
        return self._logo

    def looks_like_splash(self) -> bool:
        """Dark frame with a bright centred logo — a splash, not a dead screen.

        The one predicate `blank` shares with whatever `loading`/`splash` probe
        the integrating project registers, so the two can never disagree about a
        frame: whatever this claims, `blank` stands down for. Ordering the probes
        would not have been enough — a splash is *almost* flat, so it reaches the
        blank probe first and passes its gate.
        """
        if self.mean_luminance() > SPLASH_MAX_MEAN_LUMINANCE:
            return False
        stats = self.center_logo_stats()
        return (stats["p99"] >= LOGO_MIN_P99
                and stats["bright_fraction"] >= LOGO_MIN_BRIGHT_FRACTION)

    def bright_channel_row_fraction(
        self,
        band: Sequence[float],
        *,
        channel: int = 1,
        min_level: int = 140,
        margin: int = 15,
    ) -> float:
        """Largest fraction of one row inside `band` that is a saturated colour.

        The shape a HUD progress/health bar makes: a solid, full-width strip in
        one dominant channel, which scenery of the same hue never manages to
        fill. Row-wise (not area-wise) on purpose — such a bar is thin, so an
        area average would drown it in background.

        `channel` is the RGB index that must dominate (0=R, 1=G, 2=B), `margin`
        how far it must lead the other two, `min_level` its absolute floor.
        """
        import numpy as np

        x1, y1, x2, y2 = self.abs_roi(band)
        if x2 - x1 < 4 or y2 - y1 < 2:
            return 0.0
        crop = np.asarray(self.image.convert("RGB").crop((x1, y1, x2, y2)), dtype=np.int16)
        target = crop[:, :, channel]
        others = [crop[:, :, i] for i in range(3) if i != channel]
        mask = (target > min_level)
        for other in others:
            mask &= (target > other + margin)
        if mask.size == 0:
            return 0.0
        return float(mask.mean(axis=1).max())


# --------------------------------------------------------------------------
# helpers probes are expected to build on
# --------------------------------------------------------------------------

def find_word(texts: Sequence[SceneText], word: str) -> Optional[SceneText]:
    """First OCR item containing `word` (substring, normalized both sides)."""
    needle = normalize_text(word)
    if not needle:
        return None
    for item in texts:
        if needle in item.text:
            return item
    return None


def find_words(
    texts: Sequence[SceneText], words: Sequence[str]
) -> List[Tuple[str, SceneText]]:
    """Every distinct vocabulary word present, with the item that carried it.

    The building block for "N of M words" gates, which is how a probe survives
    one misread label without loosening into a single-word guess.
    """
    found: List[Tuple[str, SceneText]] = []
    for word in words:
        item = find_word(texts, word)
        if item is not None:
            found.append((word, item))
    return found


def looks_like_title(
    item: SceneText, *, max_center_x: float = 0.60, max_len: int = 12
) -> bool:
    """Does this OCR item read like a panel/modal title?

    Left of centre, short, and made of *words* — a countdown or a resource
    amount sitting in the same band is not a title, however well OCR read it.
    That last rule is the one that stops every panel with a timer in its header
    from reading as a popup.
    """
    return (
        item.center[0] <= max_center_x
        and 0 < len(item.text) <= max_len
        and _WORDLIKE.search(item.text) is not None
    )


def mean_score(items: Sequence[SceneText]) -> float:
    """Mean OCR confidence of the items that carried a gate — the usual way to
    turn "these words matched" into a probe confidence."""
    scores = [item.score for item in items if item.score]
    return sum(scores) / len(scores) if scores else 0.0


def scene_hit(scene: str, confidence: float, **evidence: Any) -> SceneHit:
    """Build a probe's return value: label, clamped confidence, evidence."""
    return scene, round(max(0.0, min(1.0, float(confidence))), 3), evidence


# --------------------------------------------------------------------------
# classifier
# --------------------------------------------------------------------------

class SceneClassifier:
    """Rule-based whole-frame scene classifier (no model, no network).

    Mirrors the other optional perception components: the OCR engine is
    *injected* (never constructed here — one rapidocr session per process, and
    the scrcpy warm-up ordering lives on that instance), heavy work is lazy, and
    `available()` reports whether the channel can do anything at all.

    classify() returns a SceneReading; `unknown` when nothing matched.

    The probe set comes from the module-level registry (see
    `register_scene_probe`) unless `probes=` is passed, which pins an explicit
    list and ignores the registry — useful for tests and for a process that
    drives two different games.

    scene_config keys read here:
      blank_stddev   — the built-in blank gate (default DEFAULT_BLANK_STDDEV);
      min_confidence — the reported/`scene` channel gate;
      game_packages  — optional allowlist of the app-under-test's Android
                       package names. When it is set AND classify() is given a
                       device_id, a foreground-package check runs first: the
                       cheapest signal there is, and the only one that can tell
                       "the app crashed out to the launcher" from "some screen I
                       don't know". Empty (the default) skips it, costing nothing.
    Any other key is left in `frame.config` for the registered probes to read.
    """

    def __init__(
        self,
        logger,
        ocr_engine=None,
        scene_config: Optional[Dict[str, Any]] = None,
        probes: Optional[Sequence[SceneProbe]] = None,
    ):
        config = dict(scene_config or {})
        self.logger = logger
        self.ocr_engine = ocr_engine
        self.config = config
        self.blank_stddev = float(config.get("blank_stddev", DEFAULT_BLANK_STDDEV))
        self.min_confidence = float(config.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
        packages = config.get("game_packages") or []
        self.game_packages: Tuple[str, ...] = tuple(str(p) for p in packages if p)
        self._pinned_probes: Optional[Tuple[SceneProbe, ...]] = (
            tuple(probes) if probes is not None else None
        )

    # ---------- availability ----------

    def available(self) -> bool:
        """True when at least the pixel probes can run.

        Only `blank` works without OCR; a text-based probe needs the OCR engine.
        Reported as available anyway so callers get a `blank`/`unknown` reading
        instead of nothing — degrading, not disappearing.
        """
        return True

    def ocr_available(self) -> bool:
        return bool(self.ocr_engine is not None and self.ocr_engine.available())

    def probes(self) -> Tuple[SceneProbe, ...]:
        """The image probes for this classifier, in run order.

        Built-ins first, then the registry — except that registering a label a
        built-in already owns *replaces* it rather than adding a second probe
        for the same label. Everything is then sorted by `order`, so a probe may
        also register ahead of `blank` (order < 0) if it really needs to.
        """
        if self._pinned_probes is not None:
            return tuple(sorted(self._pinned_probes, key=lambda p: p.order))
        builtin = SceneProbe(
            label="blank", fn=self._probe_blank,
            description=BUILTIN_TAXONOMY["blank"], order=0.0,
        )
        merged: Dict[str, SceneProbe] = {"blank": builtin}
        merged.update(_REGISTRY)
        return tuple(sorted(merged.values(), key=lambda p: p.order))

    def taxonomy(self) -> Dict[str, Any]:
        """The label contract, for MCP callers and docs.

        Merges the built-in labels with whatever the integrating project has
        registered, so a caller can discover the live label set at runtime
        instead of guessing from a doc.
        """
        probes = self.probes()
        labels = dict(BUILTIN_TAXONOMY)
        for probe in probes:
            if probe.description:
                labels[probe.label] = probe.description
            else:
                labels.setdefault(probe.label, "")
        return {
            "labels": labels,
            "implemented": [probe.label for probe in probes],
            "planned": list(PLANNED_SCENES),
            "unknown": SCENE_UNKNOWN,
            "signals": [SIGNAL_OTHER_APP],
        }

    # ---------- public API ----------

    def classify(self, image, *, device_id: Optional[str] = None) -> SceneReading:
        """Classify one PIL frame. `unknown` when no probe fires.

        The frame is used as given — no PNG round trip — so this stays usable on
        the hot path (`capture_image()` -> classify).
        """
        frame = SceneFrame(
            image, self.ocr_engine, self.logger,
            device_id=device_id, config=self.config,
        )
        reading = SceneReading()
        started_all = time.perf_counter()

        for name, probe_fn in self._ordered_probes(device_id):
            started = time.perf_counter()
            try:
                outcome = probe_fn(frame)
            except Exception as exc:  # noqa: BLE001 - one bad probe must not lose the rest
                self.logger.warning("scene probe '%s' failed: %s", name, exc)
                outcome = None
            reading.checked.append(name)
            reading.elapsed_ms[name] = int((time.perf_counter() - started) * 1000)
            if outcome is not None:
                reading.scene, reading.confidence, reading.evidence = outcome
                reading.evidence["signal"] = name
                break

        reading.elapsed_ms["total"] = int((time.perf_counter() - started_all) * 1000)
        log_event(
            self.logger, "scene_classify",
            scene=reading.scene,
            conf=reading.confidence,
            signal=reading.evidence.get("signal"),
            probes=len(reading.checked),
            device=device_id,
            ms=reading.elapsed_ms["total"],
        )
        return reading

    # ---------- probe sequencing ----------

    def _ordered_probes(
        self, device_id: Optional[str]
    ) -> List[Tuple[str, Callable[[SceneFrame], Optional[SceneHit]]]]:
        """The ordered (name, fn) list for this call.

        The foreground-package check is not an image probe and is not in the
        registry: it is cheaper than any of them and answers a different
        question, so it goes first whenever it is usable at all.
        """
        ordered: List[Tuple[str, Callable[[SceneFrame], Optional[SceneHit]]]] = []
        if device_id and self.game_packages:
            ordered.append((SIGNAL_OTHER_APP, self._probe_other_app))
        ordered.extend((probe.label, probe.fn) for probe in self.probes())
        return ordered

    # ---------- built-in probes ----------

    def _probe_other_app(self, frame: SceneFrame) -> Optional[SceneHit]:
        """Foreground package is not the app under test -> not its scene at all."""
        package = self._foreground_package(frame.device_id)
        if not package or package in self.game_packages:
            return None
        return scene_hit(SIGNAL_OTHER_APP, 1.0, package=package,
                         expected_packages=list(self.game_packages))

    def _probe_blank(self, frame: SceneFrame) -> Optional[SceneHit]:
        """Near-black / screen-off / empty frame — the one universal scene."""
        stddev = frame.grayscale_stddev()
        if stddev >= self.blank_stddev:
            return None
        # A splash is "black screen plus a logo". Claiming it as blank would
        # report a healthy boot as a dead screen, so blank stands down and lets
        # whatever loading/splash probe the project registered claim it (and
        # answers `unknown` honestly if it registered none). Only paid on frames
        # that are already flat, i.e. almost never.
        if frame.looks_like_splash():
            return None
        # Confidence = how far below the gate it landed (a dead-black frame
        # scores ~1.0, one hovering at the threshold ~0.0).
        return scene_hit("blank", 1.0 - stddev / self.blank_stddev,
                         stddev=round(stddev, 3), threshold=self.blank_stddev)

    # ---------- foreground package ----------

    def _foreground_package(self, device_id: Optional[str]) -> Optional[str]:
        """Package name of the resumed activity, or None when it can't be read.

        A failure here degrades to "no opinion" — the image probes still run.
        """
        if not device_id:
            return None
        try:
            proc = subprocess.run(
                ["adb", "-s", device_id, "shell", "dumpsys", "activity", "activities"],
                check=False, capture_output=True, text=True, timeout=adb_timeout_s(),
            )
        except FileNotFoundError:
            self.logger.warning("adb not found; scene foreground-package probe disabled")
            return None
        except subprocess.TimeoutExpired:
            self.logger.warning("dumpsys activity activities timed out on %s", device_id)
            return None
        output = proc.stdout or ""
        for pattern in _FOREGROUND_PATTERNS:
            match = pattern.search(output)
            if match:
                return match.group(1)
        return None
