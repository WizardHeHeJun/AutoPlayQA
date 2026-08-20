/** 全局事件 WebSocket（/ws/events）：task_changed / suite_changed。
 * AI 经内嵌 MCP 保存、或任何外部进程改了任务文件，前端由此实时感知。
 *
 * 断线补齐：记录已收到的最大 seq，重连时带 `?since_seq=`，后端补发缓冲内漏掉的
 * 事件（补发与实时流由后端同锁保证不丢不重，这里再按 seq 去重一层）。后端缓冲
 * 已淘汰或重启过时会先发一帧 `{type:"resync"}` —— 这条不当普通事件派发，改调
 * onResync，由使用方做一次全量刷新。
 */

export interface GlobalEvent {
  seq: number;
  ts: number;
  type: "task_changed" | "suite_changed" | string;
  name?: string;
  change?: "created" | "modified" | "deleted";
}

export interface EventsSocketOptions {
  /** 事件补齐失败（缓冲淘汰 / 后端重启）：使用方应做一次全量刷新 */
  onResync?: () => void;
}

export class EventsSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private retry = 0;
  /** 已处理的最大 seq；-1 = 尚未收到过事件（首连不带 since_seq） */
  private lastSeq = -1;

  constructor(
    private onEvent: (e: GlobalEvent) => void,
    private options: EventsSocketOptions = {},
  ) {}

  connect(): void {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const query = this.lastSeq >= 0 ? `?since_seq=${this.lastSeq}` : "";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/events${query}`);
    this.ws.onopen = () => {
      this.retry = 0;
    };
    this.ws.onmessage = (e) => {
      try {
        this.handle(JSON.parse(e.data) as GlobalEvent);
      } catch {
        /* 忽略坏帧 */
      }
    };
    this.ws.onclose = () => {
      if (this.closed) return;
      const delay = Math.min(1000 * 2 ** this.retry, 15_000);
      this.retry += 1;
      setTimeout(() => this.connect(), delay);
    };
  }

  private handle(event: GlobalEvent): void {
    if (event.type === "resync") {
      // 后端 seq 已对不上：从它给的 last_seq 重新起算，剩下交给全量刷新
      this.lastSeq = typeof event.seq === "number" ? event.seq : -1;
      this.options.onResync?.();
      return;
    }
    if (typeof event.seq === "number") {
      if (event.seq <= this.lastSeq) return; // 补发与实时流重叠时去重
      this.lastSeq = event.seq;
    }
    this.onEvent(event);
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
    this.ws = null;
  }
}
