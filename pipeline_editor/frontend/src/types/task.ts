/** AutoPlayQA 任务数据模型（权威来源：action/action_schema.py + task/task_loader.py）。
 * 编辑器不复刻校验规则——真值在后端 /api/validate；这里的类型只服务编辑体验。
 */

export type Roi = [number, number, number, number];

export type RecognitionType =
  | "always"
  | "ui_text"
  | "ocr"
  | "blank_screen"
  | "template"
  | "feature"
  | "yolo"
  | "and"
  | "or";

/** 识别对象：字段随 type 变化，统一放开可选字段（表单按 type 控制显隐）。 */
export interface Recognition {
  type: RecognitionType;
  expected?: string; // ui_text / ocr
  template?: string; // template / feature
  threshold?: number;
  roi?: Roi;
  scales?: number[]; // template
  grayscale?: boolean; // template
  min_matches?: number; // feature
  ratio?: number; // feature
  label?: string; // yolo
  model?: string; // yolo
  conf?: number; // yolo
  all_of?: Recognition[]; // and
  any_of?: Recognition[]; // or
  box_index?: number; // and
  [key: string]: unknown; // _comment 等透传
}

export type ActionType =
  | "click"
  | "drag"
  | "input_text"
  | "wait"
  | "key"
  | "gesture"
  | "agent"
  | "llm" // agent 的废弃别名，UI 隐藏（读到按 agent 展示，保存保留原值）
  | "none"
  | "custom";

export interface TaskAction {
  type: ActionType;
  target?: "recognized";
  params?: Record<string, unknown>;
  text?: string; // agent / llm
  name?: string; // custom
  [key: string]: unknown;
}

export interface Finding {
  severity?: "info" | "warning" | "error" | "critical";
  message: string;
}

export interface WaitStill {
  timeout_ms?: number;
  interval_ms?: number;
  threshold?: number;
}

export interface TaskNodeDef {
  step?: string;
  recognition: Recognition;
  action: TaskAction;
  next?: string[];
  on_timeout?: string;
  finding?: string | Finding;
  timeout_ms?: number | null;
  poll_interval_ms?: number | null;
  post_delay_ms?: number | null;
  wait_still?: WaitStill | null;
  [key: string]: unknown; // _comment 等透传
}

/** watchdog = 识别 spec（禁 always，由后端校验）+ QA 字段。 */
export interface Watchdog extends Recognition {
  severity?: Finding["severity"];
  message?: string;
  skip_to?: string;
  fail_task?: boolean;
}

export interface Popup {
  name?: string;
  recognition: Recognition;
  confirm?: Recognition;
  action: TaskAction; // 仅 click / key / gesture
  [key: string]: unknown;
}

export interface TaskDefaults {
  timeout_ms?: number;
  poll_interval_ms?: number;
  post_delay_ms?: number;
  wait_still?: WaitStill;
}

export interface MergeInfo {
  includes: string[];
  conflicts: string[];
  include_map: Record<string, string>; // 节点名 → 来源文件（"<task>" = 主文件）
}

export interface TaskDoc {
  entry: string;
  nodes: Record<string, TaskNodeDef>;
  includes?: string[];
  on_conflict?: "strict" | "overwrite";
  on_finding?: string;
  max_steps?: number;
  back_fallback?: boolean;
  defaults?: TaskDefaults;
  popups?: Popup[];
  watchdogs?: Watchdog[];
  _merge?: MergeInfo; // 后端生成，保存时剔除
  _steps?: Record<string, string>; // 后端生成
  _step_outline?: string; // 后端生成
  [key: string]: unknown; // _comment 等透传
}

export const MAIN_FILE_LABEL = "<task>";

export function isIncludeNode(doc: TaskDoc, nodeName: string): boolean {
  const src = doc._merge?.include_map?.[nodeName];
  return src !== undefined && src !== MAIN_FILE_LABEL;
}

export function includeSource(doc: TaskDoc, nodeName: string): string | null {
  const src = doc._merge?.include_map?.[nodeName];
  return src && src !== MAIN_FILE_LABEL ? src : null;
}
