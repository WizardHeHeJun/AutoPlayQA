/** 单向派生：TaskDoc + layout → React Flow nodes/edges。永不整图反序列化。 */
import type { Edge, Node } from "@xyflow/react";

import type { Recognition, TaskAction, TaskDoc, TaskNodeDef } from "../types/task";
import { includeSource } from "../types/task";
import type { LocalLintWarning } from "./localLint";
import { nodeDetailRows, type DetailSection } from "./nodeDetails";

export interface TaskNodeData extends Record<string, unknown> {
  name: string;
  def: TaskNodeDef;
  step?: string;
  readonly: boolean;
  includeFrom: string | null;
  isEntry: boolean;
  isTerminal: boolean; // next: [] = 成功终点
  recSummary: string;
  actSummary: string;
  showDetails: boolean; // 详情模式（分组参数行）vs 简洁模式（两行摘要）
  details: DetailSection[]; // 仅详情模式下派生，简洁模式为空
  nextTargets: string[]; // 页脚 next → …
  hasFinding: boolean;
  findingSeverity: string | null;
  hasOnTimeout: boolean;
  warnings: string[]; // localLint 注入
  errorMessage: string | null; // 后端校验错误定位
  runState: "current" | "visited" | null; // 运行高亮
}

export type TaskFlowNode = Node<TaskNodeData, "taskNode">;

export function recSummary(rec: Recognition | undefined): string {
  if (!rec) return "?";
  const t = rec.type ?? "?";
  switch (t) {
    case "ui_text":
    case "ocr":
      return `${t} "${rec.expected ?? "?"}"${rec.roi ? " roi✓" : ""}`;
    case "template":
    case "feature":
      return `${t} [${rec.template ?? "?"}]`;
    case "yolo":
      return `yolo ${rec.label ?? "*"}${rec.model ? `@${rec.model}` : ""}`;
    case "and":
      return `and(${(rec.all_of ?? []).length})`;
    case "or":
      return `or(${(rec.any_of ?? []).length})`;
    default:
      return t;
  }
}

export function actSummary(act: TaskAction | undefined): string {
  if (!act) return "?";
  const t = act.type === "llm" ? "agent" : act.type;
  switch (t) {
    case "click":
      if (act.target === "recognized") return "click(命中处)";
      return `click(${act.params?.x ?? "?"}, ${act.params?.y ?? "?"})`;
    case "custom":
      return `custom: ${act.name ?? "?"}`;
    case "agent":
      return "agent 交接";
    case "key":
      return `key ${act.params?.keycode ?? "?"}`;
    case "wait":
      return `wait ${act.params?.duration_ms ?? "?"}ms`;
    case "input_text":
      return `input "${act.params?.text ?? ""}"`;
    default:
      return t;
  }
}

export interface GraphOptions {
  layout: Record<string, { x: number; y: number }>;
  steps?: Record<string, string>;
  selected?: string | null;
  lintWarnings?: LocalLintWarning[];
  errorNode?: string | null;
  errorMessage?: string | null;
  showJumpEdges?: boolean;
  showNodeDetails?: boolean;
  runCurrent?: string | null;
  runVisited?: Set<string>;
}

export const TASK_LEVEL_NODE_ID = "__task__";

export function taskToGraph(
  doc: TaskDoc,
  opts: GraphOptions,
): { nodes: TaskFlowNode[]; edges: Edge[] } {
  const nodes: TaskFlowNode[] = [];
  const edges: Edge[] = [];
  const showDetails = opts.showNodeDetails ?? false;
  const warnByNode = new Map<string, string[]>();
  for (const w of opts.lintWarnings ?? []) {
    if (w.node) {
      const list = warnByNode.get(w.node) ?? [];
      list.push(w.message);
      warnByNode.set(w.node, list);
    }
  }

  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    const incFrom = includeSource(doc, name);
    const next = Array.isArray(def.next) ? def.next : [];
    const finding = def.finding;
    const findingSeverity =
      finding == null
        ? null
        : typeof finding === "string"
          ? "error"
          : (finding.severity ?? "error");
    nodes.push({
      id: name,
      type: "taskNode",
      position: opts.layout[name] ?? { x: 0, y: 0 },
      selected: opts.selected === name,
      data: {
        name,
        def,
        step: opts.steps?.[name] ?? (typeof def.step === "string" ? def.step : undefined),
        readonly: incFrom !== null,
        includeFrom: incFrom,
        isEntry: doc.entry === name,
        isTerminal: next.length === 0,
        recSummary: recSummary(def.recognition),
        actSummary: actSummary(def.action),
        showDetails,
        details: showDetails ? nodeDetailRows(def) : [],
        nextTargets: next,
        hasFinding: finding != null,
        findingSeverity,
        hasOnTimeout: typeof def.on_timeout === "string",
        warnings: warnByNode.get(name) ?? [],
        errorMessage: opts.errorNode === name ? (opts.errorMessage ?? null) : null,
        runState:
          opts.runCurrent === name
            ? "current"
            : opts.runVisited?.has(name)
              ? "visited"
              : null,
      },
    });

    next.forEach((target, i) => {
      if (!(target in (doc.nodes ?? {}))) return; // 悬空引用交给校验面板
      edges.push({
        id: `next:${name}→${target}:${i}`,
        source: name,
        sourceHandle: "next",
        target,
        type: "nextEdge",
        data: { order: i, total: next.length },
        animated: opts.runCurrent === target && opts.runVisited?.has(name),
      });
    });
    if (typeof def.on_timeout === "string" && def.on_timeout in (doc.nodes ?? {})) {
      edges.push({
        id: `timeout:${name}→${def.on_timeout}`,
        source: name,
        sourceHandle: "timeout",
        target: def.on_timeout,
        type: "timeoutEdge",
      });
    }
  }

  // 全局跳转层：任务级虚拟节点 → on_finding / watchdog skip_to
  if (opts.showJumpEdges) {
    const jumpTargets = new Map<string, string[]>();
    if (doc.on_finding && doc.on_finding in (doc.nodes ?? {})) {
      jumpTargets.set(doc.on_finding, ["on_finding"]);
    }
    (doc.watchdogs ?? []).forEach((w, i) => {
      if (w.skip_to && w.skip_to in (doc.nodes ?? {})) {
        const list = jumpTargets.get(w.skip_to) ?? [];
        list.push(`watchdog#${i + 1}`);
        jumpTargets.set(w.skip_to, list);
      }
    });
    if (jumpTargets.size > 0) {
      nodes.push({
        id: TASK_LEVEL_NODE_ID,
        type: "taskNode",
        position: opts.layout[TASK_LEVEL_NODE_ID] ?? { x: -260, y: 0 },
        data: {
          name: "任务级",
          def: { recognition: { type: "always" }, action: { type: "none" } },
          readonly: true,
          includeFrom: null,
          isEntry: false,
          isTerminal: false,
          recSummary: "on_finding / watchdogs",
          actSummary: "全局跳转",
          showDetails: false, // 虚拟节点没有真实定义，永远走简洁模式
          details: [],
          nextTargets: [],
          hasFinding: false,
          findingSeverity: null,
          hasOnTimeout: false,
          warnings: [],
          errorMessage: null,
          runState: null,
        },
      });
      for (const [target, sources] of jumpTargets) {
        edges.push({
          id: `jump:${target}`,
          source: TASK_LEVEL_NODE_ID,
          sourceHandle: "next",
          target,
          type: "jumpEdge",
          data: { label: sources.join(", ") },
        });
      }
    }
  }

  return { nodes, edges };
}
