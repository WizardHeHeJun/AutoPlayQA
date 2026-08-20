import type { SuiteDoc } from "../types/suite";
import type { TaskDoc } from "../types/task";
import type {
  CustomActionSchema,
  DeviceInfo,
  FileVersion,
  IncludeInfo,
  LayoutData,
  LintWarning,
  ReportListItem,
  RunSummary,
  SaveResult,
  SchemaInfo,
  StructuredError,
  SuiteListItem,
  TaskDetail,
  TaskListItem,
  TemplateInfo,
  ValidateResult,
} from "../types/api";
import { api } from "./client";

export const tasksApi = {
  list: () => api.get<TaskListItem[]>("/api/tasks"),
  get: (name: string) => api.get<TaskDetail>(`/api/tasks/${encodeURIComponent(name)}`),
  /** `baseMtimeNs` = 载入/上次保存拿到的版本令牌；给了就开乐观锁（冲突 → 409）。 */
  save: (name: string, task: TaskDoc, baseMtimeNs?: FileVersion) =>
    api.put<SaveResult>(`/api/tasks/${encodeURIComponent(name)}`, {
      task,
      base_mtime_ns: baseMtimeNs ?? null,
    }),
  delete: (name: string, force = false) =>
    api.delete<{ ok: boolean; referrers: string[] }>(
      `/api/tasks/${encodeURIComponent(name)}${force ? "?force=true" : ""}`,
    ),
  rename: (name: string, newName: string) =>
    api.post<{ ok: boolean; referrers: string[] }>(
      `/api/tasks/${encodeURIComponent(name)}/rename`,
      { new_name: newName },
    ),
  validate: (task: TaskDoc) => api.post<ValidateResult>("/api/validate", { task }),
  lint: (task: TaskDoc) =>
    api.post<{ ok: boolean; lint_warnings?: LintWarning[]; error?: StructuredError }>(
      "/api/lint",
      { task },
    ),
  renumber: (name: string) =>
    api.post<{ ok: boolean; path: string; count: number; mtime_ns?: FileVersion }>(
      `/api/tasks/${encodeURIComponent(name)}/renumber`,
    ),
  getLayout: (name: string) =>
    api.get<LayoutData>(`/api/tasks/${encodeURIComponent(name)}/layout`),
  saveLayout: (name: string, layout: LayoutData) =>
    api.put<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(name)}/layout`, layout),
};

export const includesApi = {
  list: () => api.get<IncludeInfo[]>("/api/includes"),
  get: (path: string) =>
    api.get<{ path: string; data: { nodes: Record<string, unknown> } }>(
      `/api/includes/${path}`,
    ),
};

export const metaApi = {
  schema: () => api.get<SchemaInfo>("/api/schema"),
  customActions: () => api.get<string[]>("/api/custom-actions"),
  /** handler 源码静态提取的参数表；未注册的 name → 404（调用方静默降级到 JSON 编辑）。 */
  customActionSchema: (name: string) =>
    api.get<CustomActionSchema>(
      `/api/custom-actions/${encodeURIComponent(name)}/schema`,
    ),
  templates: () => api.get<TemplateInfo[]>("/api/templates"),
  templateImageUrl: (name: string) =>
    `/api/templates/${encodeURIComponent(name)}/image`,
  yoloClasses: (model?: string) =>
    api.get<{ available: boolean; classes: Record<string, string>; models: string[] }>(
      `/api/yolo-classes${model ? `?model=${encodeURIComponent(model)}` : ""}`,
    ),
  devices: () => api.get<DeviceInfo[]>("/api/devices"),
};

export const suitesApi = {
  list: () => api.get<SuiteListItem[]>("/api/suites"),
  get: (name: string) =>
    api.get<{ name: string; raw: SuiteDoc; error: string | null; mtime_ns: FileVersion }>(
      `/api/suites/${encodeURIComponent(name)}`,
    ),
  /** `baseMtimeNs` 同 tasksApi.save：给了就开乐观锁（冲突 → 409）。 */
  save: (name: string, suite: SuiteDoc, baseMtimeNs?: FileVersion) =>
    api.put<{ ok: boolean; error?: string; mtime_ns?: FileVersion }>(
      `/api/suites/${encodeURIComponent(name)}`,
      { suite, base_mtime_ns: baseMtimeNs ?? null },
    ),
  delete: (name: string) =>
    api.delete<{ ok: boolean }>(`/api/suites/${encodeURIComponent(name)}`),
};

export const perceptionApi = {
  screenshot: (deviceId: string) =>
    api.post<{ width: number; height: number; image_base64: string }>(
      "/api/screenshot",
      { device_id: deviceId },
    ),
  ocr: (deviceId: string, roi?: number[]) =>
    api.post<{ text: string; score: number; bbox: number[]; center: number[] }[]>(
      "/api/ocr",
      { device_id: deviceId, roi },
    ),
  findText: (deviceId: string, text: string) =>
    api.post<{
      found: boolean;
      center: number[] | null;
      score: number;
      channel: string | null;
      matched_text: string | null;
    }>("/api/find-text", { device_id: deviceId, text }),
  findTemplate: (
    deviceId: string,
    template: string,
    opts?: { threshold?: number; roi?: number[]; scales?: number[]; multi?: boolean },
  ) =>
    api.post<{
      found: boolean;
      count: number;
      matches: { score: number; bbox: number[]; center: number[] }[];
      error?: string;
    }>("/api/find-template", { device_id: deviceId, template, ...opts }),
  captureTemplate: (deviceId: string, name: string, region: number[]) =>
    api.post<{ ok: boolean; name: string; path: string }>("/api/capture-template", {
      device_id: deviceId,
      name,
      region,
    }),
};

export const runsApi = {
  start: (body: {
    kind: "task" | "suite";
    name: string;
    device_id: string;
    start_after?: string;
    export_to?: string;
  }) => api.post<{ ok: boolean; run_id: string; status: string }>("/api/runs", body),
  list: () => api.get<RunSummary[]>("/api/runs"),
  get: (runId: string, since = -1) =>
    api.get<RunSummary>(`/api/runs/${runId}${since >= 0 ? `?since=${since}` : ""}`),
  stop: (runId: string) =>
    api.delete<{ ok: boolean; status: string; warning?: string; note?: string }>(
      `/api/runs/${runId}`,
    ),
};

export interface SessionInfo {
  dir: string;
  device_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  context: { kind?: string; task?: string | null; node?: string | null };
  step_count: number;
  anchored_steps: number;
}

export interface HealthNode {
  runs_seen: number;
  direct_hits: number;
  timeout_recoveries: number;
  popup_assisted_hits: number;
  drift_count: number;
  fallback_rate: number;
  [key: string]: unknown;
}

export interface HealthTask {
  runs: number;
  nodes: Record<string, HealthNode>;
  timeline: {
    date: string;
    runs: number;
    direct_hits: number;
    timeout_recoveries: number;
    popup_assisted_hits: number;
    drift_count: number;
  }[];
}

export interface HandoffNode {
  sessions: number;
  signatures: number;
  dominant_ratio: number;
  dominant_signature: [string, string | null][];
  solidify_candidate: boolean;
}

export const toolsApi = {
  sessions: (params?: { kind?: string; task?: string; node?: string }) => {
    const q = new URLSearchParams();
    if (params?.kind) q.set("kind", params.kind);
    if (params?.task) q.set("task", params.task);
    if (params?.node) q.set("node", params.node);
    const qs = q.toString();
    return api.get<SessionInfo[]>(`/api/sessions${qs ? `?${qs}` : ""}`);
  },
  session: (dir: string) =>
    api.get<Record<string, unknown>>(`/api/sessions/${encodeURIComponent(dir)}`),
  frameUrl: (dir: string, file: string) =>
    `/api/sessions/${encodeURIComponent(dir)}/frames/${encodeURIComponent(file)}`,
  toDraft: (dir: string, namePrefix?: string) =>
    api.post<{
      ok: boolean;
      draft?: TaskDoc;
      node_count?: number;
      blind_clicks?: number;
      error?: string;
    }>(`/api/sessions/${encodeURIComponent(dir)}/to-draft`, {
      name_prefix: namePrefix,
    }),
  health: (params?: { task?: string; days?: number }) => {
    const q = new URLSearchParams();
    if (params?.task) q.set("task", params.task);
    if (params?.days) q.set("days", String(params.days));
    const qs = q.toString();
    return api.get<Record<string, HealthTask>>(`/api/health${qs ? `?${qs}` : ""}`);
  },
  handoffs: (params?: { task?: string; days?: number }) => {
    const q = new URLSearchParams();
    if (params?.task) q.set("task", params.task);
    if (params?.days) q.set("days", String(params.days));
    const qs = q.toString();
    return api.get<Record<string, Record<string, HandoffNode>>>(
      `/api/handoffs${qs ? `?${qs}` : ""}`,
    );
  },
  replayCache: () =>
    api.get<{ enabled: boolean; size: number; path: string | null }>(
      "/api/replay-cache",
    ),
  clearReplayCache: () =>
    api.delete<{ cleared: number; note?: string }>("/api/replay-cache"),
};

export const reportsApi = {
  list: (params?: { date?: string; device?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.date) q.set("date", params.date);
    if (params?.device) q.set("device", params.device);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return api.get<ReportListItem[]>(`/api/reports${qs ? `?${qs}` : ""}`);
  },
  get: (date: string, device: string, runId: string) =>
    api.get<Record<string, unknown>>(`/api/reports/${date}/${device}/${runId}`),
  htmlUrl: (date: string, device: string, runId: string) =>
    `/api/reports/${date}/${device}/${runId}/html`,
  fileUrl: (date: string, device: string, runId: string, path: string) =>
    `/api/reports/${date}/${device}/${runId}/files/${path}`,
};
