"""Process-wide logger plus the per-run log file the task engine attaches.

The singleton logger itself always runs at DEBUG; the level from
`app.log_level` is applied to the *console* handler instead. That split is what
lets a run-scoped FileHandler capture the verbose step trace (poll misses,
settle waits) into `run.log` while the terminal keeps showing exactly what it
showed before.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

LOGGER_NAME = "autoplayqa"
LOG_FORMAT = "[%(asctime)s] %(levelname)s %(message)s"

#: Where a resident process (the MCP server) drops its own DEBUG trace.
DEFAULT_LOG_DIR = "outputs/logs"
#: Deliberately a constant, not a config knob: these are throwaway debug traces,
#: not evidence (findings keeps its own `findings.retention_days`). Promote it to
#: config only if someone actually needs to tune it.
LOG_RETENTION_DAYS = 14


def setup_logger(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    # Wide open at the logger level so per-run file handlers can see DEBUG
    # records; the configured level gates the console handler only.
    logger.setLevel(logging.DEBUG)
    # Propagation stays ON (tests capture these records through the root logger
    # with caplog). What must not happen is a *root handler* re-printing this
    # logger's DEBUG firehose — see mcp_server._guard_root_stderr_logging, which
    # owns that problem for the one process where it bites.
    console_level = getattr(logging, level.upper(), logging.INFO)

    if not logger.handlers:
        # stdio MCP 模式下 stdout 是 JSON-RPC 通道，日志必须锁死 stderr，
        # 不许改成 stdout（否则会把日志字节混进协议流，打坏 MCP 客户端解析）。
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(ch)

    for handler in logger.handlers:
        # FileHandler subclasses StreamHandler; only the console one follows the
        # configured level (a run.log must stay at DEBUG).
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(console_level)

    return logger


def attach_run_file_handler(logger: logging.Logger, path: Union[str, Path]) -> logging.Handler:
    """Tee everything (DEBUG included) into `path` until it is detached.

    Used by TaskEngine.run() to drop a `run.log` next to the run's report.json,
    so a finished run carries its own step-by-step trace in the same
    self-contained folder as its evidence.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return handler


def prune_old_logs(log_dir: Union[str, Path], retention_days: int = LOG_RETENTION_DAYS,
                   logger: Optional[logging.Logger] = None) -> int:
    """Delete `*.log` files under `log_dir` older than `retention_days`.

    A long-lived MCP server writes one log file per start, so without a cap the
    folder grows forever. Mirrors `findings.prune_old_runs`: best effort (a
    locked file is logged and skipped, never fatal at startup), `<= 0` disables
    it, returns how many files were removed. Age comes from the file mtime
    rather than the name, so a renamed or externally-rotated file is still
    covered.
    """
    if retention_days <= 0:
        return 0
    base = Path(log_dir)
    if not base.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for child in sorted(base.glob("*.log")):
        try:
            if not child.is_file() or child.stat().st_mtime >= cutoff:
                continue
            child.unlink()
            removed += 1
        except OSError as exc:
            if logger:
                logger.warning("Log prune failed for %s: %s", child, exc)
    if removed and logger:
        logger.info("Log retention: removed %d file(s) older than %d days",
                    removed, retention_days)
    return removed


def attach_process_log(logger: logging.Logger, log_dir: Union[str, Path] = DEFAULT_LOG_DIR,
                       prefix: str = "mcp",
                       retention_days: int = LOG_RETENTION_DAYS) -> Optional[logging.Handler]:
    """Give a resident process its own DEBUG file trace; None if it can't.

    `attach_run_file_handler` covers a task run, which is the only thing the CLI
    needs. The MCP server, though, spends most of its life *outside* a run —
    the agent's own click/swipe/screenshot calls left no file trace at all — so
    it attaches one of these at startup instead (`outputs/logs/<prefix>_<ts>.log`).

    Never raises: a process must not fail to boot because its log folder is not
    writable, it just runs without a file trace.
    """
    try:
        prune_old_logs(log_dir, retention_days, logger)
        path = Path(log_dir) / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        handler = attach_run_file_handler(logger, path)
    except OSError as exc:
        logger.warning("Process log setup failed: %s", exc)
        return None
    logger.info("Process log: %s", path)
    return handler


def _format_field(value) -> str:
    """One field value as a single grep-friendly token."""
    if isinstance(value, float):
        text = "%.3f" % value
    else:
        text = str(value)
    if text == "":
        return '""'
    # A value with whitespace would break `k=v` splitting, so quote it (inner
    # double quotes become single ones — the token stays one word either way).
    if any(ch.isspace() for ch in text) or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def log_event(logger, event: str, level: int = logging.DEBUG, /, **fields) -> None:
    """Emit one structured, greppable line: ``EVT <event> k1=v1 k2=v2``.

    The single formatting convention for every instrumentation point in the
    project. DEBUG (the default) lands in the run's `run.log` without touching
    the terminal, which is what makes it safe to sprinkle on hot paths.

    Fields whose value is None are dropped (an absent measurement should not
    show up as `score=None`), floats are rendered with 3 decimals, and values
    containing whitespace get quoted so `EVT ... k=v` stays machine-splittable.

    Callers must pass CHEAP SCALARS only — the line is built eagerly (the
    singleton logger sits at DEBUG, so lazy %-args would be formatted anyway),
    so never hand it an image, a full OCR result list or another big object;
    pass a length or a short summary instead.

    `logger` / `event` / `level` are positional-only on purpose, so that
    `event=` and `level=` remain usable as ordinary field names (the findings
    timeline mirror emits `EVT timeline event=<name>`).
    """
    check = getattr(logger, "isEnabledFor", None)
    if check is not None and not check(level):
        return
    parts = [f"EVT {event}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_format_field(value)}")
    logger.log(level, " ".join(parts))


def detach_handler(logger: logging.Logger, handler: logging.Handler) -> None:
    """Remove and close a handler (safe to call twice; never raises)."""
    try:
        logger.removeHandler(handler)
    finally:
        try:
            handler.close()
        except Exception:  # pragma: no cover - closing a closed file
            logger.debug("Log handler close failed", exc_info=True)
