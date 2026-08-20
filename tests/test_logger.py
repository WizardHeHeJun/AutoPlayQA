"""The logger split that makes run.log possible.

`app.log_level` must keep meaning "what the terminal shows", while the logger
itself stays at DEBUG so a run-scoped file handler can capture the verbose step
trace.
"""

from __future__ import annotations

import logging

import pytest

import os
import time

from core.logger import (
    LOGGER_NAME,
    attach_process_log,
    attach_run_file_handler,
    detach_handler,
    prune_old_logs,
    setup_logger,
)


@pytest.fixture
def restore_singleton():
    """setup_logger touches a process-wide singleton; put it back afterwards."""
    logger = logging.getLogger(LOGGER_NAME)
    before = (logger.level, [(h, h.level) for h in logger.handlers])
    yield logger
    logger.setLevel(before[0])
    logger.handlers = [h for h, _ in before[1]]
    for handler, level in before[1]:
        handler.setLevel(level)


def _console(logger):
    return [
        h for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]


def test_configured_level_lands_on_the_console_handler(restore_singleton):
    logger = setup_logger("WARNING")

    assert logger.level == logging.DEBUG  # so file handlers can see DEBUG
    assert [h.level for h in _console(logger)] == [logging.WARNING]


def test_repeated_setup_does_not_stack_handlers_and_updates_the_level(restore_singleton):
    setup_logger("INFO")
    logger = setup_logger("DEBUG")

    assert len(_console(logger)) == 1
    assert _console(logger)[0].level == logging.DEBUG


def test_unknown_level_falls_back_to_info(restore_singleton):
    logger = setup_logger("nonsense")

    assert _console(logger)[0].level == logging.INFO


def test_run_file_handler_captures_debug_and_detaches_cleanly(tmp_path, restore_singleton):
    logger = setup_logger("WARNING")  # console would hide INFO/DEBUG
    path = tmp_path / "nested" / "run.log"

    handler = attach_run_file_handler(logger, path)
    logger.debug("poll miss #1")
    logger.info("[step 1] action click (1, 2) ok 3ms")
    detach_handler(logger, handler)
    logger.info("after detach")

    text = path.read_text(encoding="utf-8")
    assert "poll miss #1" in text
    assert "[step 1] action click (1, 2) ok 3ms" in text
    assert "after detach" not in text
    assert handler not in logger.handlers


# --- the resident process's own log (MCP server) ------------------------------
#
# Outside a task run there is no run.log, so an agent driving the device by hand
# used to leave no file trace at all.

def test_process_log_captures_debug_in_a_timestamped_file(tmp_path, restore_singleton):
    logger = setup_logger("WARNING")  # console hides DEBUG; the file must not

    handler = attach_process_log(logger, tmp_path)
    logger.debug("EVT mcp_tool tool=click device=dev1 ms=42 ok=1")
    detach_handler(logger, handler)

    files = list(tmp_path.glob("mcp_*.log"))
    assert len(files) == 1
    assert "EVT mcp_tool tool=click" in files[0].read_text(encoding="utf-8")


def test_process_log_prunes_files_past_the_retention_window(tmp_path, restore_singleton):
    stale, fresh = tmp_path / "mcp_old.log", tmp_path / "mcp_new.log"
    stale.write_text("old", encoding="utf-8")
    fresh.write_text("new", encoding="utf-8")
    old_enough = time.time() - 20 * 86400
    os.utime(stale, (old_enough, old_enough))

    handler = attach_process_log(logger := setup_logger("WARNING"), tmp_path, retention_days=14)
    detach_handler(logger, handler)

    assert not stale.exists()
    assert fresh.exists()  # inside the window


def test_prune_is_a_noop_when_disabled_or_missing(tmp_path):
    stale = tmp_path / "mcp_old.log"
    stale.write_text("old", encoding="utf-8")
    os.utime(stale, (time.time() - 99 * 86400,) * 2)

    assert prune_old_logs(tmp_path, retention_days=0) == 0
    assert prune_old_logs(tmp_path / "missing", retention_days=14) == 0
    assert stale.exists()


def test_process_log_never_breaks_startup(tmp_path, restore_singleton):
    logger = setup_logger("WARNING")
    # A file where the log directory should be: attaching must fail soft.
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")

    assert attach_process_log(logger, blocker) is None
