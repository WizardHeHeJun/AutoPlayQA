"""枚举 / 资产 / 设备。schema 与 custom-actions 不依赖 runtime。"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import backend.autoplayqa  # noqa: F401
from backend.action_schema_introspect import CustomActionSchema, extract_params
from backend.autoplayqa import TEMPLATE_DIR, get_runtime

from action.action_schema import (
    REPEAT_PARAM_KEYS,
    REPEATABLE_ACTION_TYPES,
    TASK_ACTION_TYPES,
    TASK_SCHEMA_DOC,
)
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
from task.task_loader import (
    CONFLICT_STRATEGIES,
    SUITE_FAILURE_POLICIES,
    TASK_DEFAULT_KEYS,
    _POPUP_ACTION_TYPES,
)

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/schema")
def api_schema() -> Dict:
    return {
        "schema_doc": TASK_SCHEMA_DOC,
        "recognition_types": RECOGNITION_TYPES,
        "watchdog_types": WATCHDOG_TYPES,
        "combo_types": COMBO_TYPES,
        "combo_sub_types": COMBO_SUB_TYPES,
        "combo_list_key": COMBO_LIST_KEY,
        "max_combo_depth": MAX_COMBO_DEPTH,
        "action_types": TASK_ACTION_TYPES,
        "repeatable_action_types": REPEATABLE_ACTION_TYPES,
        "repeat_param_keys": REPEAT_PARAM_KEYS,
        "popup_action_types": _POPUP_ACTION_TYPES,
        "severities": SEVERITIES,
        "task_default_keys": TASK_DEFAULT_KEYS,
        "conflict_strategies": CONFLICT_STRATEGIES,
        "suite_failure_policies": SUITE_FAILURE_POLICIES,
        "custom_actions": list(registered_names()),
    }


@router.get("/custom-actions")
def api_custom_actions() -> List[str]:
    return list(registered_names())


@router.get("/custom-actions/{name}/schema")
def api_custom_action_schema(name: str) -> CustomActionSchema:
    """handler 源码静态提取出的参数表（尽力而为；提取不出时 params 为空）。"""
    handler = get_handler(name)
    if handler is None:
        raise HTTPException(404, f"未注册的 custom action: {name}")
    return CustomActionSchema(name=name, params=extract_params(handler))


_TEMPLATE_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]+$")


@router.get("/templates")
def api_templates() -> List[Dict]:
    if not TEMPLATE_DIR.is_dir():
        return []
    out = []
    for path in sorted(TEMPLATE_DIR.glob("*.png")):
        stat = path.stat()
        out.append({"name": path.stem, "file": path.name,
                    "size": stat.st_size, "mtime": stat.st_mtime})
    return out


@router.get("/templates/{name}/image")
def api_template_image(name: str):
    if not _TEMPLATE_NAME_RE.match(name):
        raise HTTPException(400, f"非法模板名: {name!r}")
    path = (TEMPLATE_DIR / f"{name}.png").resolve()
    if not path.is_relative_to(TEMPLATE_DIR) or not path.is_file():
        raise HTTPException(404, f"模板不存在: {name}")
    return FileResponse(path, media_type="image/png")


@router.get("/yolo-classes")
def api_yolo_classes(model: Optional[str] = None) -> Dict:
    rt = get_runtime()
    detector = rt.yolo_registry.get(model) if model else rt.yolo
    if detector is None:
        return {"available": False, "classes": {}, "models": rt.yolo_registry.names(),
                "error": f"未知 YOLO 模型 '{model}'"}
    if not detector.available():
        return {"available": False, "classes": {}, "models": rt.yolo_registry.names(),
                "error": f"模型文件不存在: {detector.model_path}"}
    return {"available": True, "classes": detector.class_names(),
            "models": rt.yolo_registry.names(), "model": model or "default"}


@router.get("/devices")
def api_devices() -> List[Dict]:
    devices = get_runtime().device_manager.discover_devices()
    return [{"device_id": d.device_id, "type": d.device_type, "model": d.model}
            for d in devices]
