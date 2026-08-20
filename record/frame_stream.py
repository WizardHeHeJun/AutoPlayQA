"""Ring-buffered scrcpy frame source for the gesture recorder.

The recorder needs each gesture's *pre-press* and *post-settle* frames without
the tap-glow / next-step contamination that a per-gesture ``screencap`` suffers.
This keeps the last few seconds of host-time-stamped frames in a ring buffer and
picks from it by host monotonic time (scrcpy frames carry no device timestamp).

Transport reuses the project's bundled scrcpy v3.1 server (the same jar/version
as ``perception.scrcpy_stream``) so there is a single server artifact in the
repo; only the *buffering* differs from that module's latest-frame pool.

Best-effort: any failure (PyAV missing, server won't launch, socket error)
leaves :meth:`start` returning False so the recorder falls back to screencap.
"""
from __future__ import annotations

import atexit
import random
import socket
import subprocess
import threading
import time
import weakref
from collections import deque
from typing import Deque, List, Optional, Tuple

from core import adb_daemon, windows_job
from perception.scrcpy_stream import (
    DEFAULT_SERVER_JAR,
    DEVICE_JAR_PATH,
    SCRCPY_VERSION,
)


# -- Pure frame-pick logic (unit-testable, no device / no av) ---------------------
#
# A buffer is a time-ordered list of (host_monotonic_ts, payload). The recorder
# stamps host time at each gesture's down/up (getevent is read in real time), and
# frames are stamped with host arrival time -- so picking by host time needs no
# device<->host clock mapping (scrcpy frames carry no device timestamp).

def pick_before(buffer: List[Tuple[float, object]], down_ts: float):
    """The pre-press frame: the last frame captured at or before the touch-down.
    Such a frame predates any tap highlight/glow. Falls back to the earliest
    buffered frame if all are newer (buffer rotated past it)."""
    chosen = None
    for fts, item in buffer:
        if fts <= down_ts:
            chosen = item
        else:
            break
    if chosen is None and buffer:
        chosen = buffer[0][1]
    return chosen


def pick_after(buffer: List[Tuple[float, object]], up_ts: float,
               settle_s: float, next_down_ts: Optional[float] = None):
    """The post-settle frame reflecting THIS step: the latest frame at or after
    ``up_ts + settle`` but strictly before the next gesture's down (so it is not
    contaminated by the next step). If nothing settled in time (next press came
    too fast), returns the last frame before the next down -- the best available."""
    target = up_ts + settle_s
    chosen = None
    last_before_next = None
    for fts, item in buffer:
        if next_down_ts is not None and fts >= next_down_ts:
            break
        last_before_next = item
        if fts >= target:
            chosen = item
    return chosen if chosen is not None else last_before_next


# -- Interpreter-exit safety net ---------------------------------------------------
#
# Mirrors ScrcpyStreamPool's atexit hook: a caller that forgets stop() (or an
# unhandled exception on the way out) would otherwise leave the adb client and
# the device-side scrcpy server running. Weak references so a dropped stream is
# still garbage-collectable; stop() de-registers.
_active_streams: "weakref.WeakSet[ScrcpyFrameStream]" = weakref.WeakSet()


def _stop_active_streams() -> None:
    for stream in list(_active_streams):
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001 - one bad stream must not skip the rest
            stream._warn("atexit cleanup failed (%s)", exc)


atexit.register(_stop_active_streams)


class ScrcpyFrameStream:
    """Continuous low-overhead frame source via the bundled scrcpy v3.1 server.

    A background thread decodes the raw H.264 stream with PyAV and keeps the last
    ``BUFFER_SECONDS`` of frames (JPEG-compressed, host-monotonic-stamped) in a
    ring buffer. The recorder picks each gesture's pre-press / post-settle frame
    from the buffer instead of issuing a per-gesture ``screencap``.
    """

    BUFFER_SECONDS = 2.5
    MAX_FPS = 30
    JPEG_QUALITY = 90
    CONNECT_TIMEOUT_S = 8.0
    FIRST_FRAME_TIMEOUT_S = 4.0

    def __init__(self, device_id: str, logger=None, max_fps: int = MAX_FPS,
                 bit_rate: int = 8_000_000, max_size: int = 0):
        self.device_id = device_id
        self.logger = logger
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._buf: Deque[Tuple[float, bytes]] = deque()
        self._lock = threading.Lock()
        self._running = False
        self._available = False

    def available(self) -> bool:
        return self._available

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> bool:
        """Push + launch the server, connect, and confirm frames decode. Returns
        True on success; on any failure cleans up and returns False (-> fallback)."""
        try:
            import av  # noqa: F401
            import cv2  # noqa: F401
        except ImportError:
            self._warn("PyAV / OpenCV not installed; falling back to screencap")
            return False
        import os
        if not os.path.isfile(DEFAULT_SERVER_JAR):
            self._warn("scrcpy server jar missing (%s); falling back", DEFAULT_SERVER_JAR)
            return False
        try:
            scid = f"{random.getrandbits(31):08x}"
            self._adb(["push", DEFAULT_SERVER_JAR, DEVICE_JAR_PATH])
            # No blanket "forward --remove-all" here: that wipes every port
            # forward on the shared adb server, including ones held by other
            # concurrent sessions/tools (e.g. a task run's own scrcpy frame
            # stream). tcp:0 below always lets adb allocate a fresh free
            # local port, so there is nothing of ours to collide with or
            # clean up in advance; stop() already removes only this stream's
            # own port (tcp:{self._port}) once it is known.
            # tcp:0 lets adb pick a free local port and print it.
            out = self._adb(["forward", "tcp:0", f"localabstract:scrcpy_{scid}"])
            self._port = int(out.strip().splitlines()[-1])
            # The daemon must already exist before the long-lived client below is
            # bound to the kill-on-close job (see core/adb_daemon.py).
            adb_daemon.ensure_adb_daemon()
            self._proc = subprocess.Popen(
                ["adb", "-s", self.device_id, "shell",
                 f"CLASSPATH={DEVICE_JAR_PATH}",
                 "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
                 f"scid={scid}",
                 "log_level=warn",
                 "audio=false",
                 "control=false",
                 "tunnel_forward=true",
                 # raw H.264 only; the server's leading dummy byte is the
                 # connection-readiness probe (see _connect_when_ready).
                 "send_device_meta=false",
                 "send_codec_meta=false",
                 "send_frame_meta=false",
                 f"max_fps={self.max_fps}",
                 f"video_bit_rate={self.bit_rate}",
                 f"max_size={self.max_size}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # A hard kill of this process never runs stop() / atexit; the job
            # object is what keeps this client from surviving as an orphan.
            windows_job.bind(self._proc)
            _active_streams.add(self)
            self._sock = self._connect_when_ready()
            self._running = True
            self._thread = threading.Thread(target=self._decode_loop, daemon=True)
            self._thread.start()
            if not self._wait_first_frame(self.FIRST_FRAME_TIMEOUT_S):
                raise RuntimeError("no frames decoded within timeout")
            self._available = True
            self._info("scrcpy frame stream up (max_fps=%d)", self.max_fps)
            return True
        except Exception as exc:  # noqa: BLE001
            self._warn("start failed (%s); falling back to screencap", exc)
            self.stop()
            return False

    def stop(self) -> None:
        _active_streams.discard(self)  # nothing left for the atexit hook to close
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._port is not None:
            try:
                self._adb(["forward", "--remove", f"tcp:{self._port}"])
            except Exception:  # noqa: BLE001
                pass
        self._proc = self._sock = self._thread = None
        self._available = False

    def _connect_when_ready(self) -> socket.socket:
        # `adb forward` accepts the local TCP connection immediately and only then
        # dials the device-side abstract socket, so a successful connect() proves
        # nothing. The server's leading dummy 0x00 byte is the readiness signal.
        deadline = time.monotonic() + self.CONNECT_TIMEOUT_S
        while True:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError("scrcpy server exited before a connection")
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", self._port), timeout=1.0)
                sock.settimeout(1.0)
                if sock.recv(1) == b"\x00":
                    sock.settimeout(2.0)
                    return sock
            except OSError:
                pass
            if sock is not None:
                sock.close()
            if time.monotonic() > deadline:
                raise RuntimeError("scrcpy server did not come up in time")
            time.sleep(0.25)

    def _wait_first_frame(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._buf:
                    return True
            time.sleep(0.05)
        return False

    # -- decode loop --------------------------------------------------------------

    def _decode_loop(self) -> None:
        import av
        import cv2
        import numpy as np  # noqa: F401

        codec = av.CodecContext.create("h264", "r")
        try:
            while self._running:
                try:
                    chunk = self._sock.recv(1 << 16)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                for pkt in codec.parse(chunk):
                    for frame in codec.decode(pkt):
                        bgr = frame.to_ndarray(format="bgr24")
                        ok, jpg = cv2.imencode(
                            ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY])
                        if not ok:
                            continue
                        ts = time.monotonic()
                        with self._lock:
                            self._buf.append((ts, jpg.tobytes()))
                            while self._buf and ts - self._buf[0][0] > self.BUFFER_SECONDS:
                                self._buf.popleft()
        except Exception as exc:  # noqa: BLE001
            self._warn("decode loop ended (%s)", exc)

    # -- picking (returns PNG bytes, matching the screencap path's format) --------

    def frame_before(self, down_ts: float) -> Optional[bytes]:
        with self._lock:
            jpg = pick_before(list(self._buf), down_ts)
        return self._jpg_to_png(jpg)

    def frame_after(self, up_ts: float, settle_ms: int,
                    next_down_ts: Optional[float] = None) -> Optional[bytes]:
        with self._lock:
            jpg = pick_after(list(self._buf), up_ts, settle_ms / 1000.0, next_down_ts)
        return self._jpg_to_png(jpg)

    @staticmethod
    def _jpg_to_png(jpg: Optional[bytes]) -> Optional[bytes]:
        if not jpg:
            return None
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        ok, png = cv2.imencode(".png", img)
        return png.tobytes() if ok else None

    # -- helpers ------------------------------------------------------------------

    def _adb(self, args) -> str:
        cmd = ["adb", "-s", self.device_id, *args]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"adb {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def _info(self, msg, *a):
        if self.logger:
            self.logger.info("frame_stream: " + msg, *a)

    def _warn(self, msg, *a):
        if self.logger:
            self.logger.warning("frame_stream: " + msg, *a)
