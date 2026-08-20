from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Action:
    action_type: str
    params: Dict[str, Any] = field(default_factory=dict)


# Action dicts exchanged between resolver/engine and executor:
#   {"type": "click",      "params": {"x": int, "y": int}}
#   {"type": "drag",       "params": {"x1": int, "y1": int, "x2": int, "y2": int, "duration_ms": int}}
#   {"type": "input_text", "params": {"text": str}}
#   {"type": "wait",       "params": {"duration_ms": int}}
#   {"type": "key",        "params": {"keycode": int}}      # adb input keyevent
#   {"type": "gesture",    "params": {"frames": [...]}}     # no-root multi-touch
#       or {"type": "gesture", "params": {"pinch": {"cx","cy","start_gap","end_gap"}}}
#       Multi-finger MotionEvent injection via the app_process dex helper
#       (action/backends/motionevent_backend.py); "frames" are display-pixel
#       pointer frames, each listing the full set of pointers down at that
#       instant with a "delay_ms" before it.
EXECUTOR_ACTION_TYPES = ("click", "drag", "input_text", "wait", "key", "gesture")

# Node-level action types in task JSON. "agent" suspends the task and hands the
# instruction back to the external agent (resume via run_task start_after);
# "llm" is a deprecated alias for "agent". "none" performs no action
# (recognition only). "click" supports target="recognized" to tap the
# recognition hit center. "custom" runs a deterministic in-process handler
# registered in task/custom_actions/ (complex multi-step logic that needs no
# agent: e.g. swipe a list until a text appears).
TASK_ACTION_TYPES = EXECUTOR_ACTION_TYPES + ("agent", "llm", "none", "custom")

# --- action-level repeat (params.*) ---
# Fire the same instantaneous action several times without going back through
# recognition: the engine replays the built action in a tight batch, optionally
# waiting for the screen to settle / a fixed delay between shots. Real-device
# background: the gap BETWEEN batches of adb taps is what swallows sub-second
# QTE windows, so the repetition has to happen inside one node execution.
REPEAT_PARAM_KEYS = ("repeat", "repeat_delay_ms", "repeat_wait_freezes_ms")

# Action types the engine repeats. Instantaneous executor actions only:
# repeating "wait" is just a longer sleep, "none"/"agent"/"llm" perform nothing
# here, and a "custom" action's params belong to its handler (which owns its own
# iteration knobs) -- the loader rejects repeat params on all of those instead of
# silently ignoring them.
REPEATABLE_ACTION_TYPES = ("click", "drag", "input_text", "key", "gesture")

# Task JSON format reference (also embedded into the task-authoring LLM prompt).
TASK_SCHEMA_DOC = """\
Task JSON format:
{
  "entry": "<entry node name>",
  "includes": ["common/popups.json"],          // optional: shared-node fragment files (common
                                               // popup handling, login, back-to-home). Paths are
                                               // RELATIVE to the task directory: an absolute path
                                               // or a ".." that escapes task_definitions/ is a
                                               // load error. Their nodes merge into this task's
                                               // node table, so next/on_timeout may point across
                                               // files in either direction. See the include file
                                               // format at the bottom of this document.
  "on_conflict": "strict",                     // optional: duplicate node names across files.
                                               // "strict" (default) = load error listing EVERY
                                               // colliding name with both source files, nothing
                                               // is merged (no silent override); "overwrite" =
                                               // later file wins (task file merges last), used
                                               // deliberately to specialize a shared node.
  "on_finding": "<recovery node>",             // optional: when a bug is REPORTED (a watchdog
                                               // hit or a logcat crash/ANR), record it then jump
                                               // here and keep testing instead of aborting. Only
                                               // a fresh finding triggers it; a bare recognition
                                               // timeout (a stall, no bug) never skips — that
                                               // stays on_timeout. A watchdog's own skip_to wins
                                               // over this; a fail_task watchdog still aborts.
  "max_steps": 50,                             // optional (default: engine config, then 50): raise
                                               // for a long translated flow whose node count
                                               // inflated past the default step budget — declare it
                                               // here instead of padding action nodes with extra
                                               // assertions to dodge the cap.
  "back_fallback": true,                       // optional (default: engine config, on): when a
                                               // stall survives the `popups` sweep, an UNKNOWN
                                               // popup/overlay is blocking the screen — the engine
                                               // records an `unknown_popup_backoff` finding pinned
                                               // to that frame, presses BACK ONCE, and only if the
                                               // screen visibly changed spends one more recognition
                                               // round. Priority: an authored `on_timeout` on any
                                               // stalled candidate WINS — the fallback is skipped
                                               // entirely and the recovery branch runs untouched.
                                               // BACK only covers dead ends that would otherwise
                                               // fail the run. It never jumps (not bug-skip).
                                               // Set false for flows where BACK would leave the
                                               // screen under test (e.g. a battle you must not exit).
  "defaults": {                                // optional: node-level defaults applied to EVERY
                                               // node (including nodes pulled in via "includes")
                                               // when the node does not set the field itself.
                                               // Priority: node field > defaults > engine default.
                                               // Expanded at LOAD time, so the engine sees ordinary
                                               // nodes; the saved file keeps the compact form.
                                               // Setting a field to null IN A NODE opts that node
                                               // out of the default (the engine default applies).
                                               // Allowed keys (anything else is a load error, so a
                                               // typo fails loudly): timeout_ms, poll_interval_ms,
                                               // post_delay_ms, wait_still — generic per-node
                                               // tuning knobs only. Control flow (recognition /
                                               // action / next / on_timeout / finding) is
                                               // deliberately NOT defaultable.
    "timeout_ms": 15000,
    "poll_interval_ms": 500,
    "post_delay_ms": 300,
    "wait_still": {"timeout_ms": 3000}
  },
  "popups": [                                  // optional: whitelist of KNOWN-benign popups
    {                                          // (user agreements, in-game warnings) — expected
                                               // noise, NOT bugs. When recognition stalls, these
                                               // are detected + dismissed automatically WITHOUT
                                               // recording a finding, then the poll retries. The
                                               // sweep runs only on a stall (no per-step capture
                                               // cost). Anything NOT whitelisted still surfaces as
                                               // a timeout/watchdog finding. Dismissed names are
                                               // returned in result["popups_dismissed"], and the
                                               // frame each dismissal was decided on is saved as
                                               // popup_NN_<name>.png in the run folder and linked
                                               // from the "popup_dismissed" timeline event (with
                                               // score + clicked center) — context, not a finding.
      "name": "user_agreement",                // optional label (logging / result)
      "recognition": {                         // how to detect it (same gate as a watchdog;
        "type": "ui_text" | "ocr" | "template" | "feature" | "blank_screen" | "yolo"
              | "scene" | "and" | "or",        // and/or: see "combined recognition" below
        "expected": "<text>",                  // required for ui_text/ocr/scene
        "roi": [x1, y1, x2, y2]                // optional
      },
      "confirm": {                             // optional SECOND gate, same schema as
                                               // "recognition" (any type, combinations
                                               // included), evaluated on the SAME frame right
                                               // after recognition matches. Miss => the popup
                                               // counts as absent: nothing is clicked, nothing
                                               // is counted dismissed, the stall falls through
                                               // to the BACK fallback and gets REPORTED.
                                               // Use it whenever the trigger anchor is not
                                               // globally unique — a shared close-X template
                                               // once matched the panel under test and the
                                               // sweep closed it (2026-08-11), yielding two
                                               // misleading findings. Rather miss a sweep than
                                               // click the wrong thing.
        "type": "ocr", "expected": "系统提示", "roi": [x1, y1, x2, y2]
      },
      "action": {                              // how to dismiss it (executor action only:
        "type": "click" | "key" | "gesture",   // click | key | gesture — no agent/custom/none)
        "target": "recognized",                // click: tap the detected element (e.g. the
                                               // "同意" button you matched), or give params x/y
        "params": {"keycode": 4}               // key: e.g. BACK to close
      }
    }
  ],
  "watchdogs": [                               // optional: negative assertions checked after
    {                                          // every step and on recognition timeouts
      "type": "ui_text" | "ocr" | "blank_screen" | "template" | "feature" | "yolo"
              | "scene" | "and" | "or",        // and/or: see "combined recognition" below
      "expected": "<forbidden text>",          // required for ui_text/ocr/scene
      "template": "<icon name>",               // required for template/feature (file in
                                               // task/templates)
      "min_matches": 4,                        // feature: ORB keypoint matches required
      "ratio": 0.75,                           // feature: Lowe ratio (0, 1]
      "label": "<class name>",                 // yolo: optional class filter (any object if omitted)
      "threshold": 0.65,                       // ui_text/ocr: similarity gate;
                                               // blank_screen: max grayscale stddev (default 8.0);
                                               // template: correlation gate (default 0.8)
      "conf": 0.25,                            // yolo: detection confidence gate (default 0.25)
      "roi": [x1, y1, x2, y2],                 // optional, ocr/template/yolo only
      "severity": "warning"|"error"|"critical",// finding severity (default "error")
      "message": "<finding message>",          // optional human-readable description
      "skip_to": "<recovery node>",            // optional: on hit, record the finding then jump
                                               // here and keep testing (recover past the bug).
                                               // Overrides on_finding and supersedes fail_task.
      "fail_task": false                       // true = abort the task on hit (unless skip_to /
                                               // on_finding routes a recovery instead)
    }
  ],
  "nodes": {
    "<node name>": {
      "recognition": {
        "type": "always" | "ui_text" | "ocr" | "blank_screen" | "template" | "feature"
              | "yolo" | "scene" | "and" | "or", // and/or: combined recognition, see below
        "all_of": [ {<recognition>}, ... ],      // "and" only: EVERY sub-recognition must hit
        "any_of": [ {<recognition>}, ... ],      // "or" only: tried in order, FIRST hit wins
        "box_index": 0,                          // "and" only (default 0): which sub-recognition's
                                                 // hit box becomes this node's hit, i.e. what
                                                 // "target": "recognized" clicks. "or" uses the
                                                 // box of whichever sub-recognition hit.
        "expected": "<text to match>",          // required for ui_text/ocr; for "scene" it is a
                                                 // scene label matched by dotted PREFIX ("popup"
                                                 // accepts "popup.error"; "menu" accepts
                                                 // "menu.settings"). The scene channel classifies
                                                 // WHICH SCREEN this is; the label set comes from
                                                 // the scene probes the integrating project
                                                 // registers (the framework itself ships only
                                                 // "blank") — see the classify_scene MCP tool for
                                                 // the labels currently available. It returns no
                                                 // click anchor, and "unknown" never matches — an
                                                 // unrecognized screen falls through to on_timeout
                                                 // instead of being guessed.
        "template": "<icon name>",               // required for template/feature: image file (stem)
                                                 // under task/templates/, matched by OpenCV. Use for
                                                 // game-surface graphics (buildings/icons) that
                                                 // ui_text/ocr can't read. Capture one with the
                                                 // capture_template MCP tool.
                                                 // MASK: a PNG's alpha channel is the match mask —
                                                 // erase the volatile parts of the crop (counters,
                                                 // avatars, level numbers) to transparent and only
                                                 // the stable art is compared/described.
        "min_matches": 4,                        // feature only: ORB keypoint correspondences that
                                                 // must survive the ratio test (default 4). The
                                                 // feature channel tolerates re-skins/rescaling that
                                                 // break pixel correlation, but needs a TEXTURE-RICH
                                                 // anchor (logos, illustrated banners, detailed
                                                 // buildings). Flat/solid-color icons grow no
                                                 // keypoints — keep those on "template".
        "ratio": 0.75,                           // feature only: Lowe nearest-neighbour ratio in
                                                 // (0, 1]; lower = stricter matches
        "label": "<class name>",                 // yolo: optional class filter; a trained YOLO
                                                 // model (task/models/yolo.onnx) detects/classifies
                                                 // buildings/units, robust to scale/occlusion where
                                                 // template matching fails. Omit to match any object.
        "threshold": 0.65,                       // optional similarity gate (blank_screen:
                                                 // max grayscale stddev, default 8.0;
                                                 // template: correlation gate, default 0.8)
        "conf": 0.25,                            // yolo only: detection confidence gate (default 0.25)
        "min_conf": 0.5,                         // scene only: classifier confidence gate (default 0.5)
        "roi": [x1, y1, x2, y2],                 // optional, ocr/template/feature/yolo only, pixels
        "scales": [0.9, 1.0, 1.1],               // template only: template sizes to sweep (default
                                                 // [1.0]); matchTemplate is NOT scale-invariant, so
                                                 // a template captured on another resolution/UI
                                                 // scale only hits with a sweep. Best scale wins and
                                                 // is returned in the hit.
        "grayscale": false                       // template only: match on luminance
      },
      "action": {
        "type": "click" | "drag" | "input_text" | "wait" | "key" | "gesture" | "agent" | "none" | "custom",
        "target": "recognized",                  // click at recognition center
        "params": {                              // executor params (x/y, duration_ms, text,
                                                 // keycode, frames…); may ALSO carry the REPEAT
                                                 // knobs below on click/drag/input_text/key/gesture:
          "repeat": 8,                           // fire the SAME action N times inside this node,
                                                 // without re-running recognition (default 1). Use
                                                 // for QTE mashing / rapid taps: the gap between
                                                 // separate node executions is what swallows
                                                 // sub-second windows. Rejected on
                                                 // wait/agent/none/custom (custom handlers own
                                                 // their own iteration params).
          "repeat_delay_ms": 0,                  // fixed pause between two shots (default 0 = as
                                                 // tight as adb allows)
          "repeat_wait_freezes_ms": 0            // >0: between two shots wait until the screen
                                                 // STOPS MOVING (same still-frame check as
                                                 // wait_still), giving up after this many ms —
                                                 // "tap, let the UI answer, tap again". Timing out
                                                 // is NOT a failure and records no finding.
        },                                       // Order: action → [freeze → delay → action] ×
                                                 // (repeat-1). A failed shot does NOT abort the
                                                 // remaining ones; the LAST shot's result decides
                                                 // whether the node failed. Watchdog sampling is
                                                 // unchanged: shot ① is taken after the whole
                                                 // repeat batch, not per shot.
        "text": "<instruction for the agent>",   // agent only: task suspends, returns
                                                 // status=agent_required + this text; the
                                                 // agent performs the step with device tools
                                                 // then resumes via run_task(start_after=<node>)
        "name": "<custom action name>"           // custom only: deterministic in-process
                                                 // handler from task/custom_actions/ (no LLM);
                                                 // "params" is passed to the handler.
                                                 // Built-in: swipe_until {recognition, swipe:
                                                 // {x1,y1,x2,y2}, max_swipes, settle_ms}
      },
      "next": ["<candidate node>", ...],         // first whose recognition hits wins; [] = task done
      "on_timeout": "<recovery node>",           // optional jump when recognition times out
      "finding": "<message>",                    // optional: entering this node reports a QA
                                                 // finding (popup/abnormal branches); or
                                                 // {"severity": "warning", "message": "..."}
      "timeout_ms": 10000,                       // recognition polling budget
      "poll_interval_ms": 1000,
      "post_delay_ms": 500,                      // sleep after the action (fixed, known-length pause)
      "wait_still": {                            // optional: after the action (and post_delay_ms),
                                                 // wait until the screen STOPS MOVING before polling
                                                 // `next` — for cutscenes / loading of unpredictable
                                                 // length, instead of guessing a big post_delay_ms.
        "timeout_ms": 5000,                      // give up waiting after this (default 5000); a
                                                 // timeout is NOT a failure and records no finding —
                                                 // it just stops waiting and lets recognition judge
        "interval_ms": 200,                      // sampling period (default 200)
        "threshold": 0.01                        // still = fewer than this fraction of pixels changed
                                                 // between two consecutive frames (default 0.01)
      },                                         // rounds spent land in node_stats.wait_still_rounds
      "step": "2.1"                              // optional, display-only: execution-order
                                                 // label (spine = 1,2,3…; dotted = fallback
                                                 // branch). Engine ignores it. Written by the
                                                 // "task renumber" CLI / step_numbering; get_task
                                                 // also returns computed labels in "_steps".
    }
  }
}

Combined recognition ("and" / "or"): for an anchor no single channel pins down
safely — an icon that also appears on other screens, a label that only means
this screen next to that icon. Sub-recognitions are ordinary recognition
objects; "always" is rejected inside them (it would stop the combination from
gating), and nesting and/or is capped at 2 levels. One evaluation reads ONE
frame shared by every sub-recognition, so they cannot vote on different moments
of an animating screen. Usable in nodes, watchdogs and popups.
COST: the shared frame saves the SCREENSHOT only — it does NOT save the
uiautomator dump. Each ui_text sub-recognition still fires its own dump
(~4.33s on real devices), so a combination's dump cost grows linearly with the
number of ui_text subs. Avoid stacking 2+ ui_text subs; prefer ocr+roi (which
reads the shared frame) for the extra conditions.
{
  "recognition": {
    "type": "and",
    "all_of": [
      {"type": "template", "template": "shop_icon", "threshold": 0.85},
      {"type": "ocr", "expected": "商店", "roi": [0, 0, 1080, 300]}
    ],
    "box_index": 0                             // click the icon, not the label
  },
  "action": {"type": "click", "target": "recognized"}
}

Include file format (referenced via "includes"; one level only, no entry/includes):
{
  "description": "<what this fragment covers>",  // optional
  "nodes": { "<shared node name>": { ... same node format ... } }
}
A fragment is NOT a runnable task: "nodes" (+ optional "description" and any
"_"-prefixed comment) are the only allowed keys. A task-level field
(entry / watchdogs / popups / on_finding / defaults / max_steps / ...) inside a
fragment is a load error, so a fragment can never be mistaken for a whole task —
those belong in the task that includes it.

Cross-file next/on_timeout references resolve against the merged node table;
validation runs on the merged result, so a task only loads if every reference
resolves (atomic — a rejected load leaves no half-merged task). Tasks are SAVED
with "includes" intact (fragment nodes are never inlined into the task file), so
editing a fragment updates every task that includes it. get_task returns the
merged view plus "_merge": {"includes": [resolved paths], "conflicts": [names],
"include_map": {node: source file}} — use include_map to tell which file a node
came from ("<task>" = the task file itself). Lint W006 flags an include whose
nodes are all unreachable from entry.

QA findings: a run reports anomalies in result["findings"] (list) and
result["report"] (summary with report.json path) even when it completes —
on_timeout recoveries taken, nodes marked "finding", watchdog hits, logcat
crash/ANR events, and task failures, each with screenshot (and on failure
ui-dump) evidence saved under outputs/findings/. Every finding also carries
flight-recorder context for the last minute before the problem: inline
"log_excerpt" (in-game logcat fragment) and "recent_flow" (the steps the
engine saw/did), plus evidence files "logcat" (full fragment), "timeline"
(flow json) and "video" (real MP4 of the recent window via rolling
on-device screenrecord; falls back to "history" frame snapshots when the
ROM forbids recording). Treat every finding as a
potential game bug to surface to the user — relay message, log excerpt and
evidence paths, not just flow noise.

Node health: a run also returns result["node_stats"] (also a top-level
report.json field) counting per node how it was reached — direct_hits /
popup_assisted_hits / back_assisted_hits / recovery_hits — plus
timeout_recoveries, anchor drift_count/drift_px, poll_rounds and
wait_still_rounds. It is
observation only and never changes the flow; a node that repeatedly needed its
timeout escape or whose anchor kept moving also yields one
"anchor_rot_suspect" warning finding per run, meaning the task's anchor is
going stale (fix the task) rather than the game being broken.
"""
