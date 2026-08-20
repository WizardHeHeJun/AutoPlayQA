from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Dict, List

from action.action_schema import REPEAT_PARAM_KEYS, REPEATABLE_ACTION_TYPES, TASK_ACTION_TYPES
from task.custom_actions import get_handler, registered_names
from task.findings import SEVERITIES
from task.recognizers import (
    COMBO_LIST_KEY,
    COMBO_SUB_TYPES,
    COMBO_TYPES,
    MAX_COMBO_DEPTH,
    RECOGNITION_TYPES,
    WATCHDOG_TYPES,
)

DEFAULT_TASK_DIR = Path("task") / "task_definitions"

#: Suite definitions ({name, cases, on_case_failure, ...}) live one level below
#: the tasks they reference; see suite_runner for what the fields mean.
DEFAULT_SUITE_DIR = DEFAULT_TASK_DIR / "suites"

_INT_FIELDS = ("timeout_ms", "poll_interval_ms", "post_delay_ms")

#: Node fields a task-level `defaults` block may pre-set (first-pass whitelist).
#: Deliberately only the generic per-node tuning knobs that already exist in the
#: node schema: control flow (recognition / action / next / on_timeout /
#: finding) is what makes a node that node, and defaulting it would hide the
#: flow. Anything outside this list is a load error, so a typo fails loudly
#: instead of silently doing nothing.
TASK_DEFAULT_KEYS = ("timeout_ms", "poll_interval_ms", "post_delay_ms", "wait_still")

CONFLICT_STRATEGIES = ("strict", "overwrite")

#: Top-level keys an include file (a shared-node fragment) may carry. A fragment
#: is NOT a runnable task: it contributes nodes and nothing else. Task-level
#: fields (entry / watchdogs / popups / on_finding / defaults / ...) are a load
#: error rather than being silently dropped, so a fragment can never be mistaken
#: for a complete task (and a task can never be included as if it were one).
#: `_`-prefixed keys pass through as free-form metadata (`_comment`), matching
#: the convention the loader already uses for computed keys like `_merge`.
INCLUDE_FILE_KEYS = ("nodes", "description")

#: Label used for the main task file in conflict reports and `_merge.include_map`.
MAIN_FILE_LABEL = "<task>"

#: Recovery policies a suite may declare for a case that ends badly.
SUITE_FAILURE_POLICIES = ("restart_retry", "restart_continue", "abort")


class TaskValidationError(Exception):
    pass


class SuiteValidationError(Exception):
    """A suite definition is malformed or references a case that isn't there."""


def list_tasks(task_dir: Path | str = DEFAULT_TASK_DIR) -> List[str]:
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        return []
    return sorted(p.stem for p in task_dir.glob("*.json"))


def get_task_path(name: str, task_dir: Path | str = DEFAULT_TASK_DIR) -> Path:
    return Path(task_dir) / f"{name}.json"


def list_suites(suite_dir: Path | str = DEFAULT_SUITE_DIR) -> List[str]:
    suite_dir = Path(suite_dir)
    if not suite_dir.is_dir():
        return []
    return sorted(p.stem for p in suite_dir.glob("*.json"))


def get_suite_path(name: str, suite_dir: Path | str = DEFAULT_SUITE_DIR) -> Path:
    return Path(suite_dir) / f"{name}.json"


def load_suite(name: str, suite_dir: Path | str = DEFAULT_SUITE_DIR,
               task_dir: Path | str = DEFAULT_TASK_DIR) -> Dict:
    """Load and validate a suite definition (a list of case task names).

    Deliberately light: it checks the shape and that every referenced case file
    exists, so a typo fails in a second instead of half an hour into a run.
    `resume_after`, `case_entry` and `landing` are required fields — there is no
    app-neutral default for a boot-skip node name or a landing-screen anchor, so
    every suite must declare its own (`landing` may be explicitly `null` to
    disable the between-case check). Whether a case can actually be resumed
    mid-flow is the runner's pre-flight (it has to load the tasks anyway).
    """
    path = get_suite_path(name, suite_dir)
    if not path.is_file():
        raise SuiteValidationError(f"Suite file not found: {path}")
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SuiteValidationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(suite, dict):
        raise SuiteValidationError(f"Suite root must be a JSON object: {path}")
    suite.setdefault("name", name)
    validate_suite(suite, task_dir)
    return suite


def validate_suite(suite: Dict, task_dir: Path | str = DEFAULT_TASK_DIR) -> None:
    """Raise SuiteValidationError describing the first problem found."""
    if not isinstance(suite, dict):
        raise SuiteValidationError("Suite root must be a JSON object")

    name = suite.get("name")
    if not isinstance(name, str) or not name:
        raise SuiteValidationError("Suite requires a non-empty string 'name'")

    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SuiteValidationError(f"Suite '{name}' requires a non-empty 'cases' list")
    if not all(isinstance(c, str) and c for c in cases):
        raise SuiteValidationError(f"Suite '{name}' 'cases' must be task-name strings")

    missing = [c for c in cases if not get_task_path(c, task_dir).is_file()]
    if missing:
        raise SuiteValidationError(
            f"Suite '{name}' references unknown case(s): {', '.join(missing)} "
            f"(no such task under {Path(task_dir)})"
        )

    policy = suite.get("on_case_failure")
    if policy is not None and policy not in SUITE_FAILURE_POLICIES:
        raise SuiteValidationError(
            f"Suite '{name}' 'on_case_failure' must be one of {SUITE_FAILURE_POLICIES}, got '{policy}'"
        )

    max_retries = suite.get("max_retries")
    if max_retries is not None and (
        not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0
    ):
        raise SuiteValidationError(f"Suite '{name}' 'max_retries' must be a non-negative integer")

    for field in ("resume_after", "case_entry"):
        if field not in suite:
            raise SuiteValidationError(
                f"Suite '{name}' requires a '{field}' field: there is no app-neutral "
                f"default boot-skip node name, so every suite must declare its own"
            )
        value = suite[field]
        if not isinstance(value, str) or not value:
            raise SuiteValidationError(f"Suite '{name}' '{field}' must be a non-empty node name")

    if "landing" not in suite:
        raise SuiteValidationError(
            f"Suite '{name}' requires a 'landing' field: a recognition object for "
            f"the between-case landing check, or null to explicitly disable it — "
            f"there is no app-neutral default landing-screen anchor"
        )
    landing = suite["landing"]
    if landing not in (None, {}) and not isinstance(landing, dict):
        raise SuiteValidationError(
            f"Suite '{name}' 'landing' must be a recognition object (or null to disable)"
        )

    # Cases that must cold-start even mid-suite (their setup lives in the boot
    # chain, e.g. GM debug mode armed on the login page).
    full_boot = suite.get("full_boot_cases")
    if full_boot is not None:
        if not isinstance(full_boot, list) or not all(isinstance(c, str) and c for c in full_boot):
            raise SuiteValidationError(f"Suite '{name}' 'full_boot_cases' must be a list of case names")
        unknown = [c for c in full_boot if c not in cases]
        if unknown:
            raise SuiteValidationError(
                f"Suite '{name}' 'full_boot_cases' lists case(s) not in 'cases': {', '.join(unknown)}"
            )


def load_task(path: Path | str) -> Dict:
    path = Path(path)
    if not path.is_file():
        raise TaskValidationError(f"Task file not found: {path}")
    task = _read_json(path)
    return resolve_task(task, path.parent)


def resolve_task(task: Dict, base_dir: Path | str) -> Dict:
    """Resolve `includes`, merge node tables, and validate the merged task.

    Include files contribute shared nodes (popup handlers etc.) reusable across
    tasks; the merged reference graph is validated as a whole, so nothing runs
    unless every file parses and every next/on_timeout reference resolves
    (atomic loading). Tasks without includes are validated and returned as-is.

    The merged task has `includes`/`on_conflict` stripped and a `_merge` key
    ({"includes": [paths], "conflicts": [node names], "include_map": {node:
    source}}) describing what was merged; the engine ignores it. `include_map`
    is how a caller (get_task, task_lint) tells a node's origin apart without
    stamping a private field into the node objects themselves — that would break
    node schema validation and leak into whatever the author saves next.

    An optional task-level `defaults` block is expanded here too (see
    `_apply_defaults`), so the engine only ever sees fully-populated nodes.
    """
    if not isinstance(task, dict):
        raise TaskValidationError("Task root must be a JSON object")

    includes = task.get("includes")
    if not includes:
        task = _apply_defaults(task)
        validate_task(task)
        return task

    if not isinstance(includes, list) or not all(isinstance(i, str) and i for i in includes):
        raise TaskValidationError("'includes' must be a list of non-empty relative path strings")
    on_conflict = task.get("on_conflict", "strict")
    if on_conflict not in CONFLICT_STRATEGIES:
        raise TaskValidationError(
            f"'on_conflict' must be one of {CONFLICT_STRATEGIES}, got '{on_conflict}'"
        )

    base_dir = Path(base_dir).resolve()
    merged_nodes: Dict = {}
    conflicts: List[str] = []
    origins: Dict[str, str] = {}
    collisions: List[str] = []
    resolved_files: List[str] = []
    seen: set = set()
    for ref in includes:
        inc_path = _resolve_include_path(ref, base_dir)
        if inc_path in seen:
            raise TaskValidationError(f"Duplicate include: {ref}")
        seen.add(inc_path)
        _merge_nodes(
            merged_nodes, _read_include_nodes(inc_path), on_conflict,
            conflicts, ref, origins, collisions,
        )
        resolved_files.append(str(inc_path))

    # Main-file nodes merge last so under "overwrite" a task can specialize a
    # shared node. A task may define no nodes of its own and just wire entry
    # into an included flow.
    main_nodes = task.get("nodes", {})
    if not isinstance(main_nodes, dict):
        raise TaskValidationError("Task 'nodes' must be an object")
    _merge_nodes(
        merged_nodes, main_nodes, on_conflict, conflicts,
        MAIN_FILE_LABEL, origins, collisions,
    )

    # Strict mode reports EVERY colliding name at once (with both source files)
    # instead of failing on the first one: renaming nodes one error-per-run is
    # exactly the loop that makes people reach for "overwrite" and shadow a
    # shared node by accident. Raised after the whole merge, before anything is
    # returned, so a rejected load leaves no half-merged task behind.
    if collisions:
        raise TaskValidationError(
            "Node name conflict(s) across merged files (nothing was merged):\n"
            + "\n".join(f"  - {line}" for line in collisions)
            + '\nRename the node(s), or set "on_conflict": "overwrite" to let the '
              "later file win (task file merges last)."
        )

    merged = {k: v for k, v in task.items() if k not in ("includes", "on_conflict", "nodes")}
    merged["nodes"] = merged_nodes
    merged["_merge"] = {
        "includes": resolved_files,
        "conflicts": conflicts,
        "include_map": origins,
    }
    # The main file's defaults cover included nodes too: shared popup handlers
    # should follow the timing of the task that pulled them in.
    merged = _apply_defaults(merged)
    validate_task(merged)
    return merged


def _apply_defaults(task: Dict) -> Dict:
    """Expand the optional task-level `defaults` block into every node.

    Priority: a node's own field > `defaults` > the engine's built-in default.
    Load-time expansion keeps the engine free of a second lookup path (it reads
    ordinary node fields as before) and keeps the *saved* file compact — the
    input dict is never mutated, so `save_task` still writes the `defaults`
    form the author wrote.

    A node that spells the field out as `null` is not "missing" it: that is how
    a node opts OUT of a default and falls back to the engine's own value.
    """
    defaults = task.get("defaults")
    if defaults is None:
        return task
    _validate_defaults(defaults)
    nodes = task.get("nodes")
    if not isinstance(nodes, dict):
        return task  # malformed nodes: let validate_task report the real problem

    expanded: Dict = {}
    for name, node in nodes.items():
        if not isinstance(node, dict):
            expanded[name] = node
            continue
        filled = dict(node)
        for key, value in defaults.items():
            if key not in filled:
                # deepcopy: a mutable default (wait_still) must not be shared
                # between nodes.
                filled[key] = copy.deepcopy(value)
        # A node that spells a whitelist field out as `null` is opting OUT of
        # the default: drop the key entirely so the engine's own
        # `.get(field, DEFAULT)` reaches its built-in default. Leaving a literal
        # None behind would make `.get()` return None (not the default), and the
        # engine's `timeout_ms / 1000` arithmetic would then raise TypeError —
        # crashing OUTSIDE `_finish`, leaking the run's recorders. This honours
        # the documented "node null = engine default" contract.
        for key in TASK_DEFAULT_KEYS:
            if key in filled and filled[key] is None:
                del filled[key]
        expanded[name] = filled

    out = dict(task)
    out["nodes"] = expanded
    return out


def _validate_defaults(defaults) -> None:
    """Validate the task-level `defaults` block (shape + whitelist)."""
    if defaults is None:
        return
    if not isinstance(defaults, dict):
        raise TaskValidationError("Task 'defaults' must be an object")
    unknown = [k for k in defaults if k not in TASK_DEFAULT_KEYS]
    if unknown:
        raise TaskValidationError(
            f"Task 'defaults' has unsupported key(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(TASK_DEFAULT_KEYS)})"
        )
    for field in _INT_FIELDS:
        value = defaults.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise TaskValidationError(
                f"Task 'defaults' field '{field}' must be a non-negative integer"
            )
    _validate_wait_still("Task 'defaults'", defaults.get("wait_still"))


def _read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaskValidationError(f"Invalid JSON in {path}: {exc}") from exc


def _resolve_include_path(ref: str, base_dir: Path) -> Path:
    if Path(ref).is_absolute():
        raise TaskValidationError(f"Include path must be relative to the task directory: {ref}")
    resolved = (base_dir / ref).resolve()
    if not resolved.is_relative_to(base_dir):
        raise TaskValidationError(f"Include path escapes the task directory: {ref}")
    if not resolved.is_file():
        raise TaskValidationError(f"Include file not found: {resolved}")
    return resolved


def _read_include_nodes(path: Path) -> Dict:
    """Read one include file and return its `nodes` table.

    A fragment carries shared nodes and nothing else: task-level fields are
    rejected here rather than silently ignored, so a fragment can never be run
    (or half-run) as if it were a complete task.
    """
    data = _read_json(path)
    if not isinstance(data, dict):
        raise TaskValidationError(f"Include file must be a JSON object: {path}")
    if "entry" in data or "includes" in data:
        raise TaskValidationError(
            f"Include file must not define 'entry' or 'includes' (includes are single-level): {path}"
        )
    task_level = sorted(
        k for k in data if k not in INCLUDE_FILE_KEYS and not str(k).startswith("_")
    )
    if task_level:
        raise TaskValidationError(
            f"Include file carries task-level field(s) {', '.join(task_level)}: {path} "
            f"(a shared fragment holds only {'/'.join(INCLUDE_FILE_KEYS)} — declare "
            f"watchdogs/popups/defaults and friends in the task that includes it)"
        )
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise TaskValidationError(f"Include file 'description' must be a string: {path}")
    nodes = data.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise TaskValidationError(f"Include file requires a non-empty 'nodes' object: {path}")
    return nodes


def _merge_nodes(
    target: Dict, source: Dict, on_conflict: str, conflicts: List[str],
    source_label: str, origins: Dict[str, str], collisions: List[str],
) -> None:
    """Merge one file's nodes into `target`, tracking origin and collisions.

    Under "strict" a collision is recorded (first definition kept) and merging
    continues, so the caller can report every conflict in one error; under
    "overwrite" the later file wins and the name is listed in `conflicts`.
    """
    for name, node in source.items():
        if name in target:
            if on_conflict == "strict":
                collisions.append(
                    f"conflict '{name}': first defined in {origins.get(name, '?')}, "
                    f"redefined by {source_label}"
                )
                continue
            conflicts.append(name)
        target[name] = node
        origins[name] = source_label


def validate_task(task: Dict) -> None:
    """Raise TaskValidationError describing the first structural problem found.

    Expects a resolved task: cross-file references only validate after
    resolve_task/load_task has merged the include files.
    """
    if not isinstance(task, dict):
        raise TaskValidationError("Task root must be a JSON object")
    if task.get("includes"):
        raise TaskValidationError(
            "Task has unresolved 'includes'; validate via resolve_task/load_task"
        )

    entry = task.get("entry")
    nodes = task.get("nodes")
    if not isinstance(entry, str) or not entry:
        raise TaskValidationError("Task requires a non-empty string 'entry'")
    if not isinstance(nodes, dict) or not nodes:
        raise TaskValidationError("Task requires a non-empty object 'nodes'")
    if entry not in nodes:
        raise TaskValidationError(f"Entry node '{entry}' not defined in nodes")

    # Node-level defaults are normally expanded by resolve_task/load_task before
    # we get here; validating the block again keeps direct validate_task callers
    # (task_editor) from saving a task with a typo'd default key.
    _validate_defaults(task.get("defaults"))

    _validate_watchdogs(task.get("watchdogs"), nodes)
    _validate_popups(task.get("popups"))

    on_finding = task.get("on_finding")
    if on_finding is not None and on_finding not in nodes:
        raise TaskValidationError(f"Task 'on_finding' references unknown node '{on_finding}'")

    # Opt-out for the engine's unknown-popup BACK fallback (config default is on).
    back_fallback = task.get("back_fallback")
    if back_fallback is not None and not isinstance(back_fallback, bool):
        raise TaskValidationError("Task 'back_fallback' must be a boolean")

    # Per-task step budget override (default: engine_config, then 50). Declare
    # this on long translated flows whose node count outgrew the shared cap,
    # instead of padding action nodes with extra assertions to dodge it.
    max_steps = task.get("max_steps")
    if max_steps is not None and (
        not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps <= 0
    ):
        raise TaskValidationError("Task 'max_steps' must be a positive integer")

    for name, node in nodes.items():
        _validate_node(name, node, nodes)


def _validate_watchdogs(watchdogs, nodes=None) -> None:
    if watchdogs is None:
        return
    if not isinstance(watchdogs, list):
        raise TaskValidationError("'watchdogs' must be a list of watchdog objects")
    for i, watchdog in enumerate(watchdogs):
        label = f"Watchdog #{i + 1}"
        if not isinstance(watchdog, dict):
            raise TaskValidationError(f"{label} must be an object")
        wtype = watchdog.get("type")
        if wtype not in WATCHDOG_TYPES:
            raise TaskValidationError(
                f"{label} has unsupported type '{wtype}' (allowed: {WATCHDOG_TYPES})"
            )
        if wtype in ("ui_text", "ocr", "scene"):
            expected = watchdog.get("expected")
            if not isinstance(expected, str) or not expected:
                raise TaskValidationError(f"{label} ({wtype}) requires non-empty 'expected'")
        if wtype in ("template", "feature"):
            template = watchdog.get("template")
            if not isinstance(template, str) or not template:
                raise TaskValidationError(f"{label} ({wtype}) requires non-empty 'template'")
        if wtype == "feature":
            _validate_feature_params(watchdog, label)
        if wtype in COMBO_TYPES:
            _validate_combo(watchdog, label)
        if wtype == "yolo":
            for key in ("label", "model"):
                if key in watchdog and (not isinstance(watchdog[key], str) or not watchdog[key]):
                    raise TaskValidationError(
                        f"{label} (yolo) '{key}' must be a non-empty string")
        severity = watchdog.get("severity")
        if severity is not None and severity not in SEVERITIES:
            raise TaskValidationError(
                f"{label} has invalid severity '{severity}' (allowed: {SEVERITIES})"
            )
        if "fail_task" in watchdog and not isinstance(watchdog["fail_task"], bool):
            raise TaskValidationError(f"{label} 'fail_task' must be a boolean")
        skip_to = watchdog.get("skip_to")
        if skip_to is not None:
            if not isinstance(skip_to, str) or not skip_to:
                raise TaskValidationError(f"{label} 'skip_to' must be a non-empty node name")
            if nodes is not None and skip_to not in nodes:
                raise TaskValidationError(f"{label} references unknown skip_to node '{skip_to}'")
        roi = watchdog.get("roi")
        if roi is not None and (not isinstance(roi, list) or len(roi) != 4):
            raise TaskValidationError(f"{label} roi must be [x1, y1, x2, y2]")


def _validate_feature_params(spec: Dict, label: str) -> None:
    """Shared checks for the `feature` (ORB) channel's tuning knobs.

    min_matches is the real gate (how many keypoint correspondences must
    survive); ratio is Lowe's nearest-neighbour distance ratio, meaningless
    outside (0, 1].
    """
    min_matches = spec.get("min_matches")
    if min_matches is not None and (
        not isinstance(min_matches, int) or isinstance(min_matches, bool) or min_matches < 1
    ):
        raise TaskValidationError(f"{label} 'min_matches' must be a positive integer")
    ratio = spec.get("ratio")
    if ratio is not None:
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise TaskValidationError(f"{label} 'ratio' must be a number in (0, 1]")
        if not 0 < float(ratio) <= 1:
            raise TaskValidationError(f"{label} 'ratio' must be a number in (0, 1]")


def _validate_combo(spec: Dict, label: str, depth: int = 1) -> None:
    """Validate an `and` / `or` combined recognition (see RecognizerHub).

    `depth` counts how many combinations enclose this one, itself included, so
    the guard rejects the third nesting level: a combination of combinations is
    still readable, anything deeper belongs in separate nodes.
    """
    rec_type = spec.get("type")
    list_key = COMBO_LIST_KEY[rec_type]
    if depth > MAX_COMBO_DEPTH:
        raise TaskValidationError(
            f"{label} nests combined recognitions deeper than {MAX_COMBO_DEPTH} levels; "
            f"split the extra conditions into their own node"
        )
    subs = spec.get(list_key)
    if not isinstance(subs, list) or not subs:
        raise TaskValidationError(
            f"{label} recognition '{rec_type}' requires a non-empty '{list_key}' list"
        )
    for index, sub in enumerate(subs):
        _validate_sub_recognition(sub, f"{label} {list_key}[{index}]", depth + 1)

    box_index = spec.get("box_index")
    if rec_type == "and":
        if box_index is not None and (
            isinstance(box_index, bool) or not isinstance(box_index, int)
            or not 0 <= box_index < len(subs)
        ):
            raise TaskValidationError(
                f"{label} 'box_index' must be an integer in [0, {len(subs) - 1}] "
                f"(which sub-recognition's hit box this node clicks)"
            )
    elif box_index is not None:
        raise TaskValidationError(
            f"{label} recognition 'or' has no 'box_index': the hit box comes from "
            f"whichever sub-recognition hit first"
        )


def _validate_sub_recognition(spec, label: str, depth: int) -> None:
    """Validate one member of an `all_of` / `any_of` list.

    A sub-recognition is an ordinary recognition object minus `always` — an
    unconditional hit inside a combination either adds nothing (AND) or makes
    the gate constant-true (OR).
    """
    if not isinstance(spec, dict):
        raise TaskValidationError(f"{label} must be a recognition object")
    rec_type = spec.get("type")
    if rec_type not in COMBO_SUB_TYPES:
        raise TaskValidationError(
            f"{label} has unsupported recognition type '{rec_type}' "
            f"(allowed inside a combination: {COMBO_SUB_TYPES}; 'always' is rejected "
            f"because it would stop the combination from gating)"
        )
    if rec_type in ("ui_text", "ocr", "scene"):
        expected = spec.get("expected")
        if not isinstance(expected, str) or not expected:
            raise TaskValidationError(f"{label} recognition '{rec_type}' requires non-empty 'expected'")
    if rec_type in ("template", "feature"):
        template = spec.get("template")
        if not isinstance(template, str) or not template:
            raise TaskValidationError(f"{label} recognition '{rec_type}' requires non-empty 'template'")
    if rec_type == "feature":
        _validate_feature_params(spec, label)
    if rec_type == "yolo":
        for key in ("label", "model"):
            if key in spec and (not isinstance(spec[key], str) or not spec[key]):
                raise TaskValidationError(f"{label} (yolo) '{key}' must be a non-empty string")
    roi = spec.get("roi")
    if roi is not None and (not isinstance(roi, list) or len(roi) != 4):
        raise TaskValidationError(f"{label} recognition roi must be [x1, y1, x2, y2]")
    if rec_type in COMBO_TYPES:
        _validate_combo(spec, label, depth)


#: Actions allowed to dismiss a known-benign popup (no agent / custom / none).
_POPUP_ACTION_TYPES = ("click", "key", "gesture")


def _validate_popup_recognition(spec: Dict, label: str, field: str) -> None:
    """Validate one popup recognition spec (`recognition` or `confirm`).

    Both fields are the same schema evaluated against the same frame, so they
    share one validator -- a `confirm` typo must fail as loudly as a
    `recognition` typo, or the gate silently degrades to "always confirmed".
    """
    rec_type = spec.get("type")
    if rec_type not in WATCHDOG_TYPES:
        raise TaskValidationError(
            f"{label} has unsupported {field} type '{rec_type}' (allowed: {WATCHDOG_TYPES})"
        )
    if rec_type in ("ui_text", "ocr", "scene") and not spec.get("expected"):
        raise TaskValidationError(f"{label} {field} '{rec_type}' requires non-empty 'expected'")
    if rec_type in ("template", "feature") and not spec.get("template"):
        raise TaskValidationError(f"{label} {field} '{rec_type}' requires non-empty 'template'")
    if rec_type == "feature":
        _validate_feature_params(spec, label)
    if rec_type in COMBO_TYPES:
        _validate_combo(spec, label)
    roi = spec.get("roi")
    if roi is not None and (not isinstance(roi, list) or len(roi) != 4):
        raise TaskValidationError(f"{label} {field} roi must be [x1, y1, x2, y2]")


def _validate_popups(popups) -> None:
    """Validate the optional `popups` whitelist of known-benign popups.

    Each entry is a {recognition, action[, name]} pair: the recognition detects
    the popup; the action (a dismiss-capable executor action) clears it. These
    are deliberately *not* findings, so they are validated like a watchdog
    trigger plus a restricted action -- not a full node (no next/on_timeout).

    An entry may also carry `confirm`: a second recognition spec (same schema,
    any type including combinations) evaluated on the same frame before the
    dismiss action fires. It exists because a lone ambiguous anchor once made
    the sweep close the panel under test (2026-08-11), so it is validated with
    exactly the same rules as `recognition`.
    """
    if popups is None:
        return
    if not isinstance(popups, list):
        raise TaskValidationError("'popups' must be a list of popup objects")
    for i, popup in enumerate(popups):
        label = f"Popup #{i + 1}"
        if not isinstance(popup, dict):
            raise TaskValidationError(f"{label} must be an object")

        recognition = popup.get("recognition")
        if not isinstance(recognition, dict):
            raise TaskValidationError(f"{label} requires a 'recognition' object")
        _validate_popup_recognition(recognition, label, "recognition")

        # Optional second gate on the same frame; absent means the entry's
        # recognition is trusted on its own (the historical behaviour).
        confirm = popup.get("confirm")
        if confirm is not None:
            if not isinstance(confirm, dict):
                raise TaskValidationError(f"{label} 'confirm' must be a recognition object")
            _validate_popup_recognition(confirm, label, "confirm")

        action = popup.get("action")
        if not isinstance(action, dict):
            raise TaskValidationError(f"{label} requires an 'action' object")
        atype = action.get("type")
        if atype not in _POPUP_ACTION_TYPES:
            raise TaskValidationError(
                f"{label} has unsupported action type '{atype}' "
                f"(allowed dismiss actions: {_POPUP_ACTION_TYPES})"
            )
        if atype == "click" and action.get("target") != "recognized":
            params = action.get("params", {})
            if "x" not in params or "y" not in params:
                raise TaskValidationError(f"{label} click action needs target='recognized' or params x/y")
        if atype == "key" and "keycode" not in action.get("params", {}):
            raise TaskValidationError(f"{label} key action requires params keycode")


def _validate_node(name: str, node: Dict, nodes: Dict) -> None:
    if not isinstance(node, dict):
        raise TaskValidationError(f"Node '{name}' must be an object")

    recognition = node.get("recognition")
    if not isinstance(recognition, dict):
        raise TaskValidationError(f"Node '{name}' requires a 'recognition' object")
    rec_type = recognition.get("type")
    if rec_type not in RECOGNITION_TYPES:
        raise TaskValidationError(
            f"Node '{name}' has unsupported recognition type '{rec_type}' (allowed: {RECOGNITION_TYPES})"
        )
    if rec_type in ("ui_text", "ocr", "scene"):
        expected = recognition.get("expected")
        if not isinstance(expected, str) or not expected:
            raise TaskValidationError(f"Node '{name}' recognition '{rec_type}' requires non-empty 'expected'")
    if rec_type in ("template", "feature"):
        template = recognition.get("template")
        if not isinstance(template, str) or not template:
            raise TaskValidationError(
                f"Node '{name}' recognition '{rec_type}' requires non-empty 'template'"
            )
    if rec_type == "feature":
        _validate_feature_params(recognition, f"Node '{name}' recognition")
    if rec_type in COMBO_TYPES:
        _validate_combo(recognition, f"Node '{name}'")
    if rec_type == "yolo":
        if "label" in recognition and (not isinstance(recognition["label"], str)
                                       or not recognition["label"]):
            raise TaskValidationError(f"Node '{name}' recognition 'yolo' 'label' must be a non-empty string")
        if "model" in recognition and (not isinstance(recognition["model"], str)
                                       or not recognition["model"]):
            raise TaskValidationError(f"Node '{name}' recognition 'yolo' 'model' must be a non-empty string")
    roi = recognition.get("roi")
    if roi is not None and (not isinstance(roi, list) or len(roi) != 4):
        raise TaskValidationError(f"Node '{name}' recognition roi must be [x1, y1, x2, y2]")

    action = node.get("action")
    if not isinstance(action, dict):
        raise TaskValidationError(f"Node '{name}' requires an 'action' object")
    action_type = action.get("type")
    if action_type not in TASK_ACTION_TYPES:
        raise TaskValidationError(
            f"Node '{name}' has unsupported action type '{action_type}' (allowed: {TASK_ACTION_TYPES})"
        )
    if action_type == "click" and action.get("target") != "recognized":
        params = action.get("params", {})
        if "x" not in params or "y" not in params:
            raise TaskValidationError(f"Node '{name}' click action needs target='recognized' or params x/y")
    if action_type in ("agent", "llm") and not action.get("text"):
        raise TaskValidationError(f"Node '{name}' agent action requires 'text'")
    if action_type == "key" and "keycode" not in action.get("params", {}):
        raise TaskValidationError(f"Node '{name}' key action requires params keycode")
    if action_type == "custom":
        handler_name = action.get("name")
        if not isinstance(handler_name, str) or not handler_name:
            raise TaskValidationError(f"Node '{name}' custom action requires non-empty 'name'")
        if get_handler(handler_name) is None:
            raise TaskValidationError(
                f"Node '{name}' references unregistered custom action '{handler_name}' "
                f"(registered: {list(registered_names())})"
            )

    next_nodes = node.get("next", [])
    if not isinstance(next_nodes, list) or not all(isinstance(n, str) for n in next_nodes):
        raise TaskValidationError(f"Node '{name}' 'next' must be a list of node names")
    for ref in next_nodes:
        if ref not in nodes:
            raise TaskValidationError(f"Node '{name}' references unknown next node '{ref}'")

    on_timeout = node.get("on_timeout")
    if on_timeout is not None and on_timeout not in nodes:
        raise TaskValidationError(f"Node '{name}' references unknown on_timeout node '{on_timeout}'")

    finding = node.get("finding")
    if finding is not None:
        if isinstance(finding, str):
            if not finding:
                raise TaskValidationError(f"Node '{name}' 'finding' string must be non-empty")
        elif isinstance(finding, dict):
            message = finding.get("message")
            if not isinstance(message, str) or not message:
                raise TaskValidationError(f"Node '{name}' 'finding' requires non-empty 'message'")
            severity = finding.get("severity")
            if severity is not None and severity not in SEVERITIES:
                raise TaskValidationError(
                    f"Node '{name}' 'finding' has invalid severity '{severity}' (allowed: {SEVERITIES})"
                )
        else:
            raise TaskValidationError(f"Node '{name}' 'finding' must be a string or object")

    _validate_repeat(name, action_type, action.get("params"))

    for field in _INT_FIELDS:
        value = node.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise TaskValidationError(f"Node '{name}' field '{field}' must be a non-negative integer")

    _validate_wait_still(f"Node '{name}'", node.get("wait_still"))


def _validate_repeat(name: str, action_type: str, params) -> None:
    """Validate the action-level repeat knobs (params.repeat & friends).

    Repeating only means something for an instantaneous executor action: a
    repeated `wait` is just a longer sleep, `agent`/`none` execute nothing here,
    and a `custom` action's params belong to its handler (which brings its own
    iteration knobs). Those are rejected rather than silently ignored — a
    "repeat" that quietly does nothing is exactly the kind of trap that makes a
    QTE task look flaky.
    """
    if not isinstance(params, dict):
        return
    present = [k for k in REPEAT_PARAM_KEYS if k in params]
    if not present:
        return
    if action_type not in REPEATABLE_ACTION_TYPES:
        raise TaskValidationError(
            f"Node '{name}' action '{action_type}' does not support "
            f"{', '.join(present)} (repeatable action types: "
            f"{', '.join(REPEATABLE_ACTION_TYPES)})"
        )
    repeat = params.get("repeat")
    if repeat is not None and (
        not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1
    ):
        raise TaskValidationError(f"Node '{name}' action 'repeat' must be an integer >= 1")
    for field in ("repeat_delay_ms", "repeat_wait_freezes_ms"):
        value = params.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise TaskValidationError(
                f"Node '{name}' action '{field}' must be a non-negative integer"
            )


def _validate_wait_still(label: str, wait_still) -> None:
    """Optional per-node "wait until the screen stops moving" settle window.

    Complements post_delay_ms: a fixed sleep for a known short pause, wait_still
    for a cutscene/loading of unpredictable length. Timing out is not a failure,
    so every field is a bound, not an assertion.

    `label` names the owner in error messages ("Node 'x'" or "Task 'defaults'"),
    since the same spec can be declared per node or once in the defaults block.
    """
    if wait_still is None:
        return
    if not isinstance(wait_still, dict):
        raise TaskValidationError(f"{label} 'wait_still' must be an object")
    for field in ("timeout_ms", "interval_ms"):
        value = wait_still.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise TaskValidationError(
                f"{label} wait_still '{field}' must be a non-negative integer"
            )
    threshold = wait_still.get("threshold")
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise TaskValidationError(
                f"{label} wait_still 'threshold' must be a number in [0, 1]"
            )
        if not 0 <= float(threshold) <= 1:
            raise TaskValidationError(
                f"{label} wait_still 'threshold' must be a number in [0, 1]"
            )
