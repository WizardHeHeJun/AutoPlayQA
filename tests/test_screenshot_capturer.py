from __future__ import annotations

import io
import logging
import struct
import threading
import time
from unittest.mock import MagicMock, patch

from PIL import Image

from perception import screenshot_capturer
from perception.screenshot_capturer import ScreenshotCapturer


class FakeProc:
    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def make_raw(width, height, header_bytes=12, fmt=1):
    """A fake `screencap` raw payload: little-endian w/h/fmt header + RGBA body."""
    head = struct.pack("<III", width, height, fmt)
    if header_bytes == 16:
        head += struct.pack("<I", 0)  # colorspace word on newer Android
    body = bytes([10, 20, 30, 255]) * (width * height)
    return head + body


def capturer(tmp_path):
    # scrcpy is the default backend now; these screencap-path tests opt out
    # explicitly so they exercise screencap deterministically (and never reach
    # for a real stream pool / real adb).
    return ScreenshotCapturer(
        logging.getLogger("test"), output_dir=str(tmp_path),
        capture_config={"backend": "screencap"},
    )


def decode(png_bytes):
    return Image.open(io.BytesIO(png_bytes))


def test_raw_capture_returns_valid_png(tmp_path):
    cap = capturer(tmp_path)
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(make_raw(4, 3)),
    ) as run:
        png = cap.capture_png_bytes("dev1")

    # raw mode uses `screencap` without the -p flag
    assert run.call_args.args[0] == ["adb", "-s", "dev1", "exec-out", "screencap"]
    img = decode(png)
    assert img.size == (4, 3)
    assert img.convert("RGB").getpixel((0, 0)) == (10, 20, 30)


def test_raw_capture_handles_16_byte_header(tmp_path):
    cap = capturer(tmp_path)
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(make_raw(5, 5, header_bytes=16)),
    ):
        img = decode(cap.capture_png_bytes("dev1"))
    assert img.size == (5, 5)


def test_falls_back_to_png_when_raw_unparseable(tmp_path):
    cap = capturer(tmp_path)
    # real PNG bytes the device would return from `screencap -p`
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), (1, 2, 3)).save(buf, format="PNG")
    png_payload = buf.getvalue()

    # 1st call: raw mode gets a too-short payload -> raises -> fallback;
    # 2nd call: png mode returns a real PNG.
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        side_effect=[FakeProc(b"\x00" * 8), FakeProc(png_payload)],
    ) as run:
        out = cap.capture_png_bytes("dev1")

    assert decode(out).size == (3, 2)
    assert run.call_count == 2
    assert run.call_args_list[1].args[0][-1] == "-p"  # fallback used screencap -p
    assert cap._raw_capture_ok is False  # later calls skip raw mode


def test_capture_image_raw_returns_image_directly(tmp_path):
    cap = capturer(tmp_path)
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(make_raw(4, 3)),
    ) as run:
        img = cap.capture_image("dev1")

    assert run.call_count == 1
    assert run.call_args.args[0] == ["adb", "-s", "dev1", "exec-out", "screencap"]
    assert isinstance(img, Image.Image)
    assert img.size == (4, 3)
    assert img.getpixel((0, 0)) == (10, 20, 30)


def test_capture_image_falls_back_to_png(tmp_path):
    cap = capturer(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), (1, 2, 3)).save(buf, format="PNG")
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        side_effect=[FakeProc(b"\x00" * 8), FakeProc(buf.getvalue())],
    ):
        img = cap.capture_image("dev1")
    assert img.size == (3, 2)
    assert cap._raw_capture_ok is False


def test_capture_to_file_captures_once(tmp_path):
    cap = capturer(tmp_path)
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(make_raw(4, 3)),
    ) as run:
        path = cap.capture_to_file("dev1")
    assert run.call_count == 1
    assert decode(open(path, "rb").read()).size == (4, 3)


def scrcpy_capturer(tmp_path):
	return ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
	)


def test_scrcpy_backend_serves_frame_without_adb(tmp_path):
	cap = scrcpy_capturer(tmp_path)
	frame = Image.new("RGB", (4, 3), (10, 20, 30))
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch("perception.screenshot_capturer.subprocess.run") as run:
		pool_cls.return_value.get_frame.return_value = frame
		img = cap.capture_image("dev1")
	assert img is frame
	assert run.call_count == 0  # no screencap round trip


def test_scrcpy_backend_png_bytes_encode_locally(tmp_path):
	cap = scrcpy_capturer(tmp_path)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch("perception.screenshot_capturer.subprocess.run") as run:
		pool_cls.return_value.get_frame.return_value = Image.new("RGB", (4, 3), (10, 20, 30))
		png = cap.capture_png_bytes("dev1")
	assert run.call_count == 0
	assert decode(png).size == (4, 3)


def test_scrcpy_stream_error_falls_back_to_screencap(tmp_path):
	cap = scrcpy_capturer(tmp_path)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		) as run:
		pool_cls.return_value.get_frame.side_effect = RuntimeError("stream died")
		img = cap.capture_image("dev1")
	assert img.size == (4, 3)
	assert run.call_args.args[0] == ["adb", "-s", "dev1", "exec-out", "screencap"]


def test_scrcpy_disabled_latches_device_and_stops_asking_pool(tmp_path):
	from perception.scrcpy_stream import ScrcpyStreamDisabled

	cap = scrcpy_capturer(tmp_path)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		pool_cls.return_value.get_frame.side_effect = ScrcpyStreamDisabled("latched")
		cap.capture_image("dev1")
		cap.capture_image("dev1")
	assert pool_cls.return_value.get_frame.call_count == 1
	assert "dev1" in cap._stream_off


def test_stream_warmup_runs_once_before_pool_creation(tmp_path):
	order = []
	cap = ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
		stream_warmup=lambda: order.append("warmup"),
	)
	frame = Image.new("RGB", (4, 3), (10, 20, 30))
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls:
		pool_cls.side_effect = lambda *a, **k: (order.append("pool"), MagicMock(get_frame=lambda *aa, **kk: frame))[1]
		cap.capture_image("dev1")
		cap.capture_image("dev1")
	# onnxruntime must initialize before any decoder thread can exist
	assert order == ["warmup", "pool"]


def test_a_warmup_that_never_returns_falls_back_to_screencap(tmp_path, monkeypatch):
	"""The bug behind the 30-minute MCP hang: the *holder* of the init lock ran
	the warmup with no bound, so the first capture in a process could block
	forever. It must give up and use the screencap chain instead."""
	monkeypatch.setattr(screenshot_capturer, "STREAM_WARMUP_TIMEOUT_S", 0.1)
	stuck = threading.Event()
	cap = ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
		stream_warmup=stuck.wait,  # never returns until the test releases it
	)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		started = time.perf_counter()
		img = cap.capture_image("dev1")
		elapsed = time.perf_counter() - started
	stuck.set()
	assert img.size == (4, 3)          # answered by the screencap fallback
	assert pool_cls.call_count == 0    # stream never started on a cold onnxruntime
	assert elapsed < 5                 # bounded, not "forever"


def test_only_the_first_capture_pays_the_warmup_wait(tmp_path, monkeypatch):
	"""A warmup that is merely slow (174.6s measured cold) must not charge every
	later capture the full timeout before falling back."""
	monkeypatch.setattr(screenshot_capturer, "STREAM_WARMUP_TIMEOUT_S", 0.3)
	stuck = threading.Event()
	cap = ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
		stream_warmup=stuck.wait,
	)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool"), \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		cap.capture_image("dev1")                     # pays the 0.3s wait
		started = time.perf_counter()
		cap.capture_image("dev1")
		second = time.perf_counter() - started
	stuck.set()
	assert second < 0.2  # poll-only from here on


def test_stream_starts_once_a_slow_warmup_finally_lands(tmp_path, monkeypatch):
	"""Timing out is 'not yet', not 'never': the warmup keeps running and the
	stream comes up on a later capture — still warmup-before-pool."""
	monkeypatch.setattr(screenshot_capturer, "STREAM_WARMUP_TIMEOUT_S", 0.1)
	order = []
	release = threading.Event()

	def slow_warmup():
		release.wait(5)
		order.append("warmup")

	cap = ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
		stream_warmup=slow_warmup,
	)
	frame = Image.new("RGB", (4, 3), (10, 20, 30))
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		pool_cls.side_effect = lambda *a, **k: (
			order.append("pool"), MagicMock(get_frame=lambda *aa, **kk: frame)
		)[1]
		cap.capture_image("dev1")          # times out -> screencap
		assert pool_cls.call_count == 0
		release.set()
		cap._warmup_done.wait(5)
		cap.capture_image("dev1")          # warm now -> stream starts
	assert order == ["warmup", "pool"]


def test_a_warmup_that_raises_leaves_the_stream_off_for_good(tmp_path):
	"""Warming up is what makes starting the stream safe, so a failed warmup must
	not be followed by a stream start (onnxruntime-vs-av deadlock)."""
	cap = ScreenshotCapturer(
		logging.getLogger("test"), output_dir=str(tmp_path),
		capture_config={"backend": "scrcpy"},
		stream_warmup=MagicMock(side_effect=RuntimeError("onnx blew up")),
	)
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		assert cap.capture_image("dev1").size == (4, 3)
		assert cap.capture_image("dev1").size == (4, 3)
	assert pool_cls.call_count == 0


def test_screencap_backend_never_touches_stream_pool(tmp_path):
	cap = capturer(tmp_path)  # explicit backend: screencap
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch(
			"perception.screenshot_capturer.subprocess.run",
			return_value=FakeProc(make_raw(4, 3)),
		):
		cap.capture_image("dev1")
	assert pool_cls.call_count == 0


def test_default_backend_uses_stream_pool(tmp_path):
	# No capture_config -> scrcpy is the default; the stream pool is consulted
	# first and screencap is only the fallback.
	cap = ScreenshotCapturer(logging.getLogger("test"), output_dir=str(tmp_path))
	frame = Image.new("RGB", (4, 3), (10, 20, 30))
	with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
		patch("perception.screenshot_capturer.subprocess.run") as run:
		pool_cls.return_value.get_frame.return_value = frame
		img = cap.capture_image("dev1")
	assert img is frame
	assert run.call_count == 0  # served from the stream, no screencap round trip


def test_after_fallback_subsequent_calls_use_png_only(tmp_path):
    cap = capturer(tmp_path)
    cap._raw_capture_ok = False
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (9, 9, 9)).save(buf, format="PNG")
    with patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(buf.getvalue()),
    ) as run:
        cap.capture_png_bytes("dev1")
    assert run.call_count == 1
    assert run.call_args.args[0][-1] == "-p"


# ---------- capture telemetry ----------
#
# The measured hot spot of a replay is the screenshot backend, so every
# successful capture leaves one `EVT capture` line naming the link of the
# fallback chain that answered, and the capturer counts them for the run
# summary TaskEngine prints at _finish.

def evt_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("EVT capture ")]


def test_raw_screencap_capture_is_logged_with_its_backend(tmp_path, caplog):
    cap = capturer(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="test"), patch(
        "perception.screenshot_capturer.subprocess.run",
        return_value=FakeProc(make_raw(4, 3)),
    ):
        cap.capture_image("dev1")

    line = evt_lines(caplog)[0]
    assert "backend=screencap_raw" in line and "device=dev1" in line and " ms=" in line


def test_stream_frames_are_logged_as_the_scrcpy_backend(tmp_path, caplog):
    cap = scrcpy_capturer(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="test"), \
            patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
            patch("perception.screenshot_capturer.subprocess.run") as run:
        pool_cls.return_value.get_frame.return_value = Image.new("RGB", (4, 3))
        cap.capture_image("dev1")

    assert "backend=scrcpy" in evt_lines(caplog)[0]
    assert run.call_count == 0


def test_a_fallback_logs_only_the_backend_that_actually_answered(tmp_path, caplog):
    cap = scrcpy_capturer(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="test"), \
            patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
            patch("perception.screenshot_capturer.subprocess.run",
                  return_value=FakeProc(make_raw(2, 2))):
        pool_cls.return_value.get_frame.side_effect = RuntimeError("stream died")
        cap.capture_image("dev1")

    # One line, and it names screencap — a failed stream attempt is a warning,
    # not a capture.
    assert [line for line in evt_lines(caplog) if "backend=screencap_raw" in line]
    assert len(evt_lines(caplog)) == 1


def test_stats_accumulate_per_backend_and_reset(tmp_path):
    cap = capturer(tmp_path)
    with patch("perception.screenshot_capturer.subprocess.run",
               return_value=FakeProc(make_raw(2, 2))):
        cap.capture_image("dev1")
        cap.capture_png_bytes("dev1")

    stats = cap.stats()
    assert stats["screencap_raw"]["n"] == 2
    assert stats["screencap_raw"]["avg_ms"] == round(
        stats["screencap_raw"]["ms"] / 2, 1
    )

    cap.reset_stats()
    assert cap.stats() == {}


def test_device_png_fallback_is_counted_under_its_own_backend(tmp_path):
    cap = capturer(tmp_path)
    cap._raw_capture_ok = False
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (9, 9, 9)).save(buf, format="PNG")
    with patch("perception.screenshot_capturer.subprocess.run",
               return_value=FakeProc(buf.getvalue())):
        cap.capture_png_bytes("dev1")

    assert list(cap.stats()) == ["screencap_png"]


def test_exact_capture_is_counted_as_screencap_not_stream(tmp_path):
    cap = scrcpy_capturer(tmp_path)
    with patch("perception.scrcpy_stream.ScrcpyStreamPool") as pool_cls, \
            patch("perception.screenshot_capturer.subprocess.run",
                  return_value=FakeProc(make_raw(2, 2))):
        cap.capture_png_bytes("dev1", exact=True)

    # Evidence must stay lossless: exact never consults the stream.
    assert list(cap.stats()) == ["screencap_raw"]
    assert pool_cls.call_count == 0
