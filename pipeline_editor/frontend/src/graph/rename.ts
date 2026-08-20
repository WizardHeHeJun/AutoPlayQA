/** 节点改名级联：entry / next / on_timeout / on_finding / watchdog.skip_to。 */
import type { TaskDoc } from "../types/task";

export interface RenameImpact {
  entry: boolean;
  onFinding: boolean;
  nextRefs: string[]; // 引用了旧名的节点
  timeoutRefs: string[];
  watchdogRefs: number[]; // watchdog 序号
}

export function renameImpact(doc: TaskDoc, oldName: string): RenameImpact {
  const nextRefs: string[] = [];
  const timeoutRefs: string[] = [];
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    if ((def.next ?? []).includes(oldName)) nextRefs.push(name);
    if (def.on_timeout === oldName) timeoutRefs.push(name);
  }
  const watchdogRefs = (doc.watchdogs ?? [])
    .map((w, i) => (w.skip_to === oldName ? i : -1))
    .filter((i) => i >= 0);
  return {
    entry: doc.entry === oldName,
    onFinding: doc.on_finding === oldName,
    nextRefs,
    timeoutRefs,
    watchdogRefs,
  };
}

/** 就地修改（配合 immer 的 draft 使用）。保持 nodes 的键顺序。 */
export function applyRename(doc: TaskDoc, oldName: string, newName: string): void {
  const nodes: TaskDoc["nodes"] = {};
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    nodes[name === oldName ? newName : name] = def;
  }
  doc.nodes = nodes;
  if (doc.entry === oldName) doc.entry = newName;
  if (doc.on_finding === oldName) doc.on_finding = newName;
  for (const def of Object.values(doc.nodes)) {
    if (Array.isArray(def.next)) {
      def.next = def.next.map((n) => (n === oldName ? newName : n));
    }
    if (def.on_timeout === oldName) def.on_timeout = newName;
  }
  for (const w of doc.watchdogs ?? []) {
    if (w.skip_to === oldName) w.skip_to = newName;
  }
  if (doc._merge?.include_map) {
    const map: Record<string, string> = {};
    for (const [name, src] of Object.entries(doc._merge.include_map)) {
      map[name === oldName ? newName : name] = src;
    }
    doc._merge.include_map = map;
  }
}
