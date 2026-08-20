"""内嵌 MCP server（streamable HTTP，挂在 /mcp）：AI 的任务编辑通道。

与 AutoPlayQA 的 stdio MCP 的关系：那边是全能力（设备/感知/运行），
这边**只做编辑面**且与编辑器同进程——AI 每次 save/renumber 落盘后，
文件监视器立即把 task_changed 推给前端，人就能在画布上实时看到 AI
的修改并接手微调。

三条硬约束（都由服务端保证，不靠工具描述提醒调用方）：

1. **与 REST 同源**：get_task 的展示态、save_task 的落盘剥离，都走
   `backend.taskio` 的共享 helper——AI 经 MCP 和人经 UI 对同一个文件的
   读写语义逐字一致，永不分叉。
2. **名字白名单**：所有接受 name 的工具先过 `taskio.is_valid_name`，
   `"../../evil"` 这类路径穿越直接返回 {ok: False}。
3. **不堵事件循环**：mcp 对同步工具是直接 await 调用（不进线程池），所以
   工具一律 `async def`，文件读写 / resolve / lint 经 anyio 卸到线程。

Claude Code 接入（.mcp.json）：
    "pipeline-editor": {"type": "http", "url": "http://127.0.0.1:8930/mcp"}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import anyio.to_thread

import backend.autoplayqa  # noqa: F401
from backend import taskio
from backend.autoplayqa import SUITE_DIR, TASK_DIR, config, logger

from mcp.server.fastmcp import FastMCP

from action.action_schema import TASK_SCHEMA_DOC
from task.custom_actions import registered_names
from task.step_numbering import compute_step_labels, write_step_labels
from task.task_lint import lint_task
from task.task_loader import (
    SuiteValidationError,
    TaskValidationError,
    get_suite_path,
    get_task_path,
    list_suites as _list_suite_names,
    list_tasks as _list_task_names,
    load_task,
    resolve_task,
    validate_suite,
)

mcp = FastMCP(
    "pipeline-editor",
    stateless_http=True,
    streamable_http_path="/",
    json_response=True,
)


def _bad_name(kind: str, name: str) -> Dict[str, Any]:
    logger.warning("mcp: rejected %s name %r (path traversal guard)", kind, name)
    return {"ok": False, "error": f"Invalid {kind} name: {name!r} "
                                  "(no path separators, no leading dot)"}


# ---------- 同步实现（跑在工作线程里，禁止在事件循环线程直接调用） ----------

def _read_display_task(name: str) -> Dict[str, Any]:
    path = get_task_path(name)
    if not path.is_file():
        return {"ok": False, "error": f"Task not found: {name}"}
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        display = taskio.build_display_task(raw)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    # 文件版本令牌：AI 回传给 save_task 的 base_mtime_ns 即开启乐观并发控制。
    # 它是生成键（`taskio.GENERATED_KEYS`），strip_for_save 必剔，绝不落盘。
    display[taskio.FILE_VERSION_KEY] = taskio.file_version(path)
    return {"ok": True, "task": display}


def _conflict(kind: str, current: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "conflict": True,
        "error": f"{kind} file on disk changed since you read it (a human or another "
                 f"tool saved it). Re-read it and re-apply your edit, or pass the "
                 f"current version as base_mtime_ns to overwrite deliberately.",
        "current_mtime_ns": current,
    }


def _write_task(name: str, task: Dict[str, Any],
                base_mtime_ns: Optional[str] = None) -> Dict[str, Any]:
    """冲突检查 → 剥离 → 校验 → lint 门 → 写盘（顺序不能换）。

    先剥离再校验：AI 拿到的展示态里 include 节点和 `includes` 声明同时存在，
    直接 resolve 会以「跨文件重名」报错，而这正是我们要剔掉的那批节点。

    冲突检查排在最前：磁盘已经不是 AI 读到的那份时，先告诉它「你的基线过期了」
    比先报一堆校验错误更有用（尽力而为，取舍见 `taskio`）。
    """
    path = get_task_path(name)
    current = taskio.version_conflict(path, base_mtime_ns)
    if current is not None:
        return _conflict("Task", current)
    to_write = taskio.strip_for_save(task)
    try:
        resolved = resolve_task(to_write, TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    warnings = [w.to_dict() for w in lint_task(resolved)]
    if warnings and config.get("lint", {}).get("strict", False):
        return {
            "ok": False,
            "error": f"lint.strict is on and {len(warnings)} warning(s) were found; save refused.",
            "lint_warnings": warnings,
        }
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_write, ensure_ascii=False, indent=2), encoding="utf-8")
    before, after = task.get("nodes"), to_write.get("nodes")
    dropped: List[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        dropped = sorted(set(before) - set(after))
    return {"ok": True, "path": str(path), "nodes": len(resolved["nodes"]),
            "include_nodes_skipped": dropped, "lint_warnings": warnings,
            "mtime_ns": taskio.file_version(path)}


def _validate_task(task: Dict[str, Any]) -> Dict[str, Any]:
    try:
        resolved = resolve_task(taskio.strip_for_save(task), TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "nodes": len(resolved["nodes"]),
        "steps": compute_step_labels(resolved),
        "lint_warnings": [w.to_dict() for w in lint_task(resolved)],
    }


def _renumber_task(name: str) -> Dict[str, Any]:
    path = get_task_path(name)
    if not path.is_file():
        return {"ok": False, "error": f"Task not found: {name}"}
    try:
        result = write_step_labels(path)
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


def _lint_saved_task(name: str) -> Dict[str, Any]:
    try:
        task = load_task(get_task_path(name))
    except TaskValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "lint_warnings": [w.to_dict() for w in lint_task(task)]}


def _scan_includes() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    common_dir = TASK_DIR / "common"
    if common_dir.is_dir():
        for p in sorted(common_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("mcp: skipping unreadable include %s (%s)", p, exc)
                continue
            out.append({"path": f"common/{p.name}",
                        "description": data.get("description"),
                        "nodes": sorted(data.get("nodes") or {})})
    return out


def _write_suite(name: str, suite: Dict[str, Any],
                 base_mtime_ns: Optional[str] = None) -> Dict[str, Any]:
    path: Path = get_suite_path(name)
    current = taskio.version_conflict(path, base_mtime_ns)
    if current is not None:
        return _conflict("Suite", current)
    suite.setdefault("name", name)
    try:
        validate_suite(suite, TASK_DIR)
    except SuiteValidationError as exc:
        return {"ok": False, "error": str(exc)}
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "mtime_ns": taskio.file_version(path)}


# ---------- MCP 工具（async：阻塞部分一律 to_thread） ----------

@mcp.tool()
async def get_task_schema() -> str:
    """Task JSON format reference (read before writing a task with save_task)."""
    return TASK_SCHEMA_DOC


@mcp.tool()
async def list_tasks() -> List[str]:
    """List saved task names in task/task_definitions/."""
    return await anyio.to_thread.run_sync(_list_task_names)


@mcp.tool()
async def get_task(name: str) -> Dict:
    """Load a saved task in the editor's display form, with step labels.

    Same form the canvas edits: includes are merged, but the task-level
    `defaults` block is NOT expanded into the nodes — round-trip safe, so
    get_task -> edit -> save_task never freezes defaults into the file.

    `_merge.include_map` maps each node to its source file ("<task>" = the
    task's own file) — nodes from shared fragments must be edited in the
    fragment file; save_task drops them instead of writing them into the task.
    `_steps` / `_step_outline` give each node's execution order.

    `_file_mtime_ns` is the file's version token (a decimal string). Pass it
    back as save_task's `base_mtime_ns` so the save is refused instead of
    silently overwriting the human's edits if they saved in the meantime.
    Like the other generated keys it is stripped on save and never written.
    """
    if not taskio.is_valid_name(name):
        return _bad_name("task", name)
    result = await anyio.to_thread.run_sync(_read_display_task, name)
    if not result["ok"]:
        return result
    return result["task"]


@mcp.tool()
async def validate_task(task_json: str) -> Dict:
    """Dry-run validate a task JSON string (includes resolved, nothing written).

    Returns {ok, nodes, steps, lint_warnings} or {ok: False, error}. Use it to
    check an edit before committing with save_task. Accepts either the raw form
    or get_task's display form (generated keys / include-sourced nodes are
    dropped exactly like save_task does).
    """
    try:
        task = json.loads(task_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    if not isinstance(task, dict):
        return {"ok": False, "error": "Task root must be a JSON object"}
    return await anyio.to_thread.run_sync(_validate_task, task)


@mcp.tool()
async def save_task(name: str, task_json: str,
                    base_mtime_ns: Optional[str] = None) -> Dict:
    """Validate and save a task JSON string; the human sees the change live.

    Feed back what get_task returned (edited): the server strips the generated
    keys (`_merge` / `_steps` / `_step_outline`) and every node whose
    include_map source is not "<task>", then validates and writes the raw form
    — `includes` / `on_conflict` / `defaults` declarations and your own
    `_comment` keys are kept verbatim. Dropped node names come back in
    `include_nodes_skipped`; to change one of those, edit its fragment file.

    Same gate as AutoPlayQA save_task (resolve -> lint -> lint.strict refusal).
    Because this server runs inside the editor backend, the canvas auto-reloads
    within ~2s of a successful save — the human may then fine-tune on top, so
    after saving WAIT for their go-ahead before further edits to the same task.

    Pass `base_mtime_ns` = the `_file_mtime_ns` you got from get_task to make
    the save fail loudly instead of clobbering someone else's concurrent edit:
    on a mismatch nothing is written and you get
    `{ok: False, conflict: True, current_mtime_ns}` — re-read with get_task and
    re-apply. Omit it and the save overwrites unconditionally (old behaviour).
    A successful save returns the new `mtime_ns`, so a chain of edits can keep
    passing the previous result forward without re-reading the file.
    """
    if not taskio.is_valid_name(name):
        return _bad_name("task", name)
    try:
        task = json.loads(task_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    if not isinstance(task, dict):
        return {"ok": False, "error": "Task root must be a JSON object"}
    try:
        base = taskio.normalize_version(base_mtime_ns)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await anyio.to_thread.run_sync(_write_task, name, task, base)


@mcp.tool()
async def renumber_task(name: str) -> Dict:
    """Recompute step labels from the graph and write them back to the file."""
    if not taskio.is_valid_name(name):
        return _bad_name("task", name)
    return await anyio.to_thread.run_sync(_renumber_task, name)


@mcp.tool()
async def lint_saved_task(name: str) -> Dict:
    """Lint a saved task by name (W-rule best-practice warnings, no write)."""
    if not taskio.is_valid_name(name):
        return _bad_name("task", name)
    return await anyio.to_thread.run_sync(_lint_saved_task, name)


@mcp.tool()
async def list_includes() -> List[Dict]:
    """Shared include fragments under task_definitions/ (via "includes")."""
    return await anyio.to_thread.run_sync(_scan_includes)


@mcp.tool()
async def list_custom_actions() -> List[str]:
    """Registered custom action names usable as {"type":"custom","name":...}."""
    return list(registered_names())


@mcp.tool()
async def list_suites() -> List[str]:
    """List saved suite names in task/task_definitions/suites/."""
    return await anyio.to_thread.run_sync(_list_suite_names)


@mcp.tool()
async def save_suite(name: str, suite_json: str,
                     base_mtime_ns: Optional[str] = None) -> Dict:
    """Validate and save a suite JSON string (cases/resume_after/landing...).

    Optional `base_mtime_ns` works exactly like save_task's: on a mismatch with
    the file on disk nothing is written and you get
    `{ok: False, conflict: True, current_mtime_ns}`. A successful save returns
    the new `mtime_ns`.
    """
    if not taskio.is_valid_name(name):
        return _bad_name("suite", name)
    try:
        suite = json.loads(suite_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON: {exc}"}
    if not isinstance(suite, dict):
        return {"ok": False, "error": "Suite root must be a JSON object"}
    try:
        base = taskio.normalize_version(base_mtime_ns)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await anyio.to_thread.run_sync(_write_suite, name, suite, base)
