"""Rolling on-device network capture (tcpdump) for protocol-level evidence.

The "bug-moment protocol snapshot" channel of the flight recorder: while a task
runs the device captures its own traffic with tcpdump, split into segments so
"the last minute of packets before a problem" is always available as a real
pcap for later multi-channel bug analysis (video + logcat + timeline + pcap).
No PC-side proxy and no game-side protocol log — the packets come straight off
the device, so TLS payloads stay opaque but timing, endpoints and frame sizes
are all there.

This mirrors RollingScreenRecorder's four-stage, engine-driven lifecycle (no
background thread on our side):
  start()   - probe tcpdump, clean leftovers, launch the first segment.
  tick()    - called between task steps: rotate when the segment is old enough
              (tcpdump has no self time-limit, so we stop -> start to segment).
  collect() - on a finding: finalize the current segment, pull not-yet-pulled
              segments into the run folder, resume. Returns the local pcap paths.
  stop()    - flush + finalize (SIGINT/SIGTERM), delete device-side files.

Everything here is opt-in and best-effort evidence. It needs root + a tcpdump
binary on the device; when the probe fails, or any adb/tcpdump call fails, the
recorder latches off for the rest of the run with a single warning and every
method becomes a no-op — packet capture is a bonus, never a reason to break a
task or drop a finding.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from core import adb_daemon, windows_job
from core.adb_timeout import adb_timeout_s

DEFAULT_SEGMENT_S = 60
DEFAULT_KEEP_SEGMENTS = 2
# 262144 (256 KiB) is tcpdump's own "whole packet" sentinel: capture full
# payloads, not just headers. Bug analysis for this QA tool wants the complete
# frames (sizes, sequencing, plaintext of unencrypted protocols); the extra
# on-device bytes are cheap next to the rolling MP4 video already being kept.
# Set `snaplen: 96` in config for header-only capture on constrained ROMs.
DEFAULT_SNAPLEN = 262144
# adb wireless (5555) and the local adb server (5037) would otherwise flood the
# capture with our own control traffic — exclude them by default so the pcap is
# game traffic, not the harness talking to the device.
DEFAULT_BPF_FILTER = "not port 5555 and not port 5037"
DEFAULT_TCPDUMP_PATH = "tcpdump"
DEFAULT_SU_MODE = "auto"  # auto | su | direct
SU_MODES = ("auto", "su", "direct")

STOP_SETTLE_S = 0.4  # let tcpdump flush the pcap buffer to disk after the signal

# Unlike screenrecord (which has --time-limit, see SEGMENT_HARD_LIMIT_S in
# screen_recorder), tcpdump runs until it is signalled. If this process is killed
# hard, the local adb client dies with it but the device-side root tcpdump keeps
# capturing forever and fills storage. So every segment is wrapped in the
# device's own `timeout`: a margin past the segment length, so normal rotation
# (tick -> _stop_current/_start_segment, also on collect()) always happens well
# before the self-kill, and the self-kill only ever fires when nobody is left to
# rotate. A late tick just sees the process gone and starts the next segment.
CAPTURE_HARD_LIMIT_MARGIN_S = 120

# /data/local/tmp is writable by a root-spawned tcpdump on ROMs where /sdcard is
# a FUSE mount tcpdump (running as root) cannot write through.
DEVICE_DIR = "/data/local/tmp"
FILE_PREFIX = "ga_pcap_"


class RollingPcapRecorder:
    """Segmented on-device tcpdump capture, pulled into findings on demand."""

    def __init__(self, logger, segment_s: int = DEFAULT_SEGMENT_S,
                 keep_segments: int = DEFAULT_KEEP_SEGMENTS, snaplen: int = DEFAULT_SNAPLEN,
                 bpf_filter: str = DEFAULT_BPF_FILTER, tcpdump_path: str = DEFAULT_TCPDUMP_PATH,
                 su_mode: str = DEFAULT_SU_MODE):
        self.logger = logger
        self.segment_s = segment_s
        self.keep_segments = keep_segments
        self.snaplen = snaplen
        self.bpf_filter = (bpf_filter or "").strip()
        self.tcpdump_path = tcpdump_path or DEFAULT_TCPDUMP_PATH
        self.su_mode = su_mode if su_mode in SU_MODES else DEFAULT_SU_MODE
        self.device_id: str = ""
        self._proc: Optional[subprocess.Popen] = None
        self._segments: List[str] = []  # device paths, oldest first
        self._pulled: Dict[str, str] = {}  # device path -> local path
        self._seq = 0
        self._segment_started = 0.0
        self._broken = False
        self._su_prefix: Optional[str] = None  # resolved "su" or "direct" after probe

    def start(self, device_id: str) -> None:
        self.device_id = device_id
        self._segments = []
        self._pulled = {}
        self._seq = 0
        self._broken = False
        self._su_prefix = None
        if not self._preflight():
            return  # _broken set + warned inside; every method now no-ops
        # A previous process killed hard leaves its root tcpdump running until
        # its own `timeout` fires; sweep it so this run's capture is the only one
        # writing (its leftover files are removed on the next line anyway).
        if not self._adb_shell(self._sweep_command()):
            self.logger.debug(
                "pcap recorder: no leftover tcpdump to sweep (or pkill/killall absent)"
            )
        self._adb_shell(f"rm -f {DEVICE_DIR}/{FILE_PREFIX}*.pcap")
        self._start_segment()

    def tick(self) -> None:
        """Rotate segments; called between task steps (cheap when nothing to do)."""
        if self._broken or self._proc is None:
            return
        if self._proc.poll() is not None:
            # tcpdump ended on its own — either its `timeout` self-kill fired
            # (ticks stalled longer than the hard limit) or it died; either way
            # the segment file is finalized on device, so begin the next one.
            self._start_segment()
            return
        if time.monotonic() - self._segment_started >= self.segment_s:
            self._stop_current()
            self._start_segment()

    def collect(self, target_dir) -> List[str]:
        """Finalize + pull recent segments into target_dir, then resume capture.

        Returns local pcap paths (oldest first). Segments already pulled for an
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
                self.logger.warning("pcap recorder: pull failed for %s", device_path)
        self._start_segment()
        recent = [self._pulled[p] for p in self._segments if p in self._pulled]
        return recent[-(self.keep_segments + 1):]

    def stop(self) -> None:
        if self._broken:
            self._proc = None
            return
        self._stop_current()
        self._adb_shell(f"rm -f {DEVICE_DIR}/{FILE_PREFIX}*.pcap")
        self._proc = None

    # ---------- internals ----------

    def _preflight(self) -> bool:
        """Probe whether the device can run tcpdump; latch off if not.

        Honors su_mode: `auto` tries root (`su -c tcpdump`) then a direct
        tcpdump, `su`/`direct` try only that path. Resolving the working mode
        here means the capture and kill commands wrap the same way. Any adb
        failure counts as "unavailable", never an exception to the caller.
        """
        modes = {"auto": ["su", "direct"], "su": ["su"], "direct": ["direct"]}[self.su_mode]
        for mode in modes:
            if self._tcpdump_available(mode):
                if not self._timeout_available(mode):
                    # Without the self-limit wrapper a hard kill of this process
                    # would leave an unbounded root tcpdump filling the device.
                    # Packet evidence is a bonus; that residue is not acceptable.
                    self._broken = True
                    self.logger.warning(
                        "pcap recorder unavailable (device has no usable `timeout` via "
                        "su_mode=%s, so a capture could not self-limit); packet evidence "
                        "disabled for this run", mode,
                    )
                    return False
                self._su_prefix = mode
                return True
        self._broken = True
        self.logger.warning(
            "pcap recorder unavailable (no runnable tcpdump via su_mode=%s); "
            "packet evidence disabled for this run", self.su_mode,
        )
        return False

    def _tcpdump_available(self, mode: str) -> bool:
        inner = f"{self.tcpdump_path} --version"
        command = f"su -c '{inner}'" if mode == "su" else inner
        result = self._adb_capture(["shell", command])
        if result is None:
            return False
        combined = ((result.stdout or "") + (result.stderr or "")).lower()
        # Some tcpdump builds print the version banner to stderr and exit
        # non-zero, so accept the banner text too — but match the *banner*
        # ("libpcap" / "tcpdump version"), not a bare "tcpdump", so a
        # "tcpdump: not found" shell error is correctly read as unavailable.
        return result.returncode == 0 or "libpcap" in combined or "tcpdump version" in combined

    def _timeout_available(self, mode: str) -> bool:
        """Probe the device's `timeout` (toybox, Android 6+) in the resolved mode.

        Probed rather than assumed because the capture command depends on it for
        its self-kill; running it wrapped the same way the capture will be
        wrapped is what makes the answer meaningful.
        """
        inner = "timeout 5 echo ok"
        command = f"su -c '{inner}'" if mode == "su" else inner
        result = self._adb_capture(["shell", command])
        return result is not None and "ok" in (result.stdout or "")

    def hard_limit_s(self) -> int:
        """Seconds the device-side capture may run before killing itself."""
        return self.segment_s + CAPTURE_HARD_LIMIT_MARGIN_S

    def _capture_command(self, device_path: str) -> str:
        # `timeout` sends SIGTERM, which tcpdump handles by flushing and closing
        # the pcap — a self-killed segment is still a readable file.
        inner = (
            f"timeout {self.hard_limit_s()} "
            f"{self.tcpdump_path} -U -s {self.snaplen} -w {device_path}"
        )
        if self.bpf_filter:
            inner += f" {self.bpf_filter}"
        # Wrap in su -c '<inner>' so the whole tcpdump invocation (with its BPF
        # words) reaches the device as one command argument.
        return f"su -c '{inner}'" if self._su_prefix == "su" else inner

    def _kill_command(self) -> str:
        # SIGINT (-2) then SIGTERM (-15) let tcpdump flush its buffer; pkill and
        # killall availability varies by ROM, so try both. A root-spawned
        # tcpdump can only be signalled from root, so wrap in su when needed.
        kill = (
            "pkill -2 tcpdump || killall -2 tcpdump || "
            "pkill -15 tcpdump || killall -15 tcpdump"
        )
        return f"su -c '{kill}'" if self._su_prefix == "su" else kill

    def _sweep_command(self) -> str:
        # Startup-only leftover sweep: -9 because there is no file of ours worth
        # flushing (leftovers get deleted right after), and a wedged capture must
        # not survive the sweep.
        sweep = "pkill -9 tcpdump || killall -9 tcpdump"
        return f"su -c '{sweep}'" if self._su_prefix == "su" else sweep

    def _start_segment(self) -> None:
        device_path = f"{DEVICE_DIR}/{FILE_PREFIX}{self._seq}.pcap"
        self._seq += 1
        try:
            # Warm the daemon up from an unbound process: this client outlives the
            # call and is bound to the kill-on-close job below, so it must not be
            # the one that forks the machine-wide adb daemon (core/adb_daemon.py).
            adb_daemon.ensure_adb_daemon()
            self._proc = subprocess.Popen(
                ["adb", "-s", self.device_id, "shell", self._capture_command(device_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Rebound per segment: every rotation spawns a fresh client.
            windows_job.bind(self._proc)
        except Exception as exc:
            self._broken = True
            self._proc = None
            self.logger.warning("pcap recorder unavailable, packet evidence disabled: %s", exc)
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
        self._adb_shell(self._kill_command())
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self.logger.warning("pcap recorder: tcpdump did not exit cleanly")
        time.sleep(STOP_SETTLE_S)

    def _adb_shell(self, command: str) -> bool:
        return self._adb(["shell", command])

    def _adb(self, args: List[str]) -> bool:
        try:
            result = subprocess.run(
                ["adb", "-s", self.device_id] + args,
                check=False, capture_output=True, encoding="utf-8",
                errors="ignore", timeout=adb_timeout_s(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("pcap recorder adb failed: %s", exc)
            return False
        return result.returncode == 0

    def _adb_capture(self, args: List[str]):
        """Like _adb but returns the CompletedProcess (or None) for output probing."""
        try:
            return subprocess.run(
                ["adb", "-s", self.device_id] + args,
                check=False, capture_output=True, encoding="utf-8",
                errors="ignore", timeout=adb_timeout_s(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("pcap recorder probe failed: %s", exc)
            return None
