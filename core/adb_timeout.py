"""One central timeout for every blocking adb round trip.

Each adb command is a blocking `subprocess.run`. Without `timeout=` it waits
*forever*: a wedged adb server, a USB hiccup or a device that stops answering
turns one missing argument into a call that never returns — the caller (CLI
step, task engine, or an MCP tool) hangs until something outside kills it.
That is a QA tool failing silently, which this project treats as unacceptable.

So the value lives here: set once from config (`adb.timeout_s`) at process
start, read at call time by every adb caller, so no call site can be forgotten
or drift out of sync.

Deliberately NOT used for calls that are long-running *by design* — the
device-side `screenrecord` segments (screen_recorder), the scrcpy video socket
(scrcpy_stream / frame_stream) and the `getevent` stream (gesture_recorder).
Those own their own lifecycle and already have bounded waits; capping them at a
few tens of seconds would break them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 30s is ~10x the slowest healthy adb round trip we measure (`uiautomator dump`
# at ~4.3s, `screencap -p` at ~2.4s), so a hit means "stuck", never "slow".
DEFAULT_ADB_TIMEOUT_S = 30.0

_timeout_s: float = DEFAULT_ADB_TIMEOUT_S


class AdbTimeout(RuntimeError):
    """An adb command blocked past its timeout — the device/adb server is stuck.

    A RuntimeError subclass on purpose: existing callers that already treat a
    RuntimeError as "this capture/probe failed" keep working, while code that
    must tell "stuck" apart from "unsupported ROM" can catch this precisely.
    """


def adb_timed_out(args, seconds: float) -> AdbTimeout:
    """Build the AdbTimeout for a timed-out command (uniform, greppable message)."""
    printable = " ".join(str(a) for a in args)
    return AdbTimeout(f"adb command timed out after {seconds:g}s: {printable}")


def configure_adb_timeout(adb_config: Optional[Dict[str, Any]]) -> float:
    """Apply the `adb.timeout_s` config value; returns the timeout now in effect.

    Entry points (main.py / mcp_server.py) call this once at startup. A missing,
    non-numeric or non-positive value keeps the default rather than disabling
    the timeout — "no timeout" is exactly the failure mode this module exists to
    prevent.
    """
    global _timeout_s
    value = (adb_config or {}).get("timeout_s")
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _timeout_s
    if seconds > 0:
        _timeout_s = seconds
    return _timeout_s


def adb_timeout_s() -> float:
    """Seconds a single adb command may block before it is considered stuck."""
    return _timeout_s


def reset_adb_timeout() -> None:
    """Restore the built-in default (used by tests)."""
    global _timeout_s
    _timeout_s = DEFAULT_ADB_TIMEOUT_S
