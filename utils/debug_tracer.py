from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4
from typing import Any, Dict, Optional


class DebugTracer:
    """Accumulates debug data for one NL command execution and flushes to disk."""

    def __init__(self, device_id: str, debug_config: Dict):
        self.enabled: bool = debug_config.get("enabled", False)
        self.device_id = device_id
        self.annotate: bool = debug_config.get("annotate", True)
        self.capture_after: bool = debug_config.get("capture_after", True)
        self.save_top: int = debug_config.get("save_top_candidates", 3)

        base_dir = debug_config.get("output_dir", "outputs/debug")
        date_str = datetime.now().strftime("%Y%m%d")
        self.trace_id: str = uuid4().hex[:8]
        self.trace_dir: str = os.path.join(base_dir, date_str, device_id, self.trace_id)

        self._data: Dict[str, Any] = {}
        self._images: Dict[str, bytes] = {}

    def record(self, **kwargs) -> None:
        """Merge key/value pairs into the debug record (JSON-serialisable values only)."""
        self._data.update(kwargs)

    def record_action_result(self, action: Dict, result: Dict) -> None:
        executed = self._data.setdefault("executed_actions", [])
        executed.append({"action": action, "result": result})

    def save_image(self, name: str, png_bytes: bytes) -> None:
        """Stage a PNG image to be written on flush()."""
        self._images[name] = png_bytes

    def flush(self) -> None:
        """Write meta.json and all staged images to trace_dir."""
        if not self.enabled:
            return
        os.makedirs(self.trace_dir, exist_ok=True)
        for name, data in self._images.items():
            with open(os.path.join(self.trace_dir, name), "wb") as f:
                f.write(data)
        meta_path = os.path.join(self.trace_dir, "meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, default=str)

    def summary_line(self) -> str:
        """Return a one-line CLI summary string."""
        found = self._data.get("found", "?")
        candidates = self._data.get("top_candidates", [])
        chosen = self._data.get("chosen_candidate")
        coord = f"center={chosen['center']}" if chosen else "no match"
        return (
            f"[DEBUG] trace={self.trace_id} | found={found} | "
            f"candidates={len(candidates)} | {coord} | dir={self.trace_dir}"
        )


class SessionRecorder:
    """Session-level log of executed NL commands, used to author reusable tasks.

    Unlike DebugTracer (one instance per command, flushed to disk), this lives
    for the whole CLI session and only keeps what task generation needs.
    """

    def __init__(self):
        self.enabled = False
        self.records: list = []

    def start(self) -> None:
        self.enabled = True

    def stop(self) -> None:
        self.enabled = False

    def clear(self) -> None:
        self.records = []

    def add(self, device_id: str, user_text: str, actions: list, results: list) -> None:
        if not self.enabled:
            return
        ok = all(r.get("ok") == "True" for r in results) if results else False
        self.records.append(
            {
                "device_id": device_id,
                "user_text": user_text,
                "actions": actions,
                "results_ok": ok,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        )
