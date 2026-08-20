"""Anchor-position cache that speeds up repeated task replays.

The cache never bypasses the recognition gate: a cached bbox only narrows the
OCR search region (fast path). A fast-path miss falls back to full-screen
recognition, and if the anchor is then found somewhere else the move is
surfaced as an `anchor_drift` finding — the UI changed, which is a QA signal,
not something to silently heal. How far it moved (`center_distance`) rides
along with the hit so the engine can tell a real relocation from layout jitter
(see `engine.drift_tolerance_px`); the tolerance decides what gets *reported*,
never what gets recognized.

Entries are keyed by (device, task, node) and carry the screen size they were
captured at; a resolution change invalidates the entry without raising drift.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from core.logger import log_event

DEFAULT_CACHE_PATH = "outputs/cache/replay_cache.json"

# The fast-path ROI is the cached bbox expanded by half its size on every
# side (at least ROI_MARGIN_MIN px), tolerating small layout shifts.
ROI_MARGIN_MIN = 30


def center_distance(previous: Sequence[float], current: Sequence[float]) -> float:
    """Euclidean distance in px between an anchor's old and new centers.

    Drives the drift *reporting* threshold only: how far an anchor moved says
    how likely the layout really changed versus a row reflowing a few pixels.
    """
    return math.hypot(
        float(current[0]) - float(previous[0]), float(current[1]) - float(previous[1])
    )


def _split_key(key: str) -> Tuple[str, str, str]:
    """`device|task|node` back into its three parts (log fields only).

    Tolerates a hand-made key that does not have both separators: a log line is
    never worth an exception.
    """
    parts = str(key).split("|", 2)
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


class ReplayCache:
    """JSON-file-backed write-through cache of recognized anchor positions."""

    def __init__(self, logger, path: str = DEFAULT_CACHE_PATH):
        self.logger = logger
        self.path = Path(path)
        self._data: Optional[Dict[str, Dict]] = None

    @staticmethod
    def make_key(device_id: str, task_name: str, node: str) -> str:
        return f"{device_id}|{task_name}|{node}"

    def get(self, key: str) -> Optional[Dict]:
        return self._load().get(key)

    def put(self, key: str, bbox: Sequence[int], center: Sequence[int], text: str,
            screen: Tuple[int, int]) -> None:
        # A write means "this anchor's position is now what the cache serves".
        # Reads are not logged here — the recognizer's own `EVT recognize ...
        # cache=hit|drift|miss` line already says what the cache did for that
        # attempt, and this class has no idea which node it is serving.
        device, task, node = _split_key(key)
        log_event(self.logger, "replay_cache_put", device=device, task=task, node=node,
                  center=f"{int(center[0])},{int(center[1])}")
        data = self._load()
        data[key] = {
            "bbox": [int(v) for v in bbox],
            "center": [int(v) for v in center],
            "text": text,
            "screen": [int(screen[0]), int(screen[1])],
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._save()

    def clear(self) -> int:
        count = len(self._load())
        self._data = {}
        self._save()
        # INFO, not DEBUG: clearing the cache changes what the next replay does
        # (full-screen re-recognition, no drift reports until it refills), so it
        # belongs in the terminal beside the command that asked for it.
        self.logger.info("Replay cache cleared (%d anchor(s))", count)
        return count

    def size(self) -> int:
        return len(self._load())

    @staticmethod
    def roi_from(entry: Dict, screen: Tuple[int, int]) -> Optional[List[int]]:
        """Expanded, screen-clamped search region for a cached entry.

        Returns None when the entry was captured at a different resolution —
        the caller should treat that as a plain miss, not drift.
        """
        if list(entry.get("screen", [])) != [int(screen[0]), int(screen[1])]:
            return None
        x1, y1, x2, y2 = entry["bbox"]
        margin_x = max(ROI_MARGIN_MIN, (x2 - x1) // 2)
        margin_y = max(ROI_MARGIN_MIN, (y2 - y1) // 2)
        return [
            max(0, x1 - margin_x),
            max(0, y1 - margin_y),
            min(int(screen[0]), x2 + margin_x),
            min(int(screen[1]), y2 + margin_y),
        ]

    def _load(self) -> Dict[str, Dict]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._data = {}
            except (OSError, json.JSONDecodeError) as exc:
                self.logger.warning("Replay cache unreadable (%s); starting empty", exc)
                self._data = {}
        return self._data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            self.logger.warning("Replay cache write failed: %s", exc)
