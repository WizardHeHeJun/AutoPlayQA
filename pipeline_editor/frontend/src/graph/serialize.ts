/** 保存序列化：合并态 doc → 可写盘的原始形态。
 *
 * 剔除后端生成键（_merge/_steps/_step_outline）与来自 include 的节点——
 * 把 include 节点固化进主文件会破坏共享语义（get_task 返回合并态、
 * save_task 写原始态，round-trip 无损是 M2 验收标准）。
 * 用户的 _comment 等 `_` 键原样保留。
 */
import type { TaskDoc } from "../types/task";
import { MAIN_FILE_LABEL } from "../types/task";

const GENERATED_KEYS = new Set(["_merge", "_steps", "_step_outline"]);

export function serializeForSave(doc: TaskDoc): TaskDoc {
  const out: TaskDoc = {} as TaskDoc;
  for (const [key, value] of Object.entries(doc)) {
    if (GENERATED_KEYS.has(key) || key === "nodes") continue;
    (out as Record<string, unknown>)[key] = value;
  }
  const includeMap = doc._merge?.include_map ?? {};
  const nodes: TaskDoc["nodes"] = {};
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    const src = includeMap[name];
    if (src !== undefined && src !== MAIN_FILE_LABEL) continue; // include 节点不落主文件
    nodes[name] = def;
  }
  out.nodes = nodes;
  return out;
}

/** dirty 判定用的稳定序列化。 */
export function stableStringify(doc: TaskDoc): string {
  return JSON.stringify(serializeForSave(doc));
}
