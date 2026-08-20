"""编辑辅助工具：录制会话→草稿 / 任务健康度 / 交接固化 / 回放缓存。

从 AutoPlayQA 的 CLI/内部函数迁移（cli_handler 的 task health / task
handoffs / task cache + 此前无任何调用方的 task_editor.action_log_to_draft）。
除缓存外全部只读、不依赖 runtime（无 adb 也可用）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

import backend.autoplayqa  # noqa: F401
from backend.autoplayqa import AUTOPLAYQA_ROOT, config, get_runtime, logger

from task.anchor_health import scan_health
from task.handoff_stats import scan_handoffs
from task.task_editor import action_log_to_draft

router = APIRouter(prefix="/api", tags=["tools"])

SESSIONS_DIR = (
    AUTOPLAYQA_ROOT
    / config.get("recording", {}).get("agent_sessions_dir", "outputs/agent_sessions")
).resolve()
FINDINGS_DIR = (
    AUTOPLAYQA_ROOT
    / config.get("findings", {}).get("output_dir", "outputs/findings")
).resolve()


def _session_dir(name: str) -> Path:
    path = (SESSIONS_DIR / name).resolve()
    if not path.is_relative_to(SESSIONS_DIR):
        raise HTTPException(400, "路径越界")
    if not (path / "session.json").is_file():
        raise HTTPException(404, f"会话不存在: {name}")
    return path


def _read_session(path: Path) -> Optional[Dict]:
    try:
        return json.loads((path / "session.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------- 录制会话 → 草稿 ----------

@router.get("/sessions")
def api_list_sessions(kind: Optional[str] = None, task: Optional[str] = None,
                      node: Optional[str] = None) -> List[Dict]:
    """列出 outputs/agent_sessions/ 下的录制会话（新到旧）。"""
    if not SESSIONS_DIR.is_dir():
        return []
    out: List[Dict] = []
    for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        session = _read_session(d)
        if session is None:
            continue
        context = session.get("context") or {}
        if kind and context.get("kind") != kind:
            continue
        if task and context.get("task") != task:
            continue
        if node and context.get("node") != node:
            continue
        steps = session.get("steps") or []
        anchored = sum(
            1 for s in steps
            if isinstance(s, dict) and isinstance(s.get("element"), dict)
            and (s["element"].get("text") or "").strip()
        )
        out.append({
            "dir": d.name,
            "device_id": session.get("device_id"),
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
            "context": context,
            "step_count": len(steps),
            "anchored_steps": anchored,
        })
    return out


@router.get("/sessions/{name}")
def api_get_session(name: str) -> Dict:
    session = _read_session(_session_dir(name))
    if session is None:
        raise HTTPException(500, "session.json 解析失败")
    return session


@router.get("/sessions/{name}/frames/{file}")
def api_session_frame(name: str, file: str):
    path = (_session_dir(name) / file).resolve()
    base = _session_dir(name)
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@router.post("/sessions/{name}/to-draft")
def api_session_to_draft(name: str, body: Dict = Body(default={})) -> Dict:
    """action_log_to_draft：录制会话 → 识别驱动草稿任务（只返回，不落盘）。

    锚点步骤（元素带文本）转成 ui_text/ocr + target:recognized；无锚点
    步骤退化为坐标点击并打 BLIND_CLICK_COMMENT 标记。补 QA 断言后再保存。
    """
    session = _read_session(_session_dir(name))
    if session is None:
        raise HTTPException(500, "session.json 解析失败")
    prefix = body.get("name_prefix") or "step"
    try:
        draft = action_log_to_draft(session, name_prefix=prefix)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    blind = sum(
        1 for n in draft["nodes"].values()
        if isinstance(n.get("comment"), str) and "盲点" in n["comment"]
    )
    return {"ok": True, "draft": draft, "node_count": len(draft["nodes"]),
            "blind_clicks": blind}


# ---------- 健康度 / 交接固化（只读统计） ----------

@router.get("/health")
def api_health(task: Optional[str] = None, days: Optional[int] = None) -> Dict:
    """按任务聚合 node_stats：fallback_rate 高 = 锚点腐化嫌疑。"""
    return scan_health(str(FINDINGS_DIR), task_name=task, days=days, logger=logger)


@router.get("/handoffs")
def api_handoffs(task: Optional[str] = None, days: Optional[int] = None) -> Dict:
    """agent 交接会话统计：solidify_candidate = 可固化为确定性节点。"""
    return scan_handoffs(str(SESSIONS_DIR), task_name=task, days=days, logger=logger)


# ---------- 回放缓存 ----------

@router.get("/replay-cache")
def api_replay_cache_status() -> Dict:
    cache = get_runtime().replay_cache
    if cache is None:
        return {"enabled": False, "size": 0, "path": None}
    return {"enabled": True, "size": cache.size(), "path": str(cache.path)}


@router.delete("/replay-cache")
def api_replay_cache_clear() -> Dict:
    cache = get_runtime().replay_cache
    if cache is None:
        return {"cleared": 0, "note": "回放缓存未启用（config replay_cache.enabled）"}
    return {"cleared": cache.clear()}
