"""FastAPI 装配：CORS、异常映射、路由挂载、事件循环捕获。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import backend.autoplayqa  # noqa: F401  (必须最先 import：sys.path + chdir)

import asyncio
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.events import event_bus, start_task_watcher
from backend.mcp_embed import mcp
from backend.routers import meta, perception, reports, runs, suites, tasks, tools
from backend.runmgr import run_manager

from task.task_loader import SuiteValidationError, TaskValidationError

# 内嵌 MCP（streamable HTTP）：AI 编辑与人共用同一后端与画布
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # worker 线程经 call_soon_threadsafe 往 WebSocket 队列送事件，需要主循环句柄
    loop = asyncio.get_running_loop()
    run_manager.set_loop(loop)
    event_bus.set_loop(loop)
    start_task_watcher()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="PipelineEditor Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(TaskValidationError)
async def task_validation_handler(request: Request, exc: TaskValidationError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(SuiteValidationError)
async def suite_validation_handler(request: Request, exc: SuiteValidationError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


app.include_router(tasks.router)
app.include_router(suites.router)
app.include_router(meta.router)
app.include_router(perception.router)
app.include_router(runs.router)
app.include_router(reports.router)
app.include_router(tools.router)

app.mount("/mcp", mcp_app)


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, since_seq: int = -1) -> None:
    """全局事件流：task_changed / suite_changed（AI 或外部改文件时前端实时刷新）。

    重连带 `?since_seq=<已收到的最大 seq>`：先补发缓冲内漏掉的事件再进实时流
    （订阅与取补发列表在 EventBus 内同锁完成，不丢不重）。缓冲已淘汰补不齐时
    先发一帧 `{"type": "resync"}`，由前端做一次全量刷新。
    """
    await websocket.accept()
    sub = event_bus.subscribe(since_seq)
    try:
        if sub.resync:
            await websocket.send_json({
                "type": "resync", "seq": sub.last_seq, "ts": time.time(),
                "reason": "history_unavailable",
            })
        for event in sub.missed:
            await websocket.send_json(event)
        while True:
            event = await sub.queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(sub.queue)


@app.get("/api/health")
def health():
    from backend.autoplayqa import AUTOPLAYQA_ROOT
    return {"ok": True, "autoplayqa_root": str(AUTOPLAYQA_ROOT)}
