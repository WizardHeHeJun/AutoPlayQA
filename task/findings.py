"""Test-finding collection: anomalies observed during a task run, with evidence.

A finding is a QA deliverable (popup branch taken, watchdog text spotted,
crash/ANR in logcat, task failure), not a developer diagnostic — so unlike
DebugTracer this recorder is always on and never gated behind a debug flag.
Evidence (screenshot / ui dump) is captured at record time and written under
output_dir/<date>/<device>/<run_id>/ together with a report.json problem list
and its human-readable twin report.html (self-contained, offline-openable).
Screenshot evidence uses a lossless screencap (exact, bypassing the default
lossy H.264 stream backend); when a transient frame is pinned to the detection
moment it is kept as-is and an exact `screenshot_exact` of the settled state is
added alongside, so a one-flash toast is never traded away for pixel accuracy.

Flight recorder: the engine feeds a rolling timeline (nodes recognized,
actions executed, recoveries taken) and per-step screenshots into the
recorder; every finding then carries the last `history_window_s` seconds of
flow — timeline json + screen frames + an in-game logcat fragment — both as
evidence files and inline (`recent_flow`, `log_excerpt`) so the caller sees
the context without extra reads.

Evidence capture failures degrade to a log warning; recording never breaks a run.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from core.logger import LOGGER_NAME, log_event
from perception.logcat_monitor import select_evidence_lines
from task.report_html import render_report_html

SEVERITIES = ("info", "warning", "error", "critical")

DEFAULT_OUTPUT_DIR = "outputs/findings"
DEFAULT_HISTORY_WINDOW_S = 60.0
DEFAULT_MAX_HISTORY_FRAMES = 12
DEFAULT_LOG_TAIL_LINES = 300
DEFAULT_RETENTION_DAYS = 14

TIMELINE_MAX_EVENTS = 400
INLINE_LOG_LINES = 40
INLINE_FLOW_EVENTS = 12


def prune_old_runs(output_dir, retention_days: int = DEFAULT_RETENTION_DAYS, logger=None) -> int:
    """Delete finding day-folders older than `retention_days` (best effort).

    Findings accumulate one `output_dir/<YYYYMMDD>/<device>/<run_id>/` tree per
    run and are never overwritten, so without a cap the folder grows without
    bound (the videos alone reach gigabytes). This removes whole date folders
    whose date is older than today − retention_days. `retention_days <= 0`
    disables pruning. Returns the number of date folders removed; failures are
    logged and skipped so startup never breaks on a locked file.
    """
    if retention_days <= 0:
        return 0
    base = Path(output_dir)
    if not base.is_dir():
        return 0
    cutoff = date.today() - timedelta(days=retention_days)
    removed = 0
    for child in sorted(base.iterdir()):
        if not child.is_dir() or not re.fullmatch(r"\d{8}", child.name):
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y%m%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            try:
                shutil.rmtree(child)
                removed += 1
            except Exception as exc:
                if logger:
                    logger.warning("Findings prune failed for %s: %s", child, exc)
    if removed and logger:
        logger.info(
            "Findings retention: removed %d day-folder(s) older than %s", removed, cutoff
        )
    return removed


class FindingsRecorder:
    """Accumulates findings for one task run and persists evidence + report."""

    def __init__(self, logger, screenshot_capturer=None, dump_matcher=None,
                 output_dir: str = DEFAULT_OUTPUT_DIR, logcat_monitor=None,
                 history: bool = True, history_window_s: float = DEFAULT_HISTORY_WINDOW_S,
                 max_history_frames: int = DEFAULT_MAX_HISTORY_FRAMES,
                 log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
                 export_dir: Optional[str] = None, screen_recorder=None, pcap_recorder=None,
                 notifiers=None):
        self.logger = logger
        self.capturer = screenshot_capturer
        self.matcher = dump_matcher
        self.logcat_monitor = logcat_monitor
        self.screen_recorder = screen_recorder
        self.pcap_recorder = pcap_recorder
        self.output_dir = Path(output_dir)
        self.history = history
        self.history_window_s = float(history_window_s)
        self.max_history_frames = max_history_frames
        self.log_tail_lines = log_tail_lines
        self.export_dir = export_dir
        # Built by core.notifier.build_notifiers (empty/None = feature off).
        self.notifiers = list(notifiers or [])

        self.device_id: str = ""
        self.task_name: Optional[str] = None
        self.run_dir: Path = self.output_dir
        self.findings: List[Dict] = []
        self.started_at: str = ""
        self._dir_ready = False
        self._timeline: List[Dict] = []
        self._frames: List[Dict] = []
        self._history_broken = False
        self._last_status: Optional[str] = None

    def open_run(self, device_id: str, task_name: Optional[str] = None) -> None:
        """Reset state for a new run; the run directory is created lazily."""
        safe_device = re.sub(r"[^\w.-]", "_", device_id) or "device"
        now = datetime.now()
        run_id = now.strftime("%H%M%S") + "_" + uuid4().hex[:6]
        self.device_id = device_id
        self.task_name = task_name
        self.run_dir = self.output_dir / now.strftime("%Y%m%d") / safe_device / run_id
        self.findings = []
        self.started_at = now.isoformat(timespec="seconds")
        self._dir_ready = False
        self._timeline = []
        self._frames = []
        self._history_broken = False
        self._last_status = None

    def ensure_run_dir(self) -> Path:
        """Create this run's folder now and return it (normally created lazily).

        The engine calls this to park `run.log` next to the future report.json,
        so even a run that produces no findings leaves its step-by-step trace in
        a self-contained folder.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._dir_ready = True
        return self.run_dir

    # ---------- flight recorder feed (engine calls these every step) ----------

    def timeline_tail(self, n: int = 10) -> List[Dict]:
        """The most recent flow events, newest last (shallow copies).

        Same stream the findings' `recent_flow` is cut from, exposed for live
        progress polling (MCP get_run_status) — a caller watching a long run
        should not have to wait for a finding to see what the engine is doing.
        """
        if n <= 0:
            return []
        return [dict(event) for event in self._timeline[-n:]]

    def add_timeline(self, event: str, **detail) -> None:
        """Append one flow event (node recognized / action executed / recovery).

        Single point of collection: every timeline event is mirrored to the log
        at DEBUG, so `run.log` carries the full flow even for events whose
        caller prints no INFO line of its own (the console stays unchanged —
        DEBUG never reaches the console handler).
        """
        self._timeline.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "mono": time.monotonic(),
                "event": event,
                "detail": detail,
            }
        )
        self._mirror_timeline(event, detail)
        if len(self._timeline) > TIMELINE_MAX_EVENTS:
            del self._timeline[: len(self._timeline) - TIMELINE_MAX_EVENTS]

    def _mirror_timeline(self, event: str, detail: Dict) -> None:
        """`EVT timeline event=<event> k=v ...` for one flow event."""
        fields: Dict = {"event": event}
        for key, value in detail.items():
            if value is None:
                continue
            if key == "event":  # the flow event's own name owns that slot
                key = "event_detail"
            if isinstance(value, (str, int, float, bool)):
                fields[key] = value
            elif hasattr(value, "__len__"):
                # Never stringify a big object on this path: a length is enough
                # to tell "5 candidates" from "none".
                fields[f"{key}_len"] = len(value)
        log_event(self.logger or logging.getLogger(LOGGER_NAME), "timeline", **fields)

    def snapshot_history(self) -> None:
        """Buffer one screen frame for the rolling pre-finding history."""
        if not self.history or self._history_broken or self.capturer is None:
            return
        try:
            png = self.capturer.capture_png_bytes(self.device_id)
        except Exception as exc:
            self._history_broken = True
            self.logger.warning("History snapshot failed; history disabled for this run: %s", exc)
            return
        now = time.monotonic()
        stamp = datetime.now().strftime("%H%M%S_%f")
        self._frames.append({"mono": now, "stamp": stamp, "png": png, "path": None})
        cutoff = now - self.history_window_s * 1.5
        while self._frames and (
            self._frames[0]["mono"] < cutoff or len(self._frames) > self.max_history_frames
        ):
            self._frames.pop(0)

    def save_context_image(self, tag: str, image) -> Optional[str]:
        """Park a context frame in the run folder; return its run-relative path.

        For screens worth keeping that are NOT anomalies — chiefly the frame a
        whitelisted-popup sweep decided to click on. Such a sweep clicks the
        live screen unattended, so the frame that justified it has to survive
        even though nothing about it is a finding: this never touches
        `self.findings`, the severity counts or report.json's problem list, it
        only drops a PNG next to them for the timeline to reference.

        Same pinning rule as a finding's evidence — the caller hands in the
        frame the decision was made on, not a re-capture taken afterwards.
        Returns None (never raises) when there is nothing to write or the write
        failed; evidence must not be able to break a run.
        """
        if image is None or self.capturer is None:
            return None
        safe = re.sub(r"[^\w.-]", "_", tag) or "context"
        try:
            png = bytes(image) if isinstance(image, (bytes, bytearray)) else self.capturer.encode_png(image)
            self.ensure_run_dir()
            path = self._write(f"{safe}.png", png)
        except Exception as exc:
            self.logger.warning("Context image '%s' save failed: %s", tag, exc)
            return None
        return self._portable(path)

    # ---------- findings ----------

    def record(self, finding_type: str, severity: str, message: str, node: Optional[str] = None,
               screenshot: bool = True, ui_dump: bool = False, extra: Optional[Dict] = None,
               image=None, force_exact: bool = False) -> Dict:
        """Append a finding, capturing the requested evidence from the device now.

        Besides the on-the-spot screenshot/ui-dump, the finding gets the flight
        recorder context: recent in-game logcat fragment, the last minute of
        flow timeline, and the buffered screen frames from before the problem.

        image: an optional pre-captured frame (PIL Image or PNG bytes) to use as
        the screenshot evidence instead of grabbing a fresh one. The engine
        passes the frame that tripped a transient watchdog so the evidence shows
        the actual toast/dialog, not a re-capture taken after it vanished.

        force_exact: always add the lossless `screenshot_exact` companion to a
        pinned frame, instead of only under a lossy stream backend. For callers
        whose pinned frame is untrustworthy as evidence for reasons the capture
        backend knows nothing about — the monitor sentinel pins downscaled 720p
        monitor frames, so on a screencap backend the default rule would leave
        the shrunken frame as the only record.
        """
        if severity not in SEVERITIES:
            severity = "warning"
        finding: Dict = {
            "seq": len(self.findings) + 1,
            "type": finding_type,
            "severity": severity,
            "node": node,
            "message": message,
            "time": datetime.now().isoformat(timespec="seconds"),
            "evidence": {},
        }
        if extra:
            finding["extra"] = extra

        safe_type = re.sub(r"[^\w-]", "_", finding_type)
        base = f"{finding['seq']:02d}_{safe_type}"
        if screenshot and self.capturer is not None:
            try:
                png = self._evidence_png(image)
                finding["evidence"]["screenshot"] = self._write(f"{base}.png", png)
            except Exception as exc:
                self.logger.warning("Finding evidence screenshot failed: %s", exc)
            # A pinned frame (transient watchdog / branch) preserves a toast that
            # may already be gone, but under the lossy stream backend it isn't
            # pixel-accurate. Add an exact screencap of the settled state so the
            # report carries both: the transient frame and a trustworthy shot.
            # force_exact makes that unconditional for callers whose pinned frame
            # is lossy/downscaled regardless of backend (see the docstring).
            if image is not None and (force_exact or getattr(self.capturer, "stream_enabled", False)):
                try:
                    exact_png = self.capturer.capture_png_bytes(self.device_id, exact=True)
                    finding["evidence"]["screenshot_exact"] = self._write(f"{base}_exact.png", exact_png)
                except Exception as exc:
                    self.logger.warning("Finding exact screenshot failed: %s", exc)
        if ui_dump and self.matcher is not None:
            try:
                xml = self.matcher.dump_ui_xml(self.device_id)
                if xml:
                    finding["evidence"]["ui_dump"] = self._write(f"{base}.xml", xml)
            except Exception as exc:
                self.logger.warning("Finding evidence ui dump failed: %s", exc)

        self._attach_log_tail(finding, base)
        self._attach_timeline(finding, base)
        # Real MP4 of the recent window beats discrete frames; the frame
        # history only steps in when recording is unavailable (DRM/ROM limits).
        if not self._attach_video(finding):
            self._attach_history(finding)
        # Protocol-level evidence sits alongside the video, not instead of it —
        # opt-in, and silently skipped when the pcap recorder is off/broken.
        self._attach_pcap(finding)

        self.findings.append(finding)
        self.logger.warning(
            "FINDING [%s] %s%s: %s", severity, finding_type, f" @{node}" if node else "", message
        )
        return finding

    def _evidence_png(self, image) -> bytes:
        """PNG bytes for a finding's screenshot: the provided frame if given
        (pinning evidence to the detection moment), else a fresh *exact* capture.

        The fresh capture forces a lossless screencap (exact=True), bypassing the
        lossy stream backend — a finding without a pinned frame is a persistent
        state (failure / crash / timeout), so a pixel-accurate shot is both safe
        and what QA evidence wants.
        """
        if image is None:
            return self.capturer.capture_png_bytes(self.device_id, exact=True)
        if isinstance(image, (bytes, bytearray)):
            return bytes(image)
        return self.capturer.encode_png(image)  # a PIL frame handed in by the caller

    def _attach_log_tail(self, finding: Dict, base: str) -> None:
        """In-game log fragment: the last window of logcat at the moment of the finding."""
        if self.logcat_monitor is None:
            return
        try:
            lines = self.logcat_monitor.tail(
                self.device_id, seconds=int(self.history_window_s), max_lines=self.log_tail_lines
            )
        except Exception as exc:
            self.logger.warning("Finding log tail failed: %s", exc)
            return
        if not lines:
            return
        # Inline excerpt is what the caller reads without opening the evidence
        # file, so it must not be the newest 40 lines of ROM noise: rank it with
        # the same tag/quota policy the monitor used for the evidence file.
        finding["log_excerpt"] = select_evidence_lines(
            lines, INLINE_LOG_LINES, getattr(self.logcat_monitor, "evidence_policy", None)
        )
        try:
            finding["evidence"]["logcat"] = self._write(f"{base}_logcat.log", "\n".join(lines) + "\n")
        except Exception as exc:
            self.logger.warning("Finding log tail write failed: %s", exc)

    def _attach_timeline(self, finding: Dict, base: str) -> None:
        """The flow of the last window: what the engine saw and did before the problem."""
        now_mono = time.monotonic()
        events = [e for e in self._timeline if now_mono - e["mono"] <= self.history_window_s]
        if not events:
            return
        payload = [
            {"ago_s": round(now_mono - e["mono"], 1), "time": e["time"], "event": e["event"], **e["detail"]}
            for e in events
        ]
        finding["recent_flow"] = [self._flow_line(p) for p in payload[-INLINE_FLOW_EVENTS:]]
        try:
            finding["evidence"]["timeline"] = self._write(
                f"{base}_timeline.json",
                json.dumps(
                    {"window_s": self.history_window_s, "events": payload},
                    ensure_ascii=False, indent=2, default=str,
                ),
            )
        except Exception as exc:
            self.logger.warning("Finding timeline write failed: %s", exc)

    @staticmethod
    def _flow_line(payload: Dict) -> str:
        detail = " ".join(
            f"{k}={v}" for k, v in payload.items() if k not in ("ago_s", "time", "event")
        )
        line = f"-{payload['ago_s']}s {payload['event']}"
        return f"{line} {detail}" if detail else line

    def _attach_video(self, finding: Dict) -> bool:
        """MP4 of the last window: pull the rolling screenrecord segments."""
        if self.screen_recorder is None:
            return False
        try:
            paths = self.screen_recorder.collect(self.run_dir / "video")
        except Exception as exc:
            self.logger.warning("Finding video collect failed: %s", exc)
            return False
        if not paths:
            return False
        self._dir_ready = True  # files landed via adb pull, not _write
        finding["evidence"]["video"] = [str(p) for p in paths]
        return True

    def _attach_pcap(self, finding: Dict) -> None:
        """Bug-moment protocol snapshot: pull the rolling tcpdump segments.

        An opt-in enhancement (needs root + tcpdump on the device); the recorder
        is None when disabled and latches itself off on any failure, so a finding
        is always recorded fully with or without pcap. Sits next to `video`, it
        does not replace the video or frame-history channels.
        """
        if self.pcap_recorder is None:
            return
        try:
            paths = self.pcap_recorder.collect(self.run_dir / "pcap")
        except Exception as exc:
            self.logger.warning("Finding pcap collect failed: %s", exc)
            return
        if not paths:
            return
        self._dir_ready = True  # files landed via adb pull, not _write
        finding["evidence"]["pcap"] = [str(p) for p in paths]

    def _attach_history(self, finding: Dict) -> None:
        """Screen frames buffered during the last window, written once and shared."""
        now_mono = time.monotonic()
        paths: List[str] = []
        for frame in self._frames:
            if now_mono - frame["mono"] > self.history_window_s:
                continue
            if frame["path"] is None:
                try:
                    frame["path"] = self._write(f"history/{frame['stamp']}.png", frame["png"])
                except Exception as exc:
                    self.logger.warning("Finding history frame write failed: %s", exc)
                    continue
            paths.append(frame["path"])
        if paths:
            finding["evidence"]["history"] = paths

    def finalize(self, status: str, error: Optional[str] = None,
                 node_stats: Optional[Dict] = None) -> Tuple[List[Dict], Dict]:
        """Close the run: attach failure evidence, write report.json + report.html,
        return (findings, summary).

        A failed run always yields a task_failure finding with screenshot +
        ui dump captured at the moment of failure — the scene is preserved even
        with debug tracing off.

        node_stats: the engine's per-node health counters, stored as a top-level
        report.json field next to the findings. It is observation, not an
        anomaly list, so it stays out of `findings` — but sitting in the same
        self-contained folder it makes each run comparable across days
        (report.json already carries run_id/task/device/started_at).
        """
        if error:
            self.record("task_failure", "error", error, screenshot=True, ui_dump=True)
        self._last_status = status

        counts: Dict[str, int] = {}
        for f in self.findings:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1

        report_path = None
        report_html_path = None
        if self.findings:
            # Evidence paths go into report.json relative to the run dir, so the
            # folder stays self-contained when exported/copied elsewhere.
            report = {
                "task": self.task_name,
                "device": self.device_id,
                "started_at": self.started_at,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "status": status,
                "error": error,
                "counts": counts,
                "findings": self._portable(self.findings),
            }
            if node_stats:
                report["node_stats"] = node_stats
            try:
                report_path = self._write(
                    "report.json", json.dumps(report, ensure_ascii=False, indent=2, default=str)
                )
            except Exception as exc:
                self.logger.warning("Failed to write findings report: %s", exc)
            # Human-readable twin of report.json: same relative evidence paths,
            # zero external assets, so QA can just double-click it (also inside
            # the exported zip). Rendering must never break the machine report.
            try:
                # run_id is added for the human report only; report.json keeps
                # its exact field set (it is identified by its folder).
                html_source = dict(report, run_id=self.run_dir.name)
                report_html_path = self._write("report.html", render_report_html(html_source))
            except Exception as exc:
                self.logger.warning("Failed to write findings HTML report: %s", exc)

        export_path = None
        if self.export_dir and self.findings and self._dir_ready:
            export_path = self.export_run(self.export_dir)

        summary = {
            "counts": counts,
            "run_dir": str(self.run_dir) if self._dir_ready else None,
            "report_path": report_path,
            "report_html_path": report_html_path,
            "export_path": export_path,
        }
        self._notify(status, error, counts, report_path, export_path)
        return list(self.findings), summary

    def _notify(self, status: str, error: Optional[str], counts: Dict[str, int],
                report_path: Optional[str], export_path: Optional[str]) -> None:
        """Push one summary per run to the configured notifiers (never raises).

        Runs last, after report.json and the auto-export, so the message can
        point at both. One message per run, not per finding — see core/notifier.
        Each notifier carries its own filter (`min_findings` / `on_status`); the
        defaults mean a clean run pushes nothing at all. Delivery is best effort:
        a chat robot being down must not change what finalize returns or what
        landed on disk, so every failure is a log line and nothing more.
        """
        if not self.notifiers:
            return
        summary = {
            "task": self.task_name,
            "device": self.device_id,
            "status": status,
            "error": error,
            "counts": counts,
            "findings": [
                {"type": f["type"], "severity": f["severity"], "message": f["message"]}
                for f in self.findings[:3]
            ],
            "report_path": report_path,
        }
        if export_path:
            summary["export_path"] = export_path
        for notifier in self.notifiers:
            try:
                gate = getattr(notifier, "should_notify", None)
                if gate is not None and not gate(status, len(self.findings)):
                    continue
                notifier.notify_run(summary)
            except Exception as exc:
                self.logger.warning("Findings notifier failed: %s", exc)

    def export_run(self, target_dir) -> Optional[str]:
        """Zip the run folder (report.json + report.html + all evidence) into target_dir.

        The archive is self-contained — both reports reference evidence by
        relative path and sit at the zip root, so report.html opens straight
        from the extracted folder with images/video intact — and named
        <timestamp>_<task>_<device>_<status>.zip, so a delivery directory is a
        flat list of one file per run instead of a sprawl of nested folders.
        Returns the .zip path, or None when there is nothing to export or the
        archive failed.
        """
        if not self._dir_ready:
            return None

        def safe(value: Optional[str]) -> str:
            return re.sub(r"[^\w.-]", "_", value or "")

        name = "_".join(
            part for part in (
                datetime.now().strftime("%Y%m%d_%H%M%S"),
                safe(self.task_name) or "task",
                safe(self.device_id) or "device",
                self._last_status or "run",
            ) if part
        )
        try:
            target = Path(target_dir)
            target.mkdir(parents=True, exist_ok=True)
            dest = target / f"{name}.zip"
            if dest.exists():
                dest = target / f"{name}_{uuid4().hex[:6]}.zip"
            # make_archive takes a base name without the extension and archives
            # the run dir's *contents* (report.json at the root).
            shutil.make_archive(str(dest.with_suffix("")), "zip", root_dir=self.run_dir)
        except Exception as exc:
            self.logger.warning("Findings export to '%s' failed: %s", target_dir, exc)
            return None
        self.logger.info("Findings exported to %s", dest)
        return str(dest)

    def _portable(self, value):
        """Rewrite absolute evidence paths to run-dir-relative (for report.json)."""
        if isinstance(value, str):
            prefix = str(self.run_dir)
            if value.startswith(prefix):
                return value[len(prefix):].lstrip("\\/").replace("\\", "/")
            return value
        if isinstance(value, list):
            return [self._portable(v) for v in value]
        if isinstance(value, dict):
            return {k: self._portable(v) for k, v in value.items()}
        return value

    def _write(self, name: str, data) -> str:
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self._dir_ready = True
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return str(path)
