"""Session + artifact plumbing between :mod:`record.gesture_recorder` and its
MCP / CLI entry points.

The recorder itself is a pure "getevent -> gestures + frames" engine: it knows
nothing about where recordings live, or about starting/stopping one per device.
This module owns that plumbing so the MCP server and the CLI behave identically:

* **Calibration cache** — per device serial under
  ``outputs/touch_calibration/<serial>.json``. A cached entry short-circuits the
  probe only while the display size still matches ``wm size``; a mismatch
  (rotation lock change, resolution override) re-probes and overwrites.
* **One session per device** — artifacts under ``outputs/recordings/<timestamp>/``,
  self-contained: ``gestures.json`` plus per-gesture ``before``/``after``/``anchor``
  PNGs, referenced from the manifest by bare file name.
* **Crash tolerance** — the manifest is rewritten after every gesture, so an
  entry point that dies mid-session still leaves everything captured so far on
  disk, and ``stop()`` returns what it has even if tearing the recorder down
  fails.

Layering: this sits with the recorder (below the interface layer); it depends on
perception (screenshot capturer, handed in) and nothing above.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from record.gesture_recorder import (
    GestureRecorder,
    GestureThresholds,
    TouchCalibration,
    _adb_text,  # deliberate reuse of same-package helpers: cache validation only
    _parse_wm_size,  # needs `wm size`, not a full getevent probe (see D6 in PRP)
    calibrate as probe_calibration,
)

DEFAULT_CALIBRATION_DIR = "outputs/touch_calibration"
DEFAULT_RECORDING_DIR = "outputs/recordings"

MANIFEST_NAME = "gestures.json"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# ---------- calibration cache ----------

def _safe_name(device_id: str) -> str:
    """adb ids may be ``ip:port`` — ``:`` is not a legal Windows file name char."""
    return _UNSAFE_NAME.sub("_", device_id)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def calibration_path(device_id: str, cache_dir: str = DEFAULT_CALIBRATION_DIR) -> Path:
    return Path(cache_dir) / f"{_safe_name(device_id)}.json"


def calibration_to_dict(calib: TouchCalibration) -> Dict:
    """Serialize to the on-disk cache schema (stable across sessions)."""
    return {
        "event_device": calib.device_path,
        "max_x": calib.panel_max_x,
        "max_y": calib.panel_max_y,
        "screen_width": calib.disp_w,
        "screen_height": calib.disp_h,
        "calibrated_at": _now_iso(),
    }


def calibration_from_dict(data: Dict) -> TouchCalibration:
    return TouchCalibration(
        device_path=data["event_device"],
        panel_max_x=int(data["max_x"]),
        panel_max_y=int(data["max_y"]),
        disp_w=int(data["screen_width"]),
        disp_h=int(data["screen_height"]),
    )


def read_cached_calibration(
    device_id: str, cache_dir: str = DEFAULT_CALIBRATION_DIR
) -> Optional[Dict]:
    path = calibration_path(device_id, cache_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    required = ("event_device", "max_x", "max_y", "screen_width", "screen_height")
    if not isinstance(data, dict) or any(k not in data for k in required):
        return None
    return data


def write_calibration(
    device_id: str, data: Dict, cache_dir: str = DEFAULT_CALIBRATION_DIR
) -> Optional[str]:
    path = calibration_path(device_id, cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None
    return path.as_posix()


def current_display_size(device_id: str) -> Optional[Tuple[int, int]]:
    """``wm size`` (Override when present) — the space taps/screencaps use."""
    try:
        return _parse_wm_size(_adb_text(device_id, "shell", "wm", "size"))
    except (RuntimeError, OSError):
        return None


def calibrate_device(
    device_id: str,
    cache_dir: str = DEFAULT_CALIBRATION_DIR,
    force: bool = False,
    logger=None,
) -> Dict:
    """Touch calibration with a display-size-checked cache short circuit.

    Returns ``{ok, device_id, cached, calibration, path}``; on a re-probe the
    result also carries ``recalibrated_reason``. Never raises: an unreachable
    device comes back as ``{ok: False, error: ...}``.
    """
    cached = None if force else read_cached_calibration(device_id, cache_dir)
    size = current_display_size(device_id)
    if cached is not None and size is not None:
        if (int(cached["screen_width"]), int(cached["screen_height"])) == size:
            return {
                "ok": True,
                "device_id": device_id,
                "cached": True,
                "calibration": cached,
                "path": calibration_path(device_id, cache_dir).as_posix(),
            }
        reason = (
            f"display size changed: cached {cached['screen_width']}x{cached['screen_height']}, "
            f"device now {size[0]}x{size[1]}"
        )
    elif force:
        reason = "forced re-calibration"
    elif cached is None:
        reason = "no cached calibration for this device"
    else:
        reason = "could not read the device display size to validate the cache"

    try:
        calib = probe_calibration(device_id)
    except (RuntimeError, OSError) as exc:
        if logger:
            logger.warning("record: touch calibration failed on %s: %s", device_id, exc)
        return {
            "ok": False,
            "device_id": device_id,
            "cached": False,
            "error": f"Touch calibration failed on '{device_id}': {exc}",
        }
    data = calibration_to_dict(calib)
    path = write_calibration(device_id, data, cache_dir)
    if logger:
        logger.info("record: calibrated %s (%s)", device_id, reason)
    return {
        "ok": True,
        "device_id": device_id,
        "cached": False,
        "recalibrated_reason": reason,
        "calibration": data,
        "path": path,
    }


# ---------- one device's recording session ----------

class GestureRecordingSession:
    """A single device's in-flight recording and its output folder."""

    def __init__(self, device_id: str, session_dir: Path, recorder, logger,
                 calibration_dir: str = DEFAULT_CALIBRATION_DIR):
        self.device_id = device_id
        self.session_dir = Path(session_dir)
        self.logger = logger
        self.calibration_dir = calibration_dir
        self._recorder = recorder
        self._lock = threading.Lock()
        self._gestures: List[Dict] = []
        self._calibration: Optional[Dict] = None
        self._started_at: Optional[str] = None
        self._stopped_at: Optional[str] = None
        self._t0 = time.monotonic()

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> Dict:
        """Begin recording. Raises whatever the recorder raises (the registry
        turns that into an error dict) — nothing is registered on failure."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = _now_iso()
        self._t0 = time.monotonic()
        calib = self._recorder.start(self._on_gesture)
        if isinstance(calib, TouchCalibration):
            self._calibration = calibration_to_dict(calib)
            # Recording always re-probes, so keep the cache fresh for free.
            write_calibration(self.device_id, self._calibration, self.calibration_dir)
        self._write_manifest()
        return {
            "ok": True,
            "device_id": self.device_id,
            "session_dir": self.session_dir.as_posix(),
            "manifest_path": (self.session_dir / MANIFEST_NAME).as_posix(),
            "started_at": self._started_at,
            "calibration": self._calibration,
        }

    def stop(self) -> Dict:
        stop_error = None
        try:
            self._recorder.stop()
        except Exception as exc:  # noqa: BLE001 - never lose what was captured
            stop_error = str(exc)
            if self.logger:
                self.logger.warning("record: recorder teardown failed: %s", exc)
        with self._lock:
            self._stopped_at = _now_iso()
            self._write_manifest_locked()
            gestures = [self._public(entry) for entry in self._gestures]
        result = {
            "ok": True,
            "device_id": self.device_id,
            "session_dir": self.session_dir.as_posix(),
            "manifest_path": (self.session_dir / MANIFEST_NAME).as_posix(),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
            "calibration": self._calibration,
            "gesture_count": len(gestures),
            "gestures": gestures,
        }
        if stop_error:
            result["warning"] = f"recorder teardown failed: {stop_error}"
        return result

    def status(self) -> Dict:
        with self._lock:
            return {
                "device_id": self.device_id,
                "session_dir": self.session_dir.as_posix(),
                "started_at": self._started_at,
                "gesture_count": len(self._gestures),
            }

    # -- gesture intake (called from the recorder's emit thread) -------------------

    def _on_gesture(self, event, images: Dict) -> None:
        try:
            entry = self._build_entry(event, images or {})
        except Exception as exc:  # noqa: BLE001 - one bad gesture must not kill the session
            if self.logger:
                self.logger.warning("record: could not persist gesture: %s", exc)
            return
        with self._lock:
            self._gestures.append(entry)
            self._write_manifest_locked()

    def _build_entry(self, event, images: Dict) -> Dict:
        stem = f"g{event.index:03d}"
        files: Dict[str, str] = {}
        for kind, key in (("before", "before_png"), ("after", "after_png"),
                          ("anchor", "anchor_png")):
            data = images.get(key)
            if not data:
                continue
            name = f"{stem}_{kind}.png"
            try:
                (self.session_dir / name).write_bytes(data)
            except OSError as exc:
                if self.logger:
                    self.logger.warning("record: writing %s failed: %s", name, exc)
                continue
            files[kind] = name
        frames = list(event.frames or [])
        return {
            "index": event.index,
            "type": event.type,
            "params": dict(event.params or {}),
            "down_point": list(event.down_point),
            "duration_ms": sum(f.get("delay_ms", 0) for f in frames),
            "pointer_frames": len(frames),
            "t_offset_ms": max(0, round((time.monotonic() - self._t0) * 1000)),
            "recorded_at": _now_iso(),
            "images": files,
            # Injector-ready pointer frames: kept in the manifest (needed to
            # replay a multi-touch faithfully) but stripped from API returns.
            "frames": frames,
        }

    def _public(self, entry: Dict) -> Dict:
        out = {k: v for k, v in entry.items() if k != "frames"}
        out["images"] = {
            kind: (self.session_dir / name).as_posix()
            for kind, name in entry["images"].items()
        }
        return out

    # -- manifest -----------------------------------------------------------------

    def _write_manifest(self) -> None:
        with self._lock:
            self._write_manifest_locked()

    def _write_manifest_locked(self) -> None:
        manifest = {
            "device_id": self.device_id,
            "session_dir": self.session_dir.as_posix(),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
            "calibration": self._calibration,
            "gesture_count": len(self._gestures),
            "gestures": self._gestures,
        }
        try:
            (self.session_dir / MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            if self.logger:
                self.logger.warning("record: writing the gesture manifest failed: %s", exc)


# ---------- per-device registry (shared by MCP + CLI) ----------

def _default_recorder_factory(capturer, logger, thresholds) -> Callable:
    def build(device_id: str):
        from record.frame_stream import ScrcpyFrameStream

        # Best effort: the recorder falls back to per-gesture screencap when the
        # stream will not come up.
        stream = ScrcpyFrameStream(device_id, logger)
        return GestureRecorder(
            device_id, capturer, logger, thresholds=thresholds, frame_stream=stream
        )

    return build


class GestureRecordingRegistry:
    """Start/stop gesture recordings per device, one session at a time.

    Both entry points (``mcp_server`` tools and the CLI ``record gestures``
    sub-commands) share an instance, so the same device cannot be recorded twice
    and errors read the same in both places. Every method returns a dict and
    never raises.
    """

    def __init__(self, capturer, logger, output_root: str = DEFAULT_RECORDING_DIR,
                 calibration_dir: str = DEFAULT_CALIBRATION_DIR,
                 device_manager=None, recorder_factory: Optional[Callable] = None,
                 thresholds: Optional[GestureThresholds] = None):
        self.capturer = capturer
        self.logger = logger
        self.output_root = Path(output_root)
        self.calibration_dir = calibration_dir
        self.device_manager = device_manager
        self.recorder_factory = recorder_factory or _default_recorder_factory(
            capturer, logger, thresholds
        )
        self._sessions: Dict[str, GestureRecordingSession] = {}
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------

    def _offline_error(self, device_id: str) -> Optional[str]:
        if self.device_manager is None:
            return None
        try:
            online = [d.device_id for d in self.device_manager.discover_devices()]
        except Exception as exc:  # noqa: BLE001 - adb missing/broken is a caller-facing error
            return f"Could not list devices (adb): {exc}"
        if device_id not in online:
            listed = ", ".join(online) if online else "none"
            return f"Device '{device_id}' is not connected (online: {listed})."
        return None

    def _new_session_dir(self) -> Path:
        base = self.output_root / time.strftime("%Y%m%d_%H%M%S")
        candidate = base
        suffix = 1
        while candidate.exists():
            candidate = Path(f"{base}-{suffix}")
            suffix += 1
        return candidate

    # -- api ----------------------------------------------------------------------

    def calibrate(self, device_id: str, force: bool = False) -> Dict:
        error = self._offline_error(device_id)
        if error:
            return {"ok": False, "device_id": device_id, "error": error}
        return calibrate_device(
            device_id, cache_dir=self.calibration_dir, force=force, logger=self.logger
        )

    def record_start(self, device_id: str) -> Dict:
        with self._lock:
            active = self._sessions.get(device_id)
            if active is not None:
                status = active.status()
                return {
                    "ok": False,
                    "device_id": device_id,
                    "error": (
                        f"A gesture recording is already active for '{device_id}' "
                        f"(started {status['started_at']}, {status['gesture_count']} gesture(s) "
                        f"in {status['session_dir']}); stop it first."
                    ),
                    "session_dir": status["session_dir"],
                }
            error = self._offline_error(device_id)
            if error:
                return {"ok": False, "device_id": device_id, "error": error}
            try:
                session = GestureRecordingSession(
                    device_id, self._new_session_dir(), self.recorder_factory(device_id),
                    self.logger, calibration_dir=self.calibration_dir,
                )
                result = session.start()
            except Exception as exc:  # noqa: BLE001 - surface probe/adb failures to the caller
                if self.logger:
                    self.logger.warning("record: could not start recording on %s: %s", device_id, exc)
                return {
                    "ok": False,
                    "device_id": device_id,
                    "error": f"Could not start gesture recording on '{device_id}': {exc}",
                }
            self._sessions[device_id] = session
            return result

    def record_stop(self, device_id: str) -> Dict:
        with self._lock:
            session = self._sessions.pop(device_id, None)
        if session is None:
            return {
                "ok": False,
                "device_id": device_id,
                "error": (
                    f"No gesture recording is active for '{device_id}'; "
                    f"call record_gestures_start first."
                ),
            }
        return session.stop()

    def status(self, device_id: Optional[str] = None) -> Dict:
        with self._lock:
            sessions = dict(self._sessions)
        if device_id is not None:
            session = sessions.get(device_id)
            if session is None:
                return {"ok": True, "device_id": device_id, "recording": False}
            return {"ok": True, "device_id": device_id, "recording": True, **session.status()}
        return {
            "ok": True,
            "recording_devices": [s.status() for s in sessions.values()],
        }
