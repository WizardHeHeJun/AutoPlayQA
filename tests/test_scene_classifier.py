"""Scene classifier — the *mechanism*, not any particular game's taxonomy.

The framework ships one probe (`blank`); everything else is registered by the
integrating project, so these tests pin the machinery that a probe set inherits
and that a game project's own tests would otherwise have to re-discover:

* relative -> pixel ROI conversion (the same layout must classify identically at
  device-native and at downscaled stream resolution);
* run order = ascending `order`, short-circuit on the first hit, which is also
  how an overlay outranks the scene it covers;
* `unknown` is reported with the probes that ran, and never guessed;
* dotted-label prefix matching;
* the `other_app` foreground-package gate;
* registration: register / unregister / clear / replace-a-builtin, and the
  per-frame OCR memo that makes probes sharing a band cost one pass.

Everything is device-free: synthetic PIL frames plus an OCR stub that returns
text placed in *relative* coordinates and filters by the absolute ROI it is
handed — exactly what a real OCR pass on that crop would see. A probe whose
relative ROI is converted wrongly therefore gets the wrong text and fails.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import pytest
from PIL import Image

from perception.scene_classifier import (
    BUILTIN_TAXONOMY,
    DEFAULT_BLANK_STDDEV,
    IMPLEMENTED_SCENES,
    PLANNED_SCENES,
    SCENE_UNKNOWN,
    SIGNAL_OTHER_APP,
    TAXONOMY,
    SceneClassifier,
    SceneFrame,
    SceneProbe,
    SceneReading,
    clear_scene_probes,
    find_word,
    find_words,
    looks_like_title,
    mean_score,
    normalize_text,
    register_scene_probe,
    registered_scene_probes,
    scene_hit,
    unregister_scene_probe,
)

LOGGER = logging.getLogger("test-scene")


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-global; no test may leak a probe into the next."""
    clear_scene_probes()
    yield
    clear_scene_probes()


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

Item = Tuple[str, Tuple[float, float, float, float], float]


class StubOcr:
    """OCR stand-in fed text in RELATIVE coordinates.

    recognize() converts the stored boxes to pixels for the frame it is given
    and returns only those intersecting the requested (absolute) ROI.
    """

    def __init__(self, items: Optional[Sequence[Item]] = None):
        self.items: List[Item] = list(items or [])
        self.calls: List[Optional[Tuple[int, int, int, int]]] = []

    @staticmethod
    def available() -> bool:
        return True

    def recognize(self, image, roi=None) -> List[Dict]:
        width, height = image.size
        rx1, ry1, rx2, ry2 = roi if roi else (0, 0, width, height)
        self.calls.append(tuple(roi) if roi else None)
        out: List[Dict] = []
        for text, box, score in self.items:
            x1, y1 = box[0] * width, box[1] * height
            x2, y2 = box[2] * width, box[3] * height
            if x2 <= rx1 or x1 >= rx2 or y2 <= ry1 or y1 >= ry2:
                continue
            bbox = [int(x1), int(y1), int(x2), int(y2)]
            out.append({
                "text": text, "score": score, "bbox": bbox,
                "center": ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2),
            })
        return out


class UnavailableOcr(StubOcr):
    @staticmethod
    def available() -> bool:
        return False


class ExplodingOcr(StubOcr):
    def recognize(self, image, roi=None):
        raise RuntimeError("ocr session died")


def make_frame(size=(1080, 2448), bar: bool = False, flat: bool = False):
    """A non-blank test frame (horizontal gray gradient).

    Gray on purpose: r == g == b means `bright_channel_row_fraction` cannot fire
    on the background, so `bar` is the only thing that can trip it.
    """
    import numpy as np

    width, height = size
    if flat:
        return Image.fromarray(np.zeros((height, width, 3), dtype=np.uint8))
    ramp = np.linspace(0, 255, width, dtype=np.uint8)
    gray = np.tile(ramp, (height, 1))
    arr = np.dstack([gray, gray, gray])
    if bar:
        y1, y2 = int(0.945 * height), int(0.955 * height)
        arr[y1:y2, int(0.10 * width):int(0.90 * width)] = (120, 220, 150)
    return Image.fromarray(arr)


def make_splash_frame(size=(1080, 2448), logo_value: int = 250):
    """A near-black frame with one bright block where a wordmark would be —
    the frame `blank` must not claim."""
    import numpy as np

    width, height = size
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[int(0.48 * height):int(0.56 * height),
        int(0.25 * width):int(0.75 * width)] = logo_value
    return Image.fromarray(arr)


def make_classifier(items=None, ocr=None, **config) -> Tuple[SceneClassifier, StubOcr]:
    engine = ocr if ocr is not None else StubOcr(items)
    return SceneClassifier(LOGGER, ocr_engine=engine, scene_config=config), engine


# --- neutral example layouts, in relative coordinates ---------------------

NAV_ROI = (0.0, 0.92, 1.0, 1.0)
NAV_WORDS = ("主界面", "背包", "设置")
HOME_NAV: List[Item] = [
    ("主界面", (0.075, 0.973, 0.150, 0.991), 1.0),
    ("背包", (0.463, 0.977, 0.535, 0.992), 1.0),
    ("设置", (0.850, 0.973, 0.926, 0.992), 1.0),
]

HEADER_ROI = (0.02, 0.24, 0.98, 0.46)
BUTTON_ROI = (0.02, 0.53, 0.98, 0.70)
POPUP_CONFIRM: List[Item] = [
    ("提示", (0.150, 0.335, 0.401, 0.363), 1.0),
    ("X", (0.830, 0.338, 0.882, 0.361), 0.55),
    ("确认", (0.469, 0.588, 0.578, 0.615), 0.99),
]

TOP_LEFT_ROI = (0.0, 0.0, 0.38, 0.06)


def probe_home(frame: SceneFrame):
    """Two of three nav words: one misread label must not lose the screen."""
    matched = find_words(frame.texts(NAV_ROI), NAV_WORDS)
    if len(matched) < int(frame.config.get("home_nav_min_words", 2)):
        return None
    items = [item for _, item in matched]
    return scene_hit("home", mean_score(items),
                     nav_words=[word for word, _ in matched],
                     roi=frame.abs_roi(NAV_ROI))


def probe_popup(frame: SceneFrame):
    """A dismiss/confirm button low on the panel, confirmed by a short title."""
    button = find_word(frame.texts(BUTTON_ROI), "确认")
    if button is None:
        return None
    title = next((t for t in frame.texts(HEADER_ROI) if looks_like_title(t)), None)
    if title is None:
        return None
    return scene_hit("popup", mean_score([title, button]),
                     title=title.raw, button=button.raw)


def register_example_probes() -> None:
    """The overlay registers *ahead* of the scene it can cover."""
    register_scene_probe("popup", probe_popup, description="模态弹窗覆盖层", order=10.0)
    register_scene_probe("home", probe_home, description="主界面（底部导航栏）", order=50.0)


# --------------------------------------------------------------------------
# taxonomy contract
# --------------------------------------------------------------------------

def test_the_framework_ships_only_the_blank_label():
    """The taxonomy belongs to the game project; the framework keeps one label.

    `blank` is the only screen state that means the same thing in every app,
    which is exactly why it is the one probe worth shipping.
    """
    assert set(BUILTIN_TAXONOMY) == {"blank"}
    assert IMPLEMENTED_SCENES == ("blank",)
    assert PLANNED_SCENES == ()
    assert TAXONOMY is BUILTIN_TAXONOMY  # kept as an import-compatible alias


def test_taxonomy_merges_registered_probes_with_the_builtin():
    register_example_probes()
    classifier, _ = make_classifier()
    taxonomy = classifier.taxonomy()

    assert taxonomy["labels"]["blank"] == BUILTIN_TAXONOMY["blank"]
    assert taxonomy["labels"]["home"] == "主界面（底部导航栏）"
    assert taxonomy["labels"]["popup"] == "模态弹窗覆盖层"
    # implemented is in run order, so it doubles as the documented probe order
    assert taxonomy["implemented"] == ["blank", "popup", "home"]
    assert taxonomy["unknown"] == SCENE_UNKNOWN
    assert taxonomy["signals"] == [SIGNAL_OTHER_APP]


def test_reading_matches_is_a_dotted_prefix_match():
    reading = SceneReading(scene="menu.settings", confidence=0.9)

    assert reading.matches("menu.settings")
    assert reading.matches("menu")          # the family
    assert not reading.matches("menu.home")
    assert not reading.matches("men")       # prefix on the *label*, not the string
    assert not reading.matches("")

    # `unknown` must never satisfy an expectation — that is the whole point of
    # not guessing: a scene node that cannot classify falls through to on_timeout.
    assert not SceneReading().matches("home")


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def test_registered_probes_run_in_ascending_order():
    register_scene_probe("late", lambda frame: None, order=90.0)
    register_scene_probe("early", lambda frame: None, order=5.0)

    assert [p.label for p in registered_scene_probes()] == ["early", "late"]


def test_ties_in_order_keep_registration_order():
    register_scene_probe("first", lambda frame: None, order=50.0)
    register_scene_probe("second", lambda frame: None, order=50.0)

    assert [p.label for p in registered_scene_probes()] == ["first", "second"]


def test_unregister_and_clear():
    register_example_probes()
    assert unregister_scene_probe("home") is True
    assert unregister_scene_probe("home") is False
    assert [p.label for p in registered_scene_probes()] == ["popup"]

    clear_scene_probes()
    assert registered_scene_probes() == ()


def test_registering_a_builtin_label_replaces_it_rather_than_duplicating():
    """The supported way to swap in a different blank-screen rule."""
    register_scene_probe(
        "blank", lambda frame: scene_hit("blank", 0.42, custom=True), order=0.0
    )
    classifier, _ = make_classifier()

    assert [p.label for p in classifier.probes()] == ["blank"]
    reading = classifier.classify(make_frame())   # a *non*-blank frame
    assert reading.scene == "blank"               # the replacement's opinion wins
    assert reading.evidence["custom"] is True


def test_a_probe_may_register_ahead_of_blank():
    register_scene_probe(
        "screen_off", lambda frame: scene_hit("screen_off", 1.0), order=-1.0
    )
    classifier, _ = make_classifier()

    assert [p.label for p in classifier.probes()] == ["screen_off", "blank"]
    assert classifier.classify(make_frame(flat=True)).scene == "screen_off"


def test_pinned_probes_ignore_the_global_registry():
    """Two games in one process: pass the probe set explicitly."""
    register_example_probes()
    pinned = SceneProbe(label="only", fn=lambda frame: None, order=1.0)
    classifier = SceneClassifier(LOGGER, ocr_engine=StubOcr(), probes=[pinned])

    assert [p.label for p in classifier.probes()] == ["only"]
    # ...including the built-in: an explicit list is exactly that.
    assert classifier.classify(make_frame(flat=True)).scene == SCENE_UNKNOWN


def test_registration_rejects_a_blank_label_or_a_non_callable():
    with pytest.raises(ValueError):
        register_scene_probe("", lambda frame: None)
    with pytest.raises(TypeError):
        register_scene_probe("bad", "not-callable")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# probe sequencing
# --------------------------------------------------------------------------

def test_blank_frame_short_circuits_before_any_ocr():
    """The cost gradient's whole point: a dead screen never pays for OCR."""
    register_example_probes()
    classifier, ocr = make_classifier(HOME_NAV)

    reading = classifier.classify(make_frame(flat=True))

    assert reading.scene == "blank"
    assert reading.confidence == pytest.approx(1.0, abs=0.01)
    assert reading.checked == ["blank"]
    assert ocr.calls == []


def test_probes_run_cheapest_first_and_stop_at_the_first_hit():
    order: List[str] = []

    def record(label, hit):
        def probe(frame):
            order.append(label)
            return scene_hit(label, 1.0) if hit else None
        return probe

    register_scene_probe("c", record("c", False), order=30.0)
    register_scene_probe("a", record("a", False), order=10.0)
    register_scene_probe("b", record("b", True), order=20.0)
    register_scene_probe("d", record("d", True), order=40.0)
    classifier, _ = make_classifier()

    reading = classifier.classify(make_frame())

    assert order == ["a", "b"]                       # "c"/"d" never ran
    assert reading.scene == "b"
    assert reading.checked == ["blank", "a", "b"]    # blank ran and missed


def test_an_overlay_outranks_the_scene_it_covers():
    """A popup drawn over the home screen is a popup, and `order` is what says so."""
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV + POPUP_CONFIRM)

    reading = classifier.classify(make_frame())

    assert reading.scene == "popup"
    assert reading.evidence["title"] == "提示"
    assert reading.evidence["signal"] == "popup"
    assert "home" not in reading.checked   # short-circuited before it


def test_the_covered_scene_still_classifies_on_its_own():
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV)

    reading = classifier.classify(make_frame())

    assert reading.scene == "home"
    assert reading.evidence["nav_words"] == list(NAV_WORDS)


def test_a_probe_may_answer_with_a_sub_label_of_the_one_it_registered():
    """One probe covers a dotted family — the label it returns is what counts."""
    register_scene_probe(
        "popup",
        lambda frame: scene_hit("popup.error", 0.95, keyword="uncaughterror"),
        order=10.0,
    )
    classifier, _ = make_classifier()

    reading = classifier.classify(make_frame())

    assert reading.scene == "popup.error"
    assert reading.matches("popup")          # still answers to the family
    assert reading.evidence["signal"] == "popup"   # ...credited to its probe


def test_unknown_is_reported_with_the_probes_that_ran():
    """No probe fired, so the reading says so and carries its evidence trail."""
    register_example_probes()
    classifier, _ = make_classifier([("完全无关的文字", (0.4, 0.4, 0.6, 0.42), 0.9)])

    reading = classifier.classify(make_frame())

    assert reading.scene == SCENE_UNKNOWN
    assert reading.confidence == 0.0
    assert reading.checked == ["blank", "popup", "home"]
    assert "total" in reading.elapsed_ms
    assert reading.to_dict()["scene"] == SCENE_UNKNOWN


def test_one_broken_probe_does_not_lose_the_rest():
    def explode(frame):
        raise RuntimeError("probe bug")

    register_scene_probe("broken", explode, order=10.0)
    register_scene_probe("home", probe_home, order=50.0)
    classifier, _ = make_classifier(HOME_NAV)

    reading = classifier.classify(make_frame())

    assert reading.scene == "home"
    assert "broken" in reading.checked   # it ran, it failed, it was recorded


# --------------------------------------------------------------------------
# relative ROIs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size", [(1080, 2448), (1080, 2400), (720, 1632)])
def test_the_same_layout_classifies_the_same_at_every_resolution(size):
    """Native screencap vs downscaled scrcpy frame must not disagree."""
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV)

    reading = classifier.classify(make_frame(size=size))

    assert reading.scene == "home"


def test_relative_rois_are_converted_against_the_actual_frame_size():
    frame = SceneFrame(make_frame(size=(720, 1632)), StubOcr(), LOGGER)

    assert frame.abs_roi((0.0, 0.92, 1.0, 1.0)) == [0, 1501, 720, 1632]
    assert frame.abs_roi((0.5, 0.5, 0.5, 0.5)) == [360, 816, 360, 816]
    # out-of-range fractions clamp instead of producing a negative crop
    assert frame.abs_roi((-1.0, -1.0, 2.0, 2.0)) == [0, 0, 720, 1632]


def test_probes_sharing_a_band_only_pay_for_one_ocr_pass():
    """The memo is why sharing ROI constants is the documented convention."""
    def probe_a(frame):
        frame.texts(NAV_ROI)
        return None

    def probe_b(frame):
        frame.texts(NAV_ROI)          # same band...
        frame.texts(TOP_LEFT_ROI)     # ...plus one of its own
        return None

    register_scene_probe("a", probe_a, order=10.0)
    register_scene_probe("b", probe_b, order=20.0)
    classifier, ocr = make_classifier(HOME_NAV)

    classifier.classify(make_frame())

    assert len(ocr.calls) == 2                  # not 3
    assert len(set(ocr.calls)) == 2


def test_the_memo_does_not_outlive_the_frame():
    classifier, ocr = make_classifier(HOME_NAV)
    register_scene_probe("a", lambda frame: frame.texts(NAV_ROI) and None, order=10.0)

    classifier.classify(make_frame())
    classifier.classify(make_frame())

    assert len(ocr.calls) == 2   # one per classify(), not one in total


# --------------------------------------------------------------------------
# degradation
# --------------------------------------------------------------------------

def test_without_ocr_only_the_pixel_probes_work():
    """Degrading, not disappearing: still a blank/unknown answer, never nothing."""
    register_example_probes()
    classifier, _ = make_classifier(ocr=UnavailableOcr(HOME_NAV))

    assert classifier.available() is True
    assert classifier.ocr_available() is False
    assert classifier.classify(make_frame(flat=True)).scene == "blank"
    assert classifier.classify(make_frame()).scene == SCENE_UNKNOWN


def test_a_failing_ocr_pass_is_a_miss_not_a_crash():
    register_example_probes()
    classifier, _ = make_classifier(ocr=ExplodingOcr())

    reading = classifier.classify(make_frame())

    assert reading.scene == SCENE_UNKNOWN
    assert reading.checked == ["blank", "popup", "home"]


def test_a_classifier_with_no_ocr_engine_at_all_still_answers():
    classifier = SceneClassifier(LOGGER)

    assert classifier.ocr_available() is False
    assert classifier.classify(make_frame(flat=True)).scene == "blank"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_config_overrides_the_builtin_thresholds():
    default, _ = make_classifier()
    assert default.blank_stddev == DEFAULT_BLANK_STDDEV

    tuned, _ = make_classifier(blank_stddev=1000.0, min_confidence=0.9)
    assert tuned.blank_stddev == 1000.0
    assert tuned.min_confidence == 0.9
    # the gate is what decides: a textured frame the default rejects (its stddev
    # is far above 8.0) reads as blank once the gate is raised past it
    assert default.classify(make_frame()).scene == SCENE_UNKNOWN
    assert tuned.classify(make_frame()).scene == "blank"


def test_unknown_config_keys_reach_the_probes():
    """Where a registered probe's own tuning knobs live — no framework change
    needed to add one."""
    register_scene_probe("home", probe_home, order=50.0)
    classifier, _ = make_classifier(HOME_NAV[:1], home_nav_min_words=1)

    assert classifier.classify(make_frame()).scene == "home"

    strict, _ = make_classifier(HOME_NAV[:1], home_nav_min_words=2)
    register_scene_probe("home", probe_home, order=50.0)
    assert strict.classify(make_frame()).scene == SCENE_UNKNOWN


# --------------------------------------------------------------------------
# the blank / splash predicate
# --------------------------------------------------------------------------

def test_blank_stands_down_for_a_splash_frame():
    """One predicate, two callers: `blank` refuses any frame carrying a logo, so
    a healthy boot is never reported as a dead screen. With no loading probe
    registered the honest answer is `unknown`, not a guess."""
    classifier, _ = make_classifier()

    reading = classifier.classify(make_splash_frame())

    assert reading.scene == SCENE_UNKNOWN
    assert reading.checked == ["blank"]


def test_the_project_probe_picks_up_what_blank_stood_down_for():
    register_scene_probe(
        "loading",
        lambda frame: scene_hit("loading", 0.9, form="splash")
        if frame.looks_like_splash() else None,
        order=20.0,
    )
    classifier, _ = make_classifier()

    assert classifier.classify(make_splash_frame()).scene == "loading"
    # ...and it does not steal the genuinely dead frame
    assert classifier.classify(make_frame(flat=True)).scene == "blank"


def test_a_logo_less_dark_frame_is_still_blank():
    classifier, _ = make_classifier()

    assert classifier.classify(make_splash_frame(logo_value=40)).scene == "blank"


def test_a_bright_screen_is_never_read_as_a_splash():
    frame = SceneFrame(make_frame(), StubOcr(), LOGGER)

    assert frame.looks_like_splash() is False


# --------------------------------------------------------------------------
# pixel helpers
# --------------------------------------------------------------------------

def test_bright_channel_row_fraction_needs_a_full_solid_row():
    band = (0.18, 0.88, 0.82, 1.0)

    with_bar = SceneFrame(make_frame(bar=True), StubOcr(), LOGGER)
    without = SceneFrame(make_frame(), StubOcr(), LOGGER)

    # a solid strip fills a whole row of the band; a gray gradient never can,
    # because r == g == b leaves no channel leading by the margin
    assert with_bar.bright_channel_row_fraction(band) == pytest.approx(1.0)
    assert without.bright_channel_row_fraction(band) == 0.0


def test_bright_channel_row_fraction_respects_the_requested_channel():
    band = (0.18, 0.88, 0.82, 1.0)
    frame = SceneFrame(make_frame(bar=True), StubOcr(), LOGGER)

    assert frame.bright_channel_row_fraction(band, channel=1) == pytest.approx(1.0)
    assert frame.bright_channel_row_fraction(band, channel=0) == 0.0  # the bar is green


def test_a_degenerate_band_is_zero_not_an_error():
    frame = SceneFrame(make_frame(), StubOcr(), LOGGER)

    assert frame.bright_channel_row_fraction((0.5, 0.5, 0.5, 0.5)) == 0.0


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def test_normalize_strips_whitespace_and_case():
    assert normalize_text("  Con firm \n") == "confirm"


def test_find_word_is_a_normalized_substring_match():
    classifier, _ = make_classifier(HOME_NAV)
    frame = SceneFrame(make_frame(), StubOcr(HOME_NAV), LOGGER)
    texts = frame.texts(NAV_ROI)

    assert find_word(texts, "设置") is not None
    assert find_word(texts, "不存在") is None
    assert find_word(texts, "") is None
    assert [w for w, _ in find_words(texts, NAV_WORDS)] == list(NAV_WORDS)
    assert find_words(texts, ("背包", "不存在")) != []
    assert len(find_words(texts, ("背包", "不存在"))) == 1


def test_looks_like_title_rejects_a_countdown_in_the_header_band():
    """A digits-only token reads perfectly but is not a title — the rule that
    stops every panel with a refresh timer in its header from reading as a popup."""
    frame = SceneFrame(
        make_frame(),
        StubOcr([
            ("提示", (0.10, 0.30, 0.25, 0.33), 1.0),
            ("18:06:12", (0.10, 0.36, 0.30, 0.39), 0.99),
            ("这是一条很长的正文不是标题内容", (0.10, 0.40, 0.90, 0.43), 0.99),
        ]),
        LOGGER,
    )
    title, countdown, body = frame.texts(HEADER_ROI)

    assert looks_like_title(title) is True
    assert looks_like_title(countdown) is False   # no word characters
    assert looks_like_title(body) is False        # too long to be a title


def test_mean_score_ignores_zero_scores_and_survives_an_empty_list():
    frame = SceneFrame(
        make_frame(),
        StubOcr([("a", (0.1, 0.30, 0.2, 0.33), 1.0), ("b", (0.3, 0.30, 0.4, 0.33), 0.0)]),
        LOGGER,
    )
    items = frame.texts(HEADER_ROI)

    assert mean_score(items) == pytest.approx(1.0)
    assert mean_score([]) == 0.0


def test_scene_hit_clamps_and_rounds_the_confidence():
    assert scene_hit("x", 1.9)[1] == 1.0
    assert scene_hit("x", -3.0)[1] == 0.0
    assert scene_hit("x", 0.123456)[1] == 0.123
    assert scene_hit("x", 0.5, word="确认")[2] == {"word": "确认"}


# --------------------------------------------------------------------------
# foreground-package gate
# --------------------------------------------------------------------------

class _Proc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


def _dumpsys(package: str) -> str:
    return f"  mResumedActivity: ActivityRecord{{abc u0 {package}/.MainActivity t42}}\n"


def test_other_app_reported_when_the_foreground_package_is_not_the_one_under_test(monkeypatch):
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV, game_packages=["com.example.game"])
    monkeypatch.setattr(
        "perception.scene_classifier.subprocess.run",
        lambda *a, **k: _Proc(_dumpsys("com.android.launcher")),
    )

    reading = classifier.classify(make_frame(), device_id="EXAMPLE_SERIAL")

    assert reading.scene == SIGNAL_OTHER_APP
    assert reading.evidence["package"] == "com.android.launcher"
    assert reading.checked == [SIGNAL_OTHER_APP]   # nothing else had to run


def test_the_app_under_test_in_the_foreground_falls_through_to_the_image_probes(monkeypatch):
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV, game_packages=["com.example.game"])
    monkeypatch.setattr(
        "perception.scene_classifier.subprocess.run",
        lambda *a, **k: _Proc(_dumpsys("com.example.game")),
    )

    reading = classifier.classify(make_frame(), device_id="EXAMPLE_SERIAL")

    assert reading.scene == "home"
    assert reading.checked[0] == SIGNAL_OTHER_APP


def test_the_package_probe_is_skipped_when_unconfigured(monkeypatch):
    """Costs nothing unless it is asked for — no device_id or no allowlist, no adb."""
    def explode(*a, **k):
        raise AssertionError("adb must not be called")

    register_example_probes()
    monkeypatch.setattr("perception.scene_classifier.subprocess.run", explode)

    no_packages, _ = make_classifier(HOME_NAV)
    assert no_packages.classify(make_frame(), device_id="EXAMPLE_SERIAL").scene == "home"

    no_device, _ = make_classifier(HOME_NAV, game_packages=["com.example.game"])
    assert no_device.classify(make_frame()).scene == "home"


def test_an_unreadable_foreground_package_degrades_to_no_opinion(monkeypatch):
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV, game_packages=["com.example.game"])
    monkeypatch.setattr(
        "perception.scene_classifier.subprocess.run",
        lambda *a, **k: _Proc("no activity records here"),
    )

    assert classifier.classify(make_frame(), device_id="EXAMPLE_SERIAL").scene == "home"


def test_a_missing_adb_binary_does_not_break_classification(monkeypatch):
    import subprocess as _sp

    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV, game_packages=["com.example.game"])

    def boom(*a, **k):
        raise FileNotFoundError("adb")

    monkeypatch.setattr("perception.scene_classifier.subprocess.run", boom)
    assert classifier.classify(make_frame(), device_id="EXAMPLE_SERIAL").scene == "home"

    def timeout(*a, **k):
        raise _sp.TimeoutExpired(cmd="adb", timeout=1)

    monkeypatch.setattr("perception.scene_classifier.subprocess.run", timeout)
    assert classifier.classify(make_frame(), device_id="EXAMPLE_SERIAL").scene == "home"


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------

def test_every_reading_carries_evidence_and_a_probe_trail():
    """A bare label is not diagnosable when it is wrong."""
    register_example_probes()
    classifier, _ = make_classifier(HOME_NAV)

    reading = classifier.classify(make_frame())
    payload = reading.to_dict()

    assert payload["evidence"]["signal"] == "home"
    assert payload["checked"] == ["blank", "popup", "home"]
    assert payload["elapsed_ms"]["total"] >= 0
    assert 0.0 < payload["confidence"] <= 1.0
