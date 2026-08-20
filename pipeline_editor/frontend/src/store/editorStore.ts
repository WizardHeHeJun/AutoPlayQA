/** 单一数据源：doc（合并态任务 JSON 镜像）+ layout。
 * React Flow nodes/edges 是派生视图（taskToGraph），画布交互全部落回这里的
 * 增量 action —— 永不从图反推 JSON。undo/redo 用 zundo 只包 {doc, layout}。
 */
import { temporal } from "zundo";
import { create } from "zustand";
import { immer } from "zustand/middleware/immer";

import { autoLayout, type NodeSize } from "../graph/autoLayout";
import { applyRename } from "../graph/rename";
import { stableStringify } from "../graph/serialize";
import type { Recognition, TaskAction, TaskDoc, TaskNodeDef } from "../types/task";
import { MAIN_FILE_LABEL } from "../types/task";
import { useUiStore } from "./uiStore";

/** 自动布局的高度估算要跟当前卡片形态一致（uiStore 不反向依赖 editorStore，无循环）。 */
function layoutOpts(): { detailed: boolean } {
  return { detailed: useUiStore.getState().showNodeDetails };
}

export interface EditorState {
  taskName: string | null;
  doc: TaskDoc | null;
  layout: Record<string, { x: number; y: number }>;
  savedSnapshot: string | null;
  layoutDirty: boolean;

  loadTask: (
    name: string,
    doc: TaskDoc,
    layout: Record<string, { x: number; y: number }> | null,
  ) => void;
  closeTask: () => void;
  markSaved: () => void;
  markLayoutSaved: () => void;

  updateNode: (name: string, patch: Partial<TaskNodeDef>) => void;
  setRecognition: (name: string, rec: Recognition) => void;
  setAction: (name: string, act: TaskAction) => void;
  addNode: (name: string, def?: TaskNodeDef, pos?: { x: number; y: number }) => void;
  deleteNode: (name: string) => void;
  renameNode: (oldName: string, newName: string) => void;

  connectNext: (source: string, target: string) => void;
  removeNext: (source: string, target: string, index?: number) => void;
  reorderNext: (source: string, from: number, to: number) => void;
  setOnTimeout: (source: string, target: string | null) => void;

  setDocField: <K extends keyof TaskDoc>(key: K, value: TaskDoc[K]) => void;

  moveNode: (name: string, pos: { x: number; y: number }) => void;
  applyAutoLayout: () => void;
  relayoutWithSizes: (sizes: Record<string, NodeSize>) => void;
}

export const DEFAULT_NODE: TaskNodeDef = {
  recognition: { type: "ocr", expected: "" },
  action: { type: "click", target: "recognized" },
  next: [],
};

export const useEditorStore = create<EditorState>()(
  temporal(
    immer((set, get) => ({
      taskName: null,
      doc: null,
      layout: {},
      savedSnapshot: null,
      layoutDirty: false,

      loadTask: (name, doc, layout) =>
        set((s) => {
          s.taskName = name;
          s.doc = doc;
          const nodeNames = Object.keys(doc.nodes ?? {});
          const covered =
            layout && nodeNames.filter((n) => layout[n]).length >= nodeNames.length * 0.6;
          s.layout = covered && layout ? layout : autoLayout(doc, layoutOpts());
          // 布局有缺口时对缺失节点补自动位置
          if (covered && layout) {
            const auto = nodeNames.some((n) => !layout[n]) ? autoLayout(doc, layoutOpts()) : null;
            for (const n of nodeNames) {
              if (!s.layout[n] && auto) s.layout[n] = auto[n];
            }
          }
          s.savedSnapshot = stableStringify(doc);
          s.layoutDirty = !covered;
        }),

      closeTask: () =>
        set((s) => {
          s.taskName = null;
          s.doc = null;
          s.layout = {};
          s.savedSnapshot = null;
          s.layoutDirty = false;
        }),

      markSaved: () =>
        set((s) => {
          if (s.doc) s.savedSnapshot = stableStringify(s.doc as TaskDoc);
        }),

      markLayoutSaved: () =>
        set((s) => {
          s.layoutDirty = false;
        }),

      updateNode: (name, patch) =>
        set((s) => {
          const node = s.doc?.nodes?.[name];
          if (!node) return;
          for (const [k, v] of Object.entries(patch)) {
            if (v === undefined) delete (node as Record<string, unknown>)[k];
            else (node as Record<string, unknown>)[k] = v;
          }
        }),

      setRecognition: (name, rec) =>
        set((s) => {
          const node = s.doc?.nodes?.[name];
          if (node) node.recognition = rec;
        }),

      setAction: (name, act) =>
        set((s) => {
          const node = s.doc?.nodes?.[name];
          if (node) node.action = act;
        }),

      addNode: (name, def, pos) =>
        set((s) => {
          if (!s.doc || s.doc.nodes[name]) return;
          s.doc.nodes[name] = def ?? structuredClone(DEFAULT_NODE);
          if (s.doc._merge) s.doc._merge.include_map[name] = MAIN_FILE_LABEL;
          s.layout[name] = pos ?? { x: 80, y: 80 };
        }),

      deleteNode: (name) =>
        set((s) => {
          if (!s.doc) return;
          delete s.doc.nodes[name];
          delete s.layout[name];
          if (s.doc._merge) delete s.doc._merge.include_map[name];
          for (const def of Object.values(s.doc.nodes)) {
            if (Array.isArray(def.next)) def.next = def.next.filter((n) => n !== name);
            if (def.on_timeout === name) delete def.on_timeout;
          }
          for (const w of s.doc.watchdogs ?? []) {
            if (w.skip_to === name) delete w.skip_to;
          }
          if (s.doc.on_finding === name) delete s.doc.on_finding;
        }),

      renameNode: (oldName, newName) =>
        set((s) => {
          if (!s.doc || !s.doc.nodes[oldName] || s.doc.nodes[newName]) return;
          applyRename(s.doc as TaskDoc, oldName, newName);
          if (s.layout[oldName]) {
            s.layout[newName] = s.layout[oldName];
            delete s.layout[oldName];
          }
        }),

      connectNext: (source, target) =>
        set((s) => {
          const node = s.doc?.nodes?.[source];
          if (!node || !s.doc?.nodes?.[target]) return;
          if (!Array.isArray(node.next)) node.next = [];
          if (!node.next.includes(target)) node.next.push(target);
        }),

      removeNext: (source, target, index) =>
        set((s) => {
          const node = s.doc?.nodes?.[source];
          if (!node || !Array.isArray(node.next)) return;
          if (index !== undefined && node.next[index] === target) {
            node.next.splice(index, 1);
          } else {
            const i = node.next.indexOf(target);
            if (i >= 0) node.next.splice(i, 1);
          }
        }),

      reorderNext: (source, from, to) =>
        set((s) => {
          const node = s.doc?.nodes?.[source];
          if (!node || !Array.isArray(node.next)) return;
          if (from < 0 || from >= node.next.length || to < 0 || to >= node.next.length)
            return;
          const [item] = node.next.splice(from, 1);
          node.next.splice(to, 0, item);
        }),

      setOnTimeout: (source, target) =>
        set((s) => {
          const node = s.doc?.nodes?.[source];
          if (!node) return;
          if (target === null) delete node.on_timeout;
          else if (s.doc?.nodes?.[target]) node.on_timeout = target;
        }),

      setDocField: (key, value) =>
        set((s) => {
          if (!s.doc) return;
          if (value === undefined) delete (s.doc as Record<string, unknown>)[key as string];
          else (s.doc as Record<string, unknown>)[key as string] = value;
        }),

      moveNode: (name, pos) =>
        set((s) => {
          s.layout[name] = pos;
          s.layoutDirty = true;
        }),

      /** 无实测尺寸的自动布局（载入兜底 / 测试用）。工具条走 relayoutWithSizes。 */
      applyAutoLayout: () =>
        set((s) => {
          if (!s.doc) return;
          s.layout = autoLayout(s.doc as TaskDoc, layoutOpts());
          s.layoutDirty = true;
        }),

      /**
       * 带 React Flow 实测尺寸的自动布局：实测优先、估算兜底。
       * 只写 layout（sidecar），**不碰 doc**——布局永远不进任务 JSON。
       */
      relayoutWithSizes: (sizes) =>
        set((s) => {
          if (!s.doc) return;
          s.layout = autoLayout(s.doc as TaskDoc, { ...layoutOpts(), sizes });
          s.layoutDirty = true;
        }),
    })),
    {
      partialize: (state) => ({ doc: state.doc, layout: state.layout }),
      limit: 100,
      equality: (past, current) => JSON.stringify(past) === JSON.stringify(current),
    },
  ),
);

export function isDirty(state: EditorState): boolean {
  if (!state.doc || state.savedSnapshot === null) return false;
  return stableStringify(state.doc) !== state.savedSnapshot;
}
