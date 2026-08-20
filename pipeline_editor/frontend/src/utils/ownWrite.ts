/** 「这次文件变更是不是我自己刚写的」判定（纯函数，可单测）。
 *
 * 背景：后端 watcher 只认文件 mtime，分不清写入来自编辑器自己、AI（内嵌 MCP）
 * 还是手改文件。旧实现只用「保存后 5s 内一律忽略」的时间窗——AI 在这 5s 内改了
 * 同一个任务，事件会被静默吞掉，画布停在旧内容，用户下次保存无感覆盖 AI 的修改。
 *
 * 现在时间窗只用来判断「疑似自写」，落到自写与否要再比内容：保存成功时记下写盘
 * payload 的规范化指纹，收到变更事件且落在窗口内时重新拉一次磁盘内容、算出等价
 * 指纹比对——一致才是自写（忽略，不清 undo）；不一致就是真实外部写入，按外部改
 * 处理（干净则重载、脏则冲突横幅）。窗口外一律按外部写入处理。
 */
import { serializeForSave } from "../graph/serialize";
import type { TaskDoc } from "../types/task";

/** 自写窗口：写盘到 watcher（1.5s 轮询）广播出来的最大延迟余量 */
export const OWN_WRITE_WINDOW_MS = 5000;

export interface OwnWriteRecord {
  /** 写盘完成时刻（Date.now()） */
  at: number;
  /**
   * 写盘内容的规范化指纹；null = 内容由后端决定（如重排步号整文件重写），
   * 窗口内一律视为自写。
   */
  fingerprint: string | null;
}

/** 键序无关的稳定序列化：对象键排序、数组保序，用作内容指纹。 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
}

/**
 * 任务的写盘等价指纹：先 serializeForSave（剔生成键与 include 节点）再规范化。
 * 合并态 doc 与后端返回的 resolved 走同一条路径，得到的是同一个「原始态」。
 */
export function taskFingerprint(doc: TaskDoc): string {
  return canonicalJson(serializeForSave(doc));
}

/** 是否落在「疑似自写」时间窗内（窗口外就不用去比内容了，直接按外部写入处理）。 */
export function withinOwnWriteWindow(
  record: OwnWriteRecord | null | undefined,
  now: number,
): boolean {
  if (!record) return false;
  const age = now - record.at;
  return age >= 0 && age < OWN_WRITE_WINDOW_MS;
}

/**
 * 最终判定：窗口内 **且** 磁盘内容与自写记录一致 ⇒ 确为自写，可安全忽略。
 * `record.fingerprint === null`（后端整文件重写）时窗口内即认自写。
 */
export function isOwnWrite(
  record: OwnWriteRecord | null | undefined,
  diskFingerprint: string | null,
  now: number,
): boolean {
  if (!withinOwnWriteWindow(record, now)) return false;
  if (record!.fingerprint === null) return true;
  if (diskFingerprint === null) return false; // 拉不到磁盘内容：宁可当外部写入
  return record!.fingerprint === diskFingerprint;
}
