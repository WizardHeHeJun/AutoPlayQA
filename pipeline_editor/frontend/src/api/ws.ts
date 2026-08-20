/** 运行 WebSocket 客户端：断线指数退避重连；重连后快照自动补齐（seq 去重）。 */
import { useRunStore } from "../store/runStore";
import type { WsMessage } from "../types/api";

export class RunSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private retry = 0;

  constructor(private runId: string) {}

  connect(): void {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/runs/${this.runId}`);
    this.ws.onopen = () => {
      this.retry = 0;
      useRunStore.getState().setWsConnected(true);
    };
    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WsMessage;
        useRunStore.getState().applyMessage(msg);
        if ("type" in msg && msg.type === "end") this.close();
      } catch {
        /* 忽略坏帧 */
      }
    };
    this.ws.onclose = () => {
      useRunStore.getState().setWsConnected(false);
      if (this.closed) return;
      const state = useRunStore.getState();
      if (state.status !== "running") return; // 已终态，无需重连
      const delay = Math.min(1000 * 2 ** this.retry, 10_000);
      this.retry += 1;
      setTimeout(() => this.connect(), delay);
    };
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
    this.ws = null;
  }
}
