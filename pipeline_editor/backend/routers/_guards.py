"""REST 侧的写入守卫：把 `taskio` 的乐观并发检测包成 HTTP 形态。

任务与套件两个 router 共用这一份——冲突的判定（`taskio.version_conflict`）和
409 的响应形状都只有一处定义，两条 PUT 不许分叉出不同语义。

MCP 通道不用这里的东西（它返回 `{ok: False, conflict: True, ...}` 而不是抛
HTTPException），但复用同一个 `taskio` 判定函数，冲突口径一致。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from backend import taskio


def base_version(body: Dict[str, Any]) -> Optional[str]:
    """请求体里的可选 `base_mtime_ns`（乐观并发控制）；缺省 = 不检测。

    非法值直接 400——静默忽略会让调用方以为开了乐观锁，其实还是覆盖式保存。
    """
    try:
        return taskio.normalize_version(body.get("base_mtime_ns"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def reject_if_stale(path: Path, expected: Optional[str], kind: str) -> None:
    """磁盘版本比调用方手里的新 → 409 拒绝覆盖（尽力而为，取舍见 `taskio`）。"""
    current = taskio.version_conflict(path, expected)
    if current is not None:
        raise HTTPException(409, {
            "conflict": True,
            "message": f"磁盘上的{kind}文件比你载入的版本更新（AI/MCP 或手改），已拒绝覆盖",
            "current_mtime_ns": current,
        })
