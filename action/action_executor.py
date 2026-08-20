from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, Optional

from action.backends.adb_backend import AdbBackend
from action.backends.motionevent_backend import MotionEventBackend

if TYPE_CHECKING:
    from utils.debug_tracer import DebugTracer


class ActionExecutor:
    def __init__(self, logger, config):
        self.logger = logger
        self.config = config
        self.adb = AdbBackend(logger)
        # No-root multi-touch (pinch / custom gestures). Construction is cheap
        # (no device I/O); the dex is pushed lazily on the first gesture.
        self.motion = MotionEventBackend(logger)

    def execute(self, device_id: str, action: Dict, tracer: Optional["DebugTracer"] = None) -> Dict:
        action_type = action.get("type")
        params = action.get("params", {})

        if action_type == "click":
            result = self.adb.click(device_id, int(params["x"]), int(params["y"]))
        elif action_type == "drag":
            duration = int(params.get("duration_ms", self.config.get("execution", {}).get("default_swipe_duration_ms", 500)))
            result = self.adb.drag(
                device_id,
                int(params["x1"]),
                int(params["y1"]),
                int(params["x2"]),
                int(params["y2"]),
                duration,
            )
        elif action_type == "input_text":
            result = self.adb.input_text(device_id, str(params.get("text", "")))
        elif action_type == "key":
            result = self.adb.press_key(device_id, int(params["keycode"]))
        elif action_type == "gesture":
            result = self._gesture(device_id, params)
        elif action_type == "wait":
            duration_ms = int(params.get("duration_ms", 1000))
            time.sleep(duration_ms / 1000)
            result = {"ok": "True", "stdout": f"waited {duration_ms}ms", "stderr": ""}
        else:
            result = {"ok": "False", "stderr": f"Unsupported action type: {action_type}"}

        if tracer and tracer.enabled:
            tracer.record_action_result(action, result)

        return result

    def _gesture(self, device_id: str, params: Dict) -> Dict:
        """Inject a multi-touch gesture via the no-root MotionEvent backend.

        Accepts either explicit pointer frames (params["frames"]) or a "pinch"
        convenience spec (params["pinch"] = {cx, cy, start_gap, end_gap, steps?})
        that synthesizes a vertical two-finger pinch (end_gap > start_gap = zoom
        out / spread).
        """
        if "frames" in params:
            gesture = {"frames": params["frames"]}
        elif "pinch" in params:
            p = params["pinch"]
            gesture = MotionEventBackend.synthesize_pinch(
                cx=int(p["cx"]), cy=int(p["cy"]),
                start_gap=int(p["start_gap"]), end_gap=int(p["end_gap"]),
                steps=int(p.get("steps", 20)),
                frame_delay_ms=int(p.get("frame_delay_ms", 16)),
            )
        else:
            return {"ok": "False", "stdout": "",
                    "stderr": "gesture action needs 'frames' or 'pinch' params"}
        return self.motion.replay(device_id, gesture)
