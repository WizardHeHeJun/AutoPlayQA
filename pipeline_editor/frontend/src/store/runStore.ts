/** 运行态（WS 事件的落点）。不入 undo 历史，不影响 doc。 */
import { create } from "zustand";

import type { RunEvent, RunSummary, WsMessage } from "../types/api";

export interface RunState {
  runId: string | null;
  kind: "task" | "suite" | null;
  status: RunSummary["status"] | null;
  deviceId: string | null;
  name: string | null;
  currentNode: string | null;
  visited: Set<string>;
  steps: number;
  case: string | null;
  caseIndex: number;
  casesTotal: number;
  casesDone: number;
  events: RunEvent[];
  engineEvents: unknown[];
  result: Record<string, unknown> | null;
  error: string | null;
  lastSeq: number;
  wsConnected: boolean;

  beginRun: (runId: string, kind: "task" | "suite", name: string, deviceId: string) => void;
  applyMessage: (msg: WsMessage) => void;
  setWsConnected: (connected: boolean) => void;
  clear: () => void;
}

const EMPTY = {
  runId: null,
  kind: null,
  status: null,
  deviceId: null,
  name: null,
  currentNode: null,
  visited: new Set<string>(),
  steps: 0,
  case: null,
  caseIndex: 0,
  casesTotal: 0,
  casesDone: 0,
  events: [] as RunEvent[],
  engineEvents: [] as unknown[],
  result: null,
  error: null,
  lastSeq: 0,
  wsConnected: false,
} as const;

export const useRunStore = create<RunState>()((set, get) => ({
  ...EMPTY,
  visited: new Set(),
  events: [],
  engineEvents: [],

  beginRun: (runId, kind, name, deviceId) =>
    set({
      ...EMPTY,
      visited: new Set(),
      events: [],
      engineEvents: [],
      runId,
      kind,
      name,
      deviceId,
      status: "running",
    }),

  applyMessage: (msg) => {
    if (msg.type === "snapshot") {
      const snap = msg as { type: "snapshot" } & RunSummary;
      const visited = new Set<string>();
      let currentNode: string | null = snap.current_node;
      for (const e of snap.events ?? []) {
        if (e.type === "node") visited.add(e.node);
      }
      set({
        runId: snap.run_id,
        kind: snap.kind,
        status: snap.status,
        deviceId: snap.device_id,
        name: snap.name,
        currentNode,
        visited,
        steps: snap.steps,
        case: snap.case ?? null,
        caseIndex: snap.case_index ?? 0,
        casesTotal: snap.cases_total ?? 0,
        casesDone: snap.cases_done ?? 0,
        events: (snap.events ?? []).filter((e) => e.type !== "recent_events"),
        result: snap.result ?? null,
        error: snap.error,
        lastSeq: snap.last_seq,
      });
      return;
    }
    const event = msg as RunEvent;
    const s = get();
    if (event.seq <= s.lastSeq) return; // 快照已覆盖
    const patch: Partial<RunState> = { lastSeq: event.seq };
    if (event.type === "recent_events") {
      patch.engineEvents = event.events;
    } else {
      patch.events = [...s.events, event].slice(-2000);
    }
    if (event.type === "node") {
      patch.currentNode = event.node;
      patch.steps = event.steps;
      patch.visited = new Set(s.visited).add(event.node);
    } else if (event.type === "suite_progress") {
      const e = event as Record<string, unknown>;
      if (e.event === "case_start") {
        patch.case = e.case as string;
        patch.caseIndex = (e.index as number) ?? 0;
        patch.currentNode = null;
        patch.visited = new Set();
      } else if (e.event === "node") {
        patch.currentNode = e.node as string;
        patch.visited = new Set(s.visited).add(e.node as string);
      } else if (e.event === "case_end" && !e.will_retry) {
        patch.casesDone = s.casesDone + 1;
      }
    } else if (event.type === "end") {
      patch.status = event.status;
      patch.result = event.result;
      patch.error = event.error;
    }
    set(patch);
  },

  setWsConnected: (connected) => set({ wsConnected: connected }),
  clear: () => set({ ...EMPTY, visited: new Set(), events: [], engineEvents: [] }),
}));
