"""后台运行管理：照抄 mcp_server 的单 run 模式 + WebSocket 事件推送。

引擎是带 per-run 实例状态的单例，同一时刻只允许一个后台 run（与 MCP 相同
约束；两个进程之间不互斥，README 已注明勿同时跑）。

事件流：引擎 worker 线程里的 on_step/on_progress 回调 → _emit →
loop.call_soon_threadsafe 送进每个 WebSocket 连接各自的 asyncio.Queue。
事件带自增 seq，全量留存在 state["events"]（封顶），断线重连时快照即补拉。

停止：优先走 TaskEngine.request_stop()（干净停止——经 _finish 收尾，
report/证据链完整）。任务 run 直接以 "stopped" 终态返回完整 result；
套件 run 里当前 case 干净收尾后，由 on_progress 抛 RunStopped 中止
编排（否则 on_case_failure 策略会把停掉的 case 当失败去冷启动重试）。
引擎没有 request_stop 时退回旧的 on_step 抛异常方案（绕过 _finish）。
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Dict, List, Optional

from backend.autoplayqa import get_runtime, logger

from task.suite_runner import SuiteRunner
from task.task_loader import get_task_path, load_suite, load_task

MAX_EVENTS = 2000

STOP_WARNING_LEGACY = (
    "引擎无干净停止支持：本次 run 在节点边界被中断，未走 _finish，"
    "report 不会生成，录制线程可能延续到下次 run。"
)
STOP_NOTE_SUITE = "套件已中止：当前 case 已干净收尾（报告完整），套件级汇总不生成。"


class RunStopped(Exception):
    pass


class RunManager:
    def __init__(self) -> None:
        self._runs: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---------- 查询 ----------

    def list_runs(self) -> List[Dict]:
        with self._lock:
            return [self._summary(s) for s in self._runs.values()]

    def get(self, run_id: str, with_events_since: int = -1) -> Optional[Dict]:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            out = self._summary(state)
            if with_events_since >= 0:
                out["events"] = [e for e in state["events"]
                                 if e["seq"] > with_events_since]
            if state["status"] != "running":
                out["result"] = state["result"]
            return out

    def _summary(self, state: Dict) -> Dict:
        end = state["ended_at"] or time.time()
        out = {
            "run_id": state["run_id"],
            "kind": state["kind"],
            "status": state["status"],
            "device_id": state["device_id"],
            "name": state["name"],
            "current_node": state["current_node"],
            "steps": state["steps"],
            "elapsed_s": round(end - state["started_at"], 1),
            "started_at": state["started_at"],
            "error": state["error"],
            "last_seq": state["events"][-1]["seq"] if state["events"] else 0,
        }
        if state["kind"] == "suite":
            out.update({k: state[k] for k in
                        ("case", "case_index", "cases_total", "cases_done")})
        return out

    # ---------- 事件 ----------

    def _emit(self, state: Dict, event: Dict) -> None:
        with self._lock:
            state["seq"] += 1
            event = {"seq": state["seq"], "ts": time.time(), **event}
            state["events"].append(event)
            if len(state["events"]) > MAX_EVENTS:
                del state["events"][: len(state["events"]) - MAX_EVENTS]
            queues = list(self._subscribers.get(state["run_id"], ()))
        if self._loop is not None:
            for q in queues:
                self._loop.call_soon_threadsafe(self._put_nowait, q, event)

    @staticmethod
    def _put_nowait(q: asyncio.Queue, event: Dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 慢消费者丢事件；快照兜底

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            queues = self._subscribers.get(run_id)
            if queues and q in queues:
                queues.remove(q)

    # ---------- 停止 ----------

    def request_stop(self, run_id: str) -> Optional[Dict]:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            if state["status"] != "running":
                return {"ok": False, "status": state["status"],
                        "error": "run 已结束"}
            state["stop_requested"] = True
        engine_stop = getattr(get_runtime().engine, "request_stop", None)
        if engine_stop is not None:
            engine_stop()
            note = (STOP_NOTE_SUITE if state["kind"] == "suite"
                    else "将在节点边界干净停止（报告完整）。")
            return {"ok": True, "status": "stop_requested", "note": note}
        return {"ok": True, "status": "stop_requested", "warning": STOP_WARNING_LEGACY}

    # ---------- 启动 ----------

    def start(self, kind: str, name: str, device_id: str,
              start_after: Optional[str] = None,
              export_to: Optional[str] = None) -> Dict:
        rt = get_runtime()
        with self._lock:
            active = next((r for r in self._runs.values()
                           if r["status"] == "running"), None)
            if active is not None:
                return {"ok": False,
                        "error": f"已有运行中的 run（{active['run_id']}，"
                                 f"{active['name']}），等它结束或先停止"}
            run_id = uuid.uuid4().hex[:12]
            state = {
                "run_id": run_id, "kind": kind, "status": "running",
                "device_id": device_id, "name": name,
                "current_node": None, "steps": 0,
                "case": None, "case_index": 0, "cases_total": 0, "cases_done": 0,
                "started_at": time.time(), "ended_at": None,
                "result": None, "error": None,
                "stop_requested": False, "seq": 0, "events": [],
            }
            self._runs[run_id] = state

        if kind == "suite":
            target = self._suite_worker
        else:
            target = self._task_worker
        threading.Thread(
            target=target, name=f"pe-run-{run_id}", daemon=True,
            args=(state, rt, name, device_id, start_after, export_to),
        ).start()
        self._start_sampler(state, rt)
        return {"ok": True, "run_id": run_id, "status": "running"}

    def _finish(self, state: Dict, status: str, result=None, error=None) -> None:
        with self._lock:
            state["status"] = status
            state["result"] = result
            state["error"] = error
            state["ended_at"] = time.time()
        self._emit(state, {"type": "end", "status": status,
                           "result": result, "error": error})

    def _task_worker(self, state, rt, name, device_id, start_after, export_to):
        engine_has_stop = hasattr(rt.engine, "request_stop")

        def on_step(node_name: str) -> None:
            # 引擎无 request_stop 时的兜底：抛异常在节点边界中断（绕过 _finish）
            if state["stop_requested"] and not engine_has_stop:
                raise RunStopped()
            with self._lock:
                state["current_node"] = node_name
                state["steps"] += 1
                steps = state["steps"]
            self._emit(state, {"type": "node", "node": node_name, "steps": steps})

        try:
            task = load_task(get_task_path(name))
            result = rt.engine.run(device_id, task, start_after=start_after,
                                   task_name=name, on_step=on_step)
            if export_to and result.get("findings") and rt.findings is not None:
                export_path = rt.findings.export_run(export_to)
                if result.get("report") is not None:
                    result["report"]["export_path"] = (
                        export_path or result["report"].get("export_path"))
            run_status = result.get("status")
            if state["stop_requested"]:
                # 干净停止：引擎经 _finish 返回（error=STOP_ERROR），报告完整
                self._finish(state, "stopped", result=result,
                             error=result.get("error"))
                return
            terminal = ("agent_required" if run_status == "agent_required"
                        else "error" if run_status == "failed" else "done")
            self._finish(state, terminal, result=result,
                         error=result.get("error"))
        except RunStopped:
            self._finish(state, "stopped", error=STOP_WARNING_LEGACY)
        except Exception as exc:  # noqa: BLE001 — 终态经 status 上报
            logger.exception("PipelineEditor run %s failed", state["run_id"])
            self._finish(state, "error", error=str(exc))

    def _suite_worker(self, state, rt, name, device_id, start_after, export_to):
        def on_progress(event: Dict) -> None:
            # 停止套件：当前 case 已被 engine.request_stop 干净收尾；这里在
            # case 边界中止编排本身，否则 on_case_failure 会把停掉的 case
            # 当失败去冷启动重试
            if state["stop_requested"] and event.get("event") != "node":
                raise RunStopped()
            with self._lock:
                etype = event.get("event")
                if etype == "case_start":
                    state["case"] = event.get("case")
                    state["case_index"] = event.get("index", 0)
                    state["current_node"] = None
                elif etype == "node":
                    state["current_node"] = event.get("node")
                    state["steps"] += 1
                elif etype == "case_end" and not event.get("will_retry"):
                    state["cases_done"] += 1
            self._emit(state, {"type": "suite_progress", **event})

        try:
            suite = load_suite(name)
            with self._lock:
                state["cases_total"] = len(suite["cases"])
            runner = SuiteRunner(rt.engine, logger, findings_recorder=rt.findings)
            result = runner.run(device_id, suite, export_to=export_to,
                                on_progress=on_progress)
            status = "error" if result.get("aborted_at") else "done"
            if state["stop_requested"]:
                status = "stopped"
            self._finish(state, status, result=result)
        except RunStopped:
            self._finish(state, "stopped", error=STOP_NOTE_SUITE)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PipelineEditor suite run %s failed", state["run_id"])
            self._finish(state, "error", error=str(exc))

    def _start_sampler(self, state: Dict, rt) -> None:
        """每 2s 采样引擎的飞行记录（popup 消除/恢复/watchdog 等在这里可见）。"""

        def sampler() -> None:
            last: List = []
            while True:
                with self._lock:
                    if state["status"] != "running":
                        return
                try:
                    events = rt.engine.recent_events()
                except Exception:  # noqa: BLE001 — 采样失败不致命
                    events = []
                if events and events != last:
                    last = list(events)
                    self._emit(state, {"type": "recent_events", "events": events})
                time.sleep(2)

        threading.Thread(target=sampler, name=f"pe-sampler-{state['run_id']}",
                         daemon=True).start()


run_manager = RunManager()
