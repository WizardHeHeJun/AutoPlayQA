from __future__ import annotations

import subprocess
from typing import Dict

from core.adb_timeout import adb_timeout_s


class AdbBackend:
    def __init__(self, logger):
        self.logger = logger

    def click(self, device_id: str, x: int, y: int) -> Dict[str, str]:
        cmd = ["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)]
        return self._run(cmd)

    def drag(self, device_id: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> Dict[str, str]:
        cmd = [
            "adb",
            "-s",
            device_id,
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        ]
        return self._run(cmd)

    def press_key(self, device_id: str, keycode: int) -> Dict[str, str]:
        cmd = ["adb", "-s", device_id, "shell", "input", "keyevent", str(keycode)]
        return self._run(cmd)

    def input_text(self, device_id: str, text: str) -> Dict[str, str]:
        text = text.strip()
        if not text:
            return {
                "ok": "False",
                "stdout": "",
                "stderr": "Input text is empty.",
            }
        escaped = text.replace(" ", "%s")
        cmd = ["adb", "-s", device_id, "shell", "input", "text", escaped]
        return self._run(cmd)

    def _run(self, cmd):
        # `input tap/swipe/text/keyevent` returns in well under a second; only a
        # stuck adb server or an unresponsive device takes longer. Report the
        # timeout as a failed action instead of blocking the caller forever —
        # the engine can then retry / record it, which a hang never allows.
        timeout = adb_timeout_s()
        try:
            result = subprocess.run(
                cmd, check=False, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            message = f"adb command timed out after {timeout:g}s: {' '.join(cmd)}"
            self.logger.error(message)
            return {"ok": "False", "stdout": "", "stderr": message}
        ok = result.returncode == 0
        if not ok:
            self.logger.error("ADB command failed: %s", result.stderr.strip())
        return {
            "ok": str(ok),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
