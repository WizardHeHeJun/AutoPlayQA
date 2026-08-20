"""Rolling on-device screen recording (adb screenrecord) for video evidence.

The device records continuously while a task runs; the recording is split into
segments so "the last minute before a problem" is always available as real MP4
(animations, flicker, the crash moment — things discrete frames miss). No
local encoder or new dependency: the device's native screenrecord produces the
files, we only rotate and pull them.

Lifecycle (engine-driven, no background threads on our side):
  start()   - clean leftovers, launch the first segment (subprocess.Popen).
  tick()    - called between task steps: rotate when the segment is old enough,
              prune device files beyond the keep window, restart if the device
              process died (screenrecord self-terminates at --time-limit).
  collect() - on a finding: finalize the current segment, pull not-yet-pulled
              segments into the run folder, resume recording. Returns the
              local mp4 paths covering the recent window.
  stop()    - finalize and delete device-side files (best effort).

Any adb/screenrecord failure disables the recorder for the rest of the run
with one warning — video is evidence, never a reason to break a task. Some
ROMs/DRM surfaces forbid screenrecord entirely; the frame-history fallback in
FindingsRecorder covers those.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from core import adb_daemon, windows_job

DEFAULT_SEGMENT_S = 60
DEFAULT_KEEP_SEGMENTS = 2
DEFAULT_BIT_RATE = 4_000_000
SEGMENT_HARD_LIMIT_S = 180  # screenrecord's own maximum
STOP_SETTLE_S = 0.4  # let screenrecord write the MP4 moov atom after SIGINT

DEVICE_DIR = "/sdcard"
FILE_PREFIX = "ga_rec_"


class RollingScreenRecorder:
    def __init__(self, logger, segment_s: int = DEFAULT_SEGMENT_S,
                 keep_segments: int = DEFAULT_KEEP_SEGMENTS, bit_rate: int = DEFAULT_BIT_RATE):
        self.logger = logger
        self.segment_s = segment_s
        self.keep_segments = keep_segments
        self.bit_rate = bit_rate
        self.device_id: str = ""
        self._proc: Optional[subprocess.Popen] = None
        self._segments: List[str] = []  # device paths, oldest first
        self._pulled: Dict[str, str] = {}  # device path -> local path
        self._seq = 0
        self._segment_started = 0.0
        self._broken = False

    def start(self, device_id: str) -> None:
        self.device_id = device_id
        self._segments = []
        self._pulled = {}
        self._seq = 0
        self._broken = False
        # A previous process that was killed hard leaves its device-side
        # screenrecord running (its --time-limit can still have minutes to go),
        # which would keep writing and fight this run for the encoder. -9 here is
        # fine: those files are leftovers we delete on the next line anyway.
        if not self._adb_shell("pkill -9 screenrecord || killall -9 screenrecord"):
            self.logger.debug(
                "screen recorder: no leftover screenrecord to sweep (or pkill/killall absent)"
            )
        self._adb_shell(f"rm -f {DEVICE_DIR}/{FILE_PREFIX}*.mp4")
        self._start_segment()

    def tick(self) -> None:
        """Rotate segments; called between task steps (cheap when nothing to do)."""
        if self._broken or self._proc is None:
            return
        if self._proc.poll() is not None:
            # screenrecord hit its own time limit (or died); the segment file
            # is finalized on device — just begin the next one.
            self._start_segment()
            return
        if time.monotonic() - self._segment_started >= self.segment_s:
            self._stop_current()
            self._start_segment()

    def collect(self, target_dir) -> List[str]:
        """Finalize + pull recent segments into target_dir, then resume recording.

        Returns local mp4 paths (oldest first). Segments already pulled for an
        earlier finding in the same run are reused, not pulled twice.
        """
        if self._broken:
            return []
        self._stop_current()
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        for device_path in self._segments:
            if device_path in self._pulled:
                continue
            local = target / device_path.rsplit("/", 1)[-1]
            if self._adb(["pull", device_path, str(local)]) and local.is_file():
                self._pulled[device_path] = str(local)
            else:
                self.logger.warning("screen recorder: pull failed for %s", device_path)
        self._start_segment()
        recent = [self._pulled[p] for p in self._segments if p in self._pulled]
        return recent[-(self.keep_segments + 1):]

    def stop(self) -> None:
        self._stop_current()
        self._adb_shell(f"rm -f {DEVICE_DIR}/{FILE_PREFIX}*.mp4")
        self._proc = None

    def _start_segment(self) -> None:
        device_path = f"{DEVICE_DIR}/{FILE_PREFIX}{self._seq}.mp4"
        self._seq += 1
        try:
            # Warm the daemon up from an unbound process: this client outlives the
            # call and is bound to the kill-on-close job below, so it must not be
            # the one that forks the machine-wide adb daemon (core/adb_daemon.py).
            adb_daemon.ensure_adb_daemon()
            self._proc = subprocess.Popen(
                [
                    "adb", "-s", self.device_id, "shell", "screenrecord",
                    "--time-limit", str(SEGMENT_HARD_LIMIT_S),
                    "--bit-rate", str(self.bit_rate),
                    device_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Rebound per segment: every rotation spawns a fresh client.
            windows_job.bind(self._proc)
        except Exception as exc:
            self._broken = True
            self._proc = None
            self.logger.warning("screen recorder unavailable, video evidence disabled: %s", exc)
            return
        self._segments.append(device_path)
        self._segment_started = time.monotonic()
        # Prune beyond the keep window: drop device files nobody will pull.
        while len(self._segments) > self.keep_segments + 1:
            old = self._segments.pop(0)
            if old not in self._pulled:
                self._adb_shell(f"rm -f {old}")

    def _stop_current(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        # SIGINT lets screenrecord finalize the MP4; pkill/killall availability
        # varies by ROM, try both.
        self._adb_shell("pkill -2 screenrecord || killall -2 screenrecord")
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self.logger.warning("screen recorder: screenrecord did not exit cleanly")
        time.sleep(STOP_SETTLE_S)

    def _adb_shell(self, command: str) -> bool:
        return self._adb(["shell", command])

    def _adb(self, args: List[str]) -> bool:
        try:
            result = subprocess.run(
                ["adb", "-s", self.device_id] + args,
                check=False, capture_output=True, encoding="utf-8", errors="ignore", timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("screen recorder adb failed: %s", exc)
            return False
        return result.returncode == 0
