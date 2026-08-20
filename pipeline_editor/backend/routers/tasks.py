"""任务 CRUD + includes + 校验/lint 干跑 + 布局。

全部不依赖 runtime（无 adb / 无感知），保证无设备也可编辑。
校验真值只有一处：task_loader.resolve_task —— 前端不复刻规则。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException

import backend.autoplayqa  # noqa: F401  (path setup must run first)
from backend import layoutstore, taskio
from backend.routers._guards import base_version, reject_if_stale
from backend.autoplayqa import SUITE_DIR, TASK_DIR, config

from task.step_numbering import compute_step_labels
from task.task_lint import lint_task
from task.task_loader import (
    SuiteValidationError,
    TaskValidationError,
    get_suite_path,
    get_task_path,
    list_suites,
    list_tasks,
    load_task,
    resolve_task,
)

router = APIRouter(prefix="/api", tags=["tasks"])


def _check_name(name: str) -> str:
    if not taskio.is_valid_name(name):
        raise HTTPException(400, f"非法任务名: {name!r}")
    return name


def _structured_error(message: str) -> Dict[str, Any]:
    return taskio.structured_error(message)


@router.get("/tasks")
def api_list_tasks() -> List[Dict]:
    out = []
    for name in list_tasks():
        path = get_task_path(name)
        entry = None
        node_count = None
        includes = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = raw.get("entry")
            nodes = raw.get("nodes")
            node_count = len(nodes) if isinstance(nodes, dict) else 0
            includes = raw.get("includes")
        except (json.JSONDecodeError, OSError):
            pass
        out.append({
            "name": name,
            "entry": entry,
            "node_count": node_count,
            "includes": includes,
            "mtime": path.stat().st_mtime if path.is_file() else None,
        })
    return out


@router.get("/tasks/{name}")
def api_get_task(name: str) -> Dict:
    """返回 {raw, resolved, mtime_ns}。

    resolved 是给编辑器的"展示态"，构造逻辑在 `taskio.build_display_task`
    ——内嵌 MCP 的 get_task 共用同一份，两条通道不许分叉。

    `mtime_ns` 是文件版本令牌，回传给 PUT 的 `base_mtime_ns` 即开启乐观并发控制。
    它挂在响应外层而**不进 resolved 任务体**——任务体里多一个键就要同步前端的
    serializeForSave 与 round-trip 回归（MCP 没有外层信封，只能走生成键，见
    `taskio.FILE_VERSION_KEY`）。
    """
    _check_name(name)
    path = get_task_path(name)
    if not path.is_file():
        raise HTTPException(404, f"任务不存在: {name}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        display = taskio.build_display_task(raw)
        error = None
    except TaskValidationError as exc:
        display = None
        error = _structured_error(str(exc))
    return {"name": name, "raw": raw, "resolved": display, "error": error,
            "mtime_ns": taskio.file_version(path)}


@router.put("/tasks/{name}")
def api_save_task(name: str, body: Dict = Body(...)) -> Dict:
    """校验并保存（与 mcp save_task 同一流程，含 lint.strict 拒绝门）。

    写盘的是 body["task"] 的原始形态（保留 includes/defaults/_comment），
    校验针对 resolve 之后的合并结果。落盘前统一过 `taskio.strip_for_save`：
    前端 serializeForSave 已经剔干净，这里是服务端兜底——生成键与 include
    节点绝不进主文件，不依赖调用方自觉。

    body 可带 `base_mtime_ns`（GET 响应里的 `mtime_ns`）：与磁盘当前版本不符就
    409 拒绝写盘（乐观并发控制）；不带则保持旧的 last-write-wins 行为。
    """
    _check_name(name)
    task = body.get("task")
    if not isinstance(task, dict):
        raise HTTPException(400, "body 需要 {task: <任务对象>}")
    path = get_task_path(name)
    reject_if_stale(path, base_version(body), "任务")
    task = taskio.strip_for_save(task)
    try:
        resolved = resolve_task(task, TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": _structured_error(str(exc))}
    warnings = [w.to_dict() for w in lint_task(resolved)]
    if warnings and config.get("lint", {}).get("strict", False):
        return {
            "ok": False,
            "error": {"scope": "task", "node": None,
                      "message": f"lint.strict 已开启且存在 {len(warnings)} 条警告，拒绝保存"},
            "lint_warnings": warnings,
        }
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "nodes": len(resolved["nodes"]),
            "lint_warnings": warnings, "mtime_ns": taskio.file_version(path)}


@router.delete("/tasks/{name}")
def api_delete_task(name: str, force: bool = False) -> Dict:
    _check_name(name)
    path = get_task_path(name)
    if not path.is_file():
        raise HTTPException(404, f"任务不存在: {name}")
    referrers = _suites_referencing(name)
    if referrers and not force:
        raise HTTPException(409, f"任务被套件引用: {', '.join(referrers)}（加 ?force=true 强制删除）")
    path.unlink()
    layoutstore.delete_layout(name)
    return {"ok": True, "referrers": referrers}


@router.post("/tasks/{name}/rename")
def api_rename_task(name: str, body: Dict = Body(...)) -> Dict:
    _check_name(name)
    new_name = body.get("new_name")
    if not isinstance(new_name, str) or not new_name:
        raise HTTPException(400, "body 需要 {new_name: <新任务名>}")
    _check_name(new_name)
    path = get_task_path(name)
    if not path.is_file():
        raise HTTPException(404, f"任务不存在: {name}")
    target = get_task_path(new_name)
    if target.is_file():
        raise HTTPException(409, f"目标任务名已存在: {new_name}")
    path.rename(target)
    layoutstore.rename_layout(name, new_name)
    return {"ok": True, "referrers": _suites_referencing(name)}


def _suites_referencing(task_name: str) -> List[str]:
    out = []
    for suite_name in list_suites():
        try:
            data = json.loads(get_suite_path(suite_name).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        cases = data.get("cases")
        if isinstance(cases, list) and task_name in cases:
            out.append(suite_name)
    return out


# ---------- includes ----------

@router.get("/includes")
def api_list_includes() -> List[Dict]:
    out = []
    for path in sorted(TASK_DIR.rglob("*.json")):
        rel = path.relative_to(TASK_DIR)
        if len(rel.parts) < 2:
            continue  # 顶层 = 任务文件
        top = rel.parts[0]
        if top in ("suites", ".layout"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        nodes = data.get("nodes")
        out.append({
            "path": rel.as_posix(),
            "description": data.get("description"),
            "node_names": sorted(nodes) if isinstance(nodes, dict) else [],
        })
    return out


@router.get("/includes/{ref:path}")
def api_get_include(ref: str) -> Dict:
    resolved = (TASK_DIR / ref).resolve()
    if not resolved.is_relative_to(TASK_DIR):
        raise HTTPException(400, f"include 路径越界: {ref}")
    if not resolved.is_file():
        raise HTTPException(404, f"include 文件不存在: {ref}")
    return {"path": ref, "data": json.loads(resolved.read_text(encoding="utf-8"))}


# ---------- 校验 / lint 干跑 ----------

@router.post("/validate")
def api_validate(body: Dict = Body(...)) -> Dict:
    """resolve_task 干跑，不落盘。恒 200（编辑中高频调用）。"""
    task = body.get("task")
    if not isinstance(task, dict):
        return {"ok": False, "error": {"scope": "task", "node": None,
                                       "message": "body 需要 {task: <任务对象>}"}}
    try:
        resolved = resolve_task(task, TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": _structured_error(str(exc))}
    return {
        "ok": True,
        "node_count": len(resolved["nodes"]),
        "steps": compute_step_labels(resolved),
        "merge": resolved.get("_merge"),
    }


@router.post("/lint")
def api_lint(body: Dict = Body(...)) -> Dict:
    task = body.get("task")
    if not isinstance(task, dict):
        return {"ok": False, "error": "body 需要 {task: <任务对象>}"}
    try:
        resolved = resolve_task(task, TASK_DIR)
    except TaskValidationError as exc:
        return {"ok": False, "error": _structured_error(str(exc))}
    return {"ok": True, "lint_warnings": [w.to_dict() for w in lint_task(resolved)]}


# ---------- 步号回写 ----------

@router.post("/tasks/{name}/renumber")
def api_renumber(name: str) -> Dict:
    """write_step_labels：按图重算步号写回文件（整文件重写、step 提到首键）。

    磁盘写操作——前端调用后必须重新 GET 任务，否则编辑器脏 buffer 会把
    重排结果覆盖回去。返回体带上写盘后的 `mtime_ns`，供前端把乐观锁基线推到
    最新版本（否则重排后的第一次保存会撞 409）。
    """
    _check_name(name)
    path = get_task_path(name)
    if not path.is_file():
        raise HTTPException(404, f"任务不存在: {name}")
    from task.step_numbering import write_step_labels

    result = write_step_labels(path)
    return {"ok": True, **result, "mtime_ns": taskio.file_version(path)}


# ---------- 布局 ----------

@router.get("/tasks/{name}/layout")
def api_get_layout(name: str) -> Dict:
    """无布局返回空 nodes（200）——前端对空布局走自动布局，避免 404 噪音。"""
    _check_name(name)
    layout = layoutstore.load_layout(name)
    return layout if layout is not None else {"nodes": {}}


@router.put("/tasks/{name}/layout")
def api_save_layout(name: str, body: Dict = Body(...)) -> Dict:
    _check_name(name)
    layoutstore.save_layout(name, body)
    return {"ok": True}
