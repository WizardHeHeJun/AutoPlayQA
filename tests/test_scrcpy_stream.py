from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from fractions import Fraction
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from perception.scrcpy_stream import (
    COLORSPACE_BT601,
    DEVICE_JAR_PATH,
    ScrcpyFrameTimeout,
    ScrcpyStreamDisabled,
    ScrcpyStreamError,
    ScrcpyStreamPool,
    describe_display_state,
    describe_exception,
    sanitize_color_tags,
)


def make_h264(width=64, height=64, frames=3, color=(10, 200, 30)) -> bytes:
    """Encode a few solid-color frames into a raw annex-b H.264 stream."""
    import av

    enc = av.CodecContext.create("libx264", "w")
    enc.width = width
    enc.height = height
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, 30)
    enc.options = {"tune": "zerolatency"}
    out = b""
    for i in range(frames):
        frame = av.VideoFrame.from_image(Image.new("RGB", (width, height), color))
        frame.pts = i
        for packet in enc.encode(frame):
            out += bytes(packet)
    for packet in enc.encode(None):
        out += bytes(packet)
    return out


class FakeSocket:
    """Serves scripted chunks, then times out per recv (stream idle) until closed.

    Mirrors the real socket after `_connect_when_ready`: a bounded timeout, so
    an idle stream keeps handing control back to the decode loop instead of
    parking it in an OS call forever.
    """

    def __init__(self, chunks, idle_timeout=0.02):
        self._chunks = list(chunks)
        self._closed = threading.Event()
        self._idle_timeout = idle_timeout
        self.shutdown_calls = 0
        self.close_calls = 0

    def recv(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._closed.wait(self._idle_timeout):
            return b""
        raise socket.timeout("timed out")

    def settimeout(self, t):
        pass

    def shutdown(self, how):
        self.shutdown_calls += 1

    def close(self):
        self.close_calls += 1
        self._closed.set()


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class FakeServerProc:
    """The adb shell ... app_process server process: keeps running."""

    def __init__(self):
        self.stdout = MagicMock()
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def communicate(self, timeout=None):
        return b"", b""


def adb_ok(cmd, **kwargs):
    if "forward" in cmd and "--remove" not in cmd:
        return FakeProc(stdout="12345\n")
    return FakeProc()


def pool(jar):
    return ScrcpyStreamPool(logging.getLogger("test"), server_jar=str(jar))


@pytest.fixture
def jar(tmp_path):
    fake_jar = tmp_path / "scrcpy-server-test"
    fake_jar.write_bytes(b"jar")
    return fake_jar


def test_stream_decodes_frames_and_builds_correct_commands(jar):
    h264 = make_h264()
    # dummy readiness byte first, then the video stream split into chunks
    chunks = [b"\x00"] + [h264[i:i + 4096] for i in range(0, len(h264), 4096)]
    server_proc = FakeServerProc()

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok) as run, \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=server_proc) as popen, \
         patch("perception.scrcpy_stream.socket.create_connection", return_value=FakeSocket(chunks)) as connect:
        p = pool(jar)
        image = p.get_frame("dev1")
        p.close()

    assert image.size == (64, 64)
    r, g, b = image.getpixel((32, 32))
    # H.264 is lossy; just require the color to be in the neighborhood
    assert abs(r - 10) < 25 and abs(g - 200) < 25 and abs(b - 30) < 25

    push_cmd = run.call_args_list[0].args[0]
    assert push_cmd[:4] == ["adb", "-s", "dev1", "push"]
    assert push_cmd[-1] == DEVICE_JAR_PATH
    forward_cmd = run.call_args_list[1].args[0]
    assert forward_cmd[3:5] == ["forward", "tcp:0"]
    server_cmd = popen.call_args.args[0]
    assert "app_process" in server_cmd
    assert "tunnel_forward=true" in server_cmd
    assert "control=false" in server_cmd
    assert "audio=false" in server_cmd
    assert connect.call_args.args[0] == ("127.0.0.1", 12345)
    # close() tears down: server terminated + forward removed
    assert server_proc.terminated is True
    remove_cmd = run.call_args_list[-1].args[0]
    assert "--remove" in remove_cmd and "tcp:12345" in remove_cmd


def test_second_frame_reuses_running_stream(jar):
    h264 = make_h264()
    chunks = [b"\x00", h264]

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok) as run, \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()) as popen, \
         patch("perception.scrcpy_stream.socket.create_connection", return_value=FakeSocket(chunks)):
        p = pool(jar)
        p.get_frame("dev1")
        p.get_frame("dev1")
        p.close()

    assert popen.call_count == 1  # one server for both grabs
    assert run.call_args_list[0].args[0][3] == "push"  # pushed only once


def test_repeated_failures_latch_device_off(jar, monkeypatch):
    monkeypatch.setattr("perception.scrcpy_stream.CONNECT_TIMEOUT_S", 0.3)

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()), \
         patch("perception.scrcpy_stream.socket.create_connection", side_effect=OSError("refused")):
        p = pool(jar)
        with pytest.raises(ScrcpyStreamError):
            p.get_frame("dev1")
        with pytest.raises(ScrcpyStreamError):
            p.get_frame("dev1")
        # latched: fails fast without touching adb again
        with pytest.raises(ScrcpyStreamDisabled):
            p.get_frame("dev1")


def test_missing_jar_raises(tmp_path):
    p = ScrcpyStreamPool(logging.getLogger("test"), server_jar=str(tmp_path / "nope"))
    with pytest.raises(ScrcpyStreamError, match="jar not found"):
        p.get_frame("dev1")


class FakeFrame:
    """A decoded frame tagged like the offending encoder: swscale can't convert it.

    Mirrors PyAV: the conversion raises an OSError subclass carrying an errno,
    and only starts working once the bogus color tags are gone.
    """

    def __init__(self, colorspace=8, color_trc=10, color_primaries=2):
        self.colorspace = colorspace
        self.color_trc = color_trc
        self.color_primaries = color_primaries
        self.calls = 0

    def to_image(self):
        self.calls += 1
        if self.colorspace == 8 or self.color_trc == 10:
            raise OSError(129, "Error number -129 occurred")
        return Image.new("RGB", (8, 8))


def stream_with_frame(jar, frame):
    """A started stream whose latest decoded frame is `frame`."""
    from perception.scrcpy_stream import _DeviceStream

    stream = _DeviceStream(logging.getLogger("test"), "dev1", str(jar), 30, 1000, 0)
    stream.alive = True
    stream._latest = frame
    stream._frame_ready.set()
    return stream


def test_unconvertible_color_tags_are_dropped_instead_of_failing(jar):
    frame = FakeFrame()
    stream = stream_with_frame(jar, frame)

    image = stream.get_frame(0.1)

    assert image.size == (8, 8)
    assert frame.calls == 2  # tried as-is, then retried with the tags dropped
    assert frame.colorspace == COLORSPACE_BT601
    assert frame.color_trc == 2 and frame.color_primaries == 2


def test_color_fixup_is_applied_up_front_after_the_first_failure(jar):
    stream = stream_with_frame(jar, FakeFrame())
    stream.get_frame(0.1)

    second = FakeFrame()
    stream._latest = second
    stream.get_frame(0.1)

    assert second.calls == 1  # no repeat exception per frame
    assert second.colorspace == COLORSPACE_BT601


def test_well_tagged_frames_are_not_touched(jar):
    frame = FakeFrame(colorspace=1, color_trc=1, color_primaries=1)
    stream = stream_with_frame(jar, frame)

    stream.get_frame(0.1)

    assert frame.calls == 1
    assert (frame.colorspace, frame.color_trc, frame.color_primaries) == (1, 1, 1)


def test_sanitize_keeps_a_convertible_matrix():
    frame = FakeFrame(colorspace=1, color_trc=10, color_primaries=9)
    sanitize_color_tags(frame)
    assert frame.colorspace == 1  # BT.709 is convertible; only trc/primaries go
    assert frame.color_trc == 2 and frame.color_primaries == 2


def test_conversion_failure_logs_type_and_errno(jar, caplog):
    class Hopeless(FakeFrame):
        def to_image(self):
            self.calls += 1
            raise OSError(129, "Error number -129 occurred")

    stream = stream_with_frame(jar, Hopeless())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(OSError):
            stream.get_frame(0.1)

    assert "OSError" in caplog.text and "errno 129" in caplog.text


def test_real_frame_with_the_bogus_color_tags_converts(jar):
    """End-to-end against the installed swscale, no device needed.

    A frame tagged YCgCo + LOG_SQRT (what some device encoders write) is exactly
    what FFmpeg 8's swscale refuses with AVERROR(ENOSYS); the fix-up has to
    produce a real image out of it.
    """
    import av

    frame = av.VideoFrame(64, 64, "yuv420p")
    frame.colorspace = 8  # AVCOL_SPC_YCGCO
    frame.color_range = 1  # limited
    frame.color_trc = 10  # AVCOL_TRC_LOG_SQRT
    frame.color_primaries = 2

    image = stream_with_frame(jar, frame).get_frame(0.1)
    assert image.size == (64, 64)


def test_describe_exception_includes_type_and_errno():
    text = describe_exception(OSError(129, "Error number -129 occurred"))
    assert "OSError" in text and "errno 129" in text
    assert "no errno" not in describe_exception(ValueError("boom"))


def test_first_frame_wait_is_bounded_not_instant(jar):
    """A frame that lands late still counts: the wait is what gates the grab."""
    from perception.scrcpy_stream import _DeviceStream

    stream = _DeviceStream(logging.getLogger("test"), "dev1", str(jar), 30, 1000, 0)
    stream.alive = True

    def land_later():
        stream._latest = FakeFrame(colorspace=1, color_trc=1)
        stream._frame_ready.set()

    threading.Timer(0.2, land_later).start()
    assert stream.get_frame(3.0).size == (8, 8)

    empty = _DeviceStream(logging.getLogger("test"), "dev2", str(jar), 30, 1000, 0)
    empty.alive = True
    with pytest.raises(ScrcpyStreamError, match="within timeout"):
        empty.get_frame(0.1)


def test_server_exit_reports_its_output(jar, monkeypatch):
    dead = FakeServerProc()
    dead.poll = lambda: 1
    dead.stdout = MagicMock()
    dead.communicate = MagicMock(return_value=(b"ERROR: something broke", b""))

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=dead):
        p = pool(jar)
        with pytest.raises(ScrcpyStreamError, match="something broke"):
            p.get_frame("dev1")
    # bounded drain, never a bare stdout.read() that waits for EOF forever
    assert dead.communicate.call_args.kwargs["timeout"] > 0


def test_server_exit_output_drain_is_bounded(jar):
    """A pipe whose write end a forked adb daemon still holds must not wedge us."""
    dead = FakeServerProc()
    dead.poll = lambda: 1
    dead.communicate = MagicMock(side_effect=subprocess.TimeoutExpired("adb", 2.0))

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=dead):
        p = pool(jar)
        with pytest.raises(ScrcpyStreamError, match="server output unavailable"):
            p.get_frame("dev1")


# ---------- asleep / silent device: the frame wait must stay bounded ----------
#
# A locked or sleeping device accepts the connection and hands over the
# readiness byte, then its encoder emits nothing at all. Before these bounds
# existed that meant a decoder thread parked in a blocking recv() forever and a
# pool lock held with it, so every later capture -- MCP `screenshot` included --
# waited with nothing in the log. Every case here asserts the same contract:
# time out, log it, fall back to screencap, latch off, leave nothing behind.


class SilentSocket:
    """Hands over the readiness byte and then never produces a video frame."""

    def __init__(self, idle_timeout=0.02):
        self._sent_ready = False
        self._idle_timeout = idle_timeout
        self._closed = threading.Event()
        self.recv_calls = 0
        self.shutdown_calls = 0
        self.close_calls = 0

    def recv(self, n):
        if not self._sent_ready:
            self._sent_ready = True
            return b"\x00"
        self.recv_calls += 1
        if self._closed.wait(self._idle_timeout):
            return b""
        raise socket.timeout("timed out")

    def settimeout(self, t):
        pass

    def shutdown(self, how):
        self.shutdown_calls += 1

    def close(self):
        self.close_calls += 1
        self._closed.set()


def silent_device(jar, monkeypatch, first_frame_s=0.15):
    """A pool wired to a device that connects fine and then stays silent."""
    monkeypatch.setattr("perception.scrcpy_stream.FIRST_FRAME_TIMEOUT_S", first_frame_s)
    monkeypatch.setattr("perception.scrcpy_stream.FRAME_TIMEOUT_S", first_frame_s)
    # the display-state hint is a diagnostic, not a device round trip in tests
    monkeypatch.setattr(
        "perception.scrcpy_stream.describe_display_state",
        lambda device_id: "device wakefulness=Asleep",
    )
    return pool(jar)


def test_silent_stream_times_out_instead_of_blocking(jar, monkeypatch, caplog):
    sock = SilentSocket()
    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()), \
         patch("perception.scrcpy_stream.socket.create_connection", return_value=sock):
        p = silent_device(jar, monkeypatch)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ScrcpyFrameTimeout, match="within timeout"):
                p.get_frame("dev1")
        elapsed = time.monotonic() - started

    assert elapsed < 5  # bounded by the frame wait, not by the socket
    # the timeout is a QA lead, so it says so and says what the screen was doing
    assert "no frame" in caplog.text and "wakefulness=Asleep" in caplog.text


def test_frame_timeout_falls_back_to_screencap(tmp_path, monkeypatch):
    """End to end through the capturer: a silent stream still yields a frame."""
    import struct

    from perception.screenshot_capturer import ScreenshotCapturer

    monkeypatch.setattr("perception.scrcpy_stream.FIRST_FRAME_TIMEOUT_S", 0.15)
    monkeypatch.setattr(
        "perception.scrcpy_stream.describe_display_state", lambda d: "device wakefulness=Asleep"
    )
    jar = tmp_path / "scrcpy-server-test"
    jar.write_bytes(b"jar")
    cap = ScreenshotCapturer(
        logging.getLogger("test"), output_dir=str(tmp_path),
        capture_config={"backend": "scrcpy", "scrcpy": {"server_jar": str(jar)}},
    )
    raw = struct.pack("<III", 2, 2, 1) + bytes([7, 8, 9, 255]) * 4

    class Screencap:
        returncode = 0
        stdout = raw
        stderr = b""

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()), \
         patch("perception.scrcpy_stream.socket.create_connection", return_value=SilentSocket()), \
         patch("perception.screenshot_capturer.subprocess.run", return_value=Screencap()) as run:
        image = cap.capture_image("dev1")

    assert image.size == (2, 2)  # screencap answered, the chain held
    assert run.call_args.args[0] == ["adb", "-s", "dev1", "exec-out", "screencap"]


def test_frame_timeouts_count_toward_latch_off(jar, monkeypatch):
    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()), \
         patch("perception.scrcpy_stream.socket.create_connection",
               side_effect=lambda *a, **k: SilentSocket()):
        p = silent_device(jar, monkeypatch)
        with pytest.raises(ScrcpyFrameTimeout):
            p.get_frame("dev1")
        with pytest.raises(ScrcpyFrameTimeout):
            p.get_frame("dev1")
        # the existing latch, reached through the new timeout path
        with pytest.raises(ScrcpyStreamDisabled):
            p.get_frame("dev1")


def test_timed_out_stream_leaves_no_thread_socket_or_forward(jar, monkeypatch):
    """Retrying after a timeout must not stack up decoders / clients / ports."""
    sockets = []
    procs = []

    def new_socket(*a, **k):
        sockets.append(SilentSocket())
        return sockets[-1]

    def new_proc(*a, **k):
        procs.append(FakeServerProc())
        return procs[-1]

    before = set(threading.enumerate())
    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok) as run, \
         patch("perception.scrcpy_stream.subprocess.Popen", side_effect=new_proc), \
         patch("perception.scrcpy_stream.socket.create_connection", side_effect=new_socket):
        p = silent_device(jar, monkeypatch)
        for _ in range(2):
            with pytest.raises(ScrcpyFrameTimeout):
                p.get_frame("dev1")

    assert len(sockets) == 2 and len(procs) == 2
    assert all(s.close_calls == 1 for s in sockets)  # no half-open sockets
    assert all(pr.terminated for pr in procs)  # no orphan adb clients
    # every attempt removed its own forward
    removes = [c.args[0] for c in run.call_args_list if "--remove" in c.args[0]]
    assert len(removes) == 2
    # and no decoder thread outlived its stream
    leftovers = [t for t in threading.enumerate()
                 if t not in before and t.name.startswith("scrcpy-")]
    assert leftovers == []


def test_decoder_thread_stops_on_close_without_yanking_the_socket(jar):
    """The decoder leaves via the stop flag, not via a socket pulled out from under it."""
    from perception.scrcpy_stream import _DeviceStream

    with patch("perception.scrcpy_stream.subprocess.run", side_effect=adb_ok), \
         patch("perception.scrcpy_stream.subprocess.Popen", return_value=FakeServerProc()), \
         patch("perception.scrcpy_stream.socket.create_connection", return_value=SilentSocket()):
        stream = _DeviceStream(logging.getLogger("test"), "dev1", str(jar), 30, 1000, 0)
        stream.start()
        thread = stream._thread
        assert thread.is_alive()
        stream.close()

    assert not thread.is_alive()
    assert stream.alive is False
    assert stream._sock is None and stream._proc is None


def test_pool_lock_timeout_falls_back_instead_of_queueing_forever(jar, monkeypatch):
    """A wedged holder must not turn every later capture into an unbounded wait."""
    monkeypatch.setattr("perception.scrcpy_stream.POOL_LOCK_TIMEOUT_S", 0.1)
    p = pool(jar)
    p._lock.acquire()  # stand in for a thread stuck mid stream transition
    try:
        started = time.monotonic()
        with pytest.raises(ScrcpyStreamError, match="pool busy"):
            p.get_frame("dev1")
        assert time.monotonic() - started < 3
        # counted like any other failure, so a permanently wedged pool latches
        # off rather than charging every capture the full wait
        with pytest.raises(ScrcpyStreamError):
            p.get_frame("dev1")
        with pytest.raises(ScrcpyStreamDisabled):
            p.get_frame("dev1")
    finally:
        p._lock.release()


def test_stream_init_lock_timeout_falls_back_to_screencap(tmp_path, monkeypatch):
    """A warmup that never returns must not take every capture down with it."""
    import struct

    from perception import screenshot_capturer as sc
    from perception.screenshot_capturer import ScreenshotCapturer

    monkeypatch.setattr(sc, "STREAM_INIT_TIMEOUT_S", 0.1)
    cap = ScreenshotCapturer(
        logging.getLogger("test"), output_dir=str(tmp_path),
        capture_config={"backend": "scrcpy"},
    )
    cap._stream_init_lock.acquire()  # a warmup that wedged (onnxruntime deadlock)
    raw = struct.pack("<III", 2, 2, 1) + bytes([7, 8, 9, 255]) * 4

    class Screencap:
        returncode = 0
        stdout = raw
        stderr = b""

    try:
        with patch("perception.screenshot_capturer.subprocess.run",
                   return_value=Screencap()) as run:
            image = cap.capture_image("dev1")
    finally:
        cap._stream_init_lock.release()

    assert image.size == (2, 2)
    assert run.call_args.args[0] == ["adb", "-s", "dev1", "exec-out", "screencap"]


def test_display_state_probe_is_read_only_and_never_raises():
    """It reports, it does not wake anything, and a broken adb is not an error."""
    with patch("perception.scrcpy_stream.subprocess.run") as run:
        run.return_value = FakeProc(stdout="  mWakefulness=Asleep\n")
        assert describe_display_state("dev1") == "device wakefulness=Asleep"
    cmd = run.call_args.args[0]
    assert "dumpsys" in " ".join(cmd)
    # never an input/keyevent: waking the screen is the caller's decision
    assert "input" not in cmd and "keyevent" not in " ".join(cmd)

    with patch("perception.scrcpy_stream.subprocess.run",
               side_effect=subprocess.TimeoutExpired("adb", 2.0)):
        assert describe_display_state("dev1") == "display state unknown"
    with patch("perception.scrcpy_stream.subprocess.run", side_effect=FileNotFoundError()):
        assert describe_display_state("dev1") == "display state unknown"
