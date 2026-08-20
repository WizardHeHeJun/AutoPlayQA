/** dagre 分层自动布局。on_timeout 边不参与分层权重（避免恢复环拉乱主干）。
 *
 * 详情模式下卡片高度随参数行数变化（150~400px 都有），固定高度会让行数多的节点在
 * dagre 分层后重叠，所以这里按 `nodeDetailRows` 的分组/行数逐节点估算高度。
 *
 * **估算只是兜底**：`opts.sizes` 传入 React Flow 的实测尺寸（`node.measured`）时优先用实测值，
 * 估算与真实渲染的偏差（字体、subpixel、徽标换行、css 改动）就不再影响布局。
 * 初次载入画布还没渲染过、拿不到实测值，才回退到下面这套与 `canvas.css` 强耦合的估算。
 */
import dagre from "@dagrejs/dagre";

import type { TaskDoc, TaskNodeDef } from "../types/task";
import { nodeDetailRows } from "./nodeDetails";

export const NODE_WIDTH = 260;

/* ────────────────────────────────────────────────────────────────────────────
 * 卡片渲染常量：全部对齐 `src/styles/canvas.css` 与 `components/canvas/TaskNode.tsx`。
 * 改 css 的 padding / line-height / font-size 必须同步改这里，否则布局会重叠。
 * ──────────────────────────────────────────────────────────────────────────── */

/** `.pe-node` border: 1.5px 上下各一条 → 3px（`canvas.css:3`） */
const BORDER_H = 3;
/** `.pe-node` font-size: 12px（`canvas.css:6`）× antd reset 的 body line-height 1.5715 ≈ 19 */
const BASE_LINE_H = 19;
/**
 * `.pe-node-header`（`canvas.css:46-54`）：padding 6px 上下 = 12
 * + border-bottom 1px + 内容行高（`.pe-step-badge` line-height 18px 与 19px 文本行取大者）
 */
const HEADER_H = 12 + 1 + BASE_LINE_H;
/** `.pe-node-body` padding: 6px 8px（`canvas.css:78`）→ 上下各 6 */
const BODY_PAD_H = 12;
/** `.pe-node-body` flex gap: 3px（`canvas.css:81`），作用于相邻分组/摘要行之间 */
const BODY_GAP = 3;
/** `.pe-node-section + .pe-node-section` margin-top: 5px（`canvas.css:97-99`），与 flex gap 叠加 */
const SECTION_MARGIN = 5;
/** `.pe-section-title` line-height: 15px（`canvas.css:105-113`） */
const SECTION_TITLE_H = 15;
/**
 * 一行参数：`.pe-detail-rows` 是 `auto minmax(0,1fr)` 的 grid，label/value 都是单行
 * line-height 16px（`canvas.css:126-152`），行间无 row-gap ⇒ 每行恰好 16px。
 * label 只加宽不换行，所以这里仍按一行算；若哪天允许 label 换行，这个常量就不成立了。
 */
const DETAIL_ROW_H = 16;
/** `.pe-node-line`：继承 `.pe-node` 12px 字号，无自定义 line-height（`canvas.css:85-89`） */
const SUMMARY_LINE_H = BASE_LINE_H;
/**
 * `.pe-node-footer`（`canvas.css:148-156`）：padding 3px 上下 = 6 + border-top 1px
 * + font-size 11px × 1.5715 ≈ 18
 */
const FOOTER_H = 6 + 1 + 18;
/**
 * `.pe-node-badges`（`canvas.css:158-163`）：padding-bottom 6px + `.pe-badge` line-height 16px
 * （`canvas.css:165-172`）。徽标可能一个都没有（此时只有 6px），按满行估算——宁大勿小。
 */
const BADGES_H = 6 + 16;
/** 估算余量：抵消字体差异 / 徽标 flex-wrap 换行 / 浏览器 subpixel 取整 */
const SAFETY_MARGIN = 10;

/* ────────────────────────────────────────────────────────────────────────────
 * dagre 边权重：**必须是整数**。
 *
 * dagre 的 network-simplex 用「割值（cut value）」判断某条树边能否被更优的边替换，
 * 割值是各边 weight 的加减累积。weight 取 0.1 这种二进制不可精确表示的小数时，
 * 割值会攒出 -2.8e-17 这类浮点噪声：`leaveEdge` 把它当成负割值挑出来，`enterEdge`
 * 再去找可替换的边却一条都找不到（本来就没有可改进的边），于是
 * `candidates.reduce(...)` 对空数组求值 → `TypeError: Reduce of empty array with
 * no initial value`，整个页面卡在「加载任务中…」。
 *
 * 触发条件是「图不连通 + 存在小数权重边」：不连通时 dagre 会加一个 `_root` 虚拟节点
 * 把各连通分量桥接起来，噪声正好落在 `_root` 的桥接边上（典型形态：一份 18 节点的启动任务，
 * 3 个自有节点与 15 个 include 节点构成两个互不相连的子图）。
 *
 * 所以「弱边」不能用小数表达，而是把两档权重整体放大成整数，比例（1:10）保持不变。
 * ──────────────────────────────────────────────────────────────────────────── */

/** `next` 主干边权重 */
const NEXT_EDGE_WEIGHT = 10;
/** `on_timeout` 兜底弱边权重（主干的 1/10，不参与分层拉扯） */
const TIMEOUT_EDGE_WEIGHT = 1;

/** 卡片除 body 外的固定部分：边框 + 表头 + 徽标条 */
const CHROME_H = BORDER_H + HEADER_H + BADGES_H;
/** 简洁 body：两行摘要 + 行间 gap + body padding（TaskNode 的 details 为空时的回退渲染） */
const SUMMARY_BODY_H = BODY_PAD_H + SUMMARY_LINE_H * 2 + BODY_GAP;

/** 简洁模式（showNodeDetails=false）下所有卡片的统一高度 */
export const COMPACT_NODE_HEIGHT = CHROME_H + SUMMARY_BODY_H + SAFETY_MARGIN;

/** 兼容旧引用：默认（详情模式）下的保底高度 */
export const NODE_HEIGHT = COMPACT_NODE_HEIGHT;

/** 节点在画布上的渲染尺寸 */
export interface NodeSize {
  width: number;
  height: number;
}

export interface AutoLayoutOptions {
  /** 详情模式（对齐 uiStore.showNodeDetails，默认 true）→ 按参数行数逐节点估算高度 */
  detailed?: boolean;
  /**
   * React Flow 实测尺寸（`node.measured`），键为节点名。命中的节点用实测值，
   * 未命中（或值不合法）的回退估算。doc 里没有的键（如虚拟节点 `__task__`）一律忽略。
   */
  sizes?: Record<string, NodeSize>;
}

/** 实测尺寸是否可用：React Flow 在首帧测量前给的是 undefined / 0 */
function usableSize(s: NodeSize | undefined): s is NodeSize {
  return (
    !!s &&
    typeof s.width === "number" &&
    typeof s.height === "number" &&
    Number.isFinite(s.width) &&
    Number.isFinite(s.height) &&
    s.width > 0 &&
    s.height > 0
  );
}

/**
 * 单个节点的估算渲染高度。
 *
 * 详情模式公式：
 *   边框 + 表头 + body padding
 *   + Σ每组(标题高 + 行数 × 行高) + (组数-1) × (flex gap + 组 margin)
 *   + (有 next 时 footer 高) + 徽标条高 + 余量
 */
export function estimateNodeHeight(def: TaskNodeDef, detailed = true): number {
  if (!detailed) return COMPACT_NODE_HEIGHT;

  const sections = nodeDetailRows(def);
  const hasNext = Array.isArray(def?.next) && def.next.length > 0;
  const footer = hasNext ? FOOTER_H : 0;

  // 详情组为空 → TaskNode 回退两行摘要（footer 仍会渲染）
  if (sections.length === 0) {
    return CHROME_H + SUMMARY_BODY_H + footer + SAFETY_MARGIN;
  }

  let sectionsH = 0;
  for (const s of sections) {
    sectionsH += SECTION_TITLE_H + s.rows.length * DETAIL_ROW_H;
  }
  sectionsH += (sections.length - 1) * (BODY_GAP + SECTION_MARGIN);

  return CHROME_H + BODY_PAD_H + sectionsH + footer + SAFETY_MARGIN;
}

/** 整份 doc 的逐节点估算高度（测试与布局共用同一套尺寸）。 */
export function estimateNodeHeights(
  doc: TaskDoc,
  opts: AutoLayoutOptions = {},
): Record<string, number> {
  const detailed = opts.detailed ?? true;
  const out: Record<string, number> = {};
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    out[name] = estimateNodeHeight(def, detailed);
  }
  return out;
}

/**
 * 每个节点最终参与布局的尺寸：**实测优先，估算兜底**。
 *
 * 只遍历 `doc.nodes` ⇒ `opts.sizes` 里的额外键（虚拟节点 `__task__` 等）天然被丢弃，
 * 不会跑进布局结果里。
 */
export function resolveNodeSizes(
  doc: TaskDoc,
  opts: AutoLayoutOptions = {},
): Record<string, NodeSize> {
  const detailed = opts.detailed ?? true;
  const out: Record<string, NodeSize> = {};
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    const measured = opts.sizes?.[name];
    out[name] = usableSize(measured)
      ? { width: measured.width, height: measured.height }
      : { width: NODE_WIDTH, height: estimateNodeHeight(def, detailed) };
  }
  return out;
}

/**
 * dagre 罢工时的兜底排布：按名字顺序码成网格（列宽/行高取最大节点尺寸）。
 *
 * 存在的意义不是「排得好看」，而是**任何输入都必须还给调用方一份有限有效的坐标**——
 * `autoLayout` 抛异常会让 `loadTask` 整条载入链路挂掉，页面永远停在「加载任务中…」，
 * 用户连手动拖节点的机会都没有。宁可给一个能看能拖的网格。
 */
function gridFallback(
  names: string[],
  sizes: Record<string, NodeSize>,
): Record<string, { x: number; y: number }> {
  const widths = names.map((n) => sizes[n]?.width ?? NODE_WIDTH);
  const heights = names.map((n) => sizes[n]?.height ?? COMPACT_NODE_HEIGHT);
  const colW = Math.max(NODE_WIDTH, ...widths) + 64;
  const rowH = Math.max(COMPACT_NODE_HEIGHT, ...heights) + 100;
  const cols = Math.max(1, Math.ceil(Math.sqrt(names.length)));
  const out: Record<string, { x: number; y: number }> = {};
  names.forEach((name, i) => {
    out[name] = { x: 40 + (i % cols) * colW, y: 40 + Math.floor(i / cols) * rowH };
  });
  return out;
}

export function autoLayout(
  doc: TaskDoc,
  opts: AutoLayoutOptions = {},
): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  // ranksep/nodesep 偏大：详情模式卡片高、连线密，留白不够会显得挤成一团
  g.setGraph({ rankdir: "TB", nodesep: 64, ranksep: 100, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));

  const sizes = resolveNodeSizes(doc, opts);
  const names = Object.keys(doc.nodes ?? {});
  if (names.length === 0) return {};
  for (const name of names) {
    const size = sizes[name] ?? { width: NODE_WIDTH, height: COMPACT_NODE_HEIGHT };
    g.setNode(name, { width: size.width, height: size.height });
  }
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    for (const target of def.next ?? []) {
      if (target in doc.nodes && target !== name) {
        g.setEdge(name, target, { weight: NEXT_EDGE_WEIGHT });
      }
    }
  }
  // on_timeout 目标若完全孤立（没有 next 入边），挂一条弱边保证它有位置
  for (const [name, def] of Object.entries(doc.nodes ?? {})) {
    const t = def.on_timeout;
    if (typeof t === "string" && t in doc.nodes && t !== name && !g.hasEdge(name, t)) {
      const hasIncoming = (g.inEdges(t) ?? []).length > 0;
      if (!hasIncoming) g.setEdge(name, t, { weight: TIMEOUT_EDGE_WEIGHT });
    }
  }

  try {
    dagre.layout(g);
  } catch (err) {
    console.warn("[autoLayout] dagre 布局失败，退回网格排布", err);
    return gridFallback(names, sizes);
  }

  const out: Record<string, { x: number; y: number }> = {};
  let fallback: Record<string, { x: number; y: number }> | undefined;
  for (const name of names) {
    const pos = g.node(name);
    // 输出左上角坐标：每个节点用它自己的尺寸回推，不能用统一常量
    const size = sizes[name] ?? { width: NODE_WIDTH, height: COMPACT_NODE_HEIGHT };
    if (pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
      out[name] = { x: pos.x - size.width / 2, y: pos.y - size.height / 2 };
    } else {
      // dagre 没给出（或给了 NaN）坐标：兜一个网格位，绝不把 NaN 传给画布
      fallback ??= gridFallback(names, sizes);
      out[name] = fallback[name];
    }
  }
  return out;
}
