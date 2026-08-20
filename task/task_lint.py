"""Task-authoring lint: "structurally valid but not robust" checks.

`task_loader.validate_task` guarantees a task *loads* (types, enums,
references all resolve). This module is orthogonal to it: it never rejects
anything by itself, it only flags patterns an author should double check —
a node that can stall with no recovery, an error-looking branch that never
gets reported as a QA finding, a cold-start task with no popup whitelist,
a click that hardcodes coordinates where a recognized anchor was already
available, or a task with zero QA assertions at all.

Call `lint_task` on an already-resolved/validated task dict (e.g. the result
of `task_loader.resolve_task`); a structurally invalid task should be
rejected by the loader first, not linted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set

from task.task_loader import MAIN_FILE_LABEL

#: Node-name / message substrings that mark a "this looks like an error
#: branch" node for W002. Checked against both the raw and lower-cased node
#: name (English keywords are typically already lower-case; the extra
#: lower-casing only matters for mixed-case English words).
_ERROR_KEYWORDS = (
    "error", "fail", "exception", "crash", "popup", "warn", "abnormal",
    "异常", "错误", "失败", "崩溃", "弹窗", "警告", "报错", "卡死",
)

#: Recognition types that never expose a meaningful click anchor (no text/
#: image match center to click on), so a hardcoded-coordinate click there is
#: not a lint concern — see W004.
_NO_ANCHOR_RECOGNITION_TYPES = ("always", "blank_screen", "scene")

RULES = {
    "W001": "非终端节点缺少 on_timeout（识别超时无兜底路径）",
    "W002": "疑似异常分支节点缺少 finding（未上报为 QA 发现）",
    "W003": "任务含冷启动（launch_app）但未声明 popups 白名单",
    "W004": "识别已给出可点击锚点，却用写死坐标 click（未用 target: recognized）",
    "W005": "任务无任何 watchdog 且无异常分支节点（缺少 QA 断言）",
    "W006": "includes 引入的片段无任何节点可达（白引一份公共片段）",
    "W007": "scene 门控节点配了 target: recognized（scene 是整屏分类，恒不产坐标，运行期会抛 ValueError）",
}


@dataclass
class LintWarning:
    rule_id: str
    node: Optional[str]
    message: str
    suggestion: str

    def to_dict(self) -> Dict:
        return asdict(self)


def lint_task(task: Dict) -> List[LintWarning]:
    """Return best-practice warnings for a resolved task dict (never raises)."""
    if not isinstance(task, dict):
        return []
    nodes = task.get("nodes")
    if not isinstance(nodes, dict):
        return []

    warnings: List[LintWarning] = []
    warnings.extend(_check_w001_missing_on_timeout(nodes))
    warnings.extend(_check_w002_error_branch_missing_finding(task, nodes))
    warnings.extend(_check_w003_cold_start_missing_popups(task, nodes))
    warnings.extend(_check_w004_hardcoded_click_with_anchor(nodes))
    warnings.extend(_check_w005_no_qa_assertions(task, nodes))
    warnings.extend(_check_w006_unreachable_include(task, nodes))
    warnings.extend(_check_w007_scene_click_recognized(nodes))
    return warnings


def _check_w001_missing_on_timeout(nodes: Dict) -> List[LintWarning]:
    """W001: a non-terminal node (has `next`) with no `on_timeout`.

    "always" recognition matches instantly (score 1.0, no polling) and can
    never time out — see `recognizers.RecognizerHub.recognize` and the same
    exclusion `WATCHDOG_TYPES` already makes — so those nodes are exempt.
    """
    out: List[LintWarning] = []
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        rec_type = (node.get("recognition") or {}).get("type")
        if rec_type == "always":
            continue
        next_nodes = node.get("next") or []
        if next_nodes and not node.get("on_timeout"):
            out.append(LintWarning(
                "W001", name,
                f"节点 '{name}' 有 next 候选但未设置 on_timeout，识别超时后无兜底路径。",
                "补充 on_timeout 指向可重新定位的恢复节点，或确认该节点允许卡住直接判失败。",
            ))
    return out


def _suspect_branch_nodes(task: Dict, nodes: Dict) -> Set[str]:
    """Nodes that look like an anomaly branch: name carries an error keyword,
    or the node is a jump target of a watchdog `skip_to` / the task-level
    `on_finding` (both exist precisely to recover from a reported bug)."""
    suspects: Set[str] = set()
    for name in nodes:
        lowered = name.lower()
        if any(kw in name or kw in lowered for kw in _ERROR_KEYWORDS):
            suspects.add(name)
    for watchdog in task.get("watchdogs") or []:
        if not isinstance(watchdog, dict):
            continue
        skip_to = watchdog.get("skip_to")
        if isinstance(skip_to, str) and skip_to in nodes:
            suspects.add(skip_to)
    on_finding = task.get("on_finding")
    if isinstance(on_finding, str) and on_finding in nodes:
        suspects.add(on_finding)
    return suspects


def _check_w002_error_branch_missing_finding(task: Dict, nodes: Dict) -> List[LintWarning]:
    out: List[LintWarning] = []
    for name in sorted(_suspect_branch_nodes(task, nodes)):
        node = nodes.get(name)
        if isinstance(node, dict) and not node.get("finding"):
            out.append(LintWarning(
                "W002", name,
                f"节点 '{name}' 疑似异常分支（名称含错误关键词，或被 watchdog skip_to / 任务 on_finding 指向）"
                "但未设置 finding，命中时不会作为 QA 发现留证。",
                "为该节点补 finding 字段（字符串或 {severity, message}），把这条分支上报为发现。",
            ))
    return out


def _cold_start_nodes(nodes: Dict) -> List[str]:
    return [
        name for name, node in nodes.items()
        if isinstance(node, dict)
        and (node.get("action") or {}).get("type") == "custom"
        and (node.get("action") or {}).get("name") == "launch_app"
    ]


def _check_w003_cold_start_missing_popups(task: Dict, nodes: Dict) -> List[LintWarning]:
    launch_nodes = _cold_start_nodes(nodes)
    if not launch_nodes or task.get("popups"):
        return []
    return [
        LintWarning(
            "W003", name,
            f"节点 '{name}' 触发冷启动（custom launch_app）但任务未声明 popups 白名单，"
            "首次启动常见的用户协议/权限/公告弹窗会被当成异常卡住识别。",
            "补充任务级 popups 白名单，覆盖启动阶段已知良性的弹窗。",
        )
        for name in launch_nodes
    ]


def _check_w004_hardcoded_click_with_anchor(nodes: Dict) -> List[LintWarning]:
    out: List[LintWarning] = []
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        action = node.get("action") or {}
        if action.get("type") != "click" or action.get("target") == "recognized":
            continue
        rec_type = (node.get("recognition") or {}).get("type")
        if rec_type is None or rec_type in _NO_ANCHOR_RECOGNITION_TYPES:
            continue
        params = action.get("params") or {}
        if "x" in params and "y" in params:
            out.append(LintWarning(
                "W004", name,
                f"节点 '{name}' 识别类型 '{rec_type}' 已能给出锚点坐标，click 动作却写死了 "
                f"params 坐标 ({params.get('x')}, {params.get('y')}) 而非 target='recognized'。",
                "改用 {\"type\": \"click\", \"target\": \"recognized\"}，让点击跟随识别结果而不是固定像素。",
            ))
    return out


def _leaf_recognition_types(spec: Dict) -> Set[str]:
    """Flatten a recognition spec to the set of leaf channel types it can hit
    through.

    A plain spec contributes its own `type`; an `and`/`or` combination
    contributes the union of its `all_of`/`any_of` members' leaf types
    (recursing through nested combinations). Used by W007 to tell "this node
    can only ever produce a scene hit" (leaf types == {"scene"}, so `center`
    is always None) apart from "scene is one option among several" (a mixed
    combo can still click).
    """
    if not isinstance(spec, dict):
        return set()
    rec_type = spec.get("type")
    if rec_type == "and":
        subs = spec.get("all_of")
    elif rec_type == "or":
        subs = spec.get("any_of")
    else:
        return {rec_type} if rec_type is not None else set()
    leaves: Set[str] = set()
    for sub in subs or []:
        leaves |= _leaf_recognition_types(sub)
    return leaves


def _check_w007_scene_click_recognized(nodes: Dict) -> List[LintWarning]:
    """W007: a node whose recognition can only ever hit via `scene` (a
    whole-frame classification with no click anchor -- `center` is always
    None, see `RecognizerHub._recognize_scene`) but whose action is a
    `target: "recognized"` click.

    This is statically detectable and always fatal at runtime: `task_engine`
    raises ValueError the moment such a node fires (`click target='recognized'
    but recognition produced no coordinates`) -- catch it at save/lint time
    instead of failing the whole run mid-replay.
    """
    out: List[LintWarning] = []
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        action = node.get("action") or {}
        if action.get("type") != "click" or action.get("target") != "recognized":
            continue
        leaf_types = _leaf_recognition_types(node.get("recognition") or {})
        if leaf_types == {"scene"}:
            out.append(LintWarning(
                "W007", name,
                f"节点 '{name}' 识别通道恒为 'scene'（整屏分类，不产坐标，center 恒 None），"
                "action 却写了 target='recognized'——运行期识别命中时会立即抛 ValueError 导致整个任务失败。",
                "scene 只做『我在哪』断言：把 action 改成 {\"type\": \"none\"} 之类不依赖坐标的动作，"
                "或改用 ocr/template 等能给出锚点的识别通道再配 target='recognized'。",
            ))
    return out


def _reachable_nodes(task: Dict, nodes: Dict) -> Set[str]:
    """Nodes the engine can actually arrive at, walking `next` / `on_timeout`.

    Seeds are every place the engine can *enter* the graph, not just `entry`:
    a watchdog's `skip_to` and the task-level `on_finding` are bug-skip landing
    pads, so a recovery branch reachable only that way is still live flow.
    """
    stack: List[str] = []
    for seed in (task.get("entry"), task.get("on_finding")):
        if isinstance(seed, str) and seed in nodes:
            stack.append(seed)
    for watchdog in task.get("watchdogs") or []:
        if isinstance(watchdog, dict):
            skip_to = watchdog.get("skip_to")
            if isinstance(skip_to, str) and skip_to in nodes:
                stack.append(skip_to)

    seen: Set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        node = nodes.get(name)
        if not isinstance(node, dict):
            continue
        refs = list(node.get("next") or [])
        refs.append(node.get("on_timeout"))
        for ref in refs:
            if isinstance(ref, str) and ref in nodes and ref not in seen:
                stack.append(ref)
    return seen


def _check_w006_unreachable_include(task: Dict, nodes: Dict) -> List[LintWarning]:
    """W006: an included fragment none of whose nodes the flow can reach.

    Reads `_merge.include_map` (node -> source file) that the loader attaches
    when it merges includes; a task loaded without includes has no map and is
    skipped. Not an error: an include costs nothing at runtime — but it is
    almost always a stale reference or a `next` that was never wired up, and it
    makes the merged node table lie about what the task does.
    """
    merge = task.get("_merge")
    if not isinstance(merge, dict):
        return []
    include_map = merge.get("include_map")
    if not isinstance(include_map, dict) or not include_map:
        return []

    by_source: Dict[str, List[str]] = {}
    for node_name, source in include_map.items():
        if source == MAIN_FILE_LABEL:  # the main task file's own nodes
            continue
        by_source.setdefault(source, []).append(node_name)
    if not by_source:
        return []

    reachable = _reachable_nodes(task, nodes)
    return [
        LintWarning(
            "W006", None,
            f"includes 引入的片段 '{source}' 共 {len(members)} 个节点，"
            f"但从 entry（含 watchdog skip_to / on_finding 入口）出发一个都不可达。",
            f"确认是否漏了跨文件 next（如把某节点的 next 指向 {sorted(members)[0]}），"
            "或该 include 已失效可从 includes 移除。",
        )
        for source, members in by_source.items()
        if not (set(members) & reachable)
    ]


def _check_w005_no_qa_assertions(task: Dict, nodes: Dict) -> List[LintWarning]:
    watchdogs = task.get("watchdogs") or []
    has_finding_node = any(isinstance(n, dict) and n.get("finding") for n in nodes.values())
    if watchdogs or has_finding_node:
        return []
    return [
        LintWarning(
            "W005", None,
            "任务没有任何 watchdog，也没有带 finding 的异常分支节点，等于没有任何 QA 断言。",
            "至少加一条任务级 watchdog（禁止文本/白屏等负向断言），或给已知异常分支节点补 finding。",
        )
    ]
