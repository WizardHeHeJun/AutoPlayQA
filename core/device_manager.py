from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.adb_timeout import adb_timeout_s

DEFAULT_TCPIP_PORT = 5555


@dataclass
class DeviceProfile:
    device_id: str
    device_type: str
    model: str = "unknown"


class DeviceManager:
    def __init__(self, logger):
        self.logger = logger

    def discover_devices(self) -> List[DeviceProfile]:
        result = self._run_adb(["devices", "-l"])
        if result is None:
            return []

        devices: List[DeviceProfile] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue

            device_id, status = parts[0], parts[1]
            if status != "device":
                continue

            model = "unknown"
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part.split(":", 1)[1]

            if "emulator" in device_id:
                device_type = "emulator"
            elif re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", device_id):
                device_type = "wireless"
            else:
                device_type = "physical"
            devices.append(DeviceProfile(device_id=device_id, device_type=device_type, model=model))

        # "No devices" and "the device I meant is unauthorized/offline" look the
        # same to every caller above; this line is where that distinction is
        # recoverable after the fact.
        self.logger.debug(
            "adb discovery: %d device(s): %s",
            len(devices), ",".join(d.device_id for d in devices) or "-",
        )
        return devices

    # ---------- wireless (adb over Wi-Fi) ----------

    def connect(self, address: str) -> Dict:
        """adb connect <ip[:port]>. Port defaults to 5555.

        adb exits 0 even on failure, so success is judged from the output text.
        """
        address = self._normalize_address(address)
        result = self._run_adb(["connect", address])
        if result is None:
            return {"ok": False, "address": address, "message": "adb not available"}
        message = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0 and "connected to" in message
        if not ok:
            self.logger.error("adb connect %s failed: %s", address, message)
        else:
            # Wireless links come and go; the success side was silent, so a log
            # could show a failed connect but never the reconnect that fixed it.
            self.logger.info("adb connected to %s", address)
        return {"ok": ok, "address": address, "message": message}

    def disconnect(self, address: Optional[str] = None) -> Dict:
        """adb disconnect [<ip[:port]>]. Without address, disconnects all TCP devices."""
        args = ["disconnect"]
        if address:
            address = self._normalize_address(address)
            args.append(address)
        result = self._run_adb(args)
        if result is None:
            return {"ok": False, "address": address, "message": "adb not available"}
        message = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0
        return {"ok": ok, "address": address, "message": message}

    def pair(self, address: str, code: str) -> Dict:
        """adb pair <ip:pairing_port> <code> — Android 11+ wireless debugging.

        The pairing port differs from the connect port; both are shown on the phone.
        """
        result = self._run_adb(["pair", address, code])
        if result is None:
            return {"ok": False, "address": address, "message": "adb not available"}
        message = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0 and "successfully paired" in message.lower()
        if not ok:
            self.logger.error("adb pair %s failed: %s", address, message)
        else:
            self.logger.info("adb paired with %s", address)
        return {"ok": ok, "address": address, "message": message}

    def enable_tcpip(self, device_id: str, port: int = DEFAULT_TCPIP_PORT) -> Dict:
        """Switch a (usually USB-connected) device's adbd to TCP/IP mode.

        Reads the device Wi-Fi IP first because adbd restarts right after tcpip.
        Returns the address to pass to connect(); resets on device reboot.
        """
        ip = self._get_device_ip(device_id)
        result = self._run_adb(["-s", device_id, "tcpip", str(port)])
        if result is None:
            return {"ok": False, "address": None, "message": "adb not available"}
        message = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0 and "restarting in tcp mode" in message.lower()
        if not ok:
            self.logger.error("adb tcpip on %s failed: %s", device_id, message)
            return {"ok": False, "address": None, "message": message}
        if not ip:
            message += " (device IP not found; check Wi-Fi and connect manually)"
        self.logger.info(
            "adb tcpip enabled on %s: address=%s", device_id, f"{ip}:{port}" if ip else "unknown"
        )
        return {"ok": True, "address": f"{ip}:{port}" if ip else None, "message": message}

    def _get_device_ip(self, device_id: str) -> Optional[str]:
        result = self._run_adb(["-s", device_id, "shell", "ip", "route"])
        if result is None or result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if "wlan" in line:
                match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", line)
                if match:
                    return match.group(1)
        match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
        return match.group(1) if match else None

    @staticmethod
    def _normalize_address(address: str) -> str:
        if ":" not in address:
            return f"{address}:{DEFAULT_TCPIP_PORT}"
        return address

    def _run_adb(self, args: List[str]) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                ["adb", *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=adb_timeout_s(),
            )
        except FileNotFoundError:
            self.logger.error("adb command not found in PATH.")
            return None
        except subprocess.TimeoutExpired:
            self.logger.error("adb %s timed out.", " ".join(args))
            return None
