/** 纯派生：TaskNodeDef → 画布卡片的分组参数行。
 *
 * 硬约束：只读节点定义**自身**的字段，绝不合并 doc.defaults —— "defaults 不展开"是项目红线，
 * 节点上没写的字段就不显示（否则用户会以为 defaults 已经固化在节点上）。
 */
import type { TaskAction, TaskNodeDef } from "../types/task";
import { recSummary } from "./taskToGraph";

export interface DetailRow {
  label: string;
  value: string;
  /** 字段的中文说明（画布卡片上挂在 label 的 Tooltip 里），没有则不包 Tooltip */
  hint?: string;
  /**
   * value 被截断时的未截断原文（TaskNode 的 value Tooltip 用它展示全文），
   * 上限 MAX_FULL_VALUE_LEN 字符，未截断（value 已是全文）时不设置。
   */
  full?: string;
}

export type DetailSectionKind = "rec" | "act" | "misc";

export interface DetailSection {
  kind: DetailSectionKind;
  title: string;
  rows: DetailRow[];
}

/** 单行值的最大字符数，超出截断加省略号 */
export const MAX_VALUE_LEN = 40;

/**
 * 常用字段的中文说明。口径对齐 `components/inspector/NodeInspector.tsx` 的时序字段提示
 * （同一个字段两处文案必须一致，改一处要顺手改另一处）。
 * 只放「含义固定」的字段；custom 动作的自定义参数走 CUSTOM_PARAM_HINT。
 */
export const FIELD_HINTS: Record<string, string> = {
  // 识别
  roi: "识别区域 [x, y, w, h]（0~1 比例或像素）",
  expected: "期望文本（OCR / 控件树里命中即算匹配）",
  template: "模板图路径（相对模板根目录）",
  threshold: "匹配阈值，越大越严格",
  scales: "模板多尺度缩放系数",
  grayscale: "灰度匹配（忽略颜色差异）",
  min_matches: "特征点最少匹配数",
  ratio: "特征匹配比值，越小越严格",
  label: "YOLO 目标类别名",
  model: "YOLO 模型（留空用默认模型）",
  conf: "YOLO 置信度阈值",
  box_index: "多个命中框时取第几个（从 0 开始）",
  // 动作
  target: "动作作用点（命中处 = 用识别结果的坐标）",
  name: "custom handler 名（在 AutoPlayQA 里注册的动作）",
  text: "交给 agent 执行的自然语言指令",
  keycode: "按键码（如 BACK / HOME / ENTER）",
  duration_ms: "动作时长（毫秒）",
  // 时序（与 NodeInspector 的时序调参一致）
  timeout_ms: "识别轮询总预算（默认 10000）",
  poll_interval_ms: "轮询间隔（默认 1000）",
  post_delay_ms: "动作后固定等待（默认 0）",
  wait_still: "画面静止再放行；超时不算失败",
  // QA
  finding: "进入此节点即上报 QA 发现",
};

/** custom 动作里未收录的 params：含义由 handler 自己定义，编辑器不猜。 */
export const CUSTOM_PARAM_HINT = "透传给 custom handler 的参数，含义由 handler 定义";

/** and/or 组合识别的子条件行 */
const CHILD_REC_HINT = "组合识别的子条件（顺序即判定顺序）";

export function truncate(s: string, max = MAX_VALUE_LEN): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** 值 → 紧凑字符串：数组 `[a,b,c,d]`，对象走 JSON，其余原样。 */
function raw(v: unknown): string {
  if (v === null) return "null";
  if (v === undefined) return "";
  if (typeof v === "string") return v.length === 0 ? '""' : v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map(raw).join(",")}]`;
  if (typeof v === "object") {
    try {
      return JSON.stringify(v) ?? String(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

/** full（Tooltip 全文）的上限：比 value 的 40 字符宽松得多，但仍要防止极端长文本。 */
export const MAX_FULL_VALUE_LEN = 200;

/** 截断成展示值，并在截断确实发生时一并给出 Tooltip 用的（较宽松上限的）全文。 */
function truncateWithFull(s: string, max = MAX_VALUE_LEN): { value: string; full?: string } {
  if (s.length <= max) return { value: s };
  return { value: `${s.slice(0, max)}…`, full: truncate(s, MAX_FULL_VALUE_LEN) };
}

/** 组装一行：hint/full 为空时不写对应键（避免序列化出一堆 undefined）。 */
function row(label: string, value: string, hint?: string, full?: string): DetailRow {
  const r: DetailRow = { label, value };
  if (hint) r.hint = hint;
  if (full) r.full = full;
  return r;
}

/** 只有显式设置（非 undefined / null）的字段才出行。 */
function pushIf(
  rows: DetailRow[],
  label: string,
  v: unknown,
  max = MAX_VALUE_LEN,
  hint: string | undefined = FIELD_HINTS[label],
): void {
  if (v === undefined || v === null) return;
  const { value, full } = truncateWithFull(raw(v), max);
  rows.push(row(label, value, hint, full));
}

/** params 里的已设字段逐行输出（未知键照实显示，顺序跟随 JSON 键序）。 */
function pushParams(rows: DetailRow[], act: TaskAction, skip: Set<string> = new Set()): void {
  const params = act.params;
  if (!params || typeof params !== "object") return;
  // custom 的参数是 handler 私有契约：表里没收录的键统一给「透传」说明，不瞎猜含义
  const fallback = act.type === "custom" ? CUSTOM_PARAM_HINT : undefined;
  for (const [k, v] of Object.entries(params)) {
    if (skip.has(k)) continue;
    pushIf(rows, k, v, MAX_VALUE_LEN, FIELD_HINTS[k] ?? fallback);
  }
}

function recognitionSection(def: TaskNodeDef): DetailSection | null {
  const rec = def.recognition;
  if (!rec) return null;
  const type = rec.type ?? "?";
  const rows: DetailRow[] = [];

  switch (type) {
    case "ui_text":
    case "ocr":
      pushIf(rows, "expected", rec.expected);
      pushIf(rows, "roi", rec.roi);
      pushIf(rows, "threshold", rec.threshold);
      break;
    case "template":
      pushIf(rows, "template", rec.template);
      pushIf(rows, "roi", rec.roi);
      pushIf(rows, "threshold", rec.threshold);
      pushIf(rows, "scales", rec.scales);
      pushIf(rows, "grayscale", rec.grayscale);
      break;
    case "feature":
      pushIf(rows, "template", rec.template);
      pushIf(rows, "roi", rec.roi);
      pushIf(rows, "threshold", rec.threshold);
      pushIf(rows, "min_matches", rec.min_matches);
      pushIf(rows, "ratio", rec.ratio);
      break;
    case "yolo":
      pushIf(rows, "label", rec.label);
      pushIf(rows, "model", rec.model);
      pushIf(rows, "conf", rec.conf);
      pushIf(rows, "roi", rec.roi);
      break;
    case "and":
    case "or": {
      const children = (type === "and" ? rec.all_of : rec.any_of) ?? [];
      children.forEach((child, i) => {
        const { value, full } = truncateWithFull(recSummary(child));
        rows.push(row(`子 ${i + 1}`, value, CHILD_REC_HINT, full));
      });
      pushIf(rows, "roi", rec.roi);
      if (type === "and") pushIf(rows, "box_index", rec.box_index);
      break;
    }
    case "blank_screen":
    case "always":
      pushIf(rows, "roi", rec.roi);
      pushIf(rows, "threshold", rec.threshold);
      break;
    default:
      // 未知识别类型：把常见标量字段照实带出来
      pushIf(rows, "expected", rec.expected);
      pushIf(rows, "template", rec.template);
      pushIf(rows, "roi", rec.roi);
      pushIf(rows, "threshold", rec.threshold);
      break;
  }

  if (rows.length === 0) return null;
  return { kind: "rec", title: `识别 · ${type}`, rows };
}

function actionSection(def: TaskNodeDef): DetailSection | null {
  const act = def.action;
  if (!act) return null;
  // llm 是 agent 的废弃别名，展示统一按 agent
  const type = act.type === "llm" ? "agent" : (act.type ?? "?");
  const rows: DetailRow[] = [];

  switch (type) {
    case "click":
      if (act.target === "recognized") {
        rows.push(row("target", "命中处", FIELD_HINTS.target));
        pushParams(rows, act);
      } else {
        pushParams(rows, act);
      }
      break;
    case "custom":
      pushIf(rows, "name", act.name);
      pushParams(rows, act);
      break;
    case "agent":
      pushIf(rows, "text", act.text);
      break;
    case "none":
      break;
    default:
      // drag / gesture / key / wait / input_text 以及未知类型：params 逐行
      if (act.target === "recognized") rows.push(row("target", "命中处", FIELD_HINTS.target));
      pushParams(rows, act);
      break;
  }

  if (rows.length === 0) return null;
  return { kind: "act", title: `动作 · ${type}`, rows };
}

/** wait_still 摘要成一行：`timeout 3000 / interval 500`。 */
function waitStillSummary(ws: NonNullable<TaskNodeDef["wait_still"]>): string {
  const parts: string[] = [];
  if (ws.timeout_ms != null) parts.push(`timeout ${ws.timeout_ms}`);
  if (ws.interval_ms != null) parts.push(`interval ${ws.interval_ms}`);
  if (ws.threshold != null) parts.push(`阈值 ${ws.threshold}`);
  return parts.length > 0 ? parts.join(" / ") : "启用";
}

function miscSection(def: TaskNodeDef): DetailSection | null {
  const rows: DetailRow[] = [];
  // ⚠ 只看节点上显式写的值，绝不回退 doc.defaults
  pushIf(rows, "timeout_ms", def.timeout_ms);
  pushIf(rows, "poll_interval_ms", def.poll_interval_ms);
  pushIf(rows, "post_delay_ms", def.post_delay_ms);
  if (def.wait_still != null && typeof def.wait_still === "object") {
    const { value, full } = truncateWithFull(waitStillSummary(def.wait_still));
    rows.push(row("wait_still", value, FIELD_HINTS.wait_still, full));
  }
  const finding = def.finding;
  if (finding != null) {
    const hint = FIELD_HINTS.finding;
    const text =
      typeof finding === "string"
        ? finding
        : `${finding.severity ?? "error"} · ${finding.message ?? ""}`;
    const { value, full } = truncateWithFull(text);
    rows.push(row("finding", value, hint, full));
  }

  if (rows.length === 0) return null;
  return { kind: "misc", title: "其他", rows };
}

/** 节点定义 → 分组参数行（空组不输出）。 */
export function nodeDetailRows(def: TaskNodeDef): DetailSection[] {
  if (!def) return [];
  const sections: DetailSection[] = [];
  const rec = recognitionSection(def);
  if (rec) sections.push(rec);
  const act = actionSection(def);
  if (act) sections.push(act);
  const misc = miscSection(def);
  if (misc) sections.push(misc);
  return sections;
}
