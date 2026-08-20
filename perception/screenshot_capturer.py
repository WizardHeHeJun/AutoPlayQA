from __future__ import annotations

import io
import os
import re
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from core.adb_timeout import AdbTimeout, adb_timed_out, adb_timeout_s
from core.logger import log_event

if TYPE_CHECKING:
	from PIL import Image

# Pillow's default compress_level (6) costs several times the encode time of
# level 1 for a marginal size win; screenshots are throwaway QA evidence.
PNG_COMPRESS_LEVEL = 1

# How long a capture may queue behind another thread's one-time stream init.
# That init legitimately runs the onnxruntime/rapidocr cold load (~12.6s
# measured), so the bound has to clear it with room to spare — but it must
# exist: a `with self._stream_init_lock` meant a warmup that never finished
# (the documented onnxruntime-vs-av deadlock) hung every capture in the
# process with nothing in the log. Timing out just means this call uses
# screencap; it never touches the stream, so the warmup-before-stream ordering
# stays intact.
STREAM_INIT_TIMEOUT_S = 30.0
# How long the *holder* of that lock may spend inside the warmup itself.
#
# Bounding the acquire above only ever protected the threads queueing behind
# the init; the one thread that actually runs the warmup had no bound at all —
# and that thread is always the process's first capture, so "the first frame
# anyone asks for" was the one call that could block forever. Measured on a
# real bench: 1.1s warm, 174.6s cold (2026-08-19 15:11:03 -> 15:13:57), and
# once observed never to return at all (an MCP classify_scene call sat 1800s
# with not one line in the log). The warmup is a C-extension init that cannot
# be interrupted, so it is run on a daemon thread and *waited for* with this
# deadline instead; past it the capture just uses screencap.
STREAM_WARMUP_TIMEOUT_S = 30.0


class ScreenshotCapturer:
	def __init__(self, logger, output_dir: str = "outputs/screenshots", capture_config: dict | None = None,
			stream_warmup=None):
		self.logger = logger
		self.output_dir = Path(output_dir)
		self.output_dir.mkdir(parents=True, exist_ok=True)
		self._raw_capture_ok = True
		capture_config = capture_config or {}
		# scrcpy frame stream is the default capture path (~tens of ms/frame vs
		# ~0.5s for a screencap round trip); set backend: screencap to opt out.
		# Any stream failure still falls back to screencap per call, and latches
		# the device off after repeated failures (see _try_stream).
		self._scrcpy_enabled = capture_config.get("backend", "scrcpy") != "screencap"
		self._scrcpy_config = capture_config.get("scrcpy", {}) or {}
		# Called once before the first stream starts. Entry points pass
		# OcrEngine.ensure_loaded: onnxruntime's first init deadlocks if an av
		# decoder thread is already running, so it must happen stream-first.
		self._stream_warmup = stream_warmup
		self._stream_pool = None
		self._stream_off: set = set()  # devices where the stream latched off
		# One capturer is shared by a background task run and by foreground MCP
		# tool calls, so the lazy pool init can be entered from two threads at
		# once. Serializing it keeps the warmup-then-stream order intact: an av
		# decoder thread running while onnxruntime does its first init deadlocks
		# on Windows, which is exactly what a double init would risk.
		self._stream_init_lock = threading.Lock()
		# The warmup runs once, on its own daemon thread, so a call that never
		# comes back cannot take the caller with it (see _run_stream_warmup).
		self._warmup_thread: "threading.Thread | None" = None
		self._warmup_done = threading.Event()
		self._warmup_error: "BaseException | None" = None
		# Only the first caller pays the bounded wait; once it has expired,
		# later captures check the flag and go straight to screencap instead of
		# each paying STREAM_WARMUP_TIMEOUT_S again.
		self._warmup_waited = False
		# Per-backend call counters (see stats()). Plain dict arithmetic with no
		# lock on purpose: this is observation, and a lost increment under
		# concurrent capture costs an approximate count, while a lock on the
		# hottest path in the project would cost real time. Never read for a
		# decision — only logged.
		self._stats: Dict[str, Dict[str, int]] = {}

	@property
	def stream_enabled(self) -> bool:
		"""True when the scrcpy (lossy H.264) stream is the active default backend.

		Lets evidence-sensitive callers (findings) know a captured frame may not
		be pixel-exact, so they can grab an extra `exact=True` screencap.
		"""
		return self._scrcpy_enabled

	def capture_image(self, device_id: str) -> "Image.Image":
		"""Capture the screen as a PIL Image (RGB).

		In-memory consumers (OCR, pixel diff, blank detection) should prefer
		this over capture_png_bytes: the raw path hands pixels straight to the
		caller, skipping a PNG encode + decode round trip per frame.
		"""
		from PIL import Image

		started = time.perf_counter()
		image = self._try_stream(device_id)
		if image is not None:
			return self._note_capture("scrcpy", device_id, started, image)
		if self._raw_capture_ok:
			try:
				return self._note_capture(
					"screencap_raw", device_id, started, self._capture_via_raw(device_id)
				)
			except AdbTimeout as exc:
				png = self._png_after_raw_timeout(device_id, exc)
				return self._note_capture(
					"screencap_png", device_id, started,
					Image.open(io.BytesIO(png)).convert("RGB"),
				)
			except Exception as exc:  # noqa: BLE001 - any parse/decode issue: fall back once
				self.logger.warning("raw screencap failed (%s); falling back to PNG mode", exc)
				self._raw_capture_ok = False
		return self._note_capture(
			"screencap_png", device_id, started,
			Image.open(io.BytesIO(self._capture_via_png(device_id))).convert("RGB"),
		)

	def capture_png_bytes(self, device_id: str, exact: bool = False) -> bytes:
		"""Capture the screen as PNG bytes (for persisting or sending over MCP).

		Prefers raw `screencap` over `screencap -p`: device-side PNG encoding
		costs ~1.8s for a 1080x2400 frame, while transferring the larger raw
		RGBA buffer over adb and encoding it locally is ~4x faster end to end.
		Falls back to device PNG if the raw header can't be parsed (older ROMs).

		exact=True skips the scrcpy stream and forces the screencap path, so the
		bytes are lossless RGBA rather than a lossy H.264 frame — used for QA
		finding evidence that must be pixel-accurate even when the stream backend
		is the default.
		"""
		started = time.perf_counter()
		if not exact:
			image = self._try_stream(device_id)
			if image is not None:
				return self._note_capture("scrcpy", device_id, started, self._encode_png(image))
		if self._raw_capture_ok:
			try:
				return self._note_capture(
					"screencap_raw", device_id, started,
					self._encode_png(self._capture_via_raw(device_id)),
				)
			except AdbTimeout as exc:
				return self._note_capture(
					"screencap_png", device_id, started,
					self._png_after_raw_timeout(device_id, exc),
				)
			except Exception as exc:  # noqa: BLE001 - any parse/decode issue: fall back once
				self.logger.warning("raw screencap failed (%s); falling back to PNG mode", exc)
				self._raw_capture_ok = False
		return self._note_capture(
			"screencap_png", device_id, started, self._capture_via_png(device_id)
		)

	# ---------- instrumentation ----------

	def _note_capture(self, backend: str, device_id: str, started: float, payload):
		"""Count + log one successful capture, returning `payload` untouched.

		Called at the return point of whichever backend actually produced the
		frame, so the fallback chain reads exactly as before — and the log says
		which link answered, which is the first question when a replay is slow.
		"""
		ms = int((time.perf_counter() - started) * 1000)
		stat = self._stats.setdefault(backend, {"n": 0, "ms": 0})
		stat["n"] += 1
		stat["ms"] += ms
		log_event(self.logger, "capture", backend=backend, ms=ms, device=device_id)
		return payload

	def stats(self) -> Dict[str, Dict[str, float]]:
		"""Snapshot of per-backend capture counters since the last reset.

		`{backend: {"n": calls, "ms": total, "avg_ms": mean}}`. Approximate under
		concurrent capture (see `_stats`); TaskEngine prints it once per run.
		"""
		return {
			backend: {
				"n": stat["n"],
				"ms": stat["ms"],
				"avg_ms": round(stat["ms"] / stat["n"], 1) if stat["n"] else 0.0,
			}
			# list() 快照：并发 capture 首次出现新 backend 时 setdefault 会增 key，
			# 迭代活 dict 会 RuntimeError；拷贝后只剩计数近似误差，不会崩。
			for backend, stat in list(self._stats.items())
		}

	def reset_stats(self) -> None:
		"""Zero the counters (a run scopes them, see TaskEngine.run)."""
		self._stats = {}

	def _try_stream(self, device_id: str) -> "Image.Image | None":
		"""Grab a frame from the scrcpy stream backend, or None to use screencap.

		The default backend; set `capture.backend: screencap` to opt out. Every
		failure falls back to the screencap chain for this call; once the pool
		latches a device off (repeated failures) we stop asking it altogether.
		"""
		if not self._scrcpy_enabled or device_id in self._stream_off:
			return None
		from perception.scrcpy_stream import ScrcpyStreamDisabled, describe_exception

		try:
			if self._stream_pool is None and not self._init_stream_pool():
				return None
			return self._stream_pool.get_frame(device_id)
		except ScrcpyStreamDisabled:
			self._stream_off.add(device_id)
			self.logger.warning("scrcpy stream latched off for %s; using screencap", device_id)
		except Exception as exc:  # noqa: BLE001 - stream is best-effort; screencap is the contract
			# Log the exception type and errno: PyAV's own message ("Error
			# number -129 occurred") says nothing about what actually broke.
			self.logger.warning(
				"scrcpy frame grab failed (%s); using screencap", describe_exception(exc)
			)
		return None

	def _init_stream_pool(self) -> bool:
		"""Create the stream pool once (warmup first). False = use screencap now.

		The lazy init is entered from several threads (task run + MCP tool calls
		+ frame monitor), so the acquire is what queues them. It is bounded:
		waiting forever on a warmup that wedged is how one stuck thread takes
		every capture in the process down with it, and there is a perfectly good
		answer — screencap — sitting one line below.
		"""
		if not self._stream_init_lock.acquire(timeout=STREAM_INIT_TIMEOUT_S):
			self.logger.warning(
				"scrcpy stream init still busy after %.0fs; using screencap",
				STREAM_INIT_TIMEOUT_S,
			)
			return False
		try:
			if self._stream_pool is None:
				from perception.scrcpy_stream import ScrcpyStreamPool

				if not self._run_stream_warmup():
					return False
				self._stream_pool = ScrcpyStreamPool(
					self.logger,
					server_jar=self._scrcpy_config.get("server_jar"),
					max_fps=self._scrcpy_config.get("max_fps", 30),
					bit_rate=self._scrcpy_config.get("bit_rate", 8_000_000),
					max_size=self._scrcpy_config.get("max_size", 0),
				)
		finally:
			self._stream_init_lock.release()
		return True

	def _run_stream_warmup(self) -> bool:
		"""Run the one-time pre-stream warmup, bounded. False = screencap for now.

		The warmup (OcrEngine.ensure_loaded) is load-bearing and stays exactly
		where it was — *before* the first stream ever starts — because
		onnxruntime's first init deadlocks against a live av decoder thread on
		Windows. What changes is that it can no longer hold the caller hostage:
		it runs on a daemon thread and this waits STREAM_WARMUP_TIMEOUT_S for it.

		Returning False means "not warm yet, so do not start the stream" — the
		caller falls back to screencap for this capture, which is the existing
		contract for every other stream failure. The warmup keeps running in the
		background, so a later capture picks the stream up once it lands; only
		the first caller pays the wait, and never more than once.
		"""
		if self._stream_warmup is None:
			return True
		if self._warmup_thread is None:
			self._warmup_thread = threading.Thread(
				target=self._warmup_worker, name="capture-stream-warmup", daemon=True
			)
			self._warmup_thread.start()
		# Poll-only (0s) once the first wait has already expired: otherwise a
		# warmup that legitimately takes minutes would charge every capture in
		# the process the full timeout before falling back.
		wait_s = 0 if self._warmup_waited else STREAM_WARMUP_TIMEOUT_S
		if not self._warmup_done.wait(wait_s):
			if not self._warmup_waited:
				self._warmup_waited = True
				self.logger.warning(
					"scrcpy stream warmup (OCR preload) still running after %.0fs; "
					"using screencap until it lands",
					STREAM_WARMUP_TIMEOUT_S,
				)
			return False
		if self._warmup_error is not None:
			# Warming up is what makes starting the stream safe, so a failed
			# warmup means the stream stays off for good — logged once, and the
			# screencap chain answers every capture from here on.
			return False
		return True

	def _warmup_worker(self) -> None:
		"""Body of the warmup thread; never raises, always releases the waiter."""
		try:
			self._stream_warmup()
		except Exception as exc:  # noqa: BLE001 - the stream is best effort
			self._warmup_error = exc
			self.logger.warning(
				"scrcpy stream warmup failed (%s); staying on screencap", exc
			)
		finally:
			self._warmup_done.set()

	def _capture_via_png(self, device_id: str) -> bytes:
		cmd = ["adb", "-s", device_id, "exec-out", "screencap", "-p"]
		result = self._run_adb(cmd)
		if result.returncode != 0:
			stderr = result.stderr.decode("utf-8", errors="ignore").strip()
			raise RuntimeError(f"Failed to capture screenshot: {stderr}")
		return result.stdout

	def _capture_via_raw(self, device_id: str) -> "Image.Image":
		from PIL import Image

		cmd = ["adb", "-s", device_id, "exec-out", "screencap"]
		result = self._run_adb(cmd)
		if result.returncode != 0 or len(result.stdout) < 16:
			stderr = result.stderr.decode("utf-8", errors="ignore").strip()
			raise RuntimeError(f"raw screencap failed: {stderr or 'short output'}")
		raw = result.stdout
		width, height, _fmt = struct.unpack("<III", raw[:12])
		pixels = width * height * 4
		# Header is 12 bytes (legacy) or 16 bytes (newer Android adds colorspace).
		for header in (12, 16):
			if len(raw) - header >= pixels:
				body = raw[header:header + pixels]
				break
		else:
			raise RuntimeError(f"raw payload too small for {width}x{height}")
		image = Image.frombuffer("RGBA", (width, height), body, "raw", "RGBA", 0, 1)
		return image.convert("RGB")

	def _png_after_raw_timeout(self, device_id: str, exc: AdbTimeout) -> bytes:
		"""Raw screencap timed out: try `screencap -p` once, keeping the chain.

		The raw path ships an uncompressed ~10MB RGBA buffer over adb, so a slow
		or flaky link can blow the timeout while the ~1MB device-encoded PNG
		still gets through — the fallback must stay available. If `-p` works,
		raw is genuinely too heavy for this link and gets latched off; if it
		times out as well the transport itself is wedged and the AdbTimeout
		propagates, leaving the raw fast path armed for the next call. Either
		way the caller gets a frame or an error, never an unbounded wait.
		"""
		self.logger.warning("%s; retrying once with screencap -p", exc)
		png = self._capture_via_png(device_id)
		self._raw_capture_ok = False
		self.logger.warning("raw screencap is too slow for this link; using screencap -p from now on")
		return png

	@staticmethod
	def _run_adb(cmd) -> "subprocess.CompletedProcess":
		"""Run a capture adb command with the shared timeout.

		`screencap` is a plain request/response round trip (~0.5-2.4s healthy),
		so exceeding the timeout means adb or the device is stuck. Raising
		AdbTimeout keeps the caller from blocking forever — the whole point of
		this path, since the screencap chain is exactly what a latched-off
		scrcpy stream falls back to.
		"""
		timeout = adb_timeout_s()
		try:
			return subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
		except subprocess.TimeoutExpired as exc:
			raise adb_timed_out(cmd, timeout) from exc

	@staticmethod
	def _encode_png(image: "Image.Image") -> bytes:
		buf = io.BytesIO()
		image.save(buf, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
		return buf.getvalue()

	def encode_png(self, image: "Image.Image") -> bytes:
		"""PNG-encode an already-captured frame with the capture path's settings.

		Lets a caller that already holds a PIL frame (e.g. a recognition shot)
		persist that exact frame as evidence instead of taking a fresh capture.
		"""
		return self._encode_png(image)

	def get_display_rotation(self, device_id: str) -> int:
		"""Return the current display rotation in degrees (0, 90, 180, 270).

		Returns 0 on any failure so callers always get a safe default.
		"""
		cmd = ["adb", "-s", device_id, "shell", "dumpsys", "display"]
		try:
			result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
		except (FileNotFoundError, subprocess.TimeoutExpired):
			return 0
		for line in result.stdout.splitlines():
			if "mCurrentDisplayRotation" in line or "mRotation" in line:
				m = re.search(r"=\s*(\d)", line)
				if m:
					return int(m.group(1)) * 90
		return 0

	def capture_to_file(self, device_id: str, prefix: str = "screen") -> str:
		return self.save_image(self.capture_image(device_id), device_id, prefix)

	def save_image(self, image: "Image.Image", device_id: str, prefix: str = "screen") -> str:
		"""Persist an already-captured frame; lets callers reuse one capture."""
		ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
		file_path = self.output_dir / f"{prefix}_{device_id}_{ts}.png"
		image.save(file_path, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
		self.logger.info("Saved screenshot: %s", str(file_path))
		return os.fspath(file_path)
