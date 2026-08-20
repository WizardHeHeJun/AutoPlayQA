"""Monitor sentinel: cheap anomaly detection while the engine is NOT running.

Why it exists: when a task hits an `agent` node the engine finalizes the whole
run and returns (`status=agent_required`), so for the entire handoff — which is
the *slowest* part of a run, a human-speed agent turn — nobody is watching the
screen and nobody is draining logcat. A crash or a white screen during that
window simply never becomes a finding. The background frame monitor is already
capturing that period; this hangs a sentinel off it.

Shape of the thing:

* **It rides the monitor's frames, it does not capture.** `on_frame` is the
  callable handed to `FrameMonitor(frame_sink=...)`, so there is no second
  capturer, no extra adb round trip, and the detection cadence is exactly the
  monitor's interval.
* **It only looks when the engine is not looking.** Every frame is gated on
  `engine.is_running`: while a run is in flight its watchdogs, its logcat poll
  and its findings recorder own that screen, and a second opinion would only
  produce duplicate findings against a run whose report is being written. The
  blank-episode state is reset while gated, so a run that ends on a legitimately
  dark frame does not hand the sentinel a half-finished episode.
* **Its findings are its own run.** The recorder here is a *separate*
  `FindingsRecorder` instance from the engine's: that recorder carries per-run
  state (run dir, timeline, frame history, findings list) for the run it is
  currently recording, and writing sentinel findings into it would corrupt the
  engine's report. Same output_dir, so `outputs/findings/<date>/<device>/<run>/`
  and everything downstream (smoke-report triage, retention, export) treats a
  sentinel run like any other.
* **Its logcat monitor is its own too.** `LogcatMonitor.poll` dedups against a
  per-instance `_seen` set and reads from a per-instance start marker, so
  sharing the engine's instance would mean the two steal each other's events.
* **Nothing here may raise.** It runs on the monitor's capture thread; an
  exception escaping `on_frame` would be swallowed by the monitor as a sink
  error, and detection would silently stop being useful. So every path is
  guarded and counted (`stats()["errors"]`) instead.

Layer: task. It may import perception/ and utils/ (and does), never the other
way round — the monitor knows this object only as an opaque callable.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from core.logger import log_event
from utils.helpers import image_grayscale_stddev

#: Grayscale stddev below this reads as a uniform frame (black/white/blank).
#: Deliberately looser than the `blank_screen` recognizer's own default: under
#: the lossy H.264 stream backend a truly blank screen still carries some
#: compression noise, and a false "not blank" is cheaper than a false finding.
DEFAULT_BLANK_STDDEV = 8.0
#: Consecutive blank frames before it counts as a stall rather than a
#: transition. Loading screens and scene swaps flash dark for a frame or two.
DEFAULT_BLANK_MIN_FRAMES = 3
#: Seconds between logcat polls. The frame cadence (often 1s) is far too fast
#: for an adb `logcat -d` dump, so polling is throttled on the frame callback
#: rather than given a thread of its own.
DEFAULT_LOGCAT_POLL_INTERVAL_S = 5.0

#: Task name the sentinel's findings runs are filed under.
SENTINEL_TASK_NAME = "monitor_sentinel"

BLANK_FINDING_TYPE = "sentinel_blank_screen"
LOGCAT_FINDING_TYPE = "sentinel_logcat_crash"


class MonitorSentinel:
    """Watches monitor frames for blank screens and crashes between runs."""

    def __init__(self, logger, device_id: str, findings_recorder,
                 logcat_monitor=None, engine=None,
                 blank_stddev: float = DEFAULT_BLANK_STDDEV,
                 blank_min_frames: int = DEFAULT_BLANK_MIN_FRAMES,
                 logcat_poll_interval_s: float = DEFAULT_LOGCAT_POLL_INTERVAL_S) -> None:
        self.logger = logger
        self.device_id = device_id
        self.recorder = findings_recorder
        self.logcat = logcat_monitor
        self.engine = engine
        self.blank_stddev = float(blank_stddev)
        self.blank_min_frames = max(1, int(blank_min_frames))
        self.logcat_poll_interval_s = float(logcat_poll_interval_s)

        # Guards every counter and the episode/run state. The frame callback
        # runs on the monitor thread while stats()/finalize() are called from
        # the MCP tool thread.
        self._lock = threading.Lock()
        self._blank_streak = 0
        self._blank_episode_active = False
        self._run_open = False
        self._finalized = False
        self._findings_count = 0
        self._checked_frames = 0
        self._gated_frames = 0
        self._logcat_polls = 0
        self._logcat_started = False
        self._last_logcat_poll: Optional[float] = None
        self._errors = 0
        # The summary the first finalize() produced, replayed by every later
        # call (see finalize): stop_monitor is idempotent, and its reply must
        # keep the same shape on the second call as on the first.
        self._final_summary: Optional[Dict] = None

    # ---------- the monitor's frame sink ----------

    def on_frame(self, device_id: str, image, meta: Optional[Dict] = None) -> None:
        """One monitor frame. Never raises — the capture loop depends on it.

        Signature matches `FrameMonitor`'s `frame_sink`: the monitor hands over
        the frame it just wrote plus that frame's metadata.
        """
        try:
            if self._engine_busy():
                # The engine owns this screen right now. Drop the episode state
                # as well, so the first frame after a run starts a fresh count
                # instead of completing one that straddles the run.
                with self._lock:
                    self._gated_frames += 1
                    self._blank_streak = 0
                    self._blank_episode_active = False
                return
            with self._lock:
                self._checked_frames += 1
            self._check_blank(image, meta)
            self._check_logcat(image)
        except Exception as exc:  # noqa: BLE001 - a sentinel must never break capture
            self._note_error("frame check", exc)

    def _engine_busy(self) -> bool:
        """True while a task run is in flight *on this device*.

        Per-device, not global: the engine is a singleton shared by every
        device, so gating on `is_running` alone would silence the sentinel on
        phone B for as long as a run occupies phone A — which is precisely the
        unwatched handoff window this thing exists for.

        When the engine says it is running but the device cannot be read (no
        `running_device` attribute, None, or an exception anywhere in the gate),
        the answer is "busy". Unknown engine state is the one case where
        over-reporting is the worse failure: the engine's own watchdogs are
        already writing findings about that screen, and a duplicate finding in
        someone else's run report costs more than one missed blank frame.
        """
        if self.engine is None:
            return False
        try:
            if not bool(getattr(self.engine, "is_running", False)):
                return False
            running_device = getattr(self.engine, "running_device", None)
        except Exception as exc:  # noqa: BLE001 - a weird engine must not raise here
            self._note_error("engine gate", exc)
            return True
        if running_device is None:
            return True
        return str(running_device) == str(self.device_id)

    # ---------- detectors ----------

    def _check_blank(self, image, meta: Optional[Dict]) -> None:
        """Blank-screen episodes: one finding per episode, re-armed on recovery."""
        if image is None:
            return
        stddev = image_grayscale_stddev(image)
        if stddev >= self.blank_stddev:
            with self._lock:
                self._blank_streak = 0
                self._blank_episode_active = False
            return

        with self._lock:
            self._blank_streak += 1
            streak = self._blank_streak
            # Episode semantics: the finding fires when the streak first reaches
            # the threshold and then stays quiet until the screen recovers.
            # Without this a stuck white screen would file one finding per frame
            # for as long as it lasts.
            should_report = streak >= self.blank_min_frames and not self._blank_episode_active
            if should_report:
                self._blank_episode_active = True
        if not should_report:
            return

        extra = {"stddev": round(stddev, 2), "threshold": self.blank_stddev,
                 "frames": streak}
        if meta:
            extra["frame"] = meta.get("path")
        self._record(
            BLANK_FINDING_TYPE, "warning",
            f"Screen has been blank for {streak} monitor frames "
            f"(grayscale stddev {stddev:.2f} < {self.blank_stddev})",
            image=image, extra=extra,
        )

    def _check_logcat(self, image) -> None:
        """Throttled crash/ANR drain; the current frame becomes the evidence."""
        if self.logcat is None:
            return
        now = time.monotonic()
        with self._lock:
            due = (
                self._last_logcat_poll is None
                or now - self._last_logcat_poll >= self.logcat_poll_interval_s
            )
            if due:
                self._last_logcat_poll = now
        if not due:
            return

        if not self._logcat_started:
            # Sets the -T marker from the device clock, so the first poll does
            # not report every crash that ever happened on this phone.
            try:
                self.logcat.start(self.device_id)
            except Exception as exc:  # noqa: BLE001 - degrade, do not stop
                self._note_error("logcat start", exc)
                return
            self._logcat_started = True

        try:
            events: List[Dict] = self.logcat.poll(self.device_id) or []
        except Exception as exc:  # noqa: BLE001 - degrade, do not stop
            self._note_error("logcat poll", exc)
            return
        with self._lock:
            self._logcat_polls += 1
        for event in events:
            self._record(
                event.get("type") or LOGCAT_FINDING_TYPE,
                event.get("severity") or "error",
                event.get("line", ""),
                image=image,
                extra={"excerpt": event.get("excerpt", []), "source": "monitor_sentinel"},
            )

    # ---------- findings run ----------

    def _record(self, finding_type: str, severity: str, message: str,
                image=None, extra: Optional[Dict] = None) -> None:
        """File one finding, opening the sentinel's own run on first use."""
        if self.recorder is None:
            self.logger.warning(
                "monitor sentinel [%s] %s on %s: %s",
                severity, finding_type, self.device_id, message,
            )
            return
        try:
            self._ensure_run()
            # image= pins the evidence to the frame that tripped the check, the
            # same rule the engine follows: a re-capture taken afterwards would
            # show whatever the screen settled into, not the anomaly.
            #
            # force_exact= is the sentinel's own requirement on top: the pinned
            # frame comes from the monitor, i.e. downscaled to a 720px short
            # edge (and lossy under the stream backend), which is a fine "what
            # tripped it" thumbnail but poor QA evidence. So a lossless
            # full-resolution screencap is always taken alongside it, whatever
            # the capture backend — the engine's default (stream backends only)
            # would leave screencap runs with the shrunken frame as the sole
            # record.
            self.recorder.record(
                finding_type, severity, message,
                node=None, screenshot=True, extra=extra, image=image,
                force_exact=True,
            )
        except Exception as exc:  # noqa: BLE001 - evidence must not break detection
            self._note_error("record finding", exc)
            return
        with self._lock:
            self._findings_count += 1
        log_event(
            self.logger, "sentinel_finding", device=self.device_id,
            type=finding_type, severity=severity,
        )

    def _ensure_run(self) -> None:
        """Open the findings run lazily — a quiet sentinel leaves no directory."""
        with self._lock:
            if self._run_open:
                return
            self._run_open = True
        self.recorder.open_run(self.device_id, SENTINEL_TASK_NAME)
        self.recorder.ensure_run_dir()

    def finalize(self) -> Optional[Dict]:
        """Seal the sentinel's findings run; returns the report summary or None.

        Idempotent in the useful sense: the run is sealed exactly once and every
        later call replays the same summary, so a repeated stop_monitor answers
        with the same "sentinel" block (report paths included) instead of a
        report that quietly disappears on the second call.

        A no-op when nothing was ever recorded — the run directory is only
        created on the first finding, so a monitor that saw nothing interesting
        leaves no empty folder behind and no summary to replay.
        """
        with self._lock:
            if self._finalized or not self._run_open:
                self._finalized = True
                return self._final_summary
            self._finalized = True
        try:
            _findings, summary = self.recorder.finalize(status="completed")
        except Exception as exc:  # noqa: BLE001 - a bad report must not fail stop_monitor
            self._note_error("finalize", exc)
            return None
        with self._lock:
            self._final_summary = summary
        self.logger.info(
            "monitor sentinel on %s recorded %d finding(s) -> %s",
            self.device_id, self._findings_count, summary.get("run_dir"),
        )
        return summary

    # ---------- introspection ----------

    def stats(self) -> Dict:
        with self._lock:
            return {
                "device_id": self.device_id,
                "findings_count": self._findings_count,
                "blank_episode_active": self._blank_episode_active,
                "blank_streak": self._blank_streak,
                "checked_frames": self._checked_frames,
                "gated_frames": self._gated_frames,
                "logcat_polls": self._logcat_polls,
                "errors": self._errors,
                "finalized": self._finalized,
                "blank_stddev": self.blank_stddev,
                "blank_min_frames": self.blank_min_frames,
            }

    def _note_error(self, what: str, exc: Exception) -> None:
        with self._lock:
            self._errors += 1
            count = self._errors
        self.logger.warning(
            "monitor sentinel: %s failed on %s (%d so far): %s",
            what, self.device_id, count, exc,
        )
