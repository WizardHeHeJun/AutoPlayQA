from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from core.adb_timeout import adb_timeout_s
from core.logger import log_event

# On-device staging path + launch target for the injector dex.
REMOTE_DEX = "/data/local/tmp/gameinjector.dex"
INJECTOR_MAIN = "com.ga.injector.GameInjector"

# Packaged dex artifact (built from injector/GameInjector.java via injector/build.ps1).
_LOCAL_DEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "injector",
    "gameinjector.dex",
)


class MotionEventBackend:
    """No-root multi-touch via ``app_process`` + ``InputManager.injectInputEvent``.

    Direct writes to ``/dev/input`` (sendevent / minitouch) are blocked by
    SELinux for the shell domain on modern MIUI/HyperOS, and ``input`` cannot
    express multi-touch. This backend instead launches a small dex helper
    (``GameInjector``) through ``app_process`` -- the same privileged path the
    platform ``input`` command uses -- which injects multi-pointer ``MotionEvent``
    objects in display-pixel coordinates, no root required.

    Gesture format (display-pixel pointer frames)::

        {
          "frames": [
            {"delay_ms": 0,  "pointers": [{"id":0,"x":360,"y":1200},{"id":1,"x":720,"y":1200}]},
            {"delay_ms": 16, "pointers": [{"id":0,"x":360,"y":1100},{"id":1,"x":720,"y":1300}]},
            {"delay_ms": 16, "pointers": []}
          ]
        }

    Each frame lists the full set of pointers currently down (display pixels).
    ``delay_ms`` is the wait *before* the frame. Pointer diffing here turns the
    frames into the injector's line protocol (one ``MotionEvent`` per line).
    """

    def __init__(self, logger):
        self.logger = logger
        self._installed_on: set = set()

    # -- Pointer diffing -> injector line protocol (unit-testable, no device) ----

    @staticmethod
    def frames_to_protocol(gesture: Dict) -> List[str]:
        """Translate pointer frames into injector protocol lines.

        Each line: ``<delay_ms> <action> <changed_id> <count> [<id> <x> <y>]...``
        where the pointer list is the full active set at that instant, in stable
        index order. Removals are emitted first, then a MOVE for survivors, then
        additions -- each as its own ``MotionEvent``.
        """
        lines: List[str] = []
        active: List[int] = []          # ordered pointer ids == pointer indices
        pos: Dict[int, Tuple[float, float]] = {}

        def fmt(num) -> str:
            return str(int(num)) if float(num).is_integer() else str(num)

        def pointer_fields(ids: List[int]) -> str:
            return " ".join(f"{i} {fmt(pos[i][0])} {fmt(pos[i][1])}" for i in ids)

        def emit(delay: int, action: str, changed: int, ids: List[int]) -> int:
            lines.append(
                f"{delay} {action} {changed} {len(ids)} {pointer_fields(ids)}".rstrip()
            )
            return 0  # subsequent events in the same frame carry no delay

        for frame in gesture.get("frames", []):
            delay = int(frame.get("delay_ms", 0))
            cur = {int(p["id"]): (p["x"], p["y"]) for p in frame.get("pointers", [])}

            removed = [pid for pid in active if pid not in cur]
            added = [pid for pid in cur if pid not in active]
            survivors = [pid for pid in active if pid in cur]

            # 1) Removals (one event each; pointer keeps its last known position).
            for pid in removed:
                if len(active) == 1:
                    delay = emit(delay, "UP", pid, list(active))
                else:
                    delay = emit(delay, "POINTER_UP", pid, list(active))
                active.remove(pid)

            # 2) Move survivors to their new positions.
            if survivors:
                moved = any(pos[pid] != cur[pid] for pid in survivors)
                for pid in survivors:
                    pos[pid] = cur[pid]
                if moved:
                    delay = emit(delay, "MOVE", -1, list(active))

            # 3) Additions (one event each).
            for pid in added:
                pos[pid] = cur[pid]
                if not active:
                    active.append(pid)
                    delay = emit(delay, "DOWN", pid, list(active))
                else:
                    active.append(pid)
                    delay = emit(delay, "POINTER_DOWN", pid, list(active))

        return lines

    # -- Synthetic gesture (validation / map-zoom helper) ------------------------

    @staticmethod
    def synthesize_pinch(
        cx: int, cy: int, start_gap: int, end_gap: int,
        steps: int = 20, frame_delay_ms: int = 16,
    ) -> Dict:
        """Build a vertical two-finger pinch in display pixels centred at (cx, cy).

        ``end_gap > start_gap`` spreads the fingers apart (zoom out); the reverse
        pinches them together (zoom in).
        """
        frames: List[Dict] = []

        def gap_at(t: float) -> int:
            return int(round(start_gap + (end_gap - start_gap) * t))

        for i in range(steps + 1):
            g = gap_at(i / steps)
            frames.append({
                "delay_ms": 0 if i == 0 else frame_delay_ms,
                "pointers": [
                    {"id": 0, "x": cx, "y": cy - g},
                    {"id": 1, "x": cx, "y": cy + g},
                ],
            })
        frames.append({"delay_ms": frame_delay_ms, "pointers": []})  # release both
        return {"frames": frames}

    # -- Device I/O --------------------------------------------------------------

    def ensure_installed(self, device_id: str, force: bool = False) -> Dict[str, str]:
        """Push the injector dex to the device if not already present."""
        if device_id in self._installed_on and not force:
            return {"ok": "True", "stdout": "cached", "stderr": ""}
        if not os.path.isfile(_LOCAL_DEX):
            return {"ok": "False", "stdout": "", "stderr": f"injector dex missing: {_LOCAL_DEX}"}

        if not force:
            # Quiet existence probe -- a missing dex is the normal first-run path,
            # not an error worth logging.
            try:
                check = subprocess.run(
                    ["adb", "-s", device_id, "shell", "ls", REMOTE_DEX],
                    check=False, capture_output=True, text=True, timeout=adb_timeout_s(),
                )
            except subprocess.TimeoutExpired:
                # Stuck probe: push instead of hanging; the push carries its own
                # timeout and reports a real error if the device is gone.
                check = None
            if check is not None and check.returncode == 0 and REMOTE_DEX in check.stdout:
                self._installed_on.add(device_id)
                return {"ok": "True", "stdout": "already present", "stderr": ""}

        push = self._run(["adb", "-s", device_id, "push", _LOCAL_DEX, REMOTE_DEX])
        if push["ok"] == "True":
            self._installed_on.add(device_id)
        return push

    def replay(self, device_id: str, gesture: Dict) -> Dict[str, str]:
        """Inject a multi-touch gesture described by pointer frames."""
        install = self.ensure_installed(device_id)
        if install["ok"] == "False":
            return install

        protocol = "\n".join(self.frames_to_protocol(gesture)) + "\n"
        cmd = [
            "adb", "-s", device_id, "shell",
            f"CLASSPATH={REMOTE_DEX} app_process / {INJECTOR_MAIN}",
        ]
        # The injector sleeps between frames on the device, so the gesture's own
        # scripted duration is legitimate wait time: budget it on top of the adb
        # timeout instead of cutting a long swipe short.
        budget = adb_timeout_s() + self._gesture_duration_s(gesture)
        started = time.perf_counter()
        try:
            result = subprocess.run(
                cmd, input=protocol, check=False, capture_output=True, text=True, timeout=budget,
            )
        except subprocess.TimeoutExpired:
            message = f"multi_touch injection timed out after {budget:g}s"
            if self.logger:
                self.logger.error(message)
            return {"ok": "False", "stdout": "", "stderr": message}
        combined = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0 and "OK" in (result.stdout or "") and "INJECT_ERROR" not in combined
        if not ok and self.logger:
            self.logger.error("multi_touch injection failed: %s", combined.strip())
        elif ok and self.logger:
            # Measured vs scripted duration: a gesture the device replayed far
            # slower than authored is the usual reason a "recorded" swipe stops
            # working, and only this line can tell the two apart afterwards.
            log_event(
                self.logger, "motionevent_replay", device=device_id,
                events=len(gesture.get("frames") or []),
                ms=int((time.perf_counter() - started) * 1000),
                scripted_ms=int(self._gesture_duration_s(gesture) * 1000),
            )
        return {
            "ok": str(ok),
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }

    def replay_file(self, device_id: str, file_path: str) -> Dict[str, str]:
        import json
        with open(file_path, encoding="utf-8") as f:
            gesture = json.load(f)
        return self.replay(device_id, gesture)

    # -- Internal ----------------------------------------------------------------

    @staticmethod
    def _gesture_duration_s(gesture: Dict) -> float:
        """Sum of the gesture's own inter-frame delays, in seconds."""
        total_ms = 0
        for frame in gesture.get("frames", []) or []:
            try:
                total_ms += int(frame.get("delay_ms", 0))
            except (TypeError, ValueError):
                continue
        return total_ms / 1000.0

    def _run(self, cmd, timeout: Optional[float] = None) -> Dict[str, str]:
        timeout = adb_timeout_s() if timeout is None else timeout
        try:
            result = subprocess.run(
                cmd, check=False, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            message = f"adb command timed out after {timeout:g}s: {' '.join(str(c) for c in cmd)}"
            if self.logger:
                self.logger.error(message)
            return {"ok": "False", "stdout": "", "stderr": message}
        ok = result.returncode == 0
        if not ok and self.logger:
            self.logger.error("adb command failed: %s", result.stderr.strip())
        return {
            "ok": str(ok),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
