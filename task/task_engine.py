from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

from action.action_schema import REPEAT_PARAM_KEYS
from core.logger import attach_run_file_handler, detach_handler, log_event
from task.custom_actions import CustomActionContext, get_handler
from task.replay_cache import ReplayCache, center_distance
from utils.helpers import image_change_ratio

if TYPE_CHECKING:
    from perception.logcat_monitor import LogcatMonitor
    from task.findings import FindingsRecorder
    from utils.debug_tracer import DebugTracer

DEFAULT_TIMEOUT_MS = 10000
DEFAULT_POLL_INTERVAL_MS = 1000
DEFAULT_POST_DELAY_MS = 0
DEFAULT_MAX_STEPS = 50

# --- node-level `wait_still` defaults (settle on a still frame) ---
DEFAULT_WAIT_STILL_TIMEOUT_MS = 5000
DEFAULT_WAIT_STILL_INTERVAL_MS = 200
DEFAULT_WAIT_STILL_THRESHOLD = 0.01

# --- engine.* config defaults (see config.yaml.example) ---
# Press BACK once when a stall survives the known-popup sweep (unknown popup).
DEFAULT_BACK_FALLBACK = True
# Fraction of pixels that must change for the BACK press to count as "it did
# something" and earn one extra recognition round.
DEFAULT_BACK_CHANGE_THRESHOLD = 0.01
# Per-run count of timeout recoveries / anchor moves on one node above which the
# node is flagged as a rot suspect (0 disables the check).
DEFAULT_ROT_SUSPECT_TIMEOUTS = 2
# Anchor moves smaller than this (px, euclidean) are layout jitter: counted in
# node_stats but not reported as an anchor_drift finding.
DEFAULT_DRIFT_TOLERANCE_PX = 30.0

KEYCODE_BACK = 4

# A long recognition wait must not leave the terminal silent, but one line per
# poll would drown the flow: misses go to DEBUG (run.log) and a single INFO
# heartbeat is emitted at most every this many seconds.
POLL_HEARTBEAT_S = 5.0


def _coerce_max_steps(value, default: int, logger) -> int:
    """Validate a max_steps override (constructor / engine_config / task JSON).

    A non-positive-int value is a misconfiguration, not a reason to crash the
    run: fall back to `default` and log a warning so it's visible without
    aborting the task.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        logger.warning("Invalid max_steps value %r; falling back to %s", value, default)
        return default
    return value


# Watchdog fields that belong to the recognition spec (the rest — severity,
# message, skip_to, fail_task — are routing, not recognition). Every pixel
# channel's selector has to be forwarded, or e.g. a template/feature watchdog
# would reach the hub without its template name and never fire.
WATCHDOG_SPEC_KEYS = (
    "type", "expected", "threshold", "roi",
    "template", "scales", "grayscale",      # template
    "min_matches", "ratio",                 # feature
    "label", "conf", "model",               # yolo ("model" picks a named YOLO model)
    "all_of", "any_of", "box_index",        # and/or combined recognition
)


class TaskEngine:
    """Recognition-driven state machine over task JSON (see action_schema.TASK_SCHEMA_DOC).

    Each step: recognize the current node (the hit supplies click coordinates),
    execute its action, then poll the `next` candidate list — the first candidate
    whose recognition hits becomes the new current node. Empty `next` ends the
    task successfully; recognition timeout falls back to the timed-out node's
    `on_timeout` or fails the task.

    `agent` actions are not executed here: the engine suspends and hands the
    instruction back to the caller (Claude/Codex over MCP, or a human at the
    CLI), who performs the step and resumes with run(..., start_after=<node>).

    Beyond driving the flow, the engine is a QA observer: anomalies are
    findings to report, not just obstacles to route around. With a
    FindingsRecorder attached, every recovery path taken (on_timeout jumps,
    nodes marked with "finding"), every task-level watchdog hit (forbidden
    text / blank screen, sampled twice per step — at action time and after the
    settle delay — so a transient toast is caught and pinned as evidence),
    every logcat crash/ANR, and every failure is recorded with on-the-spot
    evidence; the result carries `findings` and a `report` summary even when
    the run completes successfully.

    It is also a health observer: `result["node_stats"]` counts, per node, how
    it was reached (directly / after a popup sweep / after the BACK fallback /
    as a recovery target) and how often its anchor moved. Those counters are
    read-only telemetry — they never change which node runs next.
    """

    def __init__(self, recognizer_hub, action_executor, logger, max_steps: int = DEFAULT_MAX_STEPS,
                 findings_recorder: Optional["FindingsRecorder"] = None,
                 logcat_monitor: Optional["LogcatMonitor"] = None,
                 screen_recorder=None, pcap_recorder=None, engine_config: Optional[Dict] = None,
                 run_log: bool = True):
        self.hub = recognizer_hub
        self.executor = action_executor
        self.logger = logger
        self.recorder = findings_recorder
        self.logcat = logcat_monitor
        self.screen = screen_recorder
        self.pcap = pcap_recorder
        config = engine_config or {}
        # Priority for the per-run step budget: task JSON (applied in run())
        # > engine_config > the `max_steps` constructor default. A caller that
        # passes an explicit max_steps (tests, callers without config) keeps
        # acting as the fallback target if engine_config's value is invalid.
        self.max_steps = _coerce_max_steps(config.get("max_steps"), max_steps, self.logger)
        self.back_fallback_default = bool(config.get("back_fallback", DEFAULT_BACK_FALLBACK))
        self.back_change_threshold = float(
            config.get("back_fallback_change_threshold", DEFAULT_BACK_CHANGE_THRESHOLD)
        )
        self.rot_suspect_timeouts = int(
            config.get("rot_suspect_timeouts", DEFAULT_ROT_SUSPECT_TIMEOUTS)
        )
        self.drift_tolerance_px = float(
            config.get("drift_tolerance_px", DEFAULT_DRIFT_TOLERANCE_PX)
        )
        self._task_name: Optional[str] = None
        # Device of the current run, kept only so the run's log lines can name
        # it: with several devices running in parallel their lines interleave in
        # the console, and "[step 3] node 'x' recognized" alone says nothing
        # about which phone it happened on.
        self._device_id: Optional[str] = None
        self._on_finding: Optional[str] = None
        self._popups: List[Dict] = []
        self._dismissed_popups: List[str] = []
        # Serial number for popup-sweep evidence files, so several sweeps of the
        # same popup within one run do not overwrite each other's frame.
        self._popup_evidence_seq: int = 0
        self._back_fallback: bool = self.back_fallback_default
        self._node_stats: Dict[str, Dict] = {}
        # config app.run_log: tee the run's step trace (DEBUG included) into
        # run.log inside the findings run folder.
        self.run_log = bool(run_log)
        # Step counter of the main loop; only used to prefix log lines so a
        # terminal reader can tell which step a line belongs to.
        self._step: int = 0
        # Wall clock of the current run, for the one-line summary _finish emits.
        self._run_started: float = 0.0
        # Cooperative stop flag (request_stop): checked at node boundaries and
        # inside the recognition poll, so an external caller (editor backend,
        # MCP) can end the run THROUGH _finish — report written, recorders
        # stopped — instead of killing the thread and leaking them.
        self._stop_requested: bool = False
        # "A run is in flight." Read from OTHER threads (the frame monitor's
        # sentinel gates its detection on it, so it never files findings against
        # a screen the engine's own watchdogs already own), hence an Event
        # rather than a plain attribute. Purely observational: nothing in the
        # state machine branches on it.
        self._running = threading.Event()
        # Device of the in-flight run, published for those same out-of-band
        # observers (see the `running_device` property). Kept beside `_running`
        # and written in lockstep with it — `_device_id` is run bookkeeping that
        # outlives the run, which is exactly what an observer must not read.
        self._running_device: Optional[str] = None

    def run(
        self,
        device_id: str,
        task: Dict,
        tracer: Optional["DebugTracer"] = None,
        start_after: Optional[str] = None,
        task_name: Optional[str] = None,
        on_step: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        self._task_name = task_name
        self._device_id = device_id
        self._on_finding = task.get("on_finding")
        # Whitelist of KNOWN-benign popups (user agreements, in-game warnings)
        # that are expected noise, not bugs: swept and dismissed when they stall
        # recognition, without recording a finding. Anything not on this list
        # still surfaces as a finding via watchdogs / timeout recovery.
        self._popups = task.get("popups", [])
        self._dismissed_popups: List[str] = []
        self._popup_evidence_seq = 0
        # Task JSON may opt out of the unknown-popup BACK fallback (a flow where
        # BACK would leave the screen under test).
        self._back_fallback = bool(task.get("back_fallback", self.back_fallback_default))
        self._node_stats = {}
        self._step = 0
        self._run_started = time.monotonic()
        self._stop_requested = False
        # Per-backend capture counters are summarised at _finish; zero them here
        # so a long-lived engine (MCP server) does not report the previous run's
        # screenshots as part of this one.
        self._reset_capture_stats()

        if self.recorder:
            self.recorder.open_run(device_id, task_name)
        # The step trace is teed into run.log inside the run folder the recorder
        # just opened, so the flow that led to a finding ships in the same
        # self-contained folder as the evidence. Attached before anything talks
        # to the device, so even a failed monitor start lands in the file.
        run_log_handler = self._attach_run_log()
        # Device first, flag second: an observer that sees is_running True must
        # never find running_device still empty (or holding the last run's).
        self._running_device = device_id
        self._running.set()
        try:
            return self._run_flow(device_id, task, tracer, start_after, on_step)
        finally:
            # Cleared on every exit path, exceptions included: a run that blew
            # up must not leave outside observers thinking it is still going.
            # Flag first, device second — the mirror image of the set above.
            self._running.clear()
            self._running_device = None
            if run_log_handler is not None:
                detach_handler(self.logger, run_log_handler)

    @property
    def is_running(self) -> bool:
        """True between the start and the end of run(), readable from any thread.

        Exists for out-of-band observers — chiefly `task.sentinel`, which watches
        the screen only while the engine is NOT running, so the two never file
        findings about the same moment.
        """
        return self._running.is_set()

    @property
    def running_device(self) -> Optional[str]:
        """Device the in-flight run is driving, or None when no run is in flight.

        The companion of `is_running` for out-of-band observers: a sentinel on
        phone B must keep watching while a run occupies phone A, and only the
        device tells the two apart. Gated on `_running` so an idle engine never
        hands back a stale device — the pair is always read as a unit.
        """
        return self._running_device if self._running.is_set() else None

    def _attach_run_log(self):
        """Open this run's run.log (config `app.run_log`); None when unavailable.

        Needs a findings recorder to know where the run folder is; without one
        (or with run logging switched off) the run simply has no file trace —
        never an error.
        """
        if not self.run_log or self.recorder is None:
            return None
        ensure_run_dir = getattr(self.recorder, "ensure_run_dir", None)
        if ensure_run_dir is None:
            return None
        try:
            return attach_run_file_handler(self.logger, Path(ensure_run_dir()) / "run.log")
        except Exception as exc:
            self.logger.warning("run.log setup failed: %s", exc)
            return None

    def _run_flow(
        self,
        device_id: str,
        task: Dict,
        tracer: Optional["DebugTracer"],
        start_after: Optional[str],
        on_step: Optional[Callable[[str], None]],
    ) -> Dict:
        """The state machine itself (run() owns per-run setup and the run log)."""
        nodes = task["nodes"]
        watchdogs = task.get("watchdogs", [])
        steps: List[Dict] = []
        watchdog_seen: Set[int] = set()
        # A single long task can declare its own step budget (translated flows
        # can inflate node count 3.5-4x past the shared default/engine_config
        # value). Highest priority; falls back to self.max_steps if invalid.
        max_steps = _coerce_max_steps(task.get("max_steps"), self.max_steps, self.logger)

        if self.logcat:
            try:
                self.logcat.start(device_id)
            except Exception as exc:
                self.logger.warning("logcat monitor start failed: %s", exc)
        if self.screen:
            try:
                self.screen.start(device_id)
            except Exception as exc:
                self.logger.warning("screen recorder start failed: %s", exc)
        if self.pcap:
            try:
                self.pcap.start(device_id)
            except Exception as exc:
                self.logger.warning("pcap recorder start failed: %s", exc)

        if start_after is not None:
            if start_after not in nodes:
                return self._finish(
                    device_id, steps, tracer, error=f"Unknown start_after node '{start_after}'"
                )
            # Symmetric to the "agent_handoff" event logged when the engine
            # suspended on that node: the timeline of the resumed run then shows
            # where the outside agent handed control back, closing the loop.
            self.logger.info("Task resumed after node '%s' on %s", start_after, device_id)
            if self.recorder:
                self.recorder.add_timeline("agent_resume", node=start_after)
            candidates = nodes[start_after].get("next", [])
            if not candidates:
                return self._finish(device_id, steps, tracer)
            outcome = self._poll(device_id, candidates, nodes)
            if self._stop_requested:
                return self._finish(device_id, steps, tracer, error=self.STOP_ERROR)
            if outcome is None:
                outcome = self._try_recovery(device_id, candidates, nodes)
            if outcome is None:
                error, skip = self._timeout_error(
                    device_id, watchdogs, watchdog_seen, start_after,
                    f"Recognition timeout after node '{start_after}' (candidates: {candidates})",
                )
                if skip:
                    outcome = self._enter_bug_skip(device_id, skip, nodes, start_after)
                if outcome is None:
                    return self._finish(device_id, steps, tracer, error=error)
        else:
            entry = task["entry"]
            outcome = self._poll(device_id, [entry], nodes)
            if self._stop_requested:
                return self._finish(device_id, steps, tracer, error=self.STOP_ERROR)
            if outcome is None:
                outcome = self._try_recovery(device_id, [entry], nodes)
            if outcome is None:
                error, skip = self._timeout_error(
                    device_id, watchdogs, watchdog_seen, None,
                    f"Recognition timeout on entry node '{entry}'",
                )
                if skip:
                    outcome = self._enter_bug_skip(device_id, skip, nodes, entry)
                if outcome is None:
                    return self._finish(device_id, steps, tracer, error=error)

        for step_no in range(1, max_steps + 1):
            if self._stop_requested:
                return self._finish(device_id, steps, tracer, error=self.STOP_ERROR)
            current, hit = outcome
            node = nodes[current]
            # Observation only: how long this node took from "recognized" to
            # "done, about to poll the next candidates".
            node_started = time.perf_counter()
            # Step number is the main loop's own counter (a state machine has no
            # fixed total), so every line below can be tied back to one step.
            self._step = step_no
            self.logger.info(
                "[step %d] node '%s' recognized via %s score=%s on %s",
                step_no, current, hit["channel"], hit["score"], device_id,
            )
            # Progress callback at the node boundary, so a background caller can
            # poll which node we are on instead of waiting for the whole run.
            if on_step:
                on_step(current)
            if self.recorder:
                self.recorder.add_timeline(
                    "node_recognized", node=current, channel=hit["channel"], score=hit["score"]
                )

            self._record_node_finding(current, node)

            action_type = node["action"]["type"]
            if action_type in ("agent", "llm"):
                if self.recorder:
                    self.recorder.add_timeline("agent_handoff", node=current)
                steps.append({"node": current, "recognition": hit, "results": []})
                return self._finish(
                    device_id,
                    steps,
                    tracer,
                    handoff={"node": current, "instruction": node["action"].get("text", "")},
                )

            try:
                results = self._execute_node_action(device_id, node, hit, tracer)
            except Exception as exc:
                if self.recorder:
                    self.recorder.add_timeline("action", node=current, type=action_type, ok=False)
                steps.append({"node": current, "recognition": hit, "results": []})
                return self._finish(
                    device_id, steps, tracer, error=f"Action failed at node '{current}': {exc}"
                )

            steps.append({"node": current, "recognition": hit, "results": results})
            failed = [r for r in results if r.get("ok") != "True"]
            if self.recorder:
                self.recorder.add_timeline("action", node=current, type=action_type, ok=not failed)
            if failed:
                return self._finish(
                    device_id, steps, tracer,
                    error=f"Action failed at node '{current}': {failed[0].get('stderr', '')}",
                )

            logcat_skip = self._poll_logcat(device_id, node=current)
            self._tick_video()
            if logcat_skip:
                jumped = self._enter_bug_skip(device_id, logcat_skip, nodes, current)
                if jumped is None:
                    return self._finish(
                        device_id, steps, tracer,
                        error=f"Bug-skip target '{logcat_skip}' (logcat crash at '{current}') not recognized",
                    )
                outcome = jumped
                continue

            # Two-shot transient evidence: sample the negative-assertion
            # watchdogs right after the action (catches a toast/dialog that has
            # flashed away by the time the screen settles) and again after the
            # settle delay (catches one that only shows up late). Each shot pins
            # its finding's screenshot to the frame that tripped it.
            watchdog_error, watchdog_skip = self._check_watchdogs(
                device_id, watchdogs, watchdog_seen, node=current
            )

            post_delay = node.get("post_delay_ms", DEFAULT_POST_DELAY_MS)
            if post_delay:
                self.logger.debug("%spost_delay %dms", self._step_tag(), post_delay)
                time.sleep(post_delay / 1000)

            # Unpredictable transitions (cutscenes, loading) settle by watching
            # the screen rather than by guessing a delay. Runs before the second
            # watchdog shot so that shot judges the settled screen.
            self._wait_still(device_id, current, node)

            if watchdog_error is None and watchdog_skip is None:
                watchdog_error, watchdog_skip = self._check_watchdogs(
                    device_id, watchdogs, watchdog_seen, node=current
                )

            # A reported bug (watchdog hit) with a skip target recovers by
            # jumping past the broken step instead of aborting. Only a freshly
            # recorded finding sets watchdog_skip, so a bare recognition timeout
            # (no watchdog hit) never lands here — a stall alone never skips.
            if watchdog_skip:
                jumped = self._enter_bug_skip(device_id, watchdog_skip, nodes, current)
                if jumped is None:
                    return self._finish(
                        device_id, steps, tracer,
                        error=f"Bug-skip target '{watchdog_skip}' (watchdog at '{current}') not recognized",
                    )
                outcome = jumped
                continue
            if watchdog_error:
                return self._finish(device_id, steps, tracer, error=watchdog_error)

            # Settled post-step frame into the rolling history buffer, so a
            # later finding can show the last minute of screens.
            if self.recorder:
                self.recorder.snapshot_history()

            log_event(
                self.logger, "node_done", node=current, device=device_id,
                ms=int((time.perf_counter() - node_started) * 1000), via=hit.get("channel"),
            )

            candidates = node.get("next", [])
            if not candidates:
                return self._finish(device_id, steps, tracer)

            outcome = self._poll(device_id, candidates, nodes)
            if self._stop_requested:
                return self._finish(device_id, steps, tracer, error=self.STOP_ERROR)
            if outcome is None:
                outcome = self._try_recovery(device_id, candidates, nodes)
            if outcome is None:
                error, skip = self._timeout_error(
                    device_id, watchdogs, watchdog_seen, current,
                    f"Recognition timeout after node '{current}' (candidates: {candidates})",
                )
                if skip:
                    outcome = self._enter_bug_skip(device_id, skip, nodes, current)
                if outcome is None:
                    return self._finish(device_id, steps, tracer, error=error)

        return self._finish(
            device_id, steps, tracer, error=f"Max steps ({max_steps}) exceeded; possible task loop"
        )

    def _step_tag(self) -> str:
        """`[step N] ` prefix for log lines emitted inside the main loop."""
        return f"[step {self._step}] " if self._step else ""

    # ---------- capture telemetry ----------
    #
    # Node timing alone cannot say whether a slow run was slow at *looking* —
    # the measured hot spot on real hardware is the screenshot backend, not OCR.
    # The capturer counts its own calls per backend; the engine only zeroes them
    # at the start of a run and prints the totals at the end.

    def _capturer(self):
        """The capturer behind the recognizer hub, if it exposes counters."""
        capturer = getattr(self.hub, "capturer", None)
        return capturer if hasattr(capturer, "stats") else None

    def _reset_capture_stats(self) -> None:
        capturer = self._capturer()
        if capturer is not None:
            capturer.reset_stats()

    def _log_capture_stats(self, device_id: str) -> None:
        capturer = self._capturer()
        if capturer is None:
            return
        fields: Dict = {}
        for backend, stat in sorted(capturer.stats().items()):
            fields[f"{backend}_n"] = stat["n"]
            fields[f"{backend}_avg_ms"] = stat["avg_ms"]
        if fields:
            log_event(self.logger, "capture_stats", device=device_id, **fields)

    #: Error string a user-requested stop finishes with; callers match on it
    #: to tell "stopped on purpose" apart from a real failure.
    STOP_ERROR = "Stopped by user request"

    def request_stop(self) -> None:
        """Ask the current run to end at the next check point (thread-safe).

        The run finishes through _finish with error=STOP_ERROR, so the report
        and evidence chain are complete. Latency is one node execution or one
        poll interval, whichever the run is in. A no-op when nothing runs.
        """
        self._stop_requested = True

    def recent_events(self, n: int = 10) -> List[Dict]:
        """Most recent flow events of the current run (empty without a recorder).

        Live progress for a caller polling a background run (MCP
        get_run_status): the same timeline the findings' `recent_flow` is cut
        from, read straight out of memory.
        """
        if self.recorder is None:
            return []
        tail = getattr(self.recorder, "timeline_tail", None)
        return tail(n) if tail else []

    def _try_recovery(self, device_id: str, timed_out: List[str], nodes: Dict) -> Optional[Tuple[str, Dict]]:
        """Jump to the on_timeout of the first timed-out candidate that defines one.

        Taking a recovery path is itself a finding: the expected screen did not
        show up, and the screenshot of the stuck state is captured before the
        recovery node is polled.
        """
        for name in timed_out:
            recovery = nodes[name].get("on_timeout")
            if recovery:
                self.logger.warning("Task node '%s' timed out; trying on_timeout node '%s'", name, recovery)
                self._stat(name)["timeout_recoveries"] += 1
                if self.recorder:
                    self.recorder.add_timeline("timeout_recovery", node=name, recovery=recovery)
                    self.recorder.record(
                        "timeout_recovery", "warning",
                        f"Node '{name}' recognition timed out; jumping to on_timeout node '{recovery}'",
                        node=name, screenshot=True,
                    )
                return self._poll_candidates(device_id, [recovery], nodes, source="recovery")
        return None

    # Re-sweep at most this many times per stall (a dismissed popup may uncover
    # a stacked one); bounds the work and prevents a popup-thrash loop.
    MAX_POPUP_PASSES = 4

    def _poll(self, device_id: str, candidates: List[str], nodes: Dict) -> Optional[Tuple[str, Dict]]:
        """Poll candidates; if they stall, clear known-benign popups and retry once.

        The popup sweep runs only when recognition has already failed, so the
        happy path pays nothing (no extra capture per step) -- the screenshot
        cost only lands when a popup is actually blocking progress.

        A stall that survives the whitelist sweep gets one last generic escape:
        the BACK fallback (see _back_fallback_recover) — but only where the task
        author left no escape of their own. An `on_timeout` branch is a designed
        recovery and takes priority: pressing BACK first could push the screen
        off the very state that branch expects. The fallback therefore covers
        exactly the dead ends that would otherwise fail the run outright.
        """
        outcome = self._poll_candidates(device_id, candidates, nodes)
        # A requested stop must not trigger the popup sweep / BACK fallback:
        # those touch the device, and the caller asked us to stand down.
        if self._stop_requested:
            return outcome
        if outcome is None and self._popups and self._sweep_popups(device_id) > 0:
            outcome = self._poll_candidates(device_id, candidates, nodes, source="popup")
        if outcome is None and self._back_fallback and not self._has_timeout_recovery(candidates, nodes):
            outcome = self._back_fallback_recover(device_id, candidates, nodes)
        return outcome

    @staticmethod
    def _has_timeout_recovery(candidates: List[str], nodes: Dict) -> bool:
        """Does any stalled candidate define an authored on_timeout branch?

        Mirrors what _try_recovery will do next (first candidate with an
        on_timeout wins), so the two never disagree about who owns the stall.
        """
        return any(nodes[name].get("on_timeout") for name in candidates)

    def _back_fallback_recover(
        self, device_id: str, candidates: List[str], nodes: Dict
    ) -> Optional[Tuple[str, Dict]]:
        """One BACK press to escape an UNKNOWN blocking popup, reported first.

        The whitelist sweep only clears popups the task author anticipated; what
        is left blocking the screen is by definition unexpected — a QA finding.
        So the frame that shows it is grabbed and pinned as evidence
        (`unknown_popup_backoff`, warning) *before* BACK is pressed, because the
        press itself may wipe the very thing worth seeing.

        Exactly one press per stall: a chain of BACKs risks walking a
        "quit game?" confirmation all the way out of the app. If the press did
        not visibly change the screen (pixel diff below
        `engine.back_fallback_change_threshold`) it achieved nothing, so no
        retry is spent; otherwise recognition gets one more round — the gate
        itself is untouched.

        Only reached when no stalled candidate defines an `on_timeout` (see
        _poll): this is the dead-end escape, not a replacement for an authored
        recovery.

        This is NOT bug-skip: nothing jumps to `skip_to`/`on_finding` here. A
        stall stays a stall, and an unresolved one still fails the run.
        """
        before = self._grab_frame(device_id)
        message = (
            "Recognition stalled with an unknown popup/overlay blocking "
            f"{candidates}; pressing BACK once to escape (evidence pinned to the "
            "blocking frame)"
        )
        if self.recorder:
            self.recorder.add_timeline("back_fallback", candidates=list(candidates))
            self.recorder.record(
                "unknown_popup_backoff", "warning", message,
                node=None, screenshot=True, image=before,
                extra={"candidates": list(candidates)},
            )
        else:
            self.logger.warning("BACK fallback: %s", message)

        try:
            result = self.executor.execute(
                device_id, {"type": "key", "params": {"keycode": KEYCODE_BACK}}, None
            )
        except Exception as exc:
            self.logger.warning("BACK fallback press failed: %s", exc)
            return None
        if result and result.get("ok") != "True":
            self.logger.warning("BACK fallback press failed: %s", result.get("stderr", ""))
            return None

        # Same settle window as a popup dismissal: let the overlay animate out
        # before judging whether anything moved.
        time.sleep(self._popup_settle_ms() / 1000)
        if not self._screen_changed(device_id, before):
            self.logger.info("BACK fallback changed nothing; falling back to the timeout path")
            return None
        return self._poll_candidates(device_id, candidates, nodes, source="back")

    def _screen_changed(self, device_id: str, before) -> bool:
        """Did the screen visibly move since `before`? (hot path: no PNG coding)

        Both frames are PIL images from capture_image(); only a finding's
        evidence is ever encoded. When no frame is available (no capturer) the
        change cannot be measured — the cheap recognition round is granted
        rather than skipped, since it is gated as usual anyway.
        """
        after = self._grab_frame(device_id)
        if before is None or after is None:
            return True
        try:
            ratio = image_change_ratio(before, after)
        except Exception as exc:
            self.logger.warning("BACK fallback change check failed: %s", exc)
            return True
        self.logger.info(
            "BACK fallback screen change ratio %.4f (threshold %.4f)",
            ratio, self.back_change_threshold,
        )
        return ratio >= self.back_change_threshold

    def _sweep_popups(self, device_id: str) -> int:
        """Detect + dismiss whitelisted known-benign popups; return how many.

        These are expected noise (user agreements, in-game warnings), not bugs,
        so a dismissal is logged to the flow timeline but is deliberately NOT a
        finding. Any popup not on the whitelist is left untouched, so genuine
        anomalies still stall into a timeout/watchdog finding as before.

        A sweep clicks the live screen unattended, so it has two safeguards:

        - Optional `confirm` second gate. `recognition` alone can be ambiguous —
          on 2026-08-11 a shared close-X template matched the daily-task panel's
          own X and the sweep closed the panel under test, producing two
          misleading findings. `confirm` is an ordinary recognition spec
          evaluated on the SAME frame; if it misses, the popup is treated as
          absent and nothing is clicked. Missing a sweep is cheap (the stall
          falls through to the BACK fallback, which reports it); a wrong click
          silently destroys the test.
        - Evidence pinned to the deciding frame. The frame the match was made on
          is parked in the run folder before the click, so "why did it click
          there?" is answerable afterwards. This is context, not a finding: it
          never enters the findings list or the counts.
        """
        dismissed = 0
        for _ in range(self.MAX_POPUP_PASSES):
            frame = self._grab_frame(device_id)
            handled = False
            for entry in self._popups:
                name = entry.get("name", "popup")
                try:
                    hit = self.hub.recognize(device_id, entry.get("recognition", {}), image=frame)
                except Exception as exc:
                    self.logger.warning("Popup '%s' check failed: %s", name, exc)
                    continue
                if hit is None:
                    continue
                if not self._popup_confirmed(device_id, entry, name, hit, frame):
                    continue
                self.logger.info("Known popup '%s' detected; dismissing (not a finding)", name)
                evidence = self._save_popup_evidence(name, frame)
                try:
                    results = self._execute_node_action(
                        device_id, {"action": entry["action"]}, hit, None
                    )
                except Exception as exc:
                    self.logger.warning("Popup '%s' dismiss failed: %s", name, exc)
                    continue
                ok = all(r.get("ok") == "True" for r in results) if results else True
                center = hit.get("center")
                if self.recorder:
                    self.recorder.add_timeline(
                        "popup_dismissed", name=name, ok=ok,
                        score=hit.get("score"),
                        center=list(center) if center is not None else None,
                        evidence=evidence,
                    )
                # Logged as well as timelined: with a recorder attached (the
                # default) the timeline alone is invisible to whoever is
                # watching the terminal, and a swept popup is exactly the kind
                # of "why did it click that?" the log has to explain.
                proof = f" (evidence {Path(evidence).name})" if evidence else ""
                if ok:
                    self.logger.info("%spopup '%s' dismissed%s", self._step_tag(), name, proof)
                else:
                    self.logger.warning(
                        "%spopup '%s' dismiss action failed%s", self._step_tag(), name, proof
                    )
                if ok:
                    dismissed += 1
                    self._dismissed_popups.append(name)
                    handled = True
                    # Let the popup animate out before re-grabbing, then re-sweep
                    # in case it was stacked over another.
                    time.sleep(self._popup_settle_ms() / 1000)
                    break
            if not handled:
                break
        return dismissed

    def _popup_confirmed(self, device_id: str, entry: Dict, name: str, hit: Dict, frame) -> bool:
        """Does the entry's optional `confirm` gate agree the popup is really there?

        Evaluated on the SAME frame as `recognition`, so the two cannot vote on
        different moments of an animating screen. No `confirm` declared means
        the entry is self-sufficient (the historical behaviour). A confirm that
        misses -- or blows up -- means "not this popup": nothing is clicked and
        nothing is counted as dismissed, so the stall carries on to the BACK
        fallback and gets reported instead of silently mis-clicked.
        """
        spec = entry.get("confirm")
        if not spec:
            return True
        try:
            confirmed = self.hub.recognize(device_id, spec, image=frame)
        except Exception as exc:
            self.logger.warning("Popup '%s' confirm check failed: %s", name, exc)
            return False
        if confirmed is not None:
            self.logger.debug(
                "Popup '%s' confirm passed (recognition score=%s, confirm score=%s)",
                name, hit.get("score"), confirmed.get("score"),
            )
            return True
        self.logger.debug(
            "Popup '%s' recognition matched (score=%s) but confirm missed "
            "(no hit for %s); treating the popup as absent -- not clicking",
            name, hit.get("score"), spec.get("expected") or spec.get("type"),
        )
        return False

    def _save_popup_evidence(self, name: str, frame) -> Optional[str]:
        """Park the deciding frame in the run folder; return its relative path.

        Same pinning philosophy as a finding's evidence (the frame that tripped
        the detection, not a re-capture afterwards) but deliberately NOT a
        finding: an expected popup is not an anomaly, it just has to be
        auditable. Returns None when there is no recorder / no frame / the write
        failed -- evidence is never allowed to block a sweep.
        """
        if self.recorder is None or frame is None:
            return None
        saver = getattr(self.recorder, "save_context_image", None)
        if saver is None:
            return None
        self._popup_evidence_seq += 1
        return saver(f"popup_{self._popup_evidence_seq:02d}_{name}", frame)

    def _popup_settle_ms(self) -> int:
        return 300

    def _wait_still(self, device_id: str, name: str, node: Dict) -> None:
        """Wait until the screen stops moving before polling `next` (optional).

        Node field: {"wait_still": {"timeout_ms": 5000, "interval_ms": 200,
        "threshold": 0.01}}. Two consecutive frames differing in fewer than
        `threshold` of their pixels count as still, and the flow continues.

        This is a settle window, not an assertion: running out of time only
        means "stop waiting" — no failure, no finding. A screen that never
        settles (an endless spinner, a stuck animation) is diagnosed by the
        recognition gate that follows, which is where a real stall belongs.

        Hot path: frames come from capture_image() (PIL, no PNG coding) and are
        never written anywhere. Rounds spent land in node_stats as
        wait_still_rounds — telemetry only.
        """
        spec = node.get("wait_still")
        if not isinstance(spec, dict):
            return
        rounds = self._settle_until_still(
            device_id,
            timeout_ms=int(spec.get("timeout_ms", DEFAULT_WAIT_STILL_TIMEOUT_MS)),
            interval_ms=int(spec.get("interval_ms", DEFAULT_WAIT_STILL_INTERVAL_MS)),
            threshold=float(spec.get("threshold", DEFAULT_WAIT_STILL_THRESHOLD)),
            label=f"Node '{name}' wait_still",
        )
        if rounds:
            stat = self._stat(name)
            stat["wait_still_rounds"] = stat.get("wait_still_rounds", 0) + rounds

    def _settle_until_still(self, device_id: str, timeout_ms: int, interval_ms: int,
                            threshold: float, label: str) -> int:
        """Block until two consecutive frames barely differ; return rounds spent.

        Shared by the node-level `wait_still` window and the action-level
        `repeat_wait_freezes_ms` gap, so both judge "the screen has stopped
        moving" the exact same way — and both treat running out of time the same
        way: **stop waiting, don't fail**. No assertion, no finding; a screen
        that never settles is diagnosed by the recognition gate that follows.

        Hot path: frames come from capture_image() (PIL, no PNG coding) and are
        never written anywhere. Returns 0 when no capturer is reachable, i.e.
        degrade to no wait rather than block.
        """
        previous = self._grab_frame(device_id)
        if previous is None:
            return 0
        rounds = 0
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            time.sleep(interval_ms / 1000)
            frame = self._grab_frame(device_id)
            rounds += 1
            if frame is None:
                return rounds
            try:
                ratio = image_change_ratio(previous, frame)
            except Exception as exc:
                self.logger.warning("%s change check failed: %s", label, exc)
                return rounds
            if ratio < threshold:
                self.logger.info(
                    "%s: screen still after %d round(s) (change %.4f < %.4f)",
                    label, rounds, ratio, threshold,
                )
                return rounds
            if time.monotonic() >= deadline:
                self.logger.info(
                    "%s gave up after %dms (last change %.4f); continuing",
                    label, timeout_ms, ratio,
                )
                return rounds
            previous = frame

    def _poll_candidates(self, device_id: str, candidates: List[str], nodes: Dict,
                         source: str = "direct") -> Optional[Tuple[str, Dict]]:
        """Round-robin recognition over candidates until one hits or the budget runs out.

        Every candidate is evaluated at least once even with a zero timeout.

        `source` only labels the node_stats counter the hit lands in (how the
        node was reached: directly, after a popup sweep, after the BACK
        fallback, or as a recovery/skip target) — it changes nothing about the
        recognition itself.
        """
        timeout_ms = max(nodes[name].get("timeout_ms", DEFAULT_TIMEOUT_MS) for name in candidates)
        interval_ms = min(nodes[name].get("poll_interval_ms", DEFAULT_POLL_INTERVAL_MS) for name in candidates)
        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        counter = self._HIT_COUNTERS.get(source, "direct_hits")
        label = "[" + ", ".join(candidates) + "]"
        rounds = 0
        last_beat = started

        while True:
            for name in candidates:
                self._stat(name)["poll_rounds"] += 1
                hit = self._recognize_node(device_id, name, nodes[name]["recognition"])
                if hit is not None:
                    self._stat(name)[counter] += 1
                    return name, hit
            rounds += 1
            now = time.monotonic()
            self.logger.debug(
                "%spoll miss #%d on %s (source=%s)", self._step_tag(), rounds, label, source
            )
            # One INFO heartbeat per POLL_HEARTBEAT_S so a slow screen shows up
            # as "still waiting", not as a frozen terminal — without one line
            # per poll round (that stays in run.log at DEBUG).
            if now - last_beat >= POLL_HEARTBEAT_S:
                last_beat = now
                self.logger.info(
                    "%swaiting for %s (%.1fs elapsed, %d polls)",
                    self._step_tag(), label, now - started, rounds,
                )
            if now >= deadline or self._stop_requested:
                return None
            time.sleep(interval_ms / 1000)

    # ---------- node health telemetry (read-only; never gates anything) ----------

    #: How a node was reached -> the node_stats counter that records it.
    _HIT_COUNTERS = {
        "direct": "direct_hits",
        "popup": "popup_assisted_hits",
        "back": "back_assisted_hits",
        "recovery": "recovery_hits",
    }

    def _stat(self, node: str) -> Dict:
        """Per-node counters, created on first touch.

        Aggregated in memory rather than derived from the findings timeline,
        which is capped and would silently truncate long runs.
        """
        return self._node_stats.setdefault(
            node,
            {
                "poll_rounds": 0,
                "direct_hits": 0,
                "popup_assisted_hits": 0,
                "back_assisted_hits": 0,
                "recovery_hits": 0,
                "timeout_recoveries": 0,
                "drift_count": 0,
                "drift_px": [],
                # Polling rounds spent in the optional wait_still settle window.
                "wait_still_rounds": 0,
            },
        )

    def _report_rot_suspects(self) -> None:
        """Flag nodes whose anchors look rotten, once per node per run.

        A node that needed its on_timeout escape repeatedly, or whose anchor
        kept moving, is usually not a game bug but a stale task: the UI changed
        under the recorded anchor. That is worth one warning finding at the end
        of the run (no screenshot — it is a statistic about the whole run, and
        the screen at finalize time says nothing about it).
        """
        if not self.recorder or self.rot_suspect_timeouts <= 0:
            return
        for name, stat in sorted(self._node_stats.items()):
            reasons: List[str] = []
            if stat["timeout_recoveries"] >= self.rot_suspect_timeouts:
                reasons.append(f"{stat['timeout_recoveries']} timeout recoveries")
            if stat["drift_count"] >= self.rot_suspect_timeouts:
                reasons.append(f"{stat['drift_count']} anchor moves")
            if not reasons:
                continue
            self.recorder.record(
                "anchor_rot_suspect", "warning",
                f"Node '{name}' was unstable this run ({', '.join(reasons)}); "
                "its recognition anchor may have rotted — re-check the node's "
                "recognition spec against the current UI",
                node=name, screenshot=False, extra=dict(stat),
            )

    def _recognize_node(self, device_id: str, name: str, spec: Dict) -> Optional[Dict]:
        """Recognize one node, using the replay cache (ROI fast path) when available.

        The cache only narrows the OCR search region; the recognition gate stays
        intact. An anchor found outside its cached region means the UI moved —
        that is a QA finding (anchor_drift), reported with evidence, not healed
        silently. A move shorter than `engine.drift_tolerance_px` is layout
        jitter (a slightly reflowed row, a wider badge): it still lands in
        node_stats, but reporting it as a finding would bury the real ones.
        """
        cache = getattr(self.hub, "replay_cache", None)
        if cache is None or not self._task_name:
            return self.hub.recognize(device_id, spec)

        key = ReplayCache.make_key(device_id, self._task_name, name)
        hit = self.hub.recognize(device_id, spec, cache_key=key)
        if hit:
            # A combined recognition (and/or) surfaces only the box_index child's
            # cache metadata at the top level; every other sub-anchor's drift
            # lives inside sub_hits. Walk down to the leaves so a non-box_index
            # anchor that moved is still reported — never silently healed (core
            # invariant #1). A plain hit has no sub_hits and is its own leaf.
            for drift_hit in self._collect_drift_hits(hit):
                self._report_anchor_drift(name, drift_hit)
        return hit

    @classmethod
    def _collect_drift_hits(cls, hit: Dict) -> List[Dict]:
        """Flatten a (possibly combined) hit into the leaf sub-hits that drifted.

        A combination hit copies its box_index child verbatim to the top level
        AND lists every child in `sub_hits`, so the wrapper's own `cache` field
        is a duplicate of one leaf's. To avoid double-counting, a hit with
        `sub_hits` contributes nothing itself — only its leaves do (recursing
        through nested and/or). A leaf hit counts when its own cache == "drift".
        """
        subs = hit.get("sub_hits")
        if subs:
            drifted: List[Dict] = []
            for sub in subs:
                if isinstance(sub, dict):
                    drifted.extend(cls._collect_drift_hits(sub))
            return drifted
        return [hit] if hit.get("cache") == "drift" else []

    def _report_anchor_drift(self, name: str, hit: Dict) -> None:
        """Record one drifted anchor: telemetry always, finding beyond tolerance.

        A move shorter than `engine.drift_tolerance_px` is layout jitter (a
        slightly reflowed row, a wider badge): it still lands in node_stats, but
        reporting it as a finding would bury the real ones.
        """
        distance = hit.get("drift_px")
        if distance is None and hit.get("prev_center") and hit.get("center"):
            distance = round(center_distance(hit["prev_center"], hit["center"]), 1)
        stat = self._stat(name)
        stat["drift_count"] += 1
        if distance is not None:
            stat["drift_px"].append(distance)
        message = (
            f"Anchor for node '{name}' moved from {hit.get('prev_center')} to "
            f"{list(hit['center'])} by {distance}px (text='{hit['text']}'); "
            "UI layout changed since last replay"
        )
        if distance is not None and distance < self.drift_tolerance_px:
            self.logger.info(
                "Anchor for node '%s' shifted %spx (within %.1fpx tolerance); counted, not reported",
                name, distance, self.drift_tolerance_px,
            )
        elif self.recorder:
            self.recorder.record(
                "anchor_drift", "warning", message, node=name, screenshot=True,
                extra={
                    "prev_center": hit.get("prev_center"),
                    "new_center": list(hit["center"]),
                    "drift_px": distance,
                },
            )
        else:
            self.logger.warning("ANCHOR DRIFT: %s", message)

    def _record_node_finding(self, name: str, node: Dict) -> None:
        """Nodes marked with "finding" report themselves when entered (popup branches etc.)."""
        spec = node.get("finding")
        if not spec or not self.recorder:
            return
        if isinstance(spec, str):
            severity, message = "warning", spec
        else:
            severity = spec.get("severity", "warning")
            message = spec.get("message", f"Anomaly node '{name}' reached")
        self.recorder.record("anomaly_node", severity, message, node=name, screenshot=True)

    def _check_watchdogs(
        self, device_id: str, watchdogs: List[Dict], seen: Set[int], node: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Evaluate task-level negative assertions against the current screen.

        One frame is grabbed up front and shared across every watchdog and as
        the finding's evidence, so a transient toast that trips a check is the
        very image saved as proof — not a re-capture taken after it vanished.

        Each watchdog records one finding per run (first hit wins; a persistent
        error dialog should not flood the report). Returns (error, skip_target):
        a reported bug routes to a recovery node when the watchdog defines
        `skip_to` (or the task-level `on_finding` applies), so testing continues
        past the break; a fatal `fail_task` watchdog with no recovery sets the
        error instead. A skip target supersedes an abort. Routing is resolved
        only for a freshly recorded finding, so the same watchdog skips at most
        once per run (and a stall with no watchdog hit yields neither).
        """
        if not watchdogs:
            return None, None
        frame = self._grab_frame(device_id)
        error: Optional[str] = None
        skip_target: Optional[str] = None
        for idx, watchdog in enumerate(watchdogs):
            spec = {k: watchdog[k] for k in WATCHDOG_SPEC_KEYS if k in watchdog}
            try:
                hit = self.hub.recognize(device_id, spec, image=frame)
            except Exception as exc:
                self.logger.warning("Watchdog #%d check failed: %s", idx + 1, exc)
                continue
            if hit is None:
                continue
            message = watchdog.get("message") or (
                f"Watchdog hit: '{watchdog.get('expected')}' on screen (channel={hit['channel']})"
                if watchdog.get("expected")
                else f"Watchdog hit: {watchdog['type']} (score={hit['score']})"
            )
            if idx not in seen:
                seen.add(idx)
                if self.recorder:
                    self.recorder.record(
                        "watchdog", watchdog.get("severity", "error"), message,
                        node=node, screenshot=True, image=frame,
                        extra={"expected": watchdog.get("expected"), "hit": hit},
                    )
                else:
                    self.logger.warning("Watchdog: %s", message)
                # Route the reported bug: explicit skip_to wins, else a fatal
                # fail_task aborts, else the task-level on_finding (if set)
                # recovers.
                target = watchdog.get("skip_to") or (
                    None if watchdog.get("fail_task") else self._on_finding
                )
                if target and skip_target is None:
                    skip_target = target
                elif watchdog.get("fail_task") and error is None:
                    location = f" at node '{node}'" if node else ""
                    error = f"Watchdog triggered{location}: {message}"
        if skip_target:
            return None, skip_target
        return error, None

    def _enter_bug_skip(
        self, device_id: str, target: str, nodes: Dict, from_node: Optional[str]
    ) -> Optional[Tuple[str, Dict]]:
        """Recover past a reported bug by jumping to a skip target.

        Reaching here means a watchdog/crash finding was just recorded, so this
        is a routed recovery, not a stall escape — a bare recognition timeout
        never calls this. Returns the recognized skip node, or None if it can't
        be reached (its own recognition didn't hit within budget).
        """
        if target not in nodes:
            self.logger.error("Bug-skip target '%s' not defined in task nodes", target)
            return None
        self.logger.warning("Reported bug at node '%s'; skipping to '%s'", from_node, target)
        if self.recorder:
            self.recorder.add_timeline("bug_skip", node=from_node, target=target)
        return self._poll_candidates(device_id, [target], nodes, source="recovery")

    def _grab_frame(self, device_id: str):
        """One PIL frame for a watchdog shot, reused for detection + evidence.

        Returns None when no capturer is reachable (recognition then grabs its
        own frame per channel), so the path degrades instead of breaking.
        """
        capturer = getattr(self.hub, "capturer", None)
        if capturer is None:
            return None
        try:
            return capturer.capture_image(device_id)
        except Exception as exc:
            self.logger.warning("Watchdog frame capture failed: %s", exc)
            return None

    def _timeout_error(
        self, device_id: str, watchdogs: List[Dict], seen: Set[int], node: Optional[str], timeout_error: str
    ) -> Tuple[str, Optional[str]]:
        """On recognition timeout, let watchdogs explain the stall if they can.

        A stuck screen is often stuck *because* an error dialog / blank screen
        is showing; the watchdog hit is then recorded as a finding and either
        recovered via its skip target (a detected bug skips past the break
        rather than aborting) or promoted to the task error for a precise
        diagnosis. A bare timeout with no watchdog hit stays a timeout — a stall
        alone never skips. Returns (error, skip_target).
        """
        watchdog_error, skip_target = self._check_watchdogs(device_id, watchdogs, seen, node=node)
        return (watchdog_error or timeout_error), skip_target

    def _tick_video(self) -> None:
        if self.screen:
            try:
                self.screen.tick()
            except Exception as exc:
                self.logger.warning("screen recorder tick failed: %s", exc)
        if self.pcap:
            try:
                self.pcap.tick()
            except Exception as exc:
                self.logger.warning("pcap recorder tick failed: %s", exc)

    def _poll_logcat(self, device_id: str, node: Optional[str] = None) -> Optional[str]:
        """Drain logcat crash/ANR events into findings; return a skip target.

        A crash/ANR is a reported bug, so it recovers via the task-level
        `on_finding` when one is set (logcat events have no per-watchdog
        skip_to). Returns None when nothing tripped or no recovery is configured.
        """
        if not self.logcat:
            return None
        try:
            events = self.logcat.poll(device_id)
        except Exception as exc:
            self.logger.warning("logcat poll failed: %s", exc)
            return None
        skip_target: Optional[str] = None
        for event in events:
            if self.recorder:
                self.recorder.record(
                    event["type"], event["severity"], event["line"],
                    node=node, screenshot=True, extra={"excerpt": event.get("excerpt", [])},
                )
            else:
                self.logger.warning("logcat %s: %s", event["type"], event["line"])
            if self._on_finding and skip_target is None:
                skip_target = self._on_finding
        return skip_target

    def _execute_node_action(
        self, device_id: str, node: Dict, hit: Dict, tracer: Optional["DebugTracer"]
    ) -> List[Dict]:
        action = node["action"]
        action_type = action["type"]

        if action_type == "none":
            return []

        if action_type == "custom":
            handler_name = action.get("name", "")
            handler = get_handler(handler_name)
            if handler is None:
                raise ValueError(f"Unregistered custom action '{handler_name}'")
            ctx = CustomActionContext(
                device_id=device_id,
                executor=self.executor,
                hub=self.hub,
                hit=hit,
                logger=self.logger,
                tracer=tracer,
            )
            started = time.monotonic()
            results = handler(ctx, action.get("params", {}))
            self._log_action(f"custom '{handler_name}'", results, started)
            return results

        params = action.get("params", {}) or {}
        if action_type == "click" and action.get("target") == "recognized":
            center = hit.get("center")
            if not center:
                raise ValueError("click target='recognized' but recognition produced no coordinates")
            actions = [{"type": "click", "params": {"x": center[0], "y": center[1]}}]
        else:
            # The repeat knobs are engine-level, not executor params.
            actions = [{
                "type": action_type,
                "params": {k: v for k, v in params.items() if k not in REPEAT_PARAM_KEYS},
            }]

        detail = self._describe_action(actions[0])
        started = time.monotonic()
        results = [self.executor.execute(device_id, a, tracer) for a in actions]
        repeat = int(params.get("repeat", 1) or 1)
        if repeat <= 1:
            self._log_action(detail, results, started)
            return results
        results = self._repeat_action(device_id, actions, tracer, results, params, repeat)
        # A burst is ONE log line (count + total time): logging every shot of a
        # 40-tap QTE mash would bury the flow (a failed shot already warns).
        self._log_action(f"{detail} x{repeat}", results, started)
        return results

    @staticmethod
    def _describe_action(action: Dict) -> str:
        """Human-readable one-liner for an action about to run.

        Coordinates are the RESOLVED ones (a `target: "recognized"` click has
        already been turned into absolute x/y by the caller), which is the whole
        point: the log has to say where the tap actually landed.
        """
        action_type = action.get("type", "?")
        params = action.get("params", {}) or {}
        if action_type == "click":
            return f"click ({params.get('x')}, {params.get('y')})"
        if action_type == "drag":
            return (
                f"drag ({params.get('x1')}, {params.get('y1')})"
                f"->({params.get('x2')}, {params.get('y2')})"
            )
        if action_type == "key":
            return f"key keycode={params.get('keycode')}"
        if action_type == "wait":
            return f"wait {params.get('duration_ms', 1000)}ms"
        if action_type == "input_text":
            return f"input_text '{params.get('text', '')}'"
        return str(action_type)

    def _log_action(self, detail: str, results: List[Dict], started: float) -> None:
        """One line per executed action: what ran, where, verdict, elapsed."""
        elapsed_ms = int((time.monotonic() - started) * 1000)
        failed = [r for r in (results or []) if r.get("ok") != "True"]
        if failed:
            self.logger.warning(
                "%saction %s FAILED %dms: %s",
                self._step_tag(), detail, elapsed_ms, failed[0].get("stderr", ""),
            )
        else:
            self.logger.info("%saction %s ok %dms", self._step_tag(), detail, elapsed_ms)

    def _repeat_action(self, device_id: str, actions: List[Dict], tracer: Optional["DebugTracer"],
                       results: List[Dict], params: Dict, repeat: int) -> List[Dict]:
        """Fire an already-built action `repeat` times: the QTE-mashing path.

        Recognition is NOT re-run between shots — that is the whole point: on a
        real device the pause between separately-recognized node executions is
        what swallows a sub-second window, so the shots stay inside one node
        execution, as tight as the backend allows.

        Sequence: action → [wait-for-still → delay → action] × (repeat-1).
        `repeat_wait_freezes_ms` is "tap, let the UI answer, tap again" and
        shares wait_still's semantics (a timeout only stops the wait — it is not
        a failure and records no finding); `repeat_delay_ms` is a plain pause.

        A failed shot does not abort the remaining ones (Maa semantics): it is
        logged, and the LAST shot's results are returned, so the caller's
        pass/fail verdict is about the final state of the burst. Watchdog
        sampling is untouched — the caller samples once after the whole burst,
        not per shot.
        """
        delay_ms = int(params.get("repeat_delay_ms", 0) or 0)
        freeze_ms = int(params.get("repeat_wait_freezes_ms", 0) or 0)
        for index in range(1, repeat):
            failed = [r for r in results if r.get("ok") != "True"]
            if failed:
                self.logger.warning(
                    "Repeat shot %d/%d failed (%s); continuing with the remaining shots",
                    index, repeat, failed[0].get("stderr", ""),
                )
            if freeze_ms:
                self._settle_until_still(
                    device_id,
                    timeout_ms=freeze_ms,
                    # A freeze budget tighter than the standard sampling period
                    # would otherwise be rounded up to one full sample.
                    interval_ms=min(DEFAULT_WAIT_STILL_INTERVAL_MS, freeze_ms),
                    threshold=DEFAULT_WAIT_STILL_THRESHOLD,
                    label=f"repeat_wait_freezes shot {index + 1}/{repeat}",
                )
            if delay_ms:
                time.sleep(delay_ms / 1000)
            results = [self.executor.execute(device_id, a, tracer) for a in actions]
        return results

    def _finish(
        self,
        device_id: str,
        steps: List[Dict],
        tracer: Optional["DebugTracer"],
        error: Optional[str] = None,
        handoff: Optional[Dict] = None,
    ) -> Dict:
        if handoff:
            status = "agent_required"
        elif error:
            status = "failed"
        else:
            status = "completed"

        # Final logcat drain before the report is sealed, so a crash on the
        # very last step still lands in the findings.
        self._poll_logcat(device_id)

        node_stats = {name: dict(stat) for name, stat in sorted(self._node_stats.items())}

        findings: List[Dict] = []
        report: Optional[Dict] = None
        if self.recorder:
            # Health verdicts are drawn before the report is sealed, so a rot
            # suspect lands in the same report.json as the run it describes.
            self._report_rot_suspects()
            findings, report = self.recorder.finalize(
                status=status, error=error, node_stats=node_stats
            )

        # Stop recording only after finalize: a task_failure finding pulls its
        # video evidence during finalize, before device files are cleaned up.
        if self.screen:
            try:
                self.screen.stop()
            except Exception as exc:
                self.logger.warning("screen recorder stop failed: %s", exc)
        if self.pcap:
            try:
                self.pcap.stop()
            except Exception as exc:
                self.logger.warning("pcap recorder stop failed: %s", exc)

        result = {
            "ok": status == "completed",
            "status": status,
            "steps": steps,
            "error": error,
            "handoff": handoff,
            "findings": findings,
            "report": report,
            # Known-benign popups auto-cleared during the run (not findings, but
            # surfaced so the user knows what was dismissed on their behalf).
            "popups_dismissed": list(self._dismissed_popups),
            # Per-node health telemetry (how each node was reached, anchor
            # moves); observation only, also written to report.json.
            "node_stats": node_stats,
        }
        # One summary line for EVERY outcome (a clean run used to end in
        # silence): the per-branch error/handoff/findings lines below keep their
        # own levels, this only adds the numbers that close the run out.
        self.logger.info(
            "Task '%s' %s: steps=%d duration=%.1fs findings=%d device=%s",
            self._task_name or "(unnamed)", status, len(steps),
            time.monotonic() - self._run_started if self._run_started else 0.0,
            len(findings), device_id,
        )
        self._log_capture_stats(device_id)
        if error:
            self.logger.error("Task failed: %s", error)
        if handoff:
            self.logger.info(
                "Task suspended at node '%s'; agent instruction: %s", handoff["node"], handoff["instruction"]
            )
        if findings:
            self.logger.warning("Task run produced %d finding(s); see %s",
                                len(findings), (report or {}).get("report_path") or "result['findings']")
        if tracer and tracer.enabled:
            tracer.record(
                task_status=status, task_error=error, task_handoff=handoff,
                task_steps=steps, task_findings=findings,
            )
            tracer.flush()
        return result
