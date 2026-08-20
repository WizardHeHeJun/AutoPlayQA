"""MCP server exposing the device/perception/task capabilities to AI agents.

The agent (Claude Code / Codex) is the brain: it looks at screenshots, decides
what to do, and drives the deterministic tools below. Run with:

    python mcp_server.py            # stdio transport

Claude Code picks it up via the project .mcp.json; for Codex add it to
~/.codex/config.toml (see README).
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import sys
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from action.action_schema import TASK_SCHEMA_DOC
from bootstrap import build_runtime, load_app
from core.logger import DEFAULT_LOG_DIR, LOGGER_NAME, attach_process_log, log_event
from core.notifier import build_notifiers
from perception.frame_monitor import FrameMonitorRegistry
from perception.logcat_monitor import EvidencePolicy, LogcatMonitor
from perception.screen_marker import ScreenMarker
from utils.image_scale import downscale_short_edge
from record.action_log import (
    DEFAULT_SESSION_ROOT,
    ActionLogError,
    ActionLogRegistry,
    action_succeeded,
    find_element_at,
)
from record.record_session import GestureRecordingRegistry
from task.custom_actions import registered_names
from task.findings import FindingsRecorder
from task.sentinel import (
    DEFAULT_BLANK_MIN_FRAMES,
    DEFAULT_BLANK_STDDEV,
    DEFAULT_LOGCAT_POLL_INTERVAL_S,
    MonitorSentinel,
)
from task.step_numbering import compute_step_labels, format_task_outline
from task.suite_runner import SuiteRunner
from task.task_lint import lint_task
from task.task_loader import (
    DEFAULT_TASK_DIR,
    SuiteValidationError,
    TaskValidationError,
    get_task_path,
    list_suites as _list_suite_names,
    list_tasks as _list_task_names,
    load_suite,
    load_task,
    resolve_task,
)

mcp = FastMCP("autoplayqa")

# The shared object graph (perception channels, recognizer hub, findings
# evidence chain, executor, engine) is assembled once in bootstrap.py, so the
# CLI and this server read the same config the same way. The module-level
# aliases below are the server's handles on it — tests patch them by name.
_config, _logger = load_app("config.yaml")
_runtime = build_runtime(_config, _logger)
_device_manager = _runtime.device_manager
_ocr = _runtime.ocr
_capturer = _runtime.capturer
_matcher = _runtime.dump_matcher
_template_matcher = _runtime.template_matcher
_feature_matcher = _runtime.feature_matcher
_yolo = _runtime.yolo
_yolo_registry = _runtime.yolo_registry
_scene_classifier = _runtime.scene_classifier
_replay_cache = _runtime.replay_cache
_hub = _runtime.hub
_logcat = _runtime.logcat
_screen_recorder = _runtime.screen_recorder
_findings = _runtime.findings
_executor = _runtime.executor
_engine = _runtime.engine

# ---- MCP-only wiring (no CLI counterpart) ----
_marker = ScreenMarker(_logger, _capturer, _matcher, _ocr)
# Per-device table from the most recent screenshot_marked, so click_index can
# resolve an index back to its tap point across stateless MCP calls.
_last_marks: Dict[str, List[Dict]] = {}
# Gesture recordings: one live session per device, shared start/stop semantics
# with the CLI's `record gestures` sub-commands.
_gesture_registry = GestureRecordingRegistry(_capturer, _logger, device_manager=_device_manager)
# Agent action logs: what *this* agent tapped, one live session per device.
# Off unless explicitly started, so the action tools below pay nothing (a dict
# lookup) in the normal case.
_action_log = ActionLogRegistry(
    _logger,
    output_root=_config.get("recording", {}).get("agent_sessions_dir", DEFAULT_SESSION_ROOT),
)
# Suite orchestration (log in once, chain the cases): pure sequencing on top of
# the same engine, so each case still gets its own findings run directory.
_suite_runner = SuiteRunner(_engine, _logger, findings_recorder=_findings)
# Background frame monitors: one polling loop per device, sharing *this*
# capturer (never a second one — the scrcpy pool and the OCR warmup order live
# in that instance). Nothing runs until start_monitor is called.
_monitors = FrameMonitorRegistry(_logger, _capturer)

# ---- monitor sentinels: cheap anomaly watch over the monitor's frames ----
#
# The window this covers is the one nothing else does: while the engine is
# suspended on an `agent` node its run is already finalized, so the screen and
# logcat go unwatched for the whole handoff. A sentinel rides the frames the
# monitor is capturing anyway and turns a white screen or a crash there into a
# normal findings run.
#
# Two things are deliberately NOT shared with the engine:
#   * the FindingsRecorder — it holds per-run state (run dir, timeline, findings
#     list) for the run it is recording; sentinel findings would corrupt it;
#   * the LogcatMonitor — poll() dedups per instance, so two pollers on one
#     instance steal each other's events.
# The heavy evidence channels (rolling screenrecord, pcap) stay engine-only: a
# sentinel finding gets the pinned frame plus the log tail, nothing that needs a
# device-side recorder running.
_sentinel_config = _config.get("sentinel", {}) or {}
_sentinels: Dict[str, MonitorSentinel] = {}
_sentinels_lock = threading.Lock()
# Run-summary push, read from the same `findings.notifiers` config the engine's
# recorder uses (bootstrap builds its own set for that one and does not publish
# it on Runtime). A crash caught during a handoff is exactly the kind of thing
# an unattended run wants pushed, so a sentinel run must not be the one findings
# run that stays silent. Built once — notifiers are stateless senders.
_sentinel_notifiers = build_notifiers(
    (_config.get("findings", {}) or {}).get("notifiers", []), _logger
)


def _build_sentinel(device_id: str) -> MonitorSentinel:
    """One sentinel for a device, with its own recorder and logcat monitor."""
    findings_config = _config.get("findings", {}) or {}
    logcat = LogcatMonitor(
        _logger,
        evidence_policy=EvidencePolicy.from_config(findings_config.get("logcat_evidence")),
    )
    recorder = FindingsRecorder(
        _logger, _capturer, _matcher,
        # Same tree as the engine's findings, so triage / retention / export all
        # see a sentinel run as just another run.
        output_dir=findings_config.get("output_dir", "outputs/findings"),
        logcat_monitor=logcat,
        # No rolling frame history: the sentinel never calls snapshot_history,
        # and its evidence is the frame that tripped the check.
        history=False,
        log_tail_lines=findings_config.get("log_tail_lines", 300),
        export_dir=findings_config.get("export_dir"),
        notifiers=_sentinel_notifiers,
    )
    return MonitorSentinel(
        _logger, device_id, recorder, logcat_monitor=logcat, engine=_engine,
        blank_stddev=_sentinel_config.get("blank_stddev", DEFAULT_BLANK_STDDEV),
        blank_min_frames=_sentinel_config.get("blank_min_frames", DEFAULT_BLANK_MIN_FRAMES),
        logcat_poll_interval_s=_sentinel_config.get(
            "logcat_poll_interval_s", DEFAULT_LOGCAT_POLL_INTERVAL_S
        ),
    )


def _sentinel_for(device_id: str) -> Optional[MonitorSentinel]:
    with _sentinels_lock:
        return _sentinels.get(device_id)

# Background task runs (see start_task / get_run_status). The engine is a
# singleton with per-run instance state (_task_name, recorder, logcat/screen),
# so only one background run may be active at a time. Each run_id maps to a
# mutable state dict that the worker thread and the on_step callback update
# under _runs_lock; get_run_status reads it.
_runs: Dict[str, Dict] = {}
_runs_lock = threading.Lock()


def tool(pure: bool = False) -> Callable:
    """Register an MCP tool, off the event loop unless it is a pure state read.

    FastMCP's stdio server dispatches every request as an asyncio task, but a
    *synchronous* tool body is awaited inline on the event loop thread
    (mcp/server/fastmcp/utilities/func_metadata.py: `return fn(**args)`). So one
    blocking call — a wedged adb round trip inside screenshot() — freezes the
    whole server: the receive loop never runs again, and every later request,
    including a one-microsecond get_run_status poll, sits unread in the stdin
    pipe until the client's idle timeout fires. That is the "unrelated cheap
    tool hangs for 1800s" failure this indirection exists to prevent.

    So anything that can block (device I/O, disk, OCR, model loading) is wrapped
    into an async tool that runs the body in a worker thread, leaving the loop
    free to answer other calls. `pure=True` marks tools that only read in-memory
    state and are guaranteed not to block (get_run_status): those stay
    synchronous, which is both correct and one thread-hop cheaper.

    The module attribute keeps the plain synchronous function — tests and the
    CLI call these directly — while FastMCP receives the wrapper. Both go
    through `_instrument`, so a call is logged exactly once whichever way it
    arrives.
    """

    def decorator(fn: Callable) -> Callable:
        logged = _instrument(fn)
        if pure:
            mcp.tool()(logged)
            return logged

        @functools.wraps(logged)
        async def offloaded(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(logged, *args, **kwargs))

        # functools.wraps copies __wrapped__/__annotations__, so FastMCP still
        # derives the tool schema and docstring from the original signature.
        mcp.tool()(offloaded)
        return logged

    return decorator


# ---------- tool-call instrumentation ----------
#
# The MCP server is the *other* half of this project's flight recorder: an agent
# driving the device by hand (click / swipe / screenshot) never enters
# TaskEngine.run, so none of it reached run.log. Every tool call now leaves one
# `EVT mcp_tool` line in the process log instead.

#: Argument names worth logging, by kind. Anything not listed is skipped — a
#: tool argument can be a whole task JSON or a base64 blob, and the log line
#: must stay one short greppable row (never stringify a payload).
_LOG_ARGS_SCALAR = (
    "device_id", "name", "index", "x", "y", "x1", "y1", "x2", "y2",
    "key", "kind", "start_after", "node", "duration_ms", "full_resolution",
)
_LOG_ARGS_TEXT = ("text", "expected", "address", "label", "template", "model")
#: Text-ish arguments are truncated to this many characters.
MAX_LOG_TEXT = 40


def _tool_call_fields(signature: "inspect.Signature", args: Tuple, kwargs: Dict) -> Dict:
    """Cheap scalar summary of one call's arguments (never the payloads).

    Only what the caller actually passed: defaults are deliberately not filled
    in, so every screenshot line does not carry `full_resolution=False`.
    """
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:  # a bad call still gets logged, just without its args
        return {}
    fields: Dict = {}
    for key, value in bound.arguments.items():
        if key in _LOG_ARGS_SCALAR and isinstance(value, (str, int, float, bool)):
            fields["device" if key == "device_id" else key] = value
        elif key in _LOG_ARGS_TEXT and isinstance(value, str):
            fields[key] = value if len(value) <= MAX_LOG_TEXT else value[:MAX_LOG_TEXT] + "..."
    return fields


def _tool_result_fields(result) -> Dict:
    """Size-only summary of what came back (an image never gets stringified)."""
    if isinstance(result, list):
        return {"n": len(result)}
    if not isinstance(result, dict):
        return {}
    fields: Dict = {}
    if isinstance(result.get("ok"), bool):
        fields["result_ok"] = 1 if result["ok"] else 0
    width, height = result.get("image_width"), result.get("image_height")
    if isinstance(width, int) and isinstance(height, int):
        fields["img"] = f"{width}x{height}"
    for key in ("elements", "frames", "items"):
        value = result.get(key)
        if isinstance(value, list):
            fields[f"{key}_len"] = len(value)
    return fields


def _instrument(fn: Callable) -> Callable:
    """Wrap one tool body with the `EVT mcp_tool` timing line.

    Observation only: the result is passed through untouched and an exception is
    logged and re-raised, never swallowed — MCP turns it into the client's error
    response, which is the contract the tools already have.
    """
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def instrumented(*args, **kwargs):
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            _logger.warning(
                "MCP tool '%s' failed after %dms: %s: %s", fn.__name__,
                int((time.perf_counter() - started) * 1000), type(exc).__name__, exc,
            )
            raise
        log_event(
            _logger, "mcp_tool", tool=fn.__name__,
            **_tool_call_fields(signature, args, kwargs),
            ms=int((time.perf_counter() - started) * 1000), ok=1,
            **_tool_result_fields(result),
        )
        return result

    return instrumented


# ---------- devices & perception ----------

@tool()
def list_devices() -> List[Dict]:
    """List connected Android devices/emulators (adb)."""
    devices = _device_manager.discover_devices()
    return [{"device_id": d.device_id, "type": d.device_type, "model": d.model} for d in devices]


@tool()
def connect_device(address: str) -> Dict:
    """Connect to a device over Wi-Fi (adb connect). address is "ip" or "ip:port", port defaults to 5555.

    For a USB device, call enable_wireless first to switch it to TCP/IP mode and get the address.
    """
    return _device_manager.connect(address)


@tool()
def disconnect_device(address: Optional[str] = None) -> Dict:
    """Disconnect a wireless adb device; omit address to disconnect all wireless devices."""
    return _device_manager.disconnect(address)


@tool()
def enable_wireless(device_id: str, port: int = 5555) -> Dict:
    """Switch a USB-connected device's adbd to TCP/IP mode (adb tcpip).

    Returns {ok, address, message}; pass address to connect_device. Resets on device reboot.
    """
    return _device_manager.enable_tcpip(device_id, port)


@tool()
def pair_device(address: str, code: str) -> Dict:
    """Pair with an Android 11+ wireless-debugging device (adb pair). One-time per machine.

    address uses the *pairing* port shown in the phone's pairing dialog (differs from
    the connect port). After pairing, call connect_device with the connect port.
    """
    return _device_manager.pair(address, code)


@tool()
def screenshot(device_id: str, full_resolution: bool = False) -> Dict:
    """Capture the device screen to a PNG file; returns the path (read it to view) and size.

    The saved PNG is **downscaled by default** (short edge normalised to 720px,
    aspect ratio kept) to keep your image-token bill down — plenty to see what
    is on screen. Pass full_resolution=true when you need exact pixels (reading
    fine print, cropping a template region, measuring a bounding box).

    Returns {path, width, height, image_width, image_height, scale}: width/height
    are the device's native resolution and the coordinate space click() expects;
    image_width/image_height describe the saved file, and scale is
    image-pixels / device-pixels (1.0 when nothing was resized). To turn a point
    you read off the picture into a tap point, divide it by scale.
    """
    image = _capturer.capture_image(device_id)
    width, height = image.size
    # Return face only: the capture handed to recognition (ocr/find_text/
    # find_template/detect_objects) is never touched by this.
    out, scale = (image, 1.0) if full_resolution else downscale_short_edge(image)
    path = _capturer.save_image(out, device_id, "mcp")
    return {
        "path": path,
        "width": width,
        "height": height,
        "image_width": out.width,
        "image_height": out.height,
        "scale": round(scale, 4),
    }


@tool()
def ui_dump(device_id: str) -> List[Dict]:
    """Dump the UI hierarchy (uiautomator). Returns visible nodes with text/desc/center/bounds.

    Free and fast, but games rendering on a single surface return few or no nodes —
    use ocr or screenshot there.
    """
    xml_text = _matcher.dump_ui_xml(device_id)
    nodes = _matcher.extract_nodes(xml_text) if xml_text else []
    return [
        {
            "text": n["text"],
            "desc": n["desc"],
            "resource_id": n["resource_id"],
            "class": n["class_name"],
            "clickable": n["clickable"],
            "center": list(n["center"]),
            "bounds": list(n["bounds"]),
        }
        for n in nodes
        if n["text"] or n["desc"] or n["clickable"]
    ]


@tool()
def find_text(device_id: str, text: str) -> Dict:
    """Locate on-screen text and return its tap point. Tries uiautomator dump first, local OCR second.

    Returns {found, center, score, channel, matched_text}; check `found` before clicking.
    """
    node, score = _matcher.match_text(device_id, text)
    if node and score >= 0.65:
        return {
            "found": True,
            "center": list(node["center"]),
            "score": round(score, 3),
            "channel": "ui_text",
            "matched_text": node["text"] or node["desc"],
        }

    if _ocr.available():
        image = _capturer.capture_image(device_id)
        best, best_score = None, 0.0
        for item in _ocr.recognize(image):
            s = _matcher.text_similarity(text, item["text"])
            if s > best_score:
                best, best_score = item, s
        if best and best_score >= 0.65:
            return {
                "found": True,
                "center": list(best["center"]),
                "score": round(best_score, 3),
                "channel": "ocr",
                "matched_text": best["text"],
            }

    return {"found": False, "center": None, "score": 0.0, "channel": None, "matched_text": None}


@tool()
def ocr(device_id: str, roi: Optional[List[int]] = None) -> List[Dict]:
    """Run local OCR on the screen. roi: optional [x1, y1, x2, y2] pixels.

    Returns [{text, score, bbox, center}] in full-screen coordinates.
    """
    return _ocr.recognize(_capturer.capture_image(device_id), roi=roi)


@tool()
def screenshot_marked(device_id: str, source: str = "auto",
                      full_resolution: bool = False) -> Dict:
    """Capture a Set-of-Marks screenshot: numbered badges over the screen's
    tappable controls and text, so you can act by index instead of guessing
    pixel coordinates.

    Returns {path, width, height, image_width, image_height, scale, source,
    elements}; read `path` to view the annotated frame, then call
    click_index(device_id, index) to tap an element by its badge number. Each
    element is {index, source, text, desc, center, bounds, clickable} in
    absolute device pixels (red badge = clickable control, blue = plain text).
    Indices run in reading order (top-to-bottom, left-to-right).

    The annotated PNG is **downscaled by default** (short edge normalised to
    720px) to save image tokens; badges are drawn after the resize so they stay
    readable. Pass full_resolution=true for the native-resolution frame. Either
    way the element table — and therefore click_index — stays in device pixels,
    so tapping by index is unaffected; width/height are the device's native
    size, image_width/image_height describe the saved file.

    source: "auto" (uiautomator dump, with OCR fallback when the dump is sparse,
    e.g. games on a single surface), "dump", "ocr", or "both". Slower than a
    plain screenshot (it runs a dump and/or OCR), so use it for the handoff
    round, not tight loops.
    """
    result = _marker.mark(device_id, source=source, full_resolution=full_resolution)
    _last_marks[device_id] = result["elements"]
    return result


@tool()
def find_template(device_id: str, template: str, threshold: float = 0.8,
                  roi: Optional[List[int]] = None, multi: bool = False,
                  scales: Optional[List[float]] = None) -> Dict:
    """Locate an icon/building image on screen via OpenCV template matching.

    This is the eye for game-surface graphics the text channels are blind to:
    uiautomator sees no nodes on a single render surface and OCR only reads
    labels, so sprite-only buildings and icons (pure images) need template
    matching. Capture the template first with capture_template, then match it.

    template: a stored template name (file stem under task/templates/) or a
    path. threshold: TM_CCOEFF_NORMED correlation gate in [0,1] (0.8 default;
    lower = more lenient). roi: optional [x1,y1,x2,y2] to search within.
    multi: True returns every instance (e.g. every crate icon), else the best one.
    scales: optional sizes to sweep for resolution robustness, e.g.
    [0.9, 1.0, 1.1]; default matches at native size only.

    Returns {found, count, matches, center, score, bbox}: each match is
    {name, score, bbox:[x1,y1,x2,y2], center:[x,y], scale} in absolute pixels;
    center/score/bbox echo the best match. Tap a hit with click(device_id, x, y).
    """
    if not _template_matcher.available():
        return {"found": False, "error": "opencv not installed", "matches": [], "count": 0}
    image = _capturer.capture_image(device_id)
    try:
        matches = _template_matcher.match_all(
            image, template, threshold=threshold, roi=roi, scales=scales,
            max_results=20 if multi else 1,
        )
    except FileNotFoundError as exc:
        return {"found": False, "error": str(exc), "matches": [], "count": 0}
    if not matches:
        return {"found": False, "count": 0, "matches": [], "center": None, "score": 0.0}
    best = matches[0]
    return {
        "found": True,
        "count": len(matches),
        "matches": matches,
        "center": best["center"],
        "score": best["score"],
        "bbox": best["bbox"],
    }


@tool()
def list_templates() -> List[str]:
    """List stored template image names (stems) under the template directory."""
    return _template_matcher.list_templates()


@tool()
def capture_template(device_id: str, name: str, region: List[int]) -> Dict:
    """Crop a region of the current screen and save it as a reusable template.

    Closes the capture→match loop: screenshot the screen, find the building/icon's
    bounding box (e.g. from screenshot_marked or by eye), pass it here as
    region=[x1,y1,x2,y2], then locate that sprite on later frames with
    find_template(name) or a task node with recognition {"type":"template",
    "template":"<name>"}. Overwrites an existing same-name template.

    Returns {ok, name, path, size}. Crop tightly around the distinctive art and
    avoid moving overlays (timers, badges) for a stable match.
    """
    image = _capturer.capture_image(device_id)
    path = _template_matcher.save_template(image, name, region=region)
    stem = name[:-4] if name.lower().endswith(".png") else name
    return {"ok": True, "name": stem, "path": path, "region": region}


@tool()
def detect_objects(device_id: str, classes: Optional[List[str]] = None,
                   conf: float = 0.25, roi: Optional[List[int]] = None,
                   model: Optional[str] = None) -> Dict:
    """Detect & classify on-screen objects with a trained YOLO model.

    The fourth perception channel: where template matching breaks on
    scale/occlusion and the text channels can't read sprites, a YOLO model
    locates and *labels* on-screen objects. Requires a model at
    task/models/yolo.onnx (train with ultralytics, `yolo export format=onnx`);
    no model ships with the framework, so until you drop one in this returns
    found=False with a hint.

    model: which trained model to ask, for projects with several detection
    domains. Omit it for the default model (config `yolo.model`); pass a name
    to reach one of the extra models registered beside it — every other *.onnx
    in task/models/ registers under its filename stem.
    list_yolo_classes() enumerates the models and classes.
    classes: optional class-name whitelist. conf: confidence gate (default 0.25).
    roi: optional [x1,y1,x2,y2] to detect within. Returns
    {found, count, detections}, each detection
    {label, class_id, score, bbox:[x1,y1,x2,y2], center:[x,y]} in absolute
    pixels, best score first; tap one with click(device_id, x, y).
    """
    detector = _yolo_registry.get(model) if model else _yolo
    if detector is None:
        return {"found": False, "count": 0, "detections": [],
                "error": f"Unknown YOLO model '{model}'; known: {_yolo_registry.names()}"}
    if not detector.available():
        return {"found": False, "count": 0, "detections": [],
                "error": f"No YOLO model at {detector.model_path} (or onnxruntime missing)."}
    image = _capturer.capture_image(device_id)
    dets = detector.detect(image, conf=conf, classes=classes, roi=roi)
    return {"found": bool(dets), "count": len(dets), "detections": dets,
            "model": model or "default"}


@tool()
def list_yolo_classes(model: Optional[str] = None) -> Dict:
    """List a YOLO model's class names (id -> name), or report no model.

    Use to see what the detector can recognize before calling detect_objects.
    `models` in the reply lists every configured model name; pass one as `model`
    to read that model's classes instead of the default one's. When the model
    has an entry in task/models/models.json the reply also carries `version` /
    `date` / `notes` (omitted when there is no manifest entry for it).
    """
    detector = _yolo_registry.get(model) if model else _yolo
    if detector is None:
        return {"available": False, "classes": {}, "models": _yolo_registry.names(),
                "error": f"Unknown YOLO model '{model}'; known: {_yolo_registry.names()}"}
    if not detector.available():
        return {"available": False, "classes": {}, "models": _yolo_registry.names(),
                "error": f"No YOLO model at {detector.model_path} (or onnxruntime missing)."}
    result = {"available": True, "classes": detector.class_names(),
             "models": _yolo_registry.names(), "model": model or "default"}
    manifest_entry = _yolo_registry.model_info(model).get("manifest") or {}
    for key in ("version", "date", "notes"):
        if key in manifest_entry:
            result[key] = manifest_entry[key]
    return result


@tool()
def classify_scene(device_id: str) -> Dict:
    """Name which functional screen the app is currently showing.

    The channel above the anchor-hunting ones: instead of asking "is this text
    on screen", it screenshots once and runs cheap rule probes (pixel stats,
    then narrow-band OCR — never a full-screen read) to say *which functional
    screen* this is. Use it for the handoff round — orienting after a crash, a
    resume, or an unexpected state — not in a tight loop.

    The label set is NOT fixed by this framework. The built-in taxonomy ships
    exactly one scene, "blank" (near-black / screen-off / empty frame); every
    other label comes from a probe the integrating project registers with
    perception.scene_classifier.register_scene_probe(label, fn,
    description=..., order=...). So read `taxonomy` in the reply to learn what
    *this* installation can actually name, instead of assuming a vocabulary.

    Returns {scene, confidence, evidence, checked, elapsed_ms, taxonomy}:
      * `scene` is a dotted label — "blank" plus whatever the project
        registered (e.g. "popup", "popup.error", "menu.settings") — or
        "unknown" when no probe fired. **unknown is never a guess**: read
        `checked` (the probes that ran, in order) and look at a screenshot.
      * `confidence` 0..1 and `evidence` (what matched, where, how strongly)
        come with every verdict — a label alone is not diagnosable.
      * `other_app` is not a scene: it means the foreground package is not the
        app under test (only reported when `scene.game_packages` is configured).
      * `taxonomy` merges the built-ins with the registered probes: every label,
        which ones have a probe today and which are declared but not yet
        implemented.

    A task node can gate on the same labels with recognition
    {"type": "scene", "expected": "popup"} — a dotted PREFIX match, so
    "popup" also accepts "popup.error" and "menu" accepts "menu.settings".
    """
    image = _capturer.capture_image(device_id)
    reading = _scene_classifier.classify(image, device_id=device_id)
    result = reading.to_dict()
    result["taxonomy"] = _scene_classifier.taxonomy()
    result["ocr_available"] = _scene_classifier.ocr_available()
    return result


# ---------- background frame monitoring ----------

def _resolve_device(device_id: Optional[str]) -> Tuple[Optional[str], Optional[Dict]]:
    """Return (device_id, error) — resolve an omitted device_id to the only one.

    The monitor tools are meant to be cheap to call in a loop, and the common
    case is a single phone on the bench, so device_id is optional there. It is
    resolved rather than guessed: zero or several devices come back as an error
    naming what was found instead of silently picking one.
    """
    if device_id:
        return device_id, None
    devices = _device_manager.discover_devices()
    if len(devices) == 1:
        return devices[0].device_id, None
    if not devices:
        return None, {"ok": False, "error": "No device connected (adb devices is empty)."}
    ids = [d.device_id for d in devices]
    return None, {"ok": False,
                  "error": f"{len(ids)} devices connected; pass device_id explicitly: {ids}"}


@tool()
def start_monitor(device_id: Optional[str] = None, interval_ms: int = 1000,
                  max_frames: int = 200, full_resolution: bool = False,
                  sentinel: bool = True) -> Dict:
    """Start capturing the device screen in the background, every interval_ms.

    Frame supply without a turn per frame: instead of calling screenshot in a
    polling loop (one round trip *and* one agent turn per look), a background
    thread writes frames to disk and you pull whatever appeared since your last
    look with get_new_frames — read only the frames you actually care about.
    Made for watching a user demonstrate a flow (live-record) or keeping an eye
    on a long run.

    device_id may be omitted when exactly one device is connected. interval_ms
    is the target period between captures (>= 100ms; a slower capture just
    stretches the gap, frames never stack up). max_frames is the disk budget:
    only the newest N PNGs are kept, older ones are deleted as new frames land,
    so a monitor left running has a bounded footprint. Frames are downscaled to
    a 720px short edge like screenshot; pass full_resolution=true for native
    pixels.

    sentinel (default true) additionally runs a cheap anomaly watch over those
    same frames while no task run is in flight — exactly the window an `agent`
    handoff leaves unwatched. It flags a screen that stays blank and drains
    logcat for crashes/ANRs, filing them as an ordinary findings run (task
    "monitor_sentinel") with the offending frame as evidence. It costs no extra
    capture, stays silent while the engine is running (its own watchdogs own
    that screen), and writes nothing at all when it sees nothing. Pass
    sentinel=false to capture frames only.

    Starting a monitor on a device that already has one stops the old loop
    first — the reply's "restarted" says whether that happened. Returns
    {ok, device_id, running, monitor_dir, interval_ms, max_frames,
    full_resolution, frames_total, restarted, sentinel}. "sentinel" is its
    stats dict (or {"enabled": false}). Call stop_monitor when done; a loop that
    cannot capture (device gone, adb wedged) stops itself after 10 consecutive
    failures and reports it in "latched_off".
    """
    resolved, error = _resolve_device(device_id)
    if error:
        return error

    # A restart replaces the loop, so the old sentinel's run is sealed here
    # rather than left dangling with a half-written report.
    with _sentinels_lock:
        previous = _sentinels.pop(resolved, None)
    if previous is not None:
        previous.finalize()

    watcher: Optional[MonitorSentinel] = None
    if sentinel and _sentinel_config.get("enabled", True):
        watcher = _build_sentinel(resolved)

    result = _monitors.start(
        resolved, interval_ms=interval_ms, max_frames=max_frames,
        full_resolution=full_resolution,
        frame_sink=watcher.on_frame if watcher is not None else None,
    )
    if watcher is not None and result.get("ok"):
        with _sentinels_lock:
            _sentinels[resolved] = watcher
        result["sentinel"] = watcher.stats()
    else:
        result["sentinel"] = {"enabled": False}
    return result


@tool()
def get_new_frames(device_id: Optional[str] = None) -> Dict:
    """Return the frames captured since your last call (paths, not images).

    The cursor is kept server-side per device, so each call hands back exactly
    what is new — no bookkeeping on your side and no re-reading the same frame.
    Only paths come back: read the ones you need (the newest, or the one where
    something changed) and skip the rest, which is what keeps this cheap.

    Returns {ok, device_id, running, monitor_dir, frames, new_count,
    frames_total, frames_on_disk, dropped, failures, latched_off, interval_ms,
    max_frames}. Each frame is {index (capture order), path, ts_ms (wall-clock
    epoch ms), width, height}. "dropped" counts frames the ring deleted before
    you read them — if it keeps climbing, poll more often or raise max_frames.
    "failures" counts capture errors, and a non-null "latched_off" means the
    loop gave up (running is then false); the frames captured before that are
    still on disk and still drainable.

    "sentinel" carries the anomaly watch's counters when one is attached
    (findings_count, blank_episode_active, checked_frames, gated_frames — frames
    skipped because a task run was in flight — logcat_polls, errors); a rising
    findings_count means a sentinel run is being written under outputs/findings.

    Works after stop_monitor too, so you can drain the tail of a finished
    monitor. Calling it for a device that never had one returns
    {ok: False, error}.
    """
    resolved, error = _resolve_device(device_id)
    if error:
        return error
    result = _monitors.new_frames(resolved)
    watcher = _sentinel_for(resolved)
    result["sentinel"] = watcher.stats() if watcher is not None else {"enabled": False}
    return result


@tool()
def stop_monitor(device_id: Optional[str] = None) -> Dict:
    """Stop the device's background frame monitor and return its final summary.

    Idempotent: stopping an already-stopped monitor returns the same summary
    with already_stopped=true rather than an error, and the captured frames stay
    on disk (drain them with get_new_frames afterwards). Monitors are also
    stopped automatically when the server process exits.

    Returns {ok, device_id, running (false), monitor_dir, frames_total,
    frames_on_disk, dropped, failures, latched_off, already_stopped, sentinel}.
    Stopping a device that never had a monitor returns {ok: False, error}.

    The attached sentinel is sealed here: its findings run is finalized and
    "sentinel" carries the final stats plus "report" (run_dir / report_path /
    export_path) when it recorded anything, and its summary is pushed to any
    configured findings.notifiers like any other run. A sentinel that saw
    nothing leaves no run directory and no "report". Calling stop_monitor again
    replays the same "sentinel" block — the run is only sealed once.
    """
    resolved, error = _resolve_device(device_id)
    if error:
        return error
    # Stop the loop before sealing the sentinel, so no frame can arrive after
    # its report has been written.
    result = _monitors.stop(resolved)
    watcher = _sentinel_for(resolved)
    if watcher is None:
        result["sentinel"] = {"enabled": False}
        return result
    report = watcher.finalize()
    stats = watcher.stats()
    if report:
        stats["report"] = report
    result["sentinel"] = stats
    return result


# ---------- actions ----------

def _log_before_frame(session, device_id: str) -> Optional[bytes]:
    """Pre-action frame for an active action log (None when nothing is logging).

    Capturing is the expensive half of a logged step, so it is gated on the
    session: with no session running the action tools behave exactly as before.
    A capture failure must never take the action down with it — the step is
    still logged, just without evidence.
    """
    if session is None:
        return None
    try:
        return _capturer.capture_png_bytes(device_id)
    except Exception as exc:  # noqa: BLE001 - evidence is best effort, the tap is not
        _logger.warning("action log: pre-action screenshot failed on %s: %s", device_id, exc)
        return None


def _log_action(session, tool_name: str, action: Dict, result: Dict,
                element: Optional[Dict] = None, screenshot: Optional[bytes] = None) -> None:
    """Append the executed action to the device's log, if one is running."""
    if session is None or not action_succeeded(result):
        return
    session.log_step(tool_name, action, element=element, screenshot_png=screenshot)


@tool()
def click(device_id: str, x: int, y: int) -> Dict:
    """Tap the screen at absolute pixel coordinates."""
    session = _action_log.active(device_id)
    frame = _log_before_frame(session, device_id)
    action = {"type": "click", "params": {"x": x, "y": y}}
    result = _executor.execute(device_id, action)
    _log_action(session, "click", action, result, screenshot=frame,
                element=find_element_at(_last_marks.get(device_id), x, y))
    return result


@tool()
def click_index(device_id: str, index: int) -> Dict:
    """Tap the element with the given badge number from the last screenshot_marked.

    Resolves the index against the most recent screenshot_marked table for this
    device (call that first). Returns {ok, tapped: {index, text, center}} so you
    can confirm what was hit. Indices are only valid for the frame they were
    generated from — re-mark the screen after the UI changes.
    """
    marks = _last_marks.get(device_id)
    if not marks:
        return {"ok": False, "error": "No marked screen for this device; call screenshot_marked first."}
    el = next((e for e in marks if e["index"] == index), None)
    if el is None:
        return {"ok": False, "error": f"Index {index} not in last marked screen (1..{len(marks)})."}
    x, y = el["center"]
    session = _action_log.active(device_id)
    frame = _log_before_frame(session, device_id)
    action = {"type": "click", "params": {"x": x, "y": y}}
    result = _executor.execute(device_id, action)
    _log_action(session, "click_index", action, result, element=el, screenshot=frame)
    return {"ok": True, "tapped": {"index": index, "text": el["text"] or el["desc"], "center": [x, y]}}


@tool()
def swipe(device_id: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> Dict:
    """Swipe/drag from (x1,y1) to (x2,y2) over duration_ms milliseconds."""
    session = _action_log.active(device_id)
    frame = _log_before_frame(session, device_id)
    action = {"type": "drag",
              "params": {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms}}
    result = _executor.execute(device_id, action)
    _log_action(session, "swipe", action, result, screenshot=frame)
    return result


@tool()
def input_text(device_id: str, text: str) -> Dict:
    """Type text into the currently focused field (ASCII only via adb)."""
    session = _action_log.active(device_id)
    frame = _log_before_frame(session, device_id)
    action = {"type": "input_text", "params": {"text": text}}
    result = _executor.execute(device_id, action)
    _log_action(session, "input_text", action, result, screenshot=frame)
    return result


@tool()
def press_key(device_id: str, keycode: int) -> Dict:
    """Press an Android key by keycode. Common: 3=Home, 4=Back, 82=Menu, 224=Wake, 26=Power."""
    session = _action_log.active(device_id)
    frame = _log_before_frame(session, device_id)
    action = {"type": "key", "params": {"keycode": keycode}}
    result = _executor.execute(device_id, action)
    _log_action(session, "press_key", action, result, screenshot=frame)
    return result


# ---------- agent action logging (self-recording) ----------

@tool()
def record_actions_start(device_id: str, kind: str = "explore", task: Optional[str] = None,
                         node: Optional[str] = None, run_id: Optional[str] = None,
                         label: Optional[str] = None) -> Dict:
    """Start logging the actions *you* drive on this device.

    The counterpart to record_gestures_start (which records the *user's*
    fingers): while a session is open, every click / click_index / swipe /
    input_text / press_key you call is appended to a session log together with
    the frame captured just before it and, when the tap resolved to a marked
    element, that element's text and bounds. Nothing else changes — the tools
    return exactly what they always return.

    Two uses, set by `kind`:
      * "explore" (default) — you are driving the game yourself to learn a flow.
        Stop the session afterwards and turn the log into a task draft: each
        step carries the anchor text it hit, so you can write recognition-gated
        nodes instead of replaying coordinates.
      * "handoff" — a task run hit an `agent` node and handed control to you.
        Pass task=<task name>, node=<node name> and run_id from the run result
        so the manual round is archived next to the deterministic ones instead
        of being a hole in the trace.

    `label` names the output folder (defaults to `kind`). Artifacts land in
    outputs/agent_sessions/<timestamp>_<label>/ and session.json is rewritten
    after every step, so an interrupted session still leaves the log on disk.
    Returns {ok, device_id, session_dir, manifest_path, started_at, context,
    step_count}. One live log per device: starting a second returns
    {ok: False, error} naming the active session — call record_actions_stop
    first. Screenshots cost one capture per action, so stop the session when
    you are done exploring.
    """
    try:
        session = _action_log.start(device_id, kind=kind, task=task, node=node,
                                    run_id=run_id, label=label)
    except ActionLogError as exc:
        active = _action_log.active(device_id)
        return {"ok": False, "device_id": device_id, "error": str(exc),
                "session_dir": active.session_dir.as_posix() if active else None}
    except OSError as exc:
        _logger.warning("action log: could not start a session on %s: %s", device_id, exc)
        return {"ok": False, "device_id": device_id,
                "error": f"Could not start the action log for '{device_id}': {exc}"}
    return session.summary()


@tool()
def record_actions_stop(device_id: str) -> Dict:
    """Stop the device's action log and return what was captured.

    Returns {ok, device_id, session_dir, manifest_path, started_at, ended_at,
    context, step_count, steps}. Each step is {index, t_offset_ms, tool (the
    MCP tool you called), action (the executed action JSON), element (the
    marked element the tap landed on, or null), screenshot (file name of the
    pre-action frame, relative to session_dir)}.

    To turn an "explore" session into a task, work from the anchors, not the
    coordinates: for each step read its `element` text (or the before frame
    with find_text / ocr) and write the node as a recognition plus action
    {"type": "click", "target": "recognized"}.

    Stopping with no active log returns {ok: False, error}.
    """
    return _action_log.stop(device_id)


# ---------- gesture recording (observational capture) ----------

@tool()
def calibrate_touch(device_id: str, force: bool = False) -> Dict:
    """Probe (or reuse) the device's touchscreen calibration for gesture recording.

    Maps touch-panel coordinates to display pixels: the ``/dev/input/eventN``
    node plus the panel's X/Y ranges (``getevent -lp``) and the display size
    (``wm size``). The result is cached at
    outputs/touch_calibration/<serial>.json and reused across sessions; a cache
    hit is only honoured while the cached display size still matches the device,
    otherwise it re-probes and overwrites (panel ranges differ per ROM, so a
    resolution/rotation change invalidates the mapping). force=True re-probes
    unconditionally.

    Optional as a pre-flight — record_gestures_start calibrates on its own and
    refreshes the same cache. Returns {ok, device_id, cached, calibration:
    {event_device, max_x, max_y, screen_width, screen_height, calibrated_at},
    path} and, when it re-probed, recalibrated_reason. An unreachable device or
    a panel with no ABS_MT_POSITION comes back as {ok: False, error}.
    """
    return _gesture_registry.calibrate(device_id, force=force)


@tool()
def record_gestures_start(device_id: str) -> Dict:
    """Start recording the user's real finger gestures on the device.

    The eyes for observational recording: it streams ``getevent`` and segments
    every touch session into a typed gesture (tap / long_press / swipe /
    multi_touch) in display pixels, capturing each gesture's pre-press frame,
    post-settle frame and a 120x120 anchor crop around the touch point (frames
    come from a scrcpy stream so they carry no tap-glow; it falls back to
    per-gesture screencap automatically). Let the user demonstrate at normal
    speed — unlike polled screenshots this loses nothing.

    Artifacts land in outputs/recordings/<timestamp>/ and the manifest is
    rewritten after every gesture, so nothing is lost if the session is
    interrupted. Returns {ok, device_id, session_dir, manifest_path,
    started_at, calibration}. One live recording per device: starting a second
    returns {ok: False, error} naming the active session — call
    record_gestures_stop first.
    """
    return _gesture_registry.record_start(device_id)


@tool()
def record_gestures_stop(device_id: str) -> Dict:
    """Stop the device's gesture recording and return the captured sequence.

    Returns {ok, device_id, session_dir, manifest_path, started_at, stopped_at,
    calibration, gesture_count, gestures}. Each gesture is {index, type
    (tap/long_press/swipe/multi_touch), params (backend-ready: x/y, or
    x1/y1/x2/y2 + duration_ms + path for a swipe), down_point, duration_ms,
    pointer_frames, t_offset_ms (since recording start), recorded_at, images:
    {before, after, anchor}} — the image values are file paths you can read.

    Turn it into a task by *recognizing*, not by replaying coordinates: for each
    gesture read its anchor crop and its "before" frame, find what the user was
    aiming at (ui_dump / find_text / ocr on that frame), and write the node with
    that anchor plus action {"type": "click", "target": "recognized"}. The
    coordinates are for looking the anchor up, not for the task JSON. Full
    per-gesture pointer frames (multi-touch replay detail) stay in the manifest.

    Stopping with no active recording returns {ok: False, error}.
    """
    return _gesture_registry.record_stop(device_id)


# ---------- tasks (recognition-gated replay) ----------

@tool(pure=True)
def get_task_schema() -> str:
    """Return the task JSON format reference (read before writing a task with save_task)."""
    return TASK_SCHEMA_DOC


@tool()
def list_tasks() -> List[str]:
    """List saved task names in task/task_definitions/."""
    return _list_task_names()


@tool()
def get_task(name: str) -> Dict:
    """Load and return a saved task JSON by name (includes resolved and merged).

    Two computed metadata keys are added (the engine ignores `_`-prefixed keys,
    like `_merge`): `_steps` maps each node name to its execution step label
    (integer spine 1,2,3…; dotted branches like 2.1 for on_timeout/alt-next
    fallbacks), and `_step_outline` is a ready-to-read flow listing in step
    order. They tell you a node's position in the flow without tracing the graph.

    For a task with "includes", the returned node table is the MERGED one;
    `_merge.include_map` maps each node name to the file it came from ("<task>"
    = this task's own file), so you can tell a shared fragment's node from the
    task's own before editing — editing a fragment node changes every task that
    includes it, and save_task writes back only the task file.
    """
    task = load_task(get_task_path(name))
    labels = compute_step_labels(task)
    task["_steps"] = labels
    task["_step_outline"] = format_task_outline(task, labels)
    return task


@tool()
def save_task(name: str, task_json: str) -> Dict:
    """Validate and save a task JSON (string) under task/task_definitions/<name>.json.

    Use get_task_schema first. Prefer ui_text/ocr recognition anchors with
    action {"type": "click", "target": "recognized"} over hardcoded coordinates.
    Shared nodes can live in include files referenced via "includes"; they are
    validated against the merged node table here but the file is saved with the
    references intact, so include-file updates apply on every run.

    On success the result carries "lint_warnings": a list of best-practice
    warnings (missing on_timeout, an error-looking branch with no finding,
    cold-start with no popups whitelist, hardcoded coordinates where a
    recognized anchor was available, zero QA assertions) — go through them or
    note why each is a deliberate exception. These never block a save unless
    config lint.strict is true, in which case a save with warnings is
    refused (returns ok=False with the same lint_warnings list, nothing is
    written) so fix or justify them first.
    """
    try:
        task = json.loads(task_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    try:
        resolved = resolve_task(task, DEFAULT_TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    warnings = [w.to_dict() for w in lint_task(resolved)]
    if warnings and _config.get("lint", {}).get("strict", False):
        return {
            "ok": False,
            "error": f"lint.strict is on and {len(warnings)} warning(s) were found; save refused.",
            "lint_warnings": warnings,
        }
    DEFAULT_TASK_DIR.mkdir(parents=True, exist_ok=True)
    path = get_task_path(name)
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "nodes": len(resolved["nodes"]), "lint_warnings": warnings}


@tool()
def validate_task(task_json: str) -> Dict:
    """Dry-run validate a task JSON string (includes resolved, nothing written).

    Same checks as save_task minus the write. Returns {ok, nodes, steps,
    lint_warnings} on success — steps maps each node to its execution step
    label, lint_warnings are the same W-rules save_task reports — or
    {ok: False, error} with the loader's message. Use it to check an edit
    before committing it with save_task.
    """
    try:
        task = json.loads(task_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    try:
        resolved = resolve_task(task, DEFAULT_TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "nodes": len(resolved["nodes"]),
        "steps": compute_step_labels(resolved),
        "lint_warnings": [w.to_dict() for w in lint_task(resolved)],
    }


@tool()
def list_custom_actions() -> List[str]:
    """Registered custom action names usable as {"type": "custom", "name": ...}.

    The registry auto-discovers task/custom_actions/*.py, so this is the live
    list — do not hardcode action names.
    """
    return list(registered_names())


@tool()
def lint_saved_task(name: str) -> Dict:
    """Lint a saved task by name (W-rule best-practice warnings, no write)."""
    try:
        task = load_task(get_task_path(name))
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "lint_warnings": [w.to_dict() for w in lint_task(task)]}


@tool()
def get_step_labels(name: str) -> Dict:
    """Node -> execution step label (1, 2, 2.1 ...) for a saved task.

    Lighter than get_task when only the flow order is needed.
    """
    try:
        task = load_task(get_task_path(name))
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "steps": compute_step_labels(task)}


@tool()
def list_includes() -> List[Dict]:
    """Shared include fragments under task_definitions/ (referenced via "includes").

    Each entry is {path, description, nodes}; path is what a task's "includes"
    list should contain.
    """
    out = []
    common_dir = DEFAULT_TASK_DIR / "common"
    if common_dir.is_dir():
        for p in sorted(common_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append({
                "path": f"common/{p.name}",
                "description": data.get("description"),
                "nodes": sorted(data.get("nodes") or {}),
            })
    return out


@tool()
def run_task(device_id: str, name: str, start_after: Optional[str] = None,
             export_to: Optional[str] = None) -> Dict:
    """Run a saved task on a device. Token-free recognition-gated replay.

    If the result has status="agent_required", perform the handoff instruction
    yourself with the device tools, then call run_task again with
    start_after=<handoff node> to resume.

    The result carries QA findings even on success: result["findings"] lists
    anomalies observed during the run (timeout recoveries, popup branches,
    watchdog hits, logcat crash/ANR, failures) with evidence file paths, and
    result["report"] points to the report.json problem list (plus
    report["report_html_path"]: a self-contained report.html the user can open
    in a browser — hand that path over when a human wants to read it). Each finding also
    embeds the last minute of context inline — "log_excerpt" (in-game logcat
    fragment) and "recent_flow" (steps before the problem) — plus evidence
    files: logcat tail, timeline json, and "video" (MP4 of the recent window
    from rolling on-device screenrecord; "history" frame snapshots only when
    recording is unavailable). Surface these findings to the user with their
    context — they are potential game bugs, not flow noise.

    result["node_stats"] (also a report.json field) is health telemetry per
    node: how it was reached (direct_hits / popup_assisted_hits /
    back_assisted_hits / recovery_hits), timeout_recoveries, anchor
    drift_count/drift_px, poll_rounds. High timeout/drift counts — and the
    "anchor_rot_suspect" finding they raise — mean the TASK's anchor went
    stale (fix the task JSON), not that the game is broken.

    export_to: optional directory; when the run produced findings, the whole
    evidence folder (report.json + report.html + screenshots + logs + frames,
    self-contained relative paths) is zipped there as one
    <timestamp>_<task>_<device>_<status>.zip. Config findings.export_dir does
    the same automatically for every run; the result's report["export_path"]
    holds the delivered .zip path.
    """
    task = load_task(get_task_path(name))
    result = _engine.run(device_id, task, start_after=start_after, task_name=name)
    if export_to and result.get("findings") and _findings is not None:
        export_path = _findings.export_run(export_to)
        if result.get("report") is not None:
            result["report"]["export_path"] = export_path or result["report"].get("export_path")
    return result


@tool()
def start_task(device_id: str, name: str, start_after: Optional[str] = None,
               export_to: Optional[str] = None) -> Dict:
    """Run a saved task in the background; return immediately with a run_id.

    Same semantics as run_task (recognition-gated replay, findings, export_to)
    but non-blocking: use it for long / full-flow runs where the synchronous
    run_task would block with no progress feedback. Poll get_run_status(run_id)
    to follow which node the run is on and to collect the final result.

    Only one background run can be active at a time (the engine is a singleton
    with per-run state); if one is already running this returns
    {ok: False, error: ...} naming the active run_id — wait for it to reach a
    terminal state (done / error / agent_required) first. On success returns
    {ok: True, run_id, status: "running"}.
    """
    with _runs_lock:
        active = next((r for r in _runs.values() if r["status"] == "running"), None)
        if active is not None:
            return {
                "ok": False,
                "error": (
                    f"A background run is already active (run_id={active['run_id']}, "
                    f"task={active['task']}); poll get_run_status or wait for it to finish."
                ),
            }
        run_id = uuid.uuid4().hex[:12]
        state = {
            "run_id": run_id,
            "status": "running",
            "device_id": device_id,
            "task": name,
            "current_node": None,
            "steps": 0,
            "started_at": time.time(),
            "ended_at": None,
            "result": None,
            "error": None,
        }
        _runs[run_id] = state

    def on_step(node_name: str) -> None:
        with _runs_lock:
            state["current_node"] = node_name
            state["steps"] += 1

    def worker() -> None:
        try:
            task = load_task(get_task_path(name))
            result = _engine.run(
                device_id, task, start_after=start_after, task_name=name, on_step=on_step
            )
            if export_to and result.get("findings") and _findings is not None:
                export_path = _findings.export_run(export_to)
                if result.get("report") is not None:
                    result["report"]["export_path"] = export_path or result["report"].get("export_path")
            run_status = result.get("status")
            terminal = (
                "agent_required" if run_status == "agent_required"
                else "error" if run_status == "failed"
                else "done"
            )
            with _runs_lock:
                state["result"] = result
                state["status"] = terminal
                state["ended_at"] = time.time()
        except Exception as exc:  # pragma: no cover - defensive; surfaced via status
            _logger.exception("Background task run %s failed", run_id)
            with _runs_lock:
                state["status"] = "error"
                state["error"] = str(exc)
                state["ended_at"] = time.time()

    threading.Thread(target=worker, name=f"task-run-{run_id}", daemon=True).start()
    return {"ok": True, "run_id": run_id, "status": "running"}


@tool()
def list_suites() -> List[str]:
    """List saved suite names in task/task_definitions/suites/ (run one with run_suite)."""
    return _list_suite_names()


@tool()
def run_suite(name: str, device_id: str, export_to: Optional[str] = None) -> Dict:
    """Run a whole smoke suite in the background: log in once, chain the cases.

    Every case task begins with the shared boot skeleton (cold start -> login ->
    landed on the app's main scene, 54.8s measured on a real device). A suite
    pays that once: the first case boots for real, the rest resume at the
    suite's `resume_after` node (by
    default the boot skeleton's main-scene confirmation node), which is exactly
    "skip the boot chain, start recognizing the case entry". Nothing about the
    state machine changes — recognition gating, findings evidence and bug-skip
    behave exactly as in run_task, and each case still writes its own findings
    run directory / report.json (so the smoke-report flow is unchanged).

    Recovery: a case that fails, crashes, or ends somewhere other than the main
    scene triggers the suite's `on_case_failure` policy — "restart_retry" (default,
    cold-start + login, then retry that case up to max_retries), then move on;
    "restart_continue" (no retry, next case cold-starts); or "abort" (stop, the
    remaining cases are reported as skipped). A single bad case never kills the
    run.

    Long-running by nature, so this is non-blocking like start_task: it returns
    {ok, run_id, status: "running"} immediately — poll get_run_status(run_id)
    for the current case/node and, once finished, the full structured result
    (per case: status / duration_s / findings / report paths, plus a suite-level
    summary with how many boots were skipped and the measured time saved).
    Only one background run may be active at a time.

    The polled status goes "done" whenever the suite ran to the end — including
    when cases failed or produced findings, which is the tool working, not a
    broken call; read result["ok"] and result["summary"] for the verdict.
    "error" means the orchestration itself stopped early (an "abort" policy cut
    the suite short, or the run raised).

    Cases containing an `agent` handoff step are a poor fit: a suspended case is
    recorded and the suite moves on (it is never resumed mid-suite).

    export_to: optional directory; every case that produced findings is zipped
    there as its own self-contained evidence archive.
    """
    try:
        suite = load_suite(name)
    except SuiteValidationError as exc:
        return {"ok": False, "error": str(exc)}

    with _runs_lock:
        active = next((r for r in _runs.values() if r["status"] == "running"), None)
        if active is not None:
            return {
                "ok": False,
                "error": (
                    f"A background run is already active (run_id={active['run_id']}, "
                    f"task={active['task']}); poll get_run_status or wait for it to finish."
                ),
            }
        run_id = uuid.uuid4().hex[:12]
        state = {
            "run_id": run_id,
            "kind": "suite",
            "status": "running",
            "device_id": device_id,
            "task": name,
            "case": None,
            "case_index": 0,
            "cases_total": len(suite["cases"]),
            "cases_done": 0,
            "current_node": None,
            "steps": 0,
            "started_at": time.time(),
            "ended_at": None,
            "result": None,
            "error": None,
        }
        _runs[run_id] = state

    def on_progress(event: Dict) -> None:
        with _runs_lock:
            if event["event"] == "case_start":
                state["case"] = event["case"]
                state["case_index"] = event["index"]
                state["current_node"] = None
            elif event["event"] == "node":
                state["current_node"] = event["node"]
                state["steps"] += 1
            elif event["event"] == "case_end" and not event["will_retry"]:
                state["cases_done"] += 1

    def worker() -> None:
        try:
            result = _suite_runner.run(
                device_id, suite, export_to=export_to, on_progress=on_progress
            )
            with _runs_lock:
                state["result"] = result
                # "done" means the orchestration itself ran to the end. A case
                # that failed or produced findings is the tool WORKING (findings
                # are the product), so it must not read as a broken call — the
                # pass/fail detail lives in result["ok"] / result["summary"].
                # Only an aborted suite (on_case_failure="abort" cut it short)
                # or a crash leaves cases unrun, and that is an error.
                state["status"] = "error" if result.get("aborted_at") else "done"
                state["ended_at"] = time.time()
        except Exception as exc:  # pragma: no cover - defensive; surfaced via status
            _logger.exception("Background suite run %s failed", run_id)
            with _runs_lock:
                state["status"] = "error"
                state["error"] = str(exc)
                state["ended_at"] = time.time()

    threading.Thread(target=worker, name=f"suite-run-{run_id}", daemon=True).start()
    return {"ok": True, "run_id": run_id, "status": "running", "cases": list(suite["cases"])}


@tool(pure=True)
def get_run_status(run_id: str) -> Dict:
    """Poll a background run started with start_task or run_suite.

    Pure in-memory read: it touches no device and takes no lock the run thread
    holds for longer than a dict update, so it answers immediately even while
    the run is stuck in a slow adb round trip. Safe to poll in a tight loop.

    Returns {ok, run_id, status, current_node, steps, elapsed_s}; a suite run
    adds {kind: "suite", case, case_index, cases_total, cases_done} so you can
    see which case it is on. While status is "running" it also carries
    "recent_events": the last flow events the engine recorded (node recognized,
    action executed, popup dismissed, recovery taken) — enough to tell "slow but
    progressing" from "stuck", without waiting for the run to end. status is one
    of running / agent_required / done / error. Once it leaves "running" the
    full task result is attached under "result" (same shape as run_task:
    steps / findings / report / handoff), and "error" holds the message on
    failure. For agent_required, read result["handoff"], perform the step with
    the device tools, then resume with run_task(start_after=<node>) or
    start_task(start_after=<node>).
    """
    with _runs_lock:
        state = _runs.get(run_id)
        if state is None:
            return {"ok": False, "error": f"Unknown run_id '{run_id}'"}
        end = state["ended_at"] or time.time()
        out = {
            "ok": True,
            "run_id": run_id,
            "status": state["status"],
            "current_node": state["current_node"],
            "steps": state["steps"],
            "elapsed_s": round(end - state["started_at"], 1),
        }
        if state.get("kind") == "suite":
            out.update({
                "kind": "suite",
                "case": state["case"],
                "case_index": state["case_index"],
                "cases_total": state["cases_total"],
                "cases_done": state["cases_done"],
            })
        if state["status"] == "running":
            # In-memory read of the engine's flight-recorder timeline; still a
            # pure call (no device I/O, no blocking).
            out["recent_events"] = _engine.recent_events()
        else:
            out["result"] = state["result"]
            if state["error"]:
                out["error"] = state["error"]
        return out


@tool()
def clear_replay_cache() -> Dict:
    """Clear cached anchor positions (the OCR ROI fast path used by run_task).

    Use after intentional UI changes to avoid a wave of anchor_drift findings;
    replays then re-recognize from the full screen and rebuild the cache.
    """
    if _replay_cache is None:
        return {"cleared": 0, "note": "replay cache disabled (config replay_cache.enabled)"}
    return {"cleared": _replay_cache.clear()}


class _NotOurLogger(logging.Filter):
    """Drop records this project's own logger already prints (see below)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(LOGGER_NAME)


def _guard_root_stderr_logging() -> None:
    """Keep the server's stderr from becoming an unbounded write on the event loop.

    Two things converge into a 30-minute hang if this is left alone:

    * FastMCP's `run()` calls `logging.basicConfig(handlers=[RichHandler(stderr)])`.
      That handler lands at level 0, and propagation consults *handler* levels
      only — never the ancestor logger's — so every DEBUG record this project
      emits (EVT capture / ocr / recognize / timeline: hundreds per task run)
      got rendered by Rich onto stderr, on top of the console handler that
      correctly filters them out.
    * `mcp.server.lowlevel.server` logs `Processing request of type ...` at INFO
      for every request, **from the event loop thread**.

    stderr is a pipe to the client. Fill its buffer and the next write blocks
    with no timeout — on the loop — so the receive loop never runs again: the
    tool body is never entered, nothing reaches the log, and every later request
    (even a one-microsecond get_run_status) sits unread until the client's idle
    timeout fires. That is the failure `tool()` guards against one layer up, and
    it matches both observed hangs: no `EVT mcp_tool` line, no side effects, no
    worker thread, and a server that answers normally again once the client
    tears the call down.

    So the root logger is claimed *first*: `basicConfig` is a documented no-op
    when handlers already exist, which keeps the Rich handler out entirely. What
    replaces it behaves like the stdlib default that was there before FastMCP
    got involved — third-party WARNING+ still reaches stderr — minus our own
    records, which already have their own console handler and must not be
    printed twice. Called from __main__ only, before `mcp.run()`.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.WARNING)
        handler.addFilter(_NotOurLogger())
        root.addHandler(handler)
    # Belt and braces: raising the library logger's own level drops the
    # per-request line before any handler is consulted, whatever ends up on root.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)


def _start_process_log(log_dir: Optional[str] = None):
    """Attach this server's own DEBUG log file (see core.logger.attach_process_log).

    Called from __main__ only, never at import: a test that imports this module
    must not litter `outputs/logs/`. The CLI entry point deliberately skips it —
    a CLI session's interesting half is a task run, which already gets run.log.
    """
    return attach_process_log(_logger, log_dir or DEFAULT_LOG_DIR, prefix="mcp")


if __name__ == "__main__":
    _start_process_log()
    _guard_root_stderr_logging()
    mcp.run()
