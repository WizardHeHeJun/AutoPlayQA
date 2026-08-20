"""画布布局 sidecar 存储：task/task_definitions/.layout/<name>.json。

布局是编辑器私有状态，不塞进任务 JSON（避免污染 git diff 与 AI agent 读写
任务的工作流）。AutoPlayQA 的 list_tasks 只 glob 顶层 *.json，.layout/
子目录对它完全不可见，零侵入。
"""
from __future__ import annotations

import json
from typing import Dict, Optional

from backend.autoplayqa import LAYOUT_DIR


def _layout_path(name: str):
    return LAYOUT_DIR / f"{name}.json"


def load_layout(name: str) -> Optional[Dict]:
    path = _layout_path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_layout(name: str, layout: Dict) -> None:
    LAYOUT_DIR.mkdir(parents=True, exist_ok=True)
    _layout_path(name).write_text(
        json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete_layout(name: str) -> None:
    path = _layout_path(name)
    if path.is_file():
        path.unlink()


def rename_layout(old: str, new: str) -> None:
    path = _layout_path(old)
    if path.is_file():
        LAYOUT_DIR.mkdir(parents=True, exist_ok=True)
        target = _layout_path(new)
        if target.is_file():
            target.unlink()
        path.rename(target)
