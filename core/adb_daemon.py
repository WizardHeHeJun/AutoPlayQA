"""Warm up the shared adb daemon before spawning a job-bound adb client.

An adb client that finds no running server starts one itself — on Windows the
daemon is spawned as a child of that very client. That is fine normally, but
this project binds its *long-lived* adb clients to a kill-on-close Job Object
(`core.windows_job`) so they cannot outlive a hard-killed main process. If such
a client were the one to auto-start the daemon, the daemon would be created
inside the job as well and get killed with us — taking down every other adb
user on the machine (a parallel session, Android Studio, another QA tool).

So: call `ensure_adb_daemon()` *before* every long-lived `Popen` that will be
bound. It runs one short-lived, unbound `adb start-server`, so by the time the
bound client starts the daemon already exists and is nobody's job member.

Best effort by design: one attempt per adb path per process (a failure is
cached too — retrying on every segment rotation would stall the run for
`START_SERVER_TIMEOUT_S` each time), failures are logged and never raised. If
the daemon really cannot start, the adb command that follows fails on its own
and takes that subsystem's existing degradation path.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Dict

from core.logger import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

# `adb start-server` is a local operation (bind a socket, fork the daemon); 10s
# is generous. It is deliberately not the shared adb_timeout: this call must not
# grow with a device-facing timeout.
START_SERVER_TIMEOUT_S = 10.0

_lock = threading.Lock()
_ensured: Dict[str, bool] = {}  # adb path -> outcome of the single attempt


def ensure_adb_daemon(adb_path: str = "adb") -> bool:
    """Make sure an adb daemon is running, started from an unbound process.

    Returns True when the daemon is (now) up, False when the attempt failed.
    Runs at most once per adb path per process; never raises.
    """
    with _lock:
        cached = _ensured.get(adb_path)
        if cached is not None:
            return cached
        _ensured[adb_path] = False  # pessimistic until the call returns cleanly
        try:
            result = subprocess.run(
                [adb_path, "start-server"], check=False, capture_output=True,
                encoding="utf-8", errors="ignore", timeout=START_SERVER_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("adb start-server failed (%s); adb daemon not pre-warmed", exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "adb start-server exited %s (%s); adb daemon not pre-warmed",
                result.returncode, (result.stderr or "").strip()[:200],
            )
            return False
        _ensured[adb_path] = True
        logger.debug("adb daemon ready (pre-warmed outside the job object)")
        return True


def reset_state() -> None:
    """Forget which adb paths were warmed up (tests only)."""
    with _lock:
        _ensured.clear()
