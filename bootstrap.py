"""Shared startup wiring for both entry points (main.py CLI, mcp_server.py MCP).

The CLI and the MCP server drive the same machine: device manager, perception
channels, recognizer hub, findings evidence chain, action executor, task engine.
Both used to hand-assemble that graph, and two copies of the same 40-line wiring
block drift apart — the MCP copy ended up with a hardcoded log level, a
half-written ActionExecutor config, an ungated replay cache and a findings chain
that ignored `findings.enabled`. One assembly, one reading of the config, so a
config key means the same thing no matter which entry point started the process.

Lives in the repo root rather than under core/: assembly imports every layer
(perception / action / task), while core/ is forbidden from importing upwards
(.claude/rules/project-root.md). Entry-point-specific parts stay in the entry
points — wireless auto-connect, UIDetector/AgentPool (CLI), ScreenMarker /
gesture registry / suite runner / background runs (MCP).

Construction here is deliberately side-effect-light: nothing talks to a device,
the heavy resources (OCR model, scrcpy stream, YOLO) stay lazy. The only disk
touches are the screenshot output dir and the findings retention prune.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from action.action_executor import ActionExecutor
from core.adb_timeout import configure_adb_timeout
from core.config import load_config
from core.device_manager import DeviceManager
from core.logger import setup_logger
from core.notifier import build_notifiers
from perception.feature_matcher import FeatureMatcher
from perception.logcat_monitor import EvidencePolicy, LogcatMonitor
from perception.ocr_engine import OcrEngine
from perception.pcap_recorder import RollingPcapRecorder
from perception.scene_classifier import SceneClassifier
from perception.screen_recorder import RollingScreenRecorder
from perception.screenshot_capturer import ScreenshotCapturer
from perception.template_matcher import TemplateMatcher
from perception.ui_dump_matcher import UiDumpMatcher
from perception.yolo_detector import YoloDetector, YoloRegistry
from task.findings import FindingsRecorder, prune_old_runs
from task.recognizers import RecognizerHub
from task.replay_cache import DEFAULT_CACHE_PATH, ReplayCache
from task.task_engine import TaskEngine


def load_app(config_path: str = "config.yaml") -> Tuple[Dict[str, Any], Any]:
    """Load the config, build the logger from it, and arm the global adb timeout.

    Order matters: the config is read *first* so `app.log_level` can actually
    take effect — a logger created before the config always runs at INFO no
    matter what the file says. A missing config.yaml is normal (everything has
    defaults), it just yields an empty dict.
    """
    config = load_config(config_path)
    logger = setup_logger(config.get("app", {}).get("log_level", "INFO"))
    configure_adb_timeout(config.get("adb", {}))
    return config, logger


@dataclass
class Runtime:
    """The assembled object graph shared by the CLI and the MCP server."""

    device_manager: DeviceManager
    ocr: OcrEngine
    capturer: ScreenshotCapturer
    dump_matcher: UiDumpMatcher
    template_matcher: TemplateMatcher
    feature_matcher: FeatureMatcher
    yolo: YoloDetector
    yolo_registry: YoloRegistry
    scene_classifier: SceneClassifier
    replay_cache: Optional[ReplayCache]
    hub: RecognizerHub
    logcat: Optional[LogcatMonitor]
    screen_recorder: Optional[RollingScreenRecorder]
    pcap_recorder: Optional[RollingPcapRecorder]
    findings: Optional[FindingsRecorder]
    executor: ActionExecutor
    engine: TaskEngine


def build_runtime(config: Dict[str, Any], logger) -> Runtime:
    """Assemble the perception + task graph from an already-loaded config."""
    device_manager = DeviceManager(logger)

    ocr = OcrEngine(logger)
    # stream_warmup is load-bearing, not a nicety: onnxruntime's first init
    # deadlocks on Windows if an av decoder thread is already running, so OCR
    # must be warmed before the first scrcpy stream starts
    # (.claude/rules/perception-rules.md).
    capturer = ScreenshotCapturer(
        logger, capture_config=config.get("capture", {}), stream_warmup=ocr.ensure_loaded
    )
    dump_matcher = UiDumpMatcher(logger)
    template_dir = config.get("templates", {}).get("dir", "task/templates")
    template_matcher = TemplateMatcher(logger, template_dir=template_dir)
    feature_matcher = FeatureMatcher(logger, template_dir=template_dir)
    # One registry, several model files: the model at the conventional path
    # stays the default (every existing call site and task JSON keeps working),
    # while additional detection domains live in their own .onnx files, are
    # declared under `yolo.models.<name>`, and are asked for by name.
    yolo_registry = YoloRegistry(logger, config.get("yolo", {}))
    yolo = yolo_registry.default

    # Scene classifier: the other whole-frame component. Rule-based (no model
    # file, no onnxruntime), so it is always available; it borrows *this*
    # OcrEngine rather than making one, because a second rapidocr session would
    # both waste memory and reopen the scrcpy/onnxruntime warm-up deadlock
    # window (.claude/rules/perception-rules.md).
    scene_classifier = SceneClassifier(
        logger, ocr_engine=ocr, scene_config=config.get("scene", {})
    )

    cache_config = config.get("replay_cache", {})
    replay_cache = None
    if cache_config.get("enabled", True):
        replay_cache = ReplayCache(logger, path=cache_config.get("path", DEFAULT_CACHE_PATH))

    hub = RecognizerHub(
        dump_matcher, ocr, capturer, logger,
        replay_cache=replay_cache, template_matcher=template_matcher,
        yolo_detector=yolo, feature_matcher=feature_matcher,
        yolo_registry=yolo_registry, scene_classifier=scene_classifier,
    )

    # Findings are the product of this tool, so the whole evidence chain
    # (logcat crash watch, rolling screenrecord, recorder, retention prune) is
    # on by default and only disabled when the config says so explicitly.
    findings_config = config.get("findings", {})
    findings = None
    logcat = None
    screen_recorder = None
    pcap_recorder = None
    if findings_config.get("enabled", True):
        if findings_config.get("logcat", True):
            logcat = LogcatMonitor(
                logger,
                evidence_policy=EvidencePolicy.from_config(
                    findings_config.get("logcat_evidence")
                ),
            )
        if findings_config.get("video", True):
            screen_recorder = RollingScreenRecorder(
                logger, segment_s=findings_config.get("video_segment_s", 60)
            )
        # Packet capture is opt-in (needs root + tcpdump on the device), so it
        # is off unless explicitly enabled; a None recorder makes the whole
        # chain a no-op.
        pcap_config = findings_config.get("pcap", {})
        if pcap_config.get("enabled", False):
            pcap_recorder = RollingPcapRecorder(
                logger,
                segment_s=pcap_config.get("segment_s", 60),
                keep_segments=pcap_config.get("keep_segments", 2),
                snaplen=pcap_config.get("snaplen", 262144),
                bpf_filter=pcap_config.get("bpf_filter", "not port 5555 and not port 5037"),
                tcpdump_path=pcap_config.get("tcpdump_path", "tcpdump"),
                su_mode=pcap_config.get("su_mode", "auto"),
            )
        # Unattended runs (run_suite / cron) otherwise leave their findings on
        # disk unannounced; a notifier pushes one summary per run to a chat
        # robot / webhook. Off unless `findings.notifiers` is configured, and a
        # broken entry only costs the push, never the run.
        notifiers = build_notifiers(findings_config.get("notifiers", []), logger)
        findings = FindingsRecorder(
            logger, capturer, dump_matcher,
            output_dir=findings_config.get("output_dir", "outputs/findings"),
            logcat_monitor=logcat,
            history=findings_config.get("history", True),
            history_window_s=findings_config.get("history_window_s", 60),
            log_tail_lines=findings_config.get("log_tail_lines", 300),
            export_dir=findings_config.get("export_dir"),
            screen_recorder=screen_recorder,
            pcap_recorder=pcap_recorder,
            notifiers=notifiers,
        )
        prune_old_runs(
            findings_config.get("output_dir", "outputs/findings"),
            findings_config.get("retention_days", 14),
            logger,
        )

    # The executor gets the whole config, not just its own slice: it reads
    # `execution` today and any other section it grows into tomorrow.
    executor = ActionExecutor(logger, config)
    engine = TaskEngine(
        hub, executor, logger,
        findings_recorder=findings, logcat_monitor=logcat,
        screen_recorder=screen_recorder, pcap_recorder=pcap_recorder,
        engine_config=config.get("engine", {}),
        # app.run_log: tee each run's step trace into run.log next to its
        # report.json (on by default; needs the findings recorder for a folder).
        run_log=config.get("app", {}).get("run_log", True),
    )

    return Runtime(
        device_manager=device_manager,
        ocr=ocr,
        capturer=capturer,
        dump_matcher=dump_matcher,
        template_matcher=template_matcher,
        feature_matcher=feature_matcher,
        yolo=yolo,
        yolo_registry=yolo_registry,
        scene_classifier=scene_classifier,
        replay_cache=replay_cache,
        hub=hub,
        logcat=logcat,
        screen_recorder=screen_recorder,
        pcap_recorder=pcap_recorder,
        findings=findings,
        executor=executor,
        engine=engine,
    )
