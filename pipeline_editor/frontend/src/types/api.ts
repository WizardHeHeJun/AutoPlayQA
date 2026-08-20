import type { TaskDoc } from "./task";

export interface TaskListItem {
  name: string;
  entry: string | null;
  node_count: number | null;
  includes: string[] | null;
  mtime: number | null;
}

export interface StructuredError {
  scope: "task" | "node" | "watchdog" | "popup" | "suite";
  node: string | null;
  message: string;
}

/**
 * 文件版本令牌（后端的 `mtime_ns` 十进制字符串），保存时回传即开启乐观并发控制。
 * **是字符串不是数字**：mtime_ns ≈ 1.7e18 超过 Number.MAX_SAFE_INTEGER，
 * 当数字收会被舍入，回传永远对不上 → 保存永久 409。前端只存不算。
 */
export type FileVersion = string | null;

export interface TaskDetail {
  name: string;
  raw: TaskDoc;
  resolved: TaskDoc | null;
  error: StructuredError | null;
  mtime_ns: FileVersion;
}

export interface LintWarning {
  rule_id: string;
  node: string | null;
  message: string;
  suggestion: string;
}

export interface ValidateResult {
  ok: boolean;
  error?: StructuredError;
  node_count?: number;
  steps?: Record<string, string>;
  merge?: TaskDoc["_merge"];
}

export interface SaveResult {
  ok: boolean;
  error?: StructuredError;
  path?: string;
  nodes?: number;
  lint_warnings?: LintWarning[];
  /** 写盘后的新版本令牌，直接作为下一次保存的基线。 */
  mtime_ns?: FileVersion;
}

export interface SchemaInfo {
  schema_doc: string;
  recognition_types: string[];
  watchdog_types: string[];
  combo_types: string[];
  combo_sub_types: string[];
  combo_list_key: Record<string, string>;
  max_combo_depth: number;
  action_types: string[];
  repeatable_action_types: string[];
  repeat_param_keys: string[];
  popup_action_types: string[];
  severities: string[];
  task_default_keys: string[];
  conflict_strategies: string[];
  suite_failure_policies: string[];
  custom_actions: string[];
}

/** custom action 参数控件类型（后端 `action_schema_introspect.PARAM_TYPES`）。 */
export type CustomActionParamType =
  | "int"
  | "float"
  | "str"
  | "bool"
  | "enum"
  | "roi"
  | "point"
  | "json";

/**
 * 后端从 handler 源码静态提取的单个参数描述。
 * `default` **只用于 placeholder 展示**，绝不回写进任务 JSON 的 params——
 * 那等于把 handler 默认值固化进任务文件（同「defaults 不展开」红线）。
 */
export interface CustomActionParam {
  key: string;
  type: CustomActionParamType;
  default: unknown;
  default_unresolved: boolean;
  choices: string[] | null;
  required: boolean;
  /**
   * handler docstring 的 `params` 块里这个 key 的说明（原文，多为英文）。
   * docstring 没写 / 格式不认识时为 null——表单据此决定要不要挂 Tooltip。
   */
  description?: string | null;
}

export interface CustomActionSchema {
  name: string;
  /** 提取失败/认不出写法时为空数组，前端据此退回纯 JSON 编辑。 */
  params: CustomActionParam[];
}

export interface TemplateInfo {
  name: string;
  file: string;
  size: number;
  mtime: number;
}

export interface DeviceInfo {
  device_id: string;
  type: string;
  model: string;
}

export interface IncludeInfo {
  path: string;
  description: string | null;
  node_names: string[];
}

export interface SuiteListItem {
  name: string;
  cases: string[] | null;
  mtime: number | null;
}

export interface ReportListItem {
  date: string;
  device: string;
  run_id: string;
  has_html: boolean;
  mtime: number;
  task?: string | null;
  status?: string | null;
  finding_count?: number;
  severity_counts?: Record<string, number>;
}

export interface LayoutData {
  nodes: Record<string, { x: number; y: number }>;
  viewport?: { x: number; y: number; zoom: number };
}

export interface RunSummary {
  run_id: string;
  kind: "task" | "suite";
  status: "running" | "done" | "error" | "agent_required" | "stopped";
  device_id: string;
  name: string;
  current_node: string | null;
  steps: number;
  elapsed_s: number;
  started_at: number;
  error: string | null;
  last_seq: number;
  case?: string | null;
  case_index?: number;
  cases_total?: number;
  cases_done?: number;
  result?: Record<string, unknown> | null;
  events?: RunEvent[];
}

export type RunEvent = {
  seq: number;
  ts: number;
} & (
  | { type: "node"; node: string; steps: number }
  | { type: "suite_progress"; event: string; [key: string]: unknown }
  | { type: "recent_events"; events: unknown[] }
  | {
      type: "end";
      status: RunSummary["status"];
      result: Record<string, unknown> | null;
      error: string | null;
    }
);

export type WsMessage = ({ type: "snapshot" } & RunSummary) | RunEvent;
