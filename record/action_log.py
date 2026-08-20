"""Session-scoped log of the actions an agent drives through the MCP tools.

Between the gesture recorder (what the *user* did with a finger) and the task
engine (what a *task JSON* replayed) there was no record of the third actor: the
agent itself, tapping through the MCP click / swipe / input_text tools. This
module is that record — a plain, append-only session log:

* **One session per device** under ``outputs/agent_sessions/<timestamp>_<label>/``,
  self-contained: ``session.json`` plus one ``s<NNN>_before.png`` per step,
  referenced from the manifest by bare file name.
* **Two uses, one format** (``context.kind``): ``explore`` — the agent driving a
  game on its own, so the session can later be turned into a task draft; and
  ``handoff`` — archiving what the agent did during a task's ``agent`` node, so
  the manual round in an otherwise deterministic run is not a black hole.
* **Crash tolerance** — ``session.json`` is rewritten after every step, so a
  process that dies mid-session still leaves everything captured so far on disk
  (same deal as the gesture recorder's ``gestures.json``).

Layering: this is a pure data-sink. It does not import perception or task —
screenshot bytes are handed in by the caller (mcp_server), which owns the
capturer. The manifest schema below is a contract consumed by the task-draft
generator, so field names are stable:

    {device_id, started_at, ended_at, context: {kind, task, node, run_id,
     label}, steps: [{index, t_offset_ms, tool, action, element, screenshot}]}

``element`` is the marked-screen element the tap resolved to (see
:func:`find_element_at`) or ``null`` for actions that have no element
(swipe / input_text / press_key / a bare click on an unmarked screen).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from core.logger import LOGGER_NAME

DEFAULT_SESSION_ROOT = "outputs/agent_sessions"
MANIFEST_NAME = "session.json"

#: ``context.kind`` values the consumers know about; anything else is stored
#: verbatim (with a warning) rather than rejected.
KNOWN_KINDS = ("explore", "handoff")

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class ActionLogError(RuntimeError):
    """Raised by :meth:`ActionLogRegistry.start` when a session is already live.

    Refusing beats silently taking over: the running session's steps would
    otherwise be split across two folders with no way to tell which is which.
    """


def _safe_name(value: str) -> str:
    """Device ids / free-form labels may contain ``:`` or spaces — not legal in
    a Windows path component."""
    cleaned = _UNSAFE_NAME.sub("_", str(value)).strip("_")
    return cleaned or "session"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def action_succeeded(result) -> bool:
    """Did an ActionExecutor result report success?

    The adb backend reports ``ok`` as the *string* ``"True"``/``"False"``
    (action/backends/adb_backend.py), so a plain truthiness test would call
    every failure a success. Anything that is not an explicit failure counts as
    success, so a backend that stops reporting ``ok`` keeps being logged.
    """
    if not isinstance(result, dict):
        return True
    ok = result.get("ok", True)
    if isinstance(ok, str):
        return ok.strip().lower() not in ("false", "0", "")
    return bool(ok)


def find_element_at(marks: Optional[Sequence[Dict]], x: int, y: int) -> Optional[Dict]:
    """Reverse-lookup: which marked element covers the point (x, y)?

    Lets a *bare* ``click(x, y)`` carry the same semantic payload as
    ``click_index`` when the screen happens to have been marked — the draft
    generator can then write a recognition anchor instead of a hardcoded
    coordinate. Overlapping hits resolve to the smallest-area element (the
    button, not the panel it sits on). Returns ``None`` when there is no mark
    table or nothing covers the point; elements with unusable bounds are
    skipped rather than raising.
    """
    if not marks:
        return None
    best: Optional[Dict] = None
    best_area: Optional[int] = None
    for element in marks:
        if not isinstance(element, dict):
            continue
        bounds = element.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in bounds)
        except (TypeError, ValueError):
            continue
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        area = max(x2 - x1, 0) * max(y2 - y1, 0)
        if best_area is None or area < best_area:
            best, best_area = element, area
    return dict(best) if best is not None else None


def _fallback_logger(logger):
    """The caller's logger, or the project one — never a silent None.

    A missing logger used to mean "drop this line on the floor", which is how a
    write failure could disappear without a trace; the parameter stays for
    injection (tests, alternative hosts).
    """
    return logger if logger is not None else logging.getLogger(LOGGER_NAME)


def build_context(kind: str = "explore", task: Optional[str] = None,
                  node: Optional[str] = None, run_id: Optional[str] = None,
                  label: Optional[str] = None, logger=None) -> Dict:
    """Assemble the manifest's ``context`` block (all five keys always present).

    `logger` stays injectable, but omitting it no longer silences the warnings:
    it falls back to the project logger (see `_fallback_logger`).
    """
    logger = _fallback_logger(logger)
    kind = kind or "explore"
    if kind not in KNOWN_KINDS:
        logger.warning("action log: unknown session kind %r (known: %s)",
                       kind, ", ".join(KNOWN_KINDS))
    return {"kind": kind, "task": task, "node": node, "run_id": run_id, "label": label}


# ---------- one device's action log ----------

class ActionLogSession:
    """A single device's in-flight action log and its output folder."""

    def __init__(self, device_id: str, session_dir, context: Optional[Dict] = None,
                 logger=None):
        self.device_id = device_id
        self.session_dir = Path(session_dir)
        self.context = dict(context or build_context())
        self.logger = _fallback_logger(logger)
        self._lock = threading.Lock()
        self._steps: List[Dict] = []
        self._started_at = _now_iso()
        self._ended_at: Optional[str] = None
        self._t0 = time.monotonic()

    # -- lifecycle ----------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / MANIFEST_NAME

    def start(self) -> Dict:
        """Create the folder and write the (empty) manifest; returns the summary."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = _now_iso()
        self._t0 = time.monotonic()
        self._write_manifest()
        return self.summary()

    def finish(self) -> Dict:
        """Stamp ``ended_at``, flush the manifest and return the public summary."""
        with self._lock:
            self._ended_at = _now_iso()
            self._write_manifest_locked()
            steps = [dict(step) for step in self._steps]
        result = self.summary()
        result["steps"] = steps
        return result

    def summary(self) -> Dict:
        with self._lock:
            step_count = len(self._steps)
        return {
            "ok": True,
            "device_id": self.device_id,
            "session_dir": self.session_dir.as_posix(),
            "manifest_path": self.manifest_path.as_posix(),
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "context": dict(self.context),
            "step_count": step_count,
        }

    # -- step intake ----------------------------------------------------------------

    def log_step(self, tool: str, action: Dict, element: Optional[Dict] = None,
                 screenshot_png: Optional[bytes] = None) -> Dict:
        """Append one executed action; the manifest is rewritten immediately.

        ``tool`` is the MCP tool name the agent called (``click_index``), while
        ``action`` is the executor action JSON it turned into
        (``{"type": "click", "params": {...}}``) — the draft generator needs
        both. ``screenshot_png``, when given, is the frame captured *before* the
        action and is written as ``s<NNN>_before.png``. Never raises: a step is
        bookkeeping, it must not take an action down with it.
        """
        with self._lock:
            index = len(self._steps) + 1
            step = {
                "index": index,
                "t_offset_ms": max(0, round((time.monotonic() - self._t0) * 1000)),
                "tool": tool,
                "action": dict(action or {}),
                "element": dict(element) if element else None,
                "screenshot": self._write_screenshot_locked(index, screenshot_png),
            }
            self._steps.append(step)
            self._write_manifest_locked()
            return dict(step)

    def _write_screenshot_locked(self, index: int, png: Optional[bytes]) -> Optional[str]:
        if not png:
            return None
        name = f"s{index:03d}_before.png"
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            (self.session_dir / name).write_bytes(png)
        except OSError as exc:
            if self.logger:
                self.logger.warning("action log: writing %s failed: %s", name, exc)
            return None
        return name

    # -- manifest -----------------------------------------------------------------

    def _write_manifest(self) -> None:
        with self._lock:
            self._write_manifest_locked()

    def _write_manifest_locked(self) -> None:
        manifest = {
            "device_id": self.device_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "context": dict(self.context),
            "steps": self._steps,
        }
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            if self.logger:
                self.logger.warning("action log: writing the session manifest failed: %s", exc)


# ---------- per-device registry ----------

class ActionLogRegistry:
    """Start/stop action logs per device, one live session at a time."""

    def __init__(self, logger=None, output_root: str = DEFAULT_SESSION_ROOT):
        self.logger = _fallback_logger(logger)
        self.output_root = Path(output_root)
        self._sessions: Dict[str, ActionLogSession] = {}
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------

    def _new_session_dir(self, label: str) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = _safe_name(label)
        candidate = self.output_root / f"{stamp}_{suffix}"
        counter = 1
        while candidate.exists():
            candidate = self.output_root / f"{stamp}-{counter}_{suffix}"
            counter += 1
        return candidate

    # -- api ----------------------------------------------------------------------

    def active(self, device_id: str) -> Optional[ActionLogSession]:
        """The device's live session, or None. Called on every logged action, so
        it stays a plain dict lookup — no session means zero extra work."""
        with self._lock:
            return self._sessions.get(device_id)

    def start(self, device_id: str, kind: str = "explore", task: Optional[str] = None,
              node: Optional[str] = None, run_id: Optional[str] = None,
              label: Optional[str] = None) -> ActionLogSession:
        """Open a session for the device.

        Raises :class:`ActionLogError` when one is already live (the message
        names the running session's folder) — entry points turn that into an
        error payload rather than clobbering the session.
        """
        with self._lock:
            existing = self._sessions.get(device_id)
            if existing is not None:
                live = existing.summary()
                raise ActionLogError(
                    f"An action log is already active for '{device_id}' "
                    f"(started {live['started_at']}, {live['step_count']} step(s) in "
                    f"{live['session_dir']}); stop it first."
                )
            context = build_context(kind=kind, task=task, node=node, run_id=run_id,
                                    label=label, logger=self.logger)
            session = ActionLogSession(
                device_id, self._new_session_dir(label or kind or "session"),
                context, logger=self.logger,
            )
            session.start()
            self._sessions[device_id] = session
            if self.logger:
                self.logger.info("action log: recording %s actions on %s -> %s",
                                 context["kind"], device_id, session.session_dir.as_posix())
            return session

    def stop(self, device_id: str) -> Dict:
        """Close the device's session and return its summary (never raises)."""
        with self._lock:
            session = self._sessions.pop(device_id, None)
        if session is None:
            return {
                "ok": False,
                "device_id": device_id,
                "error": (
                    f"No action log is active for '{device_id}'; "
                    f"call record_actions_start first."
                ),
            }
        return session.finish()

    def status(self, device_id: Optional[str] = None) -> Dict:
        with self._lock:
            sessions = dict(self._sessions)
        if device_id is not None:
            session = sessions.get(device_id)
            if session is None:
                return {"ok": True, "device_id": device_id, "logging": False}
            return {"ok": True, "device_id": device_id, "logging": True, **session.summary()}
        return {"ok": True, "logging_devices": [s.summary() for s in sessions.values()]}
