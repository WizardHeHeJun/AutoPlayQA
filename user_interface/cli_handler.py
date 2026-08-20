from __future__ import annotations

import json

from user_interface.command_parser import parse_command
from utils.debug_tracer import SessionRecorder


HELP_TEXT = """Commands:
    device list | devices list
    device connect <ip[:port]>        # adb over Wi-Fi (port defaults to 5555)
    device disconnect [ip[:port]]     # no address = disconnect all wireless
    device tcpip <device_id> [port]   # switch USB device to Wi-Fi mode, prints address
    device pair <ip:pairing_port> <code>   # Android 11+ wireless debugging pairing
    agent list | agents list
    agent select <index|device_id|all>
    input <text>
    action <natural language>
    <natural language>    # no prefix needed, defaults to action
    click <x> <y>
    drag <x1> <y1> <x2> <y2> [duration_ms]
    task list | task show <name> | task run <name>
    task show <name>            # step-numbered flow outline, then the JSON
    task renumber <name>        # write a "step" field into each node of the file
    task resume <name> <node>   # continue after an agent-handoff node
    task suites                 # list suite definitions (task_definitions/suites/)
    task suite <name> [device]  # run a suite: log in once, chain the cases, restart on failure
    task cache [status|clear]   # replay anchor cache (OCR ROI fast path)
    task lint <name>            # best-practice warnings (W001..W005), never blocks
    task health [name] [--days N]  # cross-run node_stats trends (outputs/findings/**/report.json)
    task handoffs [name] [--days N]  # agent-handoff action logs: which agent nodes to solidify
    record on | record off | record status
    record gestures start [device_id]   # capture real finger gestures (getevent + frames)
    record gestures stop [device_id]    # stop and print the gesture sequence + artifacts
    record gestures status [device_id]
    task save <name>      # save the recorded session as a replay-draft task
    debug on | debug off | debug status
    help
    exit
"""


def _print_lint_warnings(warnings, indent: str = "  ") -> None:
    for w in warnings:
        location = f" @{w.node}" if w.node else ""
        print(f"{indent}[{w.rule_id}]{location} {w.message}")
        print(f"{indent}    suggestion: {w.suggestion}")


def _print_suite_progress(event) -> None:
    """Live suite progress: one line per case boundary, one per node."""
    kind = event["event"]
    if kind == "case_start":
        boot = "cold start + login" if event["boot"] == "full" else "resume (boot skipped)"
        attempt = f" attempt {event['attempt']}" if event["attempt"] > 1 else ""
        print(f"[{event['index']}/{event['total']}] {event['case']}: {boot}{attempt}")
    elif kind == "node":
        print(f"      .. {event['node']}")
    elif kind == "case_end":
        completed = event["status"] == "completed"
        mark = "OK" if completed and event["landed"] else event["status"].upper()
        tail = " -> restarting and retrying" if event["will_retry"] else ""
        # Only meaningful for a run that thought it finished: a failed one is
        # off-scene by definition and the error line already says why.
        landing = " [not on the landing scene]" if completed and not event["landed"] else ""
        print(f"    = {event['case']}: {mark}{landing} {event['duration_s']}s "
              f"findings={event['findings']}{tail}")
        if event["error"]:
            print(f"      error: {event['error']}")


def _resolve_device(agent_pool, explicit):
    """Pick the device a `record gestures` sub-command applies to."""
    if explicit:
        return explicit, None
    if agent_pool.selected != "all":
        return agent_pool.selected, None
    agents = agent_pool.list_agents()
    if not agents:
        return None, "No devices available; run 'device list' first."
    if len(agents) == 1:
        return agents[0].device_id, None
    return None, "Multiple devices connected; name one (record gestures <start|stop> <device_id>)."


def _print_gesture_result(result) -> None:
    print(f"record gestures: {result['gesture_count']} gesture(s) -> {result['session_dir']}")
    for g in result["gestures"]:
        images = " ".join(f"{k}={v}" for k, v in g["images"].items())
        # A swipe's full path is manifest detail, not console output.
        params = {k: v for k, v in g["params"].items() if k != "path"}
        print(f"  [{g['index']:>3}] {g['type']:<11} {params} {g['duration_ms']}ms +{g['t_offset_ms']}ms")
        if images:
            print(f"        {images}")
    print(f"  manifest: {result['manifest_path']}")
    if result.get("warning"):
        print(f"  ! {result['warning']}")
    print("  Map each gesture to a recognition anchor (see the live-record skill) before saving a task.")


def run_cli(agent_pool, device_manager, logger, config=None, task_engine=None):
    debug_config = config.get("debug", {}) if config else {}
    recorder = SessionRecorder()
    gesture_registry = None  # built on first `record gestures` use

    print("AutoPlayQA CLI ready. Type 'help' for commands.")
    while True:
        try:
            raw = input("apq> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye")
            break

        cmd = parse_command(raw)

        if cmd["type"] == "empty":
            continue
        if cmd["type"] == "help":
            print(HELP_TEXT)
            continue
        if cmd["type"] == "exit":
            print("Bye")
            break
        if cmd["type"] == "debug_control":
            sub = cmd["sub"]
            if sub == "on":
                debug_config["enabled"] = True
                print("debug: on")
            elif sub == "off":
                debug_config["enabled"] = False
                print("debug: off")
            elif sub == "status":
                print(f"debug: {'on' if debug_config.get('enabled') else 'off'}")
            continue
        if cmd["type"] == "record_control":
            sub = cmd["sub"]
            if sub == "on":
                recorder.clear()
                recorder.start()
                print("record: on (session cleared)")
            elif sub == "off":
                recorder.stop()
                print(f"record: off ({len(recorder.records)} command(s) captured)")
            else:
                state = "on" if recorder.enabled else "off"
                print(f"record: {state}, {len(recorder.records)} command(s) captured")
            continue
        if cmd["type"] == "record_gestures":
            from record.record_session import GestureRecordingRegistry

            if gesture_registry is None:
                gesture_registry = GestureRecordingRegistry(
                    agent_pool.screenshot_capturer, logger, device_manager=device_manager
                )
            device_id, error = _resolve_device(agent_pool, cmd.get("device_id"))
            if error:
                print(error)
                continue
            if cmd["sub"] == "status":
                status = gesture_registry.status(device_id)
                if status.get("recording"):
                    print(f"record gestures: on since {status['started_at']}, "
                          f"{status['gesture_count']} gesture(s) -> {status['session_dir']}")
                else:
                    print(f"record gestures: off for {device_id}")
                continue
            if cmd["sub"] == "start":
                result = gesture_registry.record_start(device_id)
                if not result["ok"]:
                    print(f"Error: {result['error']}")
                    continue
                print(f"record gestures: on ({device_id}) -> {result['session_dir']}")
                print("  Demonstrate the flow at normal speed, then: record gestures stop")
                continue
            result = gesture_registry.record_stop(device_id)
            if not result["ok"]:
                print(f"Error: {result['error']}")
                continue
            _print_gesture_result(result)
            continue
        if cmd["type"] == "task_list":
            from task.task_loader import list_tasks

            names = list_tasks()
            if not names:
                print("No tasks found in task/task_definitions/.")
            for name in names:
                print(f"  {name}")
            continue
        if cmd["type"] == "task_show":
            from task.task_loader import TaskValidationError, get_task_path, load_task
            from task.step_numbering import format_task_outline

            path = get_task_path(cmd["name"])
            if not path.is_file():
                print(f"Task not found: {path}")
                continue
            try:
                task = load_task(path)
                print(format_task_outline(task))
                print()
            except TaskValidationError as exc:
                print(f"(outline unavailable: {exc})\n")
            print(path.read_text(encoding="utf-8"))
            continue
        if cmd["type"] == "task_renumber":
            from task.task_loader import TaskValidationError, get_task_path
            from task.step_numbering import write_step_labels

            path = get_task_path(cmd["name"])
            if not path.is_file():
                print(f"Task not found: {path}")
                continue
            try:
                result = write_step_labels(path)
            except TaskValidationError as exc:
                print(f"Error: {exc}")
                continue
            print(f"Renumbered {result['count']} node(s) -> {result['path']}")
            continue
        if cmd["type"] in ("task_run", "task_resume"):
            from task.task_loader import TaskValidationError, get_task_path, load_task
            from utils.debug_tracer import DebugTracer

            if task_engine is None:
                print("Task engine not available.")
                continue
            try:
                task = load_task(get_task_path(cmd["name"]))
            except TaskValidationError as exc:
                print(f"Error: {exc}")
                continue
            start_after = cmd.get("node")
            if start_after and start_after not in task["nodes"]:
                print(f"Error: node '{start_after}' not in task '{cmd['name']}'")
                continue
            agents = agent_pool.list_agents()
            targets = [a.device_id for a in agents] if agent_pool.selected == "all" else [agent_pool.selected]
            if not targets or not agents:
                print("No agents available; run 'device list' first.")
                continue
            for device_id in targets:
                tracer = DebugTracer(device_id, debug_config) if debug_config.get("enabled") else None
                result = task_engine.run(
                    device_id, task, tracer, start_after=start_after, task_name=cmd["name"]
                )
                if result["status"] == "completed":
                    status = "OK"
                elif result["status"] == "agent_required":
                    status = "SUSPENDED (agent step required)"
                else:
                    status = f"FAILED: {result['error']}"
                print(f"[{device_id}] {status} ({len(result['steps'])} step(s))")
                for step in result["steps"]:
                    rec = step["recognition"]
                    print(f"    node={step['node']} via={rec['channel']} score={rec['score']}")
                findings = result.get("findings") or []
                if findings:
                    print(f"    !! {len(findings)} finding(s) detected:")
                    for f in findings:
                        location = f" @{f['node']}" if f.get("node") else ""
                        print(f"    !! [{f['severity']}] {f['type']}{location}: {f['message']}")
                        for kind, path in (f.get("evidence") or {}).items():
                            if isinstance(path, list):
                                print(f"         evidence {kind}: {len(path)} file(s), latest {path[-1]}")
                            else:
                                print(f"         evidence {kind}: {path}")
                        for line in (f.get("recent_flow") or [])[-5:]:
                            print(f"         flow {line}")
                    report = result.get("report") or {}
                    if report.get("report_path"):
                        print(f"    !! findings report: {report['report_path']}")
                    if report.get("report_html_path"):
                        print(f"    !! open in browser: {report['report_html_path']}")
                    if report.get("export_path"):
                        print(f"    !! exported to: {report['export_path']}")
                if result["status"] == "agent_required":
                    handoff = result["handoff"]
                    print(f"    >> agent instruction: {handoff['instruction']}")
                    print(f"    >> perform it manually (or via Claude/Codex), then run:")
                    print(f"    >>   task resume {cmd['name']} {handoff['node']}")
            continue
        if cmd["type"] == "task_suite_list":
            from task.task_loader import list_suites

            names = list_suites()
            if not names:
                print("No suites found in task/task_definitions/suites/.")
            for name in names:
                print(f"  {name}")
            continue
        if cmd["type"] == "task_suite":
            from task.suite_runner import SuiteRunner, format_suite_report
            from task.task_loader import SuiteValidationError, TaskValidationError, load_suite

            if task_engine is None:
                print("Task engine not available.")
                continue
            device_id, error = _resolve_device(agent_pool, cmd.get("device_id"))
            if error:
                print(error)
                continue
            try:
                suite = load_suite(cmd["name"])
            except SuiteValidationError as exc:
                print(f"Error: {exc}")
                continue
            runner = SuiteRunner(
                task_engine, logger, findings_recorder=getattr(task_engine, "recorder", None)
            )
            print(f"Running suite '{suite['name']}' ({len(suite['cases'])} case(s)) on {device_id} ...")
            try:
                result = runner.run(device_id, suite, on_progress=_print_suite_progress)
            except (SuiteValidationError, TaskValidationError) as exc:
                print(f"Error: {exc}")
                continue
            print(format_suite_report(result))
            for record in result["cases"]:
                if record["report_html_path"]:
                    print(f"    report [{record['case']}]: {record['report_html_path']}")
            continue
        if cmd["type"] == "task_cache":
            cache = getattr(task_engine.hub, "replay_cache", None) if task_engine else None
            if cache is None:
                print("Replay cache disabled (config replay_cache.enabled).")
            elif cmd["sub"] == "clear":
                print(f"Cleared {cache.clear()} cached anchor(s).")
            else:
                print(f"Replay cache: {cache.size()} anchor(s) in {cache.path}")
            continue
        if cmd["type"] == "task_lint":
            from task.task_loader import TaskValidationError, get_task_path, load_task
            from task.task_lint import lint_task

            path = get_task_path(cmd["name"])
            if not path.is_file():
                print(f"Task not found: {path}")
                continue
            try:
                task = load_task(path)
            except TaskValidationError as exc:
                print(f"Error: {exc}")
                continue
            warnings = lint_task(task)
            if not warnings:
                print(f"No lint warnings for '{cmd['name']}'.")
            else:
                print(f"{len(warnings)} lint warning(s) for '{cmd['name']}':")
                _print_lint_warnings(warnings)
            continue
        if cmd["type"] == "task_health":
            from task.anchor_health import format_health_report, scan_health

            findings_dir = (config or {}).get("findings", {}).get("output_dir", "outputs/findings")
            tasks = scan_health(
                findings_dir, task_name=cmd.get("task_name"), days=cmd.get("days"), logger=logger
            )
            print(format_health_report(tasks))
            continue
        if cmd["type"] == "task_handoffs":
            from task.handoff_stats import DEFAULT_SESSIONS_DIR, format_handoff_report, scan_handoffs

            sessions_dir = (config or {}).get("recording", {}).get(
                "agent_sessions_dir", DEFAULT_SESSIONS_DIR
            )
            data = scan_handoffs(
                sessions_dir, task_name=cmd.get("task_name"), days=cmd.get("days"), logger=logger
            )
            print(format_handoff_report(data))
            continue
        if cmd["type"] == "task_save":
            from task.task_editor import records_to_draft_task
            from task.task_loader import DEFAULT_TASK_DIR, TaskValidationError, get_task_path
            from task.task_lint import lint_task

            if not recorder.records:
                print("Nothing recorded. Use 'record on', run some actions, then save.")
                continue
            try:
                task = records_to_draft_task(recorder.records)
            except (TaskValidationError, ValueError) as exc:
                print(f"Task generation failed: {exc}")
                continue
            warnings = lint_task(task)
            lint_strict = bool((config or {}).get("lint", {}).get("strict", False))
            if warnings and lint_strict:
                print(f"lint.strict is on; refusing to save '{cmd['name']}' ({len(warnings)} warning(s)):")
                _print_lint_warnings(warnings)
                continue
            DEFAULT_TASK_DIR.mkdir(parents=True, exist_ok=True)
            path = get_task_path(cmd["name"])
            if path.is_file():
                print(f"Overwriting existing task: {path}")
            path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved replay-draft task '{cmd['name']}' ({len(task['nodes'])} node(s)) -> {path}")
            print("Note: draft replays literal actions; have Claude/Codex rewrite it with")
            print("ui_text/ocr recognition anchors (MCP save_task) for robustness.")
            print(f"Verify with: task run {cmd['name']}")
            if warnings:
                print(f"  ! {len(warnings)} lint warning(s) (non-strict, saved anyway):")
                _print_lint_warnings(warnings, indent="    ")
            continue
        if cmd["type"] == "device_connect":
            result = device_manager.connect(cmd["address"])
            print(result["message"])
            if result["ok"]:
                devices = device_manager.discover_devices()
                agent_pool.sync_from_devices(devices)
                print(f"{len(devices)} device(s) online.")
            continue
        if cmd["type"] == "device_disconnect":
            result = device_manager.disconnect(cmd.get("address"))
            print(result["message"] or "disconnected")
            agent_pool.sync_from_devices(device_manager.discover_devices())
            continue
        if cmd["type"] == "device_tcpip":
            result = device_manager.enable_tcpip(cmd["device_id"], cmd["port"])
            print(result["message"])
            if result["ok"] and result.get("address"):
                print(f"Unplug USB if you like, then: device connect {result['address']}")
            continue
        if cmd["type"] == "device_pair":
            result = device_manager.pair(cmd["address"], cmd["code"])
            print(result["message"])
            if result["ok"]:
                print("Paired. Now connect with the *connect* port shown on the phone:")
                print("  device connect <ip:connect_port>")
            continue
        if cmd["type"] == "device_list":
            devices = device_manager.discover_devices()
            if not devices:
                print("No devices detected.")
            for i, d in enumerate(devices, start=1):
                print(f"[{i}] {d.device_id} ({d.device_type}) model={d.model}")
            agent_pool.sync_from_devices(devices)
            continue
        if cmd["type"] == "agent_list":
            agents = agent_pool.list_agents()
            if not agents:
                print("No agents available.")
            for i, a in enumerate(agents, start=1):
                mark = "*" if a.device_id == agent_pool.selected or agent_pool.selected == "all" else " "
                print(f"{mark} [{i}] {a.device_id}")
            print(f"selected={agent_pool.selected}")
            continue
        if cmd["type"] == "agent_select":
            try:
                agent_pool.set_selected(cmd["selector"])
                print(f"selected={agent_pool.selected}")
            except Exception as exc:
                print(f"Error: {exc}")
            continue
        if cmd["type"] == "actions":
            print(agent_pool.execute_actions(cmd["actions"]))
            continue
        if cmd["type"] == "text_action":
            try:
                results = agent_pool.execute_text(cmd["text"], debug_config=debug_config, recorder=recorder)
                print(results)
                if debug_config.get("enabled"):
                    for tracer in agent_pool.get_last_tracers().values():
                        if tracer:
                            print(tracer.summary_line())
            except Exception as exc:
                logger.error("Text action failed: %s", str(exc))
                print(f"Error: {exc}")
            continue

        print("Unknown command. Type 'help'.")
