"""Both entry points assemble their object graph through bootstrap.py.

These pin the config *readings* the two hand-written copies used to disagree
on: log level, the executor's config, replay-cache gating and the findings
evidence chain. Nothing here touches a device — every component in the graph is
lazy about its heavy resources, and the one filesystem side effect (the
findings retention prune) is patched out.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import bootstrap
from action.action_executor import ActionExecutor
from task.replay_cache import DEFAULT_CACHE_PATH


@pytest.fixture
def no_prune(monkeypatch):
    """Record prune_old_runs calls instead of deleting anything on disk."""
    calls = []
    monkeypatch.setattr(bootstrap, "prune_old_runs", lambda *a, **kw: calls.append(a) or 0)
    return calls


# ---------- load_app ----------


def _console_level(logger) -> int:
    """The level app.log_level actually controls: the console handler's.

    The logger itself stays at DEBUG so a run-scoped file handler (run.log) can
    see the verbose trace; only the console follows the configured level.
    """
    console = [
        h for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert console, "console handler missing"
    return console[0].level


def test_load_app_reads_log_level_from_config(tmp_path):
    """The logger is built *after* the config, so app.log_level can take effect."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app:\n  log_level: DEBUG\n", encoding="utf-8")

    config, logger = bootstrap.load_app(str(config_file))

    assert config == {"app": {"log_level": "DEBUG"}}
    assert _console_level(logger) == logging.DEBUG
    # The logger itself is always wide open (run.log needs DEBUG records).
    assert logger.level == logging.DEBUG


def test_load_app_without_config_file_uses_defaults(tmp_path):
    config, logger = bootstrap.load_app(str(tmp_path / "missing.yaml"))

    assert config == {}
    assert _console_level(logger) == logging.INFO
    assert logger.level == logging.DEBUG


def test_load_app_applies_adb_timeout(tmp_path):
    from core import adb_timeout

    config_file = tmp_path / "config.yaml"
    config_file.write_text("adb:\n  timeout_s: 7\n", encoding="utf-8")
    try:
        bootstrap.load_app(str(config_file))
        assert adb_timeout.adb_timeout_s() == 7.0
    finally:
        adb_timeout.reset_adb_timeout()


# ---------- build_runtime: defaults ----------


def test_empty_config_builds_the_full_graph(fake_logger, no_prune):
    """No config.yaml is the normal case: everything on, nothing missing."""
    runtime = bootstrap.build_runtime({}, fake_logger)

    for field in (
        "device_manager", "ocr", "capturer", "dump_matcher", "template_matcher",
        "feature_matcher", "yolo", "replay_cache", "hub", "logcat",
        "screen_recorder", "findings", "executor", "engine",
    ):
        assert getattr(runtime, field) is not None, f"{field} was not assembled"

    # the graph is wired to the same instances, not to fresh duplicates
    assert runtime.hub.replay_cache is runtime.replay_cache
    assert runtime.hub.capturer is runtime.capturer
    assert runtime.engine.hub is runtime.hub
    assert runtime.engine.executor is runtime.executor
    assert runtime.engine.recorder is runtime.findings
    assert runtime.engine.logcat is runtime.logcat
    assert runtime.engine.screen is runtime.screen_recorder
    assert runtime.findings.logcat_monitor is runtime.logcat
    assert runtime.findings.screen_recorder is runtime.screen_recorder
    assert no_prune, "the retention prune must run when findings are enabled"


def test_ocr_warmup_is_wired_into_the_capture_stream(fake_logger, no_prune):
    """Losing this hook deadlocks onnxruntime behind the scrcpy decoder thread."""
    runtime = bootstrap.build_runtime({}, fake_logger)

    assert runtime.capturer._stream_warmup == runtime.ocr.ensure_loaded


# ---------- build_runtime: findings gating ----------


def test_findings_disabled_drops_the_whole_evidence_chain(fake_logger, no_prune):
    runtime = bootstrap.build_runtime({"findings": {"enabled": False}}, fake_logger)

    assert runtime.findings is None
    assert runtime.logcat is None
    assert runtime.screen_recorder is None
    assert runtime.engine.recorder is None
    assert runtime.engine.logcat is None
    assert runtime.engine.screen is None
    assert no_prune == [], "nothing to prune when findings are off"


def test_logcat_and_video_gate_independently(fake_logger, no_prune):
    runtime = bootstrap.build_runtime(
        {"findings": {"logcat": False, "video": False}}, fake_logger
    )

    assert runtime.logcat is None and runtime.screen_recorder is None
    assert runtime.findings is not None  # the recorder itself still collects
    assert no_prune, "retention prune still runs with the recorder on"


def test_findings_options_reach_the_recorder(fake_logger, no_prune, tmp_path):
    out_dir = str(tmp_path / "findings")
    runtime = bootstrap.build_runtime(
        {
            "findings": {
                "output_dir": out_dir,
                "history": False,
                "history_window_s": 12,
                "log_tail_lines": 42,
                "export_dir": str(tmp_path / "export"),
                "retention_days": 3,
            }
        },
        fake_logger,
    )

    recorder = runtime.findings
    assert str(recorder.output_dir) == out_dir
    assert recorder.history is False
    assert recorder.history_window_s == 12
    assert recorder.log_tail_lines == 42
    assert recorder.export_dir == str(tmp_path / "export")
    assert no_prune[0] == (out_dir, 3, fake_logger)


# ---------- build_runtime: replay cache gating ----------


def test_replay_cache_disabled_leaves_the_hub_without_one(fake_logger, no_prune):
    runtime = bootstrap.build_runtime({"replay_cache": {"enabled": False}}, fake_logger)

    assert runtime.replay_cache is None
    assert runtime.hub.replay_cache is None


def test_replay_cache_path_is_honoured(fake_logger, no_prune, tmp_path):
    path = tmp_path / "anchors.json"
    runtime = bootstrap.build_runtime({"replay_cache": {"path": str(path)}}, fake_logger)

    assert runtime.replay_cache.path == path


def test_replay_cache_defaults_to_the_standard_path(fake_logger, no_prune):
    runtime = bootstrap.build_runtime({}, fake_logger)

    assert runtime.replay_cache.path == Path(DEFAULT_CACHE_PATH)


# ---------- build_runtime: executor & engine config ----------


def test_executor_gets_the_whole_config_not_a_slice(fake_logger, no_prune):
    """The MCP copy used to hand it {"execution": {...}}, so verify_steps and
    every other section silently vanished on that side."""
    config = {
        "execution": {"default_swipe_duration_ms": 900, "verify_steps": True},
        "debug": {"enabled": True},
    }

    runtime = bootstrap.build_runtime(config, fake_logger)

    assert isinstance(runtime.executor, ActionExecutor)
    assert runtime.executor.config is config
    assert runtime.executor.config["execution"]["verify_steps"] is True
    assert runtime.executor.config["debug"] == {"enabled": True}


def test_engine_config_section_is_applied(fake_logger, no_prune):
    runtime = bootstrap.build_runtime(
        {"engine": {"max_steps": 123, "back_fallback": False, "drift_tolerance_px": 5}},
        fake_logger,
    )

    assert runtime.engine.max_steps == 123
    assert runtime.engine.back_fallback_default is False
    assert runtime.engine.drift_tolerance_px == 5


def test_run_log_defaults_on_and_follows_app_config(fake_logger, no_prune):
    assert bootstrap.build_runtime({}, fake_logger).engine.run_log is True
    runtime = bootstrap.build_runtime({"app": {"run_log": False}}, fake_logger)
    assert runtime.engine.run_log is False


def test_template_dir_config_reaches_both_matchers(fake_logger, no_prune, tmp_path):
    runtime = bootstrap.build_runtime({"templates": {"dir": str(tmp_path)}}, fake_logger)

    assert str(runtime.template_matcher.template_dir) == str(tmp_path)
    assert str(runtime.feature_matcher.template_dir) == str(tmp_path)


def test_yolo_config_reaches_the_detector(fake_logger, no_prune, tmp_path):
    model = tmp_path / "m.onnx"
    runtime = bootstrap.build_runtime(
        {"yolo": {"model": str(model), "classes": ["icon"], "conf": 0.5, "iou": 0.6,
                  "input_size": 320}},
        fake_logger,
    )

    assert str(runtime.yolo.model_path) == str(model)
    assert runtime.yolo.conf == 0.5
    assert runtime.yolo.iou == 0.6
    assert runtime.yolo.input_size == 320


def test_capture_backend_config_reaches_the_capturer(fake_logger, no_prune):
    runtime = bootstrap.build_runtime({"capture": {"backend": "screencap"}}, fake_logger)

    assert runtime.capturer._scrcpy_enabled is False
