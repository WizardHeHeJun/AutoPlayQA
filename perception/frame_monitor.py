"""Background frame monitoring: a polled frame source that outlives one call.

Why this exists: observational flows (live-record) currently make the agent
poll `screenshot` in a loop, which means every frame costs a round trip *and* a
turn — the agent is stuck being the clock. This module decouples "supplying
frames" from "deciding what to do with them": a background thread keeps writing
frames to disk at a fixed interval, and the agent pulls whatever appeared since
its last look (paths only — it reads the ones it actually cares about).

Design notes that are easy to get wrong later:

* **It never builds its own capturer.** The shared ScreenshotCapturer is handed
  in, because the scrcpy stream pool, the OCR warmup ordering and the screencap
  fallback chain all live in that one instance
  (.claude/rules/perception-rules.md). A second capturer would start a second
  device-side scrcpy server and re-open the onnxruntime deadlock window.
* **No lock around `capture_image`.** That call is already safe to enter from
  several threads: the capturer serializes its lazy pool init
  (`_stream_init_lock`) and `ScrcpyStreamPool.get_frame` takes the pool lock for
  the whole grab, while the screencap fallback is a plain `subprocess.run`. A
  monitor-side lock would only serialize the engine behind this loop for no
  correctness gain — the pool already does that, with bounded waits.
* **Frames are the return face, not evidence.** They are downscaled to a 720px
  short edge by default (`utils.image_scale`) to keep the agent's image-token
  bill down. Findings evidence keeps going through the untouched
  `screenshot_exact` path; nothing here feeds recognition.
* **Bounded on both axes.** Disk is a ring of `max_frames` files inside the
  monitor's own directory, and a loop that cannot capture (device unplugged,
  adb wedged) latches itself off after `MAX_CONSECUTIVE_FAILURES` instead of
  spinning forever.
* **Frames may also be pushed to an injected sink.** `frame_sink` is an
  *opaque callable* handed in by the entry point (mcp_server wires the task
  layer's monitor sentinel to it), called once per frame that reached disk. The
  monitor knows nothing about what it feeds: a sink that raises is counted
  (`sink_errors`) and rate-limit-logged, never allowed to break the loop, and
  no sink at all means byte-for-byte the previous behaviour. This keeps the
  layering intact — perception still imports nothing from task/ or above.

Layer: perception. It depends on `utils/` and a capturer handed in by the entry
point, and imports nothing from task/ or above.
"""
from __future__ import annotations

import atexit
import re
import threading
import time
import weakref
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, TYPE_CHECKING

from perception.screenshot_capturer import PNG_COMPRESS_LEVEL
from utils.image_scale import downscale_short_edge

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from PIL.Image import Image as PILImage

#: What an injected frame consumer looks like: (device_id, frame, metadata).
FrameSink = Callable[[str, "PILImage", Dict], None]

DEFAULT_MONITOR_ROOT = "outputs/monitor"
DEFAULT_INTERVAL_MS = 1000
DEFAULT_MAX_FRAMES = 200
MIN_INTERVAL_MS = 100
# A monitor that cannot capture is useless *and* noisy (each failure is an adb
# round trip plus a log line), so it stops itself rather than spinning until
# someone remembers it exists. 10 at the default 1s interval ~= 10s of trying.
MAX_CONSECUTIVE_FAILURES = 10
STOP_JOIN_TIMEOUT_S = 5.0
# A broken sink would otherwise log once per frame (one line per second at the
# default interval). Log the first failure, then every Nth, so a permanently
# broken sink stays visible without drowning the log.
SINK_ERROR_LOG_EVERY = 10
# Weight of the newest sample in the rolling capture-duration average. High
# enough that the number tracks a real slowdown within a few frames, low enough
# that one hiccup does not define it.
CAPTURE_MS_EMA_ALPHA = 0.3

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(device_id: str) -> str:
    """adb ids may be ``ip:port`` — ``:`` is not a legal Windows path char."""
    return _UNSAFE_NAME.sub("_", device_id)


class FrameMonitor:
    """One device's background capture loop, its ring of frames and its cursor.

    The cursor is server-side on purpose: the agent asks "what is new?" without
    having to remember where it left off, which is the whole point of splitting
    frame supply from frame consumption.
    """

    def __init__(self, logger, capturer, device_id: str,
                 interval_ms: int = DEFAULT_INTERVAL_MS,
                 max_frames: int = DEFAULT_MAX_FRAMES,
                 full_resolution: bool = False,
                 output_root: str = DEFAULT_MONITOR_ROOT,
                 frame_sink: Optional[FrameSink] = None) -> None:
        self.logger = logger
        self.capturer = capturer
        self.device_id = device_id
        self.interval_ms = max(MIN_INTERVAL_MS, int(interval_ms))
        self.max_frames = max(1, int(max_frames))
        self.full_resolution = bool(full_resolution)
        # Opaque consumer of every frame that reached disk. The monitor never
        # inspects it and never lets it fail the loop (see _notify_sink).
        self.frame_sink = frame_sink
        # Millisecond precision, not seconds: a restart (stop old, start new)
        # must land in its own directory, or the new monitor's ring would be
        # sharing a folder with the previous run's leftover frames.
        started = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.monitor_dir = Path(output_root) / _safe_name(device_id) / started

        # Guards the ring, the counters and the cursor. Held for dict/deque
        # updates and a file unlink only — never across a capture or a sleep.
        self._lock = threading.Lock()
        # Serializes stop() against itself so a double stop still joins exactly
        # once. Deliberately *not* self._lock: joining the loop thread while
        # holding the ring lock would deadlock against its next append.
        self._stop_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._frames: Deque[Dict] = deque()
        self._seq = 0            # frames captured since start (never reset)
        self._cursor = 0         # highest seq already handed to the caller
        self._dropped = 0        # frames evicted by the ring before being read
        self._failures = 0       # capture failures (total)
        self._consecutive = 0    # capture failures in a row (latch-off input)
        self._latched_off: Optional[str] = None
        self._sink_errors = 0     # frame_sink calls that raised (never fatal)
        self._overruns = 0        # rounds that took longer than interval_ms
        self._last_capture_ms: Optional[float] = None
        self._avg_capture_ms: Optional[float] = None
        self._started_at = time.time()
        self._stopped_at: Optional[float] = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Create the output directory and run the capture loop in a thread."""
        self.monitor_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._loop, name=f"frame-monitor-{_safe_name(self.device_id)}", daemon=True
        )
        _active_monitors.add(self)
        self._thread.start()
        self.logger.info(
            "frame monitor started for %s (every %dms, keeping %d frames) -> %s",
            self.device_id, self.interval_ms, self.max_frames, self.monitor_dir.as_posix(),
        )

    def stop(self, timeout_s: float = STOP_JOIN_TIMEOUT_S) -> Dict:
        """Stop the loop and return the final summary. Idempotent and atomic.

        The second caller does not join a thread the first one already reaped;
        it gets the same summary with ``already_stopped``.
        """
        with self._stop_lock:
            thread, self._thread = self._thread, None
            already = thread is None and self._stop_event.is_set()
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                # Only reachable if a capture is wedged past the adb timeout;
                # the thread is a daemon, so it cannot hold the process open.
                self.logger.warning(
                    "frame monitor for %s did not stop within %.1fs; leaving it to exit",
                    self.device_id, timeout_s,
                )
        if self._stopped_at is None:
            self._stopped_at = time.time()
        _active_monitors.discard(self)
        summary = self.status()
        summary["already_stopped"] = already
        if not already:
            # The loop runs in the background with nothing on the terminal; its
            # one-line obituary is where "the monitor was quietly latched off /
            # dropping frames" becomes visible. Only for the caller that really
            # stopped it, so a double stop does not print twice.
            self.logger.info(
                "frame monitor stopped for %s: frames=%d on_disk=%d dropped=%d "
                "failures=%d latched_off=%s",
                self.device_id, summary["frames_total"], summary["frames_on_disk"],
                summary["dropped"], summary["failures"], summary["latched_off"],
            )
        return summary

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---------- capture loop ----------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            began = time.monotonic()
            self._capture_once()
            if self._latched_off:
                break
            # Sleep the *remainder* of the interval so a slow capture does not
            # stack on top of it, and wake immediately when stop() is called.
            remaining = self.interval_ms / 1000.0 - (time.monotonic() - began)
            if remaining > 0:
                self._stop_event.wait(remaining)
            else:
                # The round ate its whole budget: frames are arriving slower
                # than the caller asked for. Counted rather than logged (it can
                # happen every round), so `overruns` climbing next to
                # `avg_capture_ms` says "raise interval_ms", not "adb is down".
                with self._lock:
                    self._overruns += 1
        if self._stopped_at is None:
            self._stopped_at = time.time()

    def _capture_once(self) -> None:
        began = time.monotonic()
        try:
            image = self.capturer.capture_image(self.device_id)
        except Exception as exc:  # noqa: BLE001 - any capture failure is countable, not fatal
            with self._lock:
                self._failures += 1
                self._consecutive += 1
                consecutive = self._consecutive
            self.logger.warning(
                "frame monitor: capture failed on %s (%d in a row): %s",
                self.device_id, consecutive, exc,
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                self._latched_off = (
                    f"{consecutive} consecutive capture failures; last error: {exc}"
                )
                self.logger.warning(
                    "frame monitor for %s latched off: %s", self.device_id, self._latched_off
                )
            return

        # Return face only: nothing downstream recognizes on these frames.
        out = image if self.full_resolution else downscale_short_edge(image)[0]
        ts_ms = int(time.time() * 1000)
        with self._lock:
            self._consecutive = 0
            seq = self._seq + 1
            self._seq = seq
        # Zero-padded sequence first, so the files sort in capture order even
        # when two frames land inside the same millisecond.
        name = f"frame_{seq:06d}_{ts_ms}.png"
        path = self.monitor_dir / name
        try:
            out.save(path, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
        except OSError as exc:
            with self._lock:
                self._failures += 1
            self.logger.warning("frame monitor: could not write %s: %s", path.as_posix(), exc)
            return
        meta = {
            "index": seq,
            "path": path.as_posix(),
            "name": name,
            "ts_ms": ts_ms,
            "width": out.width,
            "height": out.height,
        }
        with self._lock:
            self._frames.append(meta)
            self._prune_locked()
            self._record_capture_ms_locked((time.monotonic() - began) * 1000.0)
        # Last, and outside the lock: the sink may do real work (the sentinel
        # runs a blank check and may write a finding), and it must never be able
        # to hold up a reader of the ring — nor to break this loop.
        self._notify_sink(out, dict(meta))

    def _record_capture_ms_locked(self, elapsed_ms: float) -> None:
        self._last_capture_ms = round(elapsed_ms, 1)
        previous = self._avg_capture_ms
        blended = elapsed_ms if previous is None else (
            CAPTURE_MS_EMA_ALPHA * elapsed_ms + (1 - CAPTURE_MS_EMA_ALPHA) * previous
        )
        self._avg_capture_ms = round(blended, 1)

    def _notify_sink(self, image, meta: Dict) -> None:
        """Hand one frame to the injected consumer; a failing sink is contained.

        The sink is somebody else's code (the task layer's sentinel), so it is
        treated as untrusted: any exception is counted and rate-limit-logged,
        and the capture loop carries on. A monitor whose sink is broken still
        supplies frames — that is its actual job.
        """
        sink = self.frame_sink
        if sink is None:
            return
        try:
            sink(self.device_id, image, meta)
        except Exception as exc:  # noqa: BLE001 - a sink must never break capture
            with self._lock:
                self._sink_errors += 1
                errors = self._sink_errors
            if errors == 1 or errors % SINK_ERROR_LOG_EVERY == 0:
                self.logger.warning(
                    "frame monitor: frame sink failed on %s (%d so far): %s",
                    self.device_id, errors, exc,
                )

    def _prune_locked(self) -> None:
        """Ring cleanup: keep the newest ``max_frames`` files, delete the rest.

        Only ever unlinks files this monitor created in its own directory, so a
        misconfigured root can never eat someone else's artifacts.
        """
        while len(self._frames) > self.max_frames:
            old = self._frames.popleft()
            if old["index"] > self._cursor:
                # Evicted before the caller ever saw it: that is a dropped
                # frame, and the caller is told rather than silently shorted.
                self._dropped += 1
            try:
                Path(old["path"]).unlink()
            except OSError as exc:
                self.logger.warning(
                    "frame monitor: could not delete %s: %s", old["path"], exc
                )

    # ---------- reads ----------

    def take_new(self) -> Dict:
        """Return the frames captured since the last call and advance the cursor."""
        with self._lock:
            new: List[Dict] = [f for f in self._frames if f["index"] > self._cursor]
            if new:
                self._cursor = new[-1]["index"]
            status = self._status_locked()
        status["frames"] = [
            {"index": f["index"], "path": f["path"], "ts_ms": f["ts_ms"],
             "width": f["width"], "height": f["height"]}
            for f in new
        ]
        status["new_count"] = len(new)
        return status

    def status(self) -> Dict:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> Dict:
        return {
            "ok": True,
            "device_id": self.device_id,
            "running": self.running,
            "monitor_dir": self.monitor_dir.as_posix(),
            "interval_ms": self.interval_ms,
            "max_frames": self.max_frames,
            "full_resolution": self.full_resolution,
            "frames_total": self._seq,
            "frames_on_disk": len(self._frames),
            "dropped": self._dropped,
            "failures": self._failures,
            "latched_off": self._latched_off,
            # Loop health: rounds that blew past interval_ms, frames whose sink
            # raised, and how long a capture actually takes right now.
            "overruns": self._overruns,
            "sink_errors": self._sink_errors,
            "last_capture_ms": self._last_capture_ms,
            "avg_capture_ms": self._avg_capture_ms,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }


class FrameMonitorRegistry:
    """One live monitor per device, mirroring the gesture/action-log registries.

    Entries survive ``stop()`` so a caller can still drain the tail of a
    finished monitor; ``start()`` on a device that already has one stops the old
    loop first, so a device is never captured by two threads at once.
    """

    def __init__(self, logger, capturer, output_root: str = DEFAULT_MONITOR_ROOT) -> None:
        self.logger = logger
        self.capturer = capturer
        self.output_root = output_root
        self._monitors: Dict[str, FrameMonitor] = {}
        self._lock = threading.Lock()
        _active_registries.add(self)

    def start(self, device_id: str, interval_ms: int = DEFAULT_INTERVAL_MS,
              max_frames: int = DEFAULT_MAX_FRAMES,
              full_resolution: bool = False,
              frame_sink: Optional[FrameSink] = None) -> Dict:
        """Start (or restart) this device's loop; `frame_sink` is passed through.

        The sink belongs to the caller, not to the registry: whoever starts a
        monitor knows what should consume its frames and owns that object's own
        lifecycle. Omitting it keeps the previous behaviour exactly.
        """
        with self._lock:
            previous = self._monitors.get(device_id)
            restarted = previous is not None and previous.running
            if previous is not None:
                # Bounded by the join timeout, and the loop wakes from its sleep
                # on the stop event, so this holds the registry lock briefly.
                previous.stop()
            monitor = FrameMonitor(
                self.logger, self.capturer, device_id,
                interval_ms=interval_ms, max_frames=max_frames,
                full_resolution=full_resolution, output_root=self.output_root,
                frame_sink=frame_sink,
            )
            try:
                monitor.start()
            except OSError as exc:
                self._monitors.pop(device_id, None)
                self.logger.warning(
                    "frame monitor: could not start on %s: %s", device_id, exc
                )
                return {"ok": False, "device_id": device_id,
                        "error": f"Could not start the frame monitor for '{device_id}': {exc}"}
            self._monitors[device_id] = monitor
        result = monitor.status()
        result["restarted"] = restarted
        return result

    def new_frames(self, device_id: str) -> Dict:
        monitor = self._get(device_id)
        if monitor is None:
            return self._unknown(device_id)
        return monitor.take_new()

    def status(self, device_id: str) -> Dict:
        monitor = self._get(device_id)
        if monitor is None:
            return self._unknown(device_id)
        return monitor.status()

    def stop(self, device_id: str) -> Dict:
        monitor = self._get(device_id)
        if monitor is None:
            return self._unknown(device_id)
        return monitor.stop()

    def stop_all(self) -> None:
        with self._lock:
            monitors = list(self._monitors.values())
        for monitor in monitors:
            try:
                monitor.stop()
            except Exception as exc:  # noqa: BLE001 - one bad monitor must not skip the rest
                self.logger.warning(
                    "frame monitor: cleanup failed for %s: %s", monitor.device_id, exc
                )

    def _get(self, device_id: str) -> Optional[FrameMonitor]:
        with self._lock:
            return self._monitors.get(device_id)

    @staticmethod
    def _unknown(device_id: str) -> Dict:
        return {"ok": False, "device_id": device_id, "running": False,
                "error": f"No frame monitor for '{device_id}'; call start_monitor first."}


# -- Interpreter-exit safety net ----------------------------------------------
#
# Mirrors the frame_stream / scrcpy pool hooks: a host that exits without
# stopping its monitors would otherwise leave threads hammering adb during
# shutdown. Weak references so a dropped registry stays collectable.
_active_registries: "weakref.WeakSet[FrameMonitorRegistry]" = weakref.WeakSet()
_active_monitors: "weakref.WeakSet[FrameMonitor]" = weakref.WeakSet()


def stop_all_monitors() -> None:
    """Stop every live monitor (atexit hook; safe to call more than once)."""
    for registry in list(_active_registries):
        registry.stop_all()
    for monitor in list(_active_monitors):
        try:
            monitor.stop()
        except Exception:  # noqa: BLE001 - best effort during interpreter shutdown
            pass


atexit.register(stop_all_monitors)
