"""套件 CRUD。校验走 task_loader.validate_suite（与引擎同一真值）。"""
from __future__ import annotations

import json
from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException

import backend.autoplayqa  # noqa: F401
from backend import taskio
from backend.autoplayqa import SUITE_DIR, TASK_DIR
from backend.routers._guards import base_version, reject_if_stale

from task.task_loader import (
    SuiteValidationError,
    get_suite_path,
    list_suites,
    load_suite,
    validate_suite,
)

router = APIRouter(prefix="/api", tags=["suites"])


def _check_name(name: str) -> str:
    """名字白名单（唯一的路径穿越防线）见 `taskio.is_valid_name`。"""
    if not taskio.is_valid_name(name):
        raise HTTPException(400, f"非法套件名: {name!r}")
    return name


@router.get("/suites")
def api_list_suites() -> List[Dict]:
    out = []
    for name in list_suites():
        path = get_suite_path(name)
        cases = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cases = data.get("cases")
        except (json.JSONDecodeError, OSError):
            pass
        out.append({"name": name, "cases": cases,
                    "mtime": path.stat().st_mtime if path.is_file() else None})
    return out


@router.get("/suites/{name}")
def api_get_suite(name: str) -> Dict:
    """返回 {raw, error, mtime_ns}；`mtime_ns` 回传给 PUT 即开启乐观并发控制。"""
    _check_name(name)
    path = get_suite_path(name)
    if not path.is_file():
        raise HTTPException(404, f"套件不存在: {name}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    error = None
    try:
        load_suite(name)
    except SuiteValidationError as exc:
        error = str(exc)
    return {"name": name, "raw": raw, "error": error,
            "mtime_ns": taskio.file_version(path)}


@router.put("/suites/{name}")
def api_save_suite(name: str, body: Dict = Body(...)) -> Dict:
    """校验并保存套件。

    body 可带 `base_mtime_ns`（GET 响应里的 `mtime_ns`）：与磁盘当前版本不符就
    409 拒绝写盘；不带则保持旧的 last-write-wins 行为（向后兼容）。
    """
    _check_name(name)
    suite = body.get("suite")
    if not isinstance(suite, dict):
        raise HTTPException(400, "body 需要 {suite: <套件对象>}")
    path = get_suite_path(name)
    reject_if_stale(path, base_version(body), "套件")
    suite.setdefault("name", name)
    try:
        validate_suite(suite, TASK_DIR)
    except SuiteValidationError as exc:
        return {"ok": False, "error": str(exc)}
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "mtime_ns": taskio.file_version(path)}


@router.delete("/suites/{name}")
def api_delete_suite(name: str) -> Dict:
    _check_name(name)
    path = get_suite_path(name)
    if not path.is_file():
        raise HTTPException(404, f"套件不存在: {name}")
    path.unlink()
    return {"ok": True}
