"""全局事件总线 + 任务文件 mtime 监视。

目的：AI（经内嵌 MCP）或任何外部进程改了 task_definitions/*.json，
编辑器前端能实时看到。事件源统一走文件 mtime 轮询——不管写入来自
内嵌 MCP、编辑器自己的保存、AutoPlayQA 的 stdio MCP 还是手改文件，
一律触发 task_changed；"是不是自己刚保存的"由前端比对内容判断。

断线补齐：事件带自增 seq，并留一份有限历史缓冲（HISTORY_MAX 条）。
WS 连接带 `?since_seq=` 重连时，`subscribe()` 在同一把锁里完成
「先订阅 + 取补发列表」，保证补发与实时流之间不丢不重（与 runmgr 的
「先订阅后快照」同构）。缓冲已淘汰导致补不齐时置 resync，由前端做一次
全量刷新。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Deque, Dict, List, NamedTuple, Optional

from backend.autoplayqa import SUITE_DIR, TASK_DIR

HISTORY_MAX = 200


class Subscription(NamedTuple):
    """一次订阅的结果：队列 + 需要先补发的历史事件 + 补齐失败标志。"""

    queue: asyncio.Queue
    missed: List[Dict]
    resync: bool
    last_seq: int


class EventBus:
    """线程安全的一对多广播：worker/watcher 线程发，WS 连接收。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seq = 0
        self._history: Deque[Dict] = deque(maxlen=HISTORY_MAX)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self, since_seq: int = -1) -> Subscription:
        """订阅事件流；`since_seq >= 0` 时同时取回缓冲内 seq > since_seq 的事件。

        订阅与取历史在同一把锁内完成：emit 也持同一把锁，所以任一事件要么
        已在 missed 里（此时该连接还没进 _subscribers，不会重复入队），要么
        走实时队列，二者互斥。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
            last_seq = self._seq
            if since_seq < 0:
                return Subscription(q, [], False, last_seq)
            if since_seq > last_seq:
                # 后端重启过（seq 从 0 重新计数），旧 seq 无从对齐
                return Subscription(q, [], True, last_seq)
            if self._history and self._history[0]["seq"] > since_seq + 1:
                # 断点之后的事件已被挤出缓冲，补不齐
                return Subscription(q, [], True, last_seq)
            missed = [e for e in self._history if e["seq"] > since_seq]
            return Subscription(q, missed, False, last_seq)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, event: Dict) -> None:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), **event}
            self._history.append(event)
            queues = list(self._subscribers)
        if self._loop is None:
            return
        for q in queues:
            self._loop.call_soon_threadsafe(self._put_nowait, q, event)

    @staticmethod
    def _put_nowait(q: asyncio.Queue, event: Dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


event_bus = EventBus()


def start_task_watcher(interval_s: float = 1.5) -> None:
    """轮询任务/套件 JSON 的 mtime，变更即广播（30 个文件量级，成本可忽略）。"""

    def snapshot() -> Dict[str, float]:
        out: Dict[str, float] = {}
        for base, kind in ((TASK_DIR, "task"), (SUITE_DIR, "suite")):
            if not base.is_dir():
                continue
            for p in base.glob("*.json"):
                try:
                    out[f"{kind}:{p.stem}"] = p.stat().st_mtime
                except OSError:
                    pass
        return out

    def watcher() -> None:
        last = snapshot()
        while True:
            time.sleep(interval_s)
            current = snapshot()
            for key, mtime in current.items():
                kind, name = key.split(":", 1)
                if key not in last:
                    event_bus.emit({"type": f"{kind}_changed", "name": name,
                                    "change": "created"})
                elif mtime != last[key]:
                    event_bus.emit({"type": f"{kind}_changed", "name": name,
                                    "change": "modified"})
            for key in last:
                if key not in current:
                    kind, name = key.split(":", 1)
                    event_bus.emit({"type": f"{kind}_changed", "name": name,
                                    "change": "deleted"})
            last = current

    threading.Thread(target=watcher, name="pe-task-watcher", daemon=True).start()
