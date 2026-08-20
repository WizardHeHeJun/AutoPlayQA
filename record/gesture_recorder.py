from __future__ import annotations

import atexit
import queue
import re
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core import adb_daemon, windows_job
from core.adb_timeout import adb_timed_out, adb_timeout_s

# -- getevent -lt line format (verified on goodix_ts / Android 14) ---------------
#   [   550369.227909] EV_ABS       ABS_MT_POSITION_X    0000e100
_LINE_RE = re.compile(r"\[\s*(\d+\.\d+)\]\s+(\w+)\s+(\w+)\s+(\w+)")

_TRACKING_UP = 0xFFFFFFFF  # ABS_MT_TRACKING_ID value meaning "finger lifted"


@dataclass
class TouchCalibration:
    """Maps touch-panel coordinates to display pixels.

    Panel ranges come from ``getevent -lp`` (ABS_MT_POSITION_*/max); the display
    size from ``wm size`` (Override when present -- the space ``input``/screencap/
    the injector all use, so recordings round-trip to replay).
    """
    device_path: str
    panel_max_x: int
    panel_max_y: int
    disp_w: int
    disp_h: int

    def to_pixels(self, px: int, py: int) -> Tuple[int, int]:
        x = round(px / self.panel_max_x * self.disp_w)
        y = round(py / self.panel_max_y * self.disp_h)
        x = min(max(x, 0), self.disp_w - 1)
        y = min(max(y, 0), self.disp_h - 1)
        return x, y


@dataclass
class GestureThresholds:
    move_px: int = 24            # >= this displacement -> swipe (not tap/long_press)
    long_press_ms: int = 500     # >= this hold with little movement -> long_press


@dataclass
class GestureEvent:
    """One segmented gesture in display-pixel space (no screenshots -- those are
    attached by the live recorder layer)."""
    index: int
    type: str                                   # tap | long_press | swipe | multi_touch
    params: Dict = field(default_factory=dict)  # backend-ready params (see classify)
    frames: List[Dict] = field(default_factory=list)  # pointer frames (injector format)
    down_point: Tuple[int, int] = (0, 0)        # anchor centre (display px)


# -- Device probing ---------------------------------------------------------------

def calibrate(device_id: str) -> TouchCalibration:
    """Probe the touchscreen node + panel ranges + display size."""
    lp = _adb_text(device_id, "shell", "getevent", "-lp")
    path, max_x, max_y = _parse_touch_device(lp)
    disp_w, disp_h = _parse_wm_size(_adb_text(device_id, "shell", "wm", "size"))
    return TouchCalibration(path, max_x, max_y, disp_w, disp_h)


def _parse_touch_device(lp_dump: str) -> Tuple[str, int, int]:
    cur: Optional[str] = None
    blocks: Dict[str, Dict[str, int]] = {}
    for line in lp_dump.splitlines():
        m = re.search(r"add device \d+:\s*(/dev/input/event\d+)", line)
        if m:
            cur = m.group(1)
            blocks[cur] = {}
        elif cur:
            mx = re.search(r"ABS_MT_POSITION_X.*max\s+(\d+)", line)
            my = re.search(r"ABS_MT_POSITION_Y.*max\s+(\d+)", line)
            if mx:
                blocks[cur]["x"] = int(mx.group(1))
            if my:
                blocks[cur]["y"] = int(my.group(1))
    for path, rng in blocks.items():
        if "x" in rng and "y" in rng:
            return path, rng["x"], rng["y"]
    raise RuntimeError("No touchscreen with ABS_MT_POSITION found in getevent -lp")


def _parse_wm_size(out: str) -> Tuple[int, int]:
    override = re.search(r"Override size:\s*(\d+)x(\d+)", out)
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    m = override or physical
    if not m:
        raise RuntimeError(f"could not parse wm size: {out!r}")
    return int(m.group(1)), int(m.group(2))


def _adb_text(device_id: str, *args) -> str:
    """One-shot adb probe (getevent -lp / wm size) as text.

    Raises RuntimeError when adb blocks past the shared timeout: callers treat
    that like any other probe failure ("calibration failed") instead of leaving
    a recording session — or the MCP tool that started it — hung forever.
    """
    timeout = adb_timeout_s()
    try:
        return subprocess.run(
            ["adb", "-s", device_id, *args], check=False, capture_output=True,
            text=True, timeout=timeout,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        raise adb_timed_out(["adb", "-s", device_id, *args], timeout) from exc


# -- Pure segmenter: getevent lines -> gestures (unit-testable, no device) ---------

class GestureSegmenter:
    """Feed ``getevent -lt`` lines; emit a GestureEvent each time all fingers lift.

    Protocol-B slot model: ABS_MT_SLOT selects the active slot; ABS_MT_TRACKING_ID
    (>=0 down / 0xffffffff up) toggles a slot's contact; ABS_MT_POSITION_X/Y update
    it; SYN_REPORT commits a frame. A *session* spans first-finger-down to
    all-fingers-up. Slot index is reused as the injector pointer id.
    """

    def __init__(
        self,
        calib: TouchCalibration,
        thresholds: Optional[GestureThresholds] = None,
        on_gesture: Optional[Callable[[GestureEvent], None]] = None,
        on_session_start: Optional[Callable[[], None]] = None,
    ):
        self.calib = calib
        self.th = thresholds or GestureThresholds()
        self.on_gesture = on_gesture
        self.on_session_start = on_session_start

        self._cur_slot = 0
        self._slots: Dict[int, Dict] = {}      # slot -> {id, x, y} (panel coords)
        self._index = 0

        # Current session state
        self._open = False
        self._frames: List[Dict] = []          # pointer frames (display px)
        self._prev_ts: Optional[float] = None
        self._max_pointers = 0
        self._down_point: Optional[Tuple[int, int]] = None

    def feed_line(self, line: str) -> None:
        m = _LINE_RE.search(line)
        if not m:
            return
        ts_s, etype, code, raw = m.groups()

        if code == "ABS_MT_SLOT":
            self._cur_slot = int(raw, 16)
            return
        if code == "ABS_MT_TRACKING_ID":
            val = int(raw, 16)
            slot = self._slots.setdefault(self._cur_slot, {"id": -1, "x": 0, "y": 0})
            slot["id"] = -1 if val == _TRACKING_UP else val
            return
        if code == "ABS_MT_POSITION_X":
            self._slots.setdefault(self._cur_slot, {"id": -1, "x": 0, "y": 0})["x"] = int(raw, 16)
            return
        if code == "ABS_MT_POSITION_Y":
            self._slots.setdefault(self._cur_slot, {"id": -1, "x": 0, "y": 0})["y"] = int(raw, 16)
            return
        if code == "SYN_REPORT":
            self._commit_frame(float(ts_s))

    def _commit_frame(self, ts: float) -> None:
        active = [(s, st) for s, st in sorted(self._slots.items()) if st["id"] != -1]

        if active and not self._open:
            # Session start
            self._open = True
            self._frames = []
            self._prev_ts = None
            self._max_pointers = 0
            self._down_point = None
            if self.on_session_start:
                self.on_session_start()

        if self._open:
            pointers = []
            for slot, st in active:
                x, y = self.calib.to_pixels(st["x"], st["y"])
                pointers.append({"id": slot, "x": x, "y": y})
            delay_ms = 0 if self._prev_ts is None else max(0, round((ts - self._prev_ts) * 1000))
            self._prev_ts = ts
            self._frames.append({"delay_ms": delay_ms, "pointers": pointers})
            self._max_pointers = max(self._max_pointers, len(pointers))
            if pointers and self._down_point is None:
                self._down_point = (pointers[0]["x"], pointers[0]["y"])

            if not active:
                self._end_session()

    def _end_session(self) -> None:
        self._open = False
        gesture = self._classify(self._frames, self._max_pointers, self._down_point or (0, 0))
        if gesture is not None:
            self._index += 1
            gesture.index = self._index
            if self.on_gesture:
                self.on_gesture(gesture)

    def _classify(self, frames: List[Dict], max_pointers: int, down_point) -> Optional[GestureEvent]:
        contact = [f for f in frames if f["pointers"]]
        if not contact:
            return None

        total_ms = sum(f["delay_ms"] for f in frames)

        if max_pointers >= 2:
            return GestureEvent(0, "multi_touch", params={}, frames=frames, down_point=down_point)

        # Single-finger: trace pointer 0's path.
        path = [(f["pointers"][0]["x"], f["pointers"][0]["y"]) for f in contact]
        x0, y0 = path[0]
        x1, y1 = path[-1]
        move = max(((px - x0) ** 2 + (py - y0) ** 2) ** 0.5 for px, py in path)

        if move >= self.th.move_px:
            return GestureEvent(
                0, "swipe",
                params={"x1": x0, "y1": y0, "x2": x1, "y2": y1,
                        "duration_ms": total_ms, "path": path},
                frames=frames, down_point=(x0, y0),
            )
        if total_ms >= self.th.long_press_ms:
            return GestureEvent(
                0, "long_press",
                params={"x": x0, "y": y0, "duration_ms": total_ms},
                frames=frames, down_point=(x0, y0),
            )
        return GestureEvent(
            0, "tap", params={"x": x0, "y": y0}, frames=frames, down_point=(x0, y0)
        )


# -- Live recorder: getevent stream + per-gesture screenshots ----------------------

# Interpreter-exit safety net, mirroring ScrcpyStreamPool's atexit hook: an MCP
# session that ends without record_gestures_stop (or a CLI that raises on the way
# out) would otherwise leave the `getevent` adb client — and its frame stream —
# running. Weak references so a dropped recorder stays garbage-collectable;
# stop() de-registers.
_active_recorders: "weakref.WeakSet[GestureRecorder]" = weakref.WeakSet()


def _stop_active_recorders() -> None:
    for recorder in list(_active_recorders):
        try:
            recorder.stop()
        except Exception as exc:  # noqa: BLE001 - one bad recorder must not skip the rest
            if recorder.logger:
                recorder.logger.warning("record: atexit cleanup failed: %s", exc)


atexit.register(_stop_active_recorders)


class GestureRecorder:
    """Streams ``getevent -lt`` from a device and emits screenshot-bearing
    gestures via ``on_gesture``. Screenshot capture runs off the parser thread so
    it never stalls event reading.
    """

    ANCHOR_SIZE = 120
    AFTER_SETTLE_MS = 400        # screencap path: delay before grabbing 'after'
    STREAM_SETTLE_MS = 350       # stream path: 'after' = this much past lift

    def __init__(self, device_id: str, screenshot_capturer, logger,
                 thresholds: Optional[GestureThresholds] = None,
                 frame_stream=None):
        self.device_id = device_id
        self.capturer = screenshot_capturer
        self.logger = logger
        self.thresholds = thresholds or GestureThresholds()
        self.frame_stream = frame_stream
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._calib: Optional[TouchCalibration] = None
        # (capture-thread, result-holder) for the in-flight session's before frame.
        # Bound per session so a fast next gesture can't clobber the previous
        # gesture's before screenshot (sessions alternate start->gesture strictly).
        self._pending_before = None
        self._on_gesture_cb: Optional[Callable] = None
        # Frame-stream state (set up in start() when a stream is available).
        self._use_stream = False
        self._cur_down_ts: Optional[float] = None
        self._stream_pending: Optional[Dict] = None
        self._emit_q: Optional[queue.Queue] = None
        self._emit_thread: Optional[threading.Thread] = None

    def start(self, on_gesture: Callable[[GestureEvent, Dict], None]) -> TouchCalibration:
        """Begin recording. ``on_gesture(event, images)`` is called per gesture,
        where images = {before_png, after_png, anchor_png}."""
        self._calib = calibrate(self.device_id)
        self._on_gesture_cb = on_gesture
        # Bring up the frame stream (best-effort). On any failure we transparently
        # fall back to per-gesture screencap (glow/contamination-prone, but works).
        if self.frame_stream is not None:
            self._use_stream = self.frame_stream.start()
        if self._use_stream:
            # A single serialized worker drains the emit queue -> gestures reach the
            # callback in strict order (counter-based step indexing depends on it).
            self._emit_q = queue.Queue()
            self._emit_thread = threading.Thread(target=self._emit_worker, daemon=True)
            self._emit_thread.start()
            if self.logger:
                self.logger.info("record: using scrcpy frame stream (glow-free)")
        elif self.logger:
            self.logger.info("record: using per-gesture screencap (degraded)")
        segmenter = GestureSegmenter(
            self._calib, self.thresholds,
            on_gesture=self._handle_gesture,
            on_session_start=self._handle_session_start,
        )
        # ``-tt`` forces PTY allocation so getevent line-buffers (a plain pipe is
        # block-buffered -> events sit unflushed until the buffer fills and are lost
        # on terminate). stdin=DEVNULL is essential: without it ``-tt`` puts the
        # console into raw mode and forwards all keystrokes (incl. Enter) to the
        # device, starving the caller's input() -- the recorder would hang on stop.
        # Warm the daemon up from an unbound process first: this client lives for
        # the whole recording session and is bound to the kill-on-close job
        # below, so it must not be the one that forks the machine-wide adb
        # daemon (see core/adb_daemon.py).
        adb_daemon.ensure_adb_daemon()
        self._proc = subprocess.Popen(
            ["adb", "-s", self.device_id, "shell", "-tt",
             "getevent", "-lt", self._calib.device_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        # A hard kill of this process never runs stop() / atexit; the job object
        # is what keeps this client from surviving as an orphan.
        windows_job.bind(self._proc)
        _active_recorders.add(self)
        self._thread = threading.Thread(target=self._read_loop, args=(segmenter,), daemon=True)
        self._thread.start()
        return self._calib

    def stop(self) -> None:
        _active_recorders.discard(self)  # nothing left for the atexit hook to close
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=3)
        self._proc = None
        self._thread = None

        if self._use_stream:
            # Let the last gesture's post-settle frame land in the buffer, then
            # flush it (no next-down bound) and tear the stream down in order:
            # getevent stopped -> flush pending (stream still buffering) -> stop stream.
            if self._stream_pending is not None:
                time.sleep(self.STREAM_SETTLE_MS / 1000 + 0.15)
                self._emit_q.put((self._stream_pending, None))
                self._stream_pending = None
            self._emit_q.put(None)  # sentinel
            if self._emit_thread:
                self._emit_thread.join(timeout=5)
            if self.frame_stream:
                self.frame_stream.stop()
            self._use_stream = False

    def _read_loop(self, segmenter: GestureSegmenter) -> None:
        assert self._proc and self._proc.stdout
        # readline (not ``for line in``) avoids the iterator's read-ahead buffering,
        # so each event is parsed as soon as it arrives.
        for line in iter(self._proc.stdout.readline, ""):
            segmenter.feed_line(line)

    def _handle_session_start(self) -> None:
        if self._use_stream:
            # Stamp host time at finger-down. The previous gesture's 'after' frame
            # is now bounded by this press -> finalize it (avoids contamination).
            ts_down = time.monotonic()
            prev = self._stream_pending
            self._stream_pending = None
            if prev is not None:
                self._emit_q.put((prev, ts_down))
            self._cur_down_ts = ts_down
            return
        # Screencap path: capture the "before" frame off the parser thread, into a
        # holder bound to THIS session; the finalizer joins the thread and reads the
        # holder, so a fast subsequent gesture can never overwrite this before frame.
        holder = {"png": None}
        thread = threading.Thread(target=self._capture_into, args=(holder,), daemon=True)
        self._pending_before = (thread, holder)
        thread.start()

    def _capture_into(self, holder: dict) -> None:
        try:
            holder["png"] = self.capturer.capture_png_bytes(self.device_id)
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("record: before-screenshot failed: %s", exc)
            holder["png"] = None

    def _handle_gesture(self, event: GestureEvent) -> None:
        if self._use_stream:
            # Stash at lift; emit is deferred to the next session-start (or stop)
            # so the 'after' frame can be bounded by the next press.
            self._stream_pending = {"event": event, "down_ts": self._cur_down_ts,
                                    "up_ts": time.monotonic()}
            return
        pending = self._pending_before
        self._pending_before = None
        threading.Thread(
            target=self._finalize_gesture, args=(event, pending), daemon=True
        ).start()

    def _emit_worker(self) -> None:
        """Serialized emit: pick this gesture's before/after frames from the stream
        buffer and fire the callback, preserving gesture order."""
        while True:
            task = self._emit_q.get()
            if task is None:
                return
            pending, next_down_ts = task
            try:
                self._emit_stream_gesture(pending, next_down_ts)
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("record: stream emit failed: %s", exc)

    def _emit_stream_gesture(self, pending: Dict, next_down_ts) -> None:
        event = pending["event"]
        down_ts = pending["down_ts"]
        before = self.frame_stream.frame_before(down_ts) if down_ts is not None else None
        after = self.frame_stream.frame_after(pending["up_ts"], self.STREAM_SETTLE_MS, next_down_ts)
        anchor = self._crop_anchor(before, event.down_point) if before else None
        images = {"before_png": before, "after_png": after, "anchor_png": anchor}
        if self._on_gesture_cb:
            self._on_gesture_cb(event, images)

    def _finalize_gesture(self, event: GestureEvent, pending) -> None:
        before = None
        if pending:
            thread, holder = pending
            thread.join(timeout=2.0)  # ensure the before-screenshot landed
            before = holder["png"]
        time.sleep(self.AFTER_SETTLE_MS / 1000)
        try:
            after = self.capturer.capture_png_bytes(self.device_id)
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("record: after-screenshot failed: %s", exc)
            after = None

        anchor = self._crop_anchor(before, event.down_point) if before else None
        images = {"before_png": before, "after_png": after, "anchor_png": anchor}
        if self._on_gesture_cb:
            self._on_gesture_cb(event, images)

    def _crop_anchor(self, png: bytes, center: Tuple[int, int]) -> Optional[bytes]:
        try:
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(png))
            half = self.ANCHOR_SIZE // 2
            cx, cy = center
            box = (max(cx - half, 0), max(cy - half, 0),
                   min(cx + half, img.width), min(cy + half, img.height))
            buf = io.BytesIO()
            img.crop(box).save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("record: anchor crop failed: %s", exc)
            return None
