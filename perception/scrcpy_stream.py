"""Persistent scrcpy video stream as a screenshot source.

Pushes the bundled scrcpy-server jar to the device, starts it over adb with a
video-only raw H.264 stream (no audio, no control socket, no frame metadata),
and decodes frames locally with PyAV. Grabbing "a screenshot" then means
returning the latest decoded frame -- tens of milliseconds instead of a full
`screencap` round trip per frame.

Trade-offs vs `screencap` (why this is opt-in, config `capture.backend`):
- H.264 is lossy; pixel-diff / blank-screen thresholds see slightly noisy
  pixels compared to the exact RGBA `screencap` returns.
- A stream + decoder thread + adb forward must stay alive per device; any
  failure falls back to `screencap` (handled by ScreenshotCapturer).
- The device-side encoder only emits frames when the screen changes; the
  latest frame is still the current screen content, just not re-encoded.
"""
from __future__ import annotations

import atexit
import random
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

from core import adb_daemon, windows_job

if TYPE_CHECKING:
    from PIL import Image

# The server validates that this exactly matches the jar's build version.
SCRCPY_VERSION = "3.1"
DEFAULT_SERVER_JAR = str(
    Path(__file__).resolve().parent.parent / "vendor" / f"scrcpy-server-v{SCRCPY_VERSION}"
)
DEVICE_JAR_PATH = "/data/local/tmp/ga_scrcpy_server.jar"

CONNECT_TIMEOUT_S = 8.0
# Cold start measured at ~0.6s to the first decoded frame (1080x2400), so 5s is
# ~8x headroom: past it the encoder is not producing, which is what a
# locked/asleep screen looks like from here.
FIRST_FRAME_TIMEOUT_S = 5.0
# Steady state: a live stream has already latched a frame, so this wait returns
# instantly on the healthy path and only bites when the decoder is wedged.
# ~2x a raw screencap round trip (0.57s) — past that, falling back is cheaper
# than waiting for the fast backend.
FRAME_TIMEOUT_S = 1.5
# How long the decoder blocks in one recv() before re-checking the stop flag.
# The socket used to be left fully blocking (settimeout(None)): a device that
# emits no frames parked that thread in an OS call forever, and close() could
# only get rid of it by yanking the socket out from under it (WinError 10038).
RECV_TIMEOUT_S = 1.0
# Give a decoder parked in recv() ~3 timeout windows to notice the stop flag.
THREAD_JOIN_TIMEOUT_S = 3.0
# terminate() -> wait -> kill() -> wait for the adb client that hosts the server.
PROC_EXIT_TIMEOUT_S = 2.0
# Draining a dead server's output must not wait for EOF forever (see
# _read_server_output).
SERVER_OUTPUT_TIMEOUT_S = 2.0
# A caller may queue this long behind another thread's stream transition. A
# healthy transition (jar already pushed + forward + connect + first frame) is
# ~1.3s measured, so this is ~6x headroom; beyond it a screencap answers in
# ~0.6s, making the wait strictly worse than the fallback it is blocking.
POOL_LOCK_TIMEOUT_S = 8.0
# Read-only "is the screen even on?" probe used in the timeout log only.
DISPLAY_PROBE_TIMEOUT_S = 2.0
MAX_CONSECUTIVE_FAILURES = 2

# AVColorSpace / AVColorTransferCharacteristic values, see libavutil/pixfmt.h.
COLORSPACE_BT601 = 6  # AVCOL_SPC_SMPTE170M
COLOR_TRC_UNSPECIFIED = 2
COLOR_PRIMARIES_UNSPECIFIED = 2
# Matrices swscale can actually convert; anything else (YCgCo, BT2020_CL,
# ICtCp, ...) makes sws_scale_frame bail out with AVERROR(ENOSYS).
CONVERTIBLE_COLORSPACES = frozenset({0, 1, 2, 4, 5, 6, 7, 9})


class ScrcpyStreamError(RuntimeError):
    """Stream could not be started / produced no frame; caller should fall back."""


class ScrcpyStreamDisabled(ScrcpyStreamError):
    """Stream failed repeatedly and is latched off for this device."""


class ScrcpyFrameTimeout(ScrcpyStreamError):
    """The stream is up but produced no frame in time (asleep screen, wedged encoder).

    A distinct type only so the pool can attach the one diagnostic that is
    actually relevant here — the device's display state — without paying for an
    adb round trip on failures where the screen is beside the point (missing
    jar, server refused to start).
    """


def describe_display_state(device_id: str) -> str:
    """Best-effort one-line answer to "is this device's screen even on?".

    A locked / sleeping device connects and hands over the readiness byte just
    fine and then never emits a frame, so the timeout log would otherwise read
    as a bare "no frame" with no clue whose fault it is. This turns that into a
    QA lead.

    Read-only on purpose: waking the screen is the *caller's* decision (a task
    step, a tool call), never something the eyes do behind its back. Never
    raises, never blocks past DISPLAY_PROBE_TIMEOUT_S, and only ever runs on a
    failure path.
    """
    try:
        result = subprocess.run(
            ["adb", "-s", device_id, "shell",
             "dumpsys power | grep -E 'mWakefulness=|mScreenOn='"],
            check=False, capture_output=True, text=True,
            timeout=DISPLAY_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return "display state unknown"
    match = re.search(r"mWakefulness=(\w+)", result.stdout or "")
    if match:
        return f"device wakefulness={match.group(1)}"
    match = re.search(r"mScreenOn=(\w+)", result.stdout or "")
    if match:
        return f"device screen_on={match.group(1)}"
    return "display state unknown"


def describe_exception(exc: BaseException) -> str:
    """Render an exception with its type and errno.

    PyAV wraps FFmpeg return codes in OSError subclasses whose message is the
    useless "Error number -129 occurred"; the class name plus errno is what
    actually identifies the failure when reading a log after the fact.
    """
    errno = getattr(exc, "errno", None)
    suffix = f", errno {errno}" if errno is not None else ""
    return f"{type(exc).__module__}.{type(exc).__name__}: {exc}{suffix}"


def sanitize_color_tags(frame) -> None:
    """Drop color metadata that swscale refuses to convert.

    Some device encoders stamp nonsense into the H.264 SPS VUI: one gaming
    phone ROM (Android 13) writes matrix_coefficients=8 (YCgCo) and
    transfer_characteristics=10 (LOG_SQRT) while emitting perfectly ordinary
    limited-range YUV. FFmpeg 8's rewritten swscale honours those tags and has
    no such conversion path, so every frame.to_image() fails with
    AVERROR(ENOSYS) (-129) and the whole stream gets latched off.

    Clearing the tags reproduces what swscale did before the rewrite: unknown
    matrices are read as BT.601 and transfer/primaries are ignored. The color
    range tag is genuine and kept.
    """
    if frame.colorspace not in CONVERTIBLE_COLORSPACES:
        frame.colorspace = COLORSPACE_BT601
    frame.color_trc = COLOR_TRC_UNSPECIFIED
    frame.color_primaries = COLOR_PRIMARIES_UNSPECIFIED


class _DeviceStream:
    """One scrcpy server + video socket + decoder thread for one device."""

    def __init__(self, logger, device_id: str, server_jar: str,
                 max_fps: int, bit_rate: int, max_size: int):
        self.logger = logger
        self.device_id = device_id
        self.server_jar = server_jar
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self.alive = False
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._frame_ready = threading.Event()
        self._lock = threading.Lock()
        self._latest = None  # av.VideoFrame
        # Latched once this device's frames turn out to carry color metadata
        # swscale can't convert, so later frames are fixed up up front instead
        # of raising and retrying per frame (see sanitize_color_tags).
        self._color_fixup = False

    def start(self) -> None:
        if not Path(self.server_jar).is_file():
            raise ScrcpyStreamError(f"scrcpy server jar not found: {self.server_jar}")
        scid = f"{random.getrandbits(31):08x}"
        self._adb(["push", self.server_jar, DEVICE_JAR_PATH])
        # tcp:0 lets adb pick a free local port and print it
        out = self._adb(["forward", "tcp:0", f"localabstract:scrcpy_{scid}"])
        self._port = int(out.strip().splitlines()[-1])
        server_cmd = [
            "adb", "-s", self.device_id, "shell",
            f"CLASSPATH={DEVICE_JAR_PATH}",
            "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            f"scid={scid}",
            "log_level=warn",
            "audio=false",
            "control=false",
            "tunnel_forward=true",
            # raw H.264 only; keep the dummy byte as a connection-readiness probe
            "send_device_meta=false",
            "send_codec_meta=false",
            "send_frame_meta=false",
            f"max_fps={self.max_fps}",
            f"video_bit_rate={self.bit_rate}",
            f"max_size={self.max_size}",
        ]
        # Warm the daemon up from an unbound process first: this client lives for
        # the whole run and is bound to the kill-on-close job below, so it must
        # not be the one that forks the machine-wide adb daemon (see
        # core/adb_daemon.py).
        adb_daemon.ensure_adb_daemon()
        self._proc = subprocess.Popen(
            server_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        # A hard kill of this process never runs close(); the job object is what
        # keeps this client from surviving as an orphan.
        windows_job.bind(self._proc)
        self._sock = self._connect_when_ready()
        self.alive = True
        self._thread = threading.Thread(
            target=self._decode_loop, name=f"scrcpy-{self.device_id}", daemon=True
        )
        self._thread.start()

    def _adb(self, args) -> str:
        cmd = ["adb", "-s", self.device_id] + args
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise ScrcpyStreamError(f"adb {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def _connect_when_ready(self) -> socket.socket:
        # `adb forward` accepts the TCP connection immediately and only then
        # dials the device-side abstract socket, so a successful connect()
        # proves nothing. The server's dummy byte is the readiness signal.
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while True:
            if self._proc is not None and self._proc.poll() is not None:
                raise ScrcpyStreamError(f"scrcpy server exited: {self._read_server_output()}")
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", self._port), timeout=1.0)
                sock.settimeout(1.0)
                if sock.recv(1) == b"\x00":
                    # Bounded from here on, never settimeout(None): the decoder
                    # has to come back regularly to see the stop flag, or a
                    # device that stops emitting frames leaves it parked in an
                    # OS call that only a socket yank can end (see close()).
                    sock.settimeout(RECV_TIMEOUT_S)
                    return sock
            except OSError:
                pass
            if sock is not None:
                sock.close()
            if time.monotonic() > deadline:
                raise ScrcpyStreamError("scrcpy server did not come up in time")
            time.sleep(0.25)

    def _read_server_output(self) -> str:
        """Drain a dead server client's output without risking a blocked read.

        `proc.stdout.read()` waits for EOF, i.e. for *every* handle on the write
        end to close — and on Windows a forked adb daemon can hold one. That
        turned "the server died, tell me why" into an unbounded wait taken while
        the pool lock is held, which is how a single bad stream start could hang
        every later capture. communicate() gives up instead.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return ""
        try:
            out, _ = proc.communicate(timeout=SERVER_OUTPUT_TIMEOUT_S)
        except (subprocess.SubprocessError, OSError, ValueError):
            return "(server output unavailable)"
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="ignore")
        return (out or "").strip()[:500]

    def _decode_loop(self) -> None:
        import av

        # Local handle: close() clears self._sock so nothing here can trip over
        # it turning into None mid-loop.
        sock = self._sock
        codec = av.CodecContext.create("h264", "r")
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(1 << 16)
                except socket.timeout:
                    # Idle stream (static screen, or a display that is off and
                    # feeding the encoder nothing): keep waiting, but come back
                    # so close() can end this thread cleanly.
                    continue
                if not data:
                    break
                for packet in codec.parse(data):
                    for frame in codec.decode(packet):
                        with self._lock:
                            self._latest = frame
                        self._frame_ready.set()
        except OSError as exc:
            # Socket teardown and PyAV decode errors both land here (av wraps
            # FFmpeg codes in OSError subclasses). Waiters already fail over to
            # screencap; log what killed the stream so the next run can tell a
            # decoder problem apart from our own close(), which yanks the socket
            # out from under this recv on purpose.
            log = self.logger.debug if self._stop.is_set() else self.logger.warning
            log(
                "scrcpy decode loop for %s stopped (%s)",
                self.device_id, describe_exception(exc),
            )
        finally:
            self.alive = False
            self._frame_ready.set()  # unblock any waiter so it can fail fast

    def get_frame(self, timeout: float = FIRST_FRAME_TIMEOUT_S) -> "Image.Image":
        if not self._frame_ready.wait(timeout):
            raise ScrcpyFrameTimeout(
                f"no frame from scrcpy stream within timeout ({timeout:g}s)"
            )
        if not self.alive:
            # A stale last frame may predate whatever killed the stream; for a
            # QA tool a fresh screencap fallback beats silently wrong evidence.
            raise ScrcpyStreamError("scrcpy stream ended")
        with self._lock:
            frame = self._latest
        if frame is None:
            raise ScrcpyStreamError("scrcpy stream produced no frame")
        return self._to_image(frame)

    def _to_image(self, frame) -> "Image.Image":
        """YUV -> RGB, working around encoders that mis-tag their color space.

        The conversion is tried as-is first, so a well-tagged stream pays
        nothing. A device whose frames swscale refuses (see sanitize_color_tags)
        gets its tags dropped and the conversion retried once; from then on the
        fix-up is applied up front rather than through an exception per frame.
        """
        if not self._color_fixup:
            try:
                return frame.to_image()
            except Exception as exc:  # noqa: BLE001 - retry once with tags dropped
                self.logger.warning(
                    "scrcpy frame conversion failed for %s (%s); "
                    "retrying with the encoder's color tags dropped",
                    self.device_id, describe_exception(exc),
                )
                self._color_fixup = True
        sanitize_color_tags(frame)
        return frame.to_image()

    def close(self) -> None:
        """Tear the stream down completely: socket, decoder thread, adb client, forward.

        Every step is bounded and every step runs even if an earlier one
        misbehaves — this is the path a *repeated* timeout-and-retry cycle takes,
        so anything left behind here accumulates one decoder thread, one adb
        client and one forwarded port per retry.
        """
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            # The decoder wakes from recv() within RECV_TIMEOUT_S and sees the
            # stop flag; joining is what proves it is gone rather than assuming.
            thread.join(THREAD_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self.logger.warning(
                    "scrcpy decoder thread for %s still alive after %.1fs",
                    self.device_id, THREAD_JOIN_TIMEOUT_S,
                )
        self._close_proc()
        if self._port is not None:
            try:
                self._adb(["forward", "--remove", f"tcp:{self._port}"])
            except (ScrcpyStreamError, subprocess.SubprocessError, OSError):
                pass
            self._port = None
        self.alive = False

    def _close_proc(self) -> None:
        """Stop the adb client hosting the device-side server, then release its pipe."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=PROC_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=PROC_EXIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    self.logger.warning(
                        "scrcpy server client for %s ignored terminate+kill",
                        self.device_id,
                    )
        # stdout=PIPE is never drained on the happy path; closing it here keeps
        # a retry loop from leaking one pipe handle per attempt.
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass


class ScrcpyStreamPool:
    """Lazily keeps one stream per device; latches a device off after repeated failures."""

    def __init__(self, logger, server_jar: Optional[str] = None,
                 max_fps: int = 30, bit_rate: int = 8_000_000, max_size: int = 0):
        self.logger = logger
        self.server_jar = server_jar or DEFAULT_SERVER_JAR
        self.max_fps = max_fps
        self.bit_rate = bit_rate
        self.max_size = max_size
        self._streams: Dict[str, _DeviceStream] = {}
        self._failures: Dict[str, int] = {}
        self._disabled: set = set()
        # The pool is shared between a background task run and foreground MCP
        # tool calls, which are no longer serialized by the server's event loop.
        # Starting/closing a stream mutates several dicts plus the device-side
        # server, so those transitions are serialized here. Every wait inside is
        # bounded (adb 15s / connect 8s / first frame 5s) *and* the acquire
        # itself is bounded (POOL_LOCK_TIMEOUT_S): a plain `with self._lock`
        # meant one wedged holder turned every later capture into an unbounded
        # wait with nothing in the log — a screenshot that never returns.
        self._lock = threading.Lock()
        # Guards the failure counters, which are also touched from callers that
        # could not get the pool lock. Always the innermost lock.
        self._failure_lock = threading.Lock()
        atexit.register(self.close)

    def get_frame(self, device_id: str, timeout: Optional[float] = None) -> "Image.Image":
        """Latest decoded frame, or raise so the caller falls back to screencap.

        `timeout` defaults to FIRST_FRAME_TIMEOUT_S when this call has to start
        the stream and to the shorter FRAME_TIMEOUT_S when it reuses a running
        one.
        """
        if device_id in self._disabled:
            # Checked before the lock: a latched-off device must fail instantly,
            # not queue behind whatever transition is in flight.
            raise ScrcpyStreamDisabled(
                f"scrcpy stream disabled for {device_id} after "
                f"{MAX_CONSECUTIVE_FAILURES} consecutive failures"
            )
        if not self._lock.acquire(timeout=POOL_LOCK_TIMEOUT_S):
            # Not a stream failure of this device's own making, but from the
            # caller's seat it is indistinguishable — and counting it is what
            # keeps a permanently wedged pool from charging every later capture
            # POOL_LOCK_TIMEOUT_S before it falls back. A single successful grab
            # resets the counter, so ordinary contention never latches anything.
            self._note_failure(
                device_id,
                ScrcpyStreamError(
                    f"scrcpy pool busy for more than {POOL_LOCK_TIMEOUT_S:g}s"
                ),
            )
            raise ScrcpyStreamError(
                f"scrcpy stream pool busy for more than {POOL_LOCK_TIMEOUT_S:g}s"
            )
        try:
            return self._get_frame_locked(device_id, timeout)
        finally:
            self._lock.release()

    def _get_frame_locked(self, device_id: str, timeout: Optional[float]) -> "Image.Image":
        if device_id in self._disabled:
            raise ScrcpyStreamDisabled(
                f"scrcpy stream disabled for {device_id} after "
                f"{MAX_CONSECUTIVE_FAILURES} consecutive failures"
            )
        try:
            stream = self._streams.get(device_id)
            if stream is None or not stream.alive:
                if stream is not None:
                    stream.close()
                stream = _DeviceStream(
                    self.logger, device_id, self.server_jar,
                    self.max_fps, self.bit_rate, self.max_size,
                )
                # Registered before start() so a start that raises half way
                # through still has its adb client / forwarded port reclaimed by
                # close_device below instead of leaking one per retry.
                self._streams[device_id] = stream
                stream.start()
                self.logger.info(
                    "scrcpy stream started for %s (port %s)", device_id, stream._port
                )
                wait_s = FIRST_FRAME_TIMEOUT_S if timeout is None else timeout
            else:
                wait_s = FRAME_TIMEOUT_S if timeout is None else timeout
            image = stream.get_frame(wait_s)
        except Exception as exc:  # noqa: BLE001 - every failure feeds the same latch
            self.close_device(device_id)
            self._note_failure(device_id, exc)
            raise
        with self._failure_lock:
            self._failures[device_id] = 0
        return image

    def _note_failure(self, device_id: str, exc: BaseException) -> None:
        """Count one failed grab and latch the device off once there are enough.

        The single place failures are recorded, so the timeout paths added for
        asleep devices feed the *existing* fallback contract (screencap for this
        call, latch off after MAX_CONSECUTIVE_FAILURES) instead of growing a
        parallel one.
        """
        if isinstance(exc, ScrcpyFrameTimeout):
            # The one failure where "is the screen even on?" is the question:
            # a locked/asleep device connects fine and then emits nothing.
            self.logger.warning(
                "scrcpy stream for %s: %s (%s); using screencap for this capture",
                device_id, exc, describe_display_state(device_id),
            )
        with self._failure_lock:
            count = self._failures.get(device_id, 0) + 1
            self._failures[device_id] = count
            latched = count >= MAX_CONSECUTIVE_FAILURES
            if latched:
                self._disabled.add(device_id)
        if latched:
            self.logger.warning(
                "scrcpy stream for %s failed %d times; latching it off", device_id, count
            )

    def close_device(self, device_id: str) -> None:
        stream = self._streams.pop(device_id, None)
        if stream is not None:
            stream.close()

    def close(self) -> None:
        for device_id in list(self._streams):
            self.close_device(device_id)
