/**
 * custom action 参数表单的纯逻辑：schema → 控件取值 / 写回。
 *
 * **写入纪律（对齐「defaults 不展开」红线）**：表单只把用户**显式设置**的键写进
 * `params`；schema 的默认值只做 placeholder 展示，绝不批量灌进任务 JSON——
 * 否则保存就把 handler 的默认值固化进了任务文件，handler 改默认值时任务再也跟不上。
 * 清空一个字段 = 从 `params` 删掉这个键（回到「跟随 handler 默认」）。
 */
import type { CustomActionParam } from "../../types/api";
import type { Roi } from "../../types/task";

export type Params = Record<string, unknown>;

/** schema 描述过的键集合。 */
export function schemaKeys(schema: CustomActionParam[]): Set<string> {
  return new Set(schema.map((p) => p.key));
}

/**
 * params 里存在、但 schema 没描述的键（手写的未知键 / handler 改过名的旧键）。
 * 表单必须原样列出它们，绝不能因为「表单里没有这个字段」就把数据吃掉。
 */
export function unknownParamKeys(
  params: Params | undefined,
  schema: CustomActionParam[],
): string[] {
  const known = schemaKeys(schema);
  return Object.keys(params ?? {}).filter((k) => !known.has(k));
}

/** 该键是否被用户显式设置过（决定控件显示实值还是 placeholder）。 */
export function isSet(params: Params | undefined, key: string): boolean {
  return params != null && Object.prototype.hasOwnProperty.call(params, key);
}

/**
 * 写一个键并返回新的 params。`value === undefined` = 删键；
 * 删空后返回 `undefined`，让 `params` 整个从 action 上消失（保持最小 diff）。
 */
export function setParam(
  params: Params | undefined,
  key: string,
  value: unknown,
): Params | undefined {
  const next: Params = { ...(params ?? {}) };
  if (value === undefined) delete next[key];
  else next[key] = value;
  return Object.keys(next).length ? next : undefined;
}

/** placeholder 文案 = handler 的默认值；求不出就直说，不编一个假默认。 */
export function placeholderOf(param: CustomActionParam): string {
  if (param.required) return "必填";
  if (param.default_unresolved) return "默认值（源码里未静态求出）";
  if (param.default === undefined || param.default === null) return "未设置（handler 默认）";
  if (typeof param.default === "string") return `默认 ${param.default}`;
  return `默认 ${JSON.stringify(param.default)}`;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function asBool(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

/** 只有「4 个数字」才当 roi 交给 RoiField，其它形态退回 undefined（由 JSON 行兜底）。 */
export function asRoi(value: unknown): Roi | undefined {
  if (!Array.isArray(value) || value.length !== 4) return undefined;
  if (!value.every((v) => typeof v === "number" && Number.isFinite(v))) return undefined;
  return [value[0], value[1], value[2], value[3]] as Roi;
}

/** 值 → 单行 JSON 文本（未设置 = 空串，不是 "null"）。 */
export function toJsonLine(value: unknown): string {
  return value === undefined ? "" : JSON.stringify(value);
}

export type JsonParse =
  | { ok: true; value: unknown }
  | { ok: false; error: string };

/** 单行 JSON 输入 → 值。空串 = 删键（value: undefined）。 */
export function parseJsonLine(text: string): JsonParse {
  if (!text.trim()) return { ok: true, value: undefined };
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

/** float 字段的步进：小数默认值给细步进，其余给 1。 */
export function stepOf(param: CustomActionParam): number {
  if (param.type !== "float") return 1;
  const d = param.default;
  if (typeof d === "number" && Number.isFinite(d) && Math.abs(d) < 10) return 0.01;
  return 0.1;
}
