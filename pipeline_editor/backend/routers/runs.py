"""运行：启动 / 状态 / 停止 / WebSocket 实时推送。"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

import backend.autoplayqa  # noqa: F401
from backend.runmgr import run_manager

router = APIRouter(tags=["runs"])


@router.post("/api/runs")
def api_start_run(body: Dict = Body(...)) -> Dict:
    kind = body.get("kind", "task")
    if kind not in ("task", "suite"):
        raise HTTPException(400, "kind 必须是 task 或 suite")
    name = body.get("name")
    device_id = body.get("device_id")
    if not name or not device_id:
        raise HTTPException(400, "body 需要 {kind, name, device_id}")
    result = run_manager.start(
        kind, name, device_id,
        start_after=body.get("start_after"),
        export_to=body.get("export_to"),
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "启动失败"))
    return result


@router.get("/api/runs")
def api_list_runs() -> List[Dict]:
    return run_manager.list_runs()


@router.get("/api/runs/{run_id}")
def api_get_run(run_id: str, since: int = -1) -> Dict:
    state = run_manager.get(run_id, with_events_since=since)
    if state is None:
        raise HTTPException(404, f"未知 run_id: {run_id}")
    return state


@router.delete("/api/runs/{run_id}")
def api_stop_run(run_id: str) -> Dict:
    result = run_manager.request_stop(run_id)
    if result is None:
        raise HTTPException(404, f"未知 run_id: {run_id}")
    return result


@router.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str) -> None:
    state = run_manager.get(run_id, with_events_since=0)
    if state is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    # 先订阅、后发快照：快照带全量事件（seq 去重交给前端），中间不丢事件
    queue = run_manager.subscribe(run_id)
    try:
        await websocket.send_json({"type": "snapshot", **state})
        if state["status"] != "running":
            return
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event.get("type") == "end":
                return
    except WebSocketDisconnect:
        pass
    finally:
        run_manager.unsubscribe(run_id, queue)
