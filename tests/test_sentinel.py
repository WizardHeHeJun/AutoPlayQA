"""Monitor sentinel: the anomaly watch that runs while the engine does not.

No device and no real findings chain beyond a real FindingsRecorder writing to
tmp_path — the seams the sentinel actually has are the frame callback, the
engine's `is_running` flag and the logcat monitor, so all three are fakes here.
Frames are PIL images fed straight to `on_frame`, which is exactly how the frame
monitor calls it.
"""
from __future__ import annotations

import logging

import pytest
from PIL import Image

from task.findings import FindingsRecorder
from task.sentinel import BLANK_FINDING_TYPE, SENTINEL_TASK_NAME, MonitorSentinel

LOGGER = logging.getLogger("test")


class FakeCapturer:
    """Only encode_png / capture_png_bytes are reachable from a finding."""

    stream_enabled = False

    def encode_png(self, image):
        return b"\x89PNG-fake"

    def capture_png_bytes(self, device_id, exact=False):
        return b"\x89PNG-fake"


class FakeEngine:
    """The two attributes the gate reads: the flag and whose screen it owns."""

    def __init__(self, running=False, running_device="dev1"):
        self.is_running = running
        self.running_device = running_device if running else None


class FakeLogcat:
    """Stands in for LogcatMonitor: start() marks, poll() hands back events."""

    def __init__(self, events=None, fail_on_poll=False):
        self.events = list(events or [])
        self.fail_on_poll = fail_on_poll
        self.started = []
        self.polls = 0

    def start(self, device_id):
        self.started.append(device_id)

    def poll(self, device_id):
        self.polls += 1
        if self.fail_on_poll:
            raise RuntimeError("adb wedged")
        out, self.events = self.events, []
        return out

    def tail(self, device_id, seconds=60, max_lines=300):
        return []


def _blank():
    return Image.new("RGB", (64, 64), "black")


def _busy():
    """A frame with real texture, so its grayscale stddev is well above blank."""
    image = Image.new("RGB", (64, 64), "black")
    for x in range(0, 64, 2):
        for y in range(64):
            image.putpixel((x, y), (255, 255, 255))
    return image


def _recorder(tmp_path, logcat=None):
    return FindingsRecorder(
        LOGGER, FakeCapturer(), None, output_dir=str(tmp_path / "findings"),
        logcat_monitor=logcat, history=False,
    )


def _sentinel(tmp_path, engine=None, logcat=None, **kwargs):
    return MonitorSentinel(
        LOGGER, "dev1", _recorder(tmp_path, logcat), logcat_monitor=logcat,
        engine=engine if engine is not None else FakeEngine(False), **kwargs
    )


def _feed(sentinel, image, times=1):
    for i in range(times):
        sentinel.on_frame("dev1", image, {"index": i + 1, "path": f"frame_{i}.png"})


# ---------- blank episodes ----------

def test_a_blank_episode_is_reported_once(tmp_path):
    sen = _sentinel(tmp_path, blank_min_frames=3)
    _feed(sen, _blank(), times=6)

    assert sen.stats()["findings_count"] == 1
    assert sen.stats()["blank_episode_active"] is True
    finding = sen.recorder.findings[0]
    assert finding["type"] == BLANK_FINDING_TYPE
    assert finding["severity"] == "warning"
    # Evidence is pinned to the frame that tripped it, not re-captured after.
    assert finding["evidence"]["screenshot"].endswith(".png")


def test_a_short_dark_flash_is_not_a_finding(tmp_path):
    sen = _sentinel(tmp_path, blank_min_frames=3)
    _feed(sen, _blank(), times=2)
    _feed(sen, _busy())
    assert sen.stats()["findings_count"] == 0


def test_recovery_re_arms_the_episode(tmp_path):
    sen = _sentinel(tmp_path, blank_min_frames=2)
    _feed(sen, _blank(), times=4)
    assert sen.stats()["findings_count"] == 1

    _feed(sen, _busy())                      # screen came back: episode over
    assert sen.stats()["blank_episode_active"] is False
    _feed(sen, _blank(), times=2)            # and it can fire again
    assert sen.stats()["findings_count"] == 2


# ---------- the engine gate ----------

def test_frames_are_ignored_while_a_run_is_in_flight(tmp_path):
    engine = FakeEngine(running=True)
    sen = _sentinel(tmp_path, engine=engine, blank_min_frames=2)
    _feed(sen, _blank(), times=5)

    stats = sen.stats()
    assert stats["findings_count"] == 0, "the engine's own watchdogs own that screen"
    assert stats["gated_frames"] == 5
    assert stats["checked_frames"] == 0


def test_the_gate_resets_a_half_finished_episode(tmp_path):
    engine = FakeEngine(running=False)
    sen = _sentinel(tmp_path, engine=engine, blank_min_frames=3)
    _feed(sen, _blank(), times=2)            # 2 of 3 blank frames

    engine.is_running, engine.running_device = True, "dev1"
    _feed(sen, _blank())                     # gated: must not complete the streak
    engine.is_running, engine.running_device = False, None
    _feed(sen, _blank())                     # counting starts over

    assert sen.stats()["findings_count"] == 0
    _feed(sen, _blank(), times=2)
    assert sen.stats()["findings_count"] == 1


def test_a_run_on_another_device_does_not_gate_this_sentinel(tmp_path):
    # The engine is a singleton shared by every device: gating on is_running
    # alone would silence phone B's sentinel for the whole of phone A's run,
    # which is the unwatched window this thing exists for.
    engine = FakeEngine(running=True, running_device="other-phone")
    sen = _sentinel(tmp_path, engine=engine, blank_min_frames=2)
    _feed(sen, _blank(), times=3)

    stats = sen.stats()
    assert stats["findings_count"] == 1
    assert stats["gated_frames"] == 0 and stats["checked_frames"] == 3


def test_an_unknown_running_device_gates_conservatively(tmp_path):
    # Engine running but its device unreadable: gate. The engine's own watchdogs
    # may well own this screen, and a duplicate finding written into someone
    # else's run report costs more than one missed blank frame.
    engine = FakeEngine(running=True, running_device=None)
    sen = _sentinel(tmp_path, engine=engine, blank_min_frames=1)
    _feed(sen, _blank(), times=3)

    assert sen.stats()["findings_count"] == 0
    assert sen.stats()["gated_frames"] == 3


def test_an_engine_without_the_attribute_gates_conservatively(tmp_path):
    # Older/duck-typed engine that only has is_running: same conservative answer.
    class LegacyEngine:
        is_running = True

    sen = _sentinel(tmp_path, engine=LegacyEngine(), blank_min_frames=1)
    _feed(sen, _blank(), times=2)

    assert sen.stats()["findings_count"] == 0
    assert sen.stats()["gated_frames"] == 2


def test_no_engine_means_always_watching(tmp_path):
    sen = MonitorSentinel(LOGGER, "dev1", _recorder(tmp_path), blank_min_frames=1)
    _feed(sen, _blank())
    assert sen.stats()["findings_count"] == 1


# ---------- logcat ----------

def test_a_crash_event_becomes_a_finding_with_the_current_frame(tmp_path):
    logcat = FakeLogcat(events=[
        {"type": "crash", "severity": "critical",
         "line": "FATAL EXCEPTION: main", "excerpt": ["at com.game.Boom"]},
    ])
    sen = _sentinel(tmp_path, logcat=logcat, logcat_poll_interval_s=0)
    _feed(sen, _busy())

    assert logcat.started == ["dev1"], "start() sets the -T marker first"
    finding = sen.recorder.findings[0]
    assert finding["type"] == "crash" and finding["severity"] == "critical"
    assert finding["extra"]["excerpt"] == ["at com.game.Boom"]
    assert finding["evidence"]["screenshot"].endswith(".png")


def test_logcat_polling_is_throttled(tmp_path):
    logcat = FakeLogcat()
    sen = _sentinel(tmp_path, logcat=logcat, logcat_poll_interval_s=3600)
    _feed(sen, _busy(), times=5)
    assert logcat.polls == 1, "one poll per interval, not one per frame"
    assert sen.stats()["logcat_polls"] == 1


def test_a_broken_logcat_is_counted_not_raised(tmp_path):
    logcat = FakeLogcat(fail_on_poll=True)
    sen = _sentinel(tmp_path, logcat=logcat, logcat_poll_interval_s=0)
    _feed(sen, _busy(), times=2)
    assert sen.stats()["errors"] >= 1
    assert sen.stats()["findings_count"] == 0


# ---------- lazy run + finalize ----------

def test_a_quiet_sentinel_leaves_no_directory(tmp_path):
    sen = _sentinel(tmp_path)
    _feed(sen, _busy(), times=5)

    assert sen.finalize() is None
    assert not (tmp_path / "findings").exists()


def test_the_run_opens_on_the_first_finding_and_finalizes(tmp_path):
    sen = _sentinel(tmp_path, blank_min_frames=1)
    _feed(sen, _blank())

    assert sen.recorder.task_name == SENTINEL_TASK_NAME
    summary = sen.finalize()
    assert summary is not None and summary["run_dir"]
    report = tmp_path / "findings"
    assert list(report.rglob("report.json")), "a sentinel run writes a normal report"


def test_finalize_is_idempotent_and_replays_the_summary(tmp_path):
    sen = _sentinel(tmp_path, blank_min_frames=1)
    _feed(sen, _blank())

    first = sen.finalize()
    second = sen.finalize()
    assert first is not None and first["report_path"]
    # Same block, report included: stop_monitor is idempotent, so its reply must
    # not lose the report on the second call.
    assert second == first
    assert sen.stats()["finalized"] is True
    assert len(sen.recorder.findings) == 1, "the run is sealed exactly once"


def test_a_sentinel_run_is_pushed_to_the_notifiers(tmp_path):
    """A crash caught during a handoff is exactly what an unattended run wants
    pushed — a sentinel run must not be the one findings run that stays silent."""

    class FakeNotifier:
        def __init__(self):
            self.pushed = []

        def should_notify(self, status, finding_count):
            return finding_count >= 1

        def notify_run(self, summary):
            self.pushed.append(summary)
            return True

    notifier = FakeNotifier()
    recorder = FindingsRecorder(
        LOGGER, FakeCapturer(), None, output_dir=str(tmp_path / "findings"),
        history=False, notifiers=[notifier],
    )
    sen = MonitorSentinel(LOGGER, "dev1", recorder, blank_min_frames=1)
    _feed(sen, _blank())
    sen.finalize()

    assert len(notifier.pushed) == 1
    pushed = notifier.pushed[0]
    assert pushed["task"] == SENTINEL_TASK_NAME and pushed["device"] == "dev1"
    assert pushed["findings"][0]["type"] == BLANK_FINDING_TYPE


def test_sentinel_evidence_gets_a_lossless_companion_shot(tmp_path):
    """The pinned frame is a downscaled monitor frame, so an exact screencap is
    added even on the screencap backend (where the engine's rule would not)."""
    sen = _sentinel(tmp_path, blank_min_frames=1)
    assert sen.recorder.capturer.stream_enabled is False
    _feed(sen, _blank())

    evidence = sen.recorder.findings[0]["evidence"]
    assert evidence["screenshot"].endswith(".png")
    assert evidence["screenshot_exact"].endswith("_exact.png")


# ---------- containment ----------

@pytest.mark.parametrize("attribute", ["engine", "recorder"])
def test_an_exploding_collaborator_never_escapes_on_frame(tmp_path, attribute):
    """on_frame runs on the capture thread; raising there would kill detection."""

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError(f"{attribute} is broken")

    sen = _sentinel(tmp_path, blank_min_frames=1)
    setattr(sen, attribute, Exploding())

    _feed(sen, _blank(), times=2)  # must not raise

    assert sen.stats()["errors"] >= 1


def test_a_sentinel_without_a_recorder_only_logs(tmp_path):
    sen = MonitorSentinel(LOGGER, "dev1", None, blank_min_frames=1)
    _feed(sen, _blank())
    assert sen.stats()["findings_count"] == 0
    assert sen.finalize() is None
