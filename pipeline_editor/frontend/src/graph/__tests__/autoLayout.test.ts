import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskDoc } from "../../types/task";
import {
  COMPACT_NODE_HEIGHT,
  NODE_WIDTH,
  autoLayout,
  estimateNodeHeight,
  estimateNodeHeights,
  resolveNodeSizes,
} from "../autoLayout";

/**
 * 样例任务 `sample_flow`：按真实任务的规模与结构构造（8 节点 + 环形重试 + 多出口），
 * 未做任何简化。选它是因为「主操作执行 / 主操作重试」的 custom 动作带 10 个 params
 * + 3 个时序字段，详情模式下要渲染 16 行——固定高度 180 时正是这类节点在画布上叠成一团。
 *
 * 动作名 `demo_action` 是合成样例（不对应任何内置 handler），只为凑出「参数很多」的渲染场景。
 */
function sampleStageLoop(): TaskDoc {
  const demoAction = {
    type: "custom" as const,
    name: "demo_action",
    params: {
      target_label: "item_a",
      mode: "auto",
      counter_roi: [46, 146, 469, 239],
      conf: 0.25,
      max_empty_scans: 30,
      budget_ms: 90000,
      scan_step_px: 400,
      repeat_target: 2,
      gain_x: 1.616,
      gain_y: 2.275,
    },
  };
  return {
    entry: "流程就绪",
    max_steps: 14,
    back_fallback: false,
    watchdogs: [
      { type: "ocr", expected: "数据错误", severity: "critical", message: "本轮中止", fail_task: true },
      { type: "ocr", expected: "[Code]", severity: "error", message: "服务器错误弹窗" },
      { type: "blank_screen", threshold: 5, severity: "warning", message: "疑似黑屏/白屏" },
    ],
    nodes: {
      流程就绪: {
        recognition: { type: "ocr", expected: "进行中", roi: [0, 60, 1080, 400] },
        action: { type: "none" },
        next: ["主操作执行"],
        timeout_ms: 15000,
        poll_interval_ms: 1500,
        on_timeout: "流程未就绪",
      },
      流程未就绪: {
        recognition: { type: "always" },
        action: { type: "none" },
        finding: { severity: "error", message: "任务起跑时未识别到目标界面标识" },
        next: [],
      },
      主操作执行: {
        recognition: { type: "ocr", expected: "进行中", roi: [0, 60, 1080, 400] },
        action: demoAction,
        next: ["流程成功", "流程失败", "主操作重试", "流程状态未知"],
        timeout_ms: 15000,
        poll_interval_ms: 1200,
        on_timeout: "流程状态未知",
        post_delay_ms: 2500,
      },
      主操作重试: {
        recognition: { type: "ocr", expected: "进行中", roi: [0, 60, 1080, 400] },
        action: demoAction,
        next: ["流程成功", "流程失败", "主操作重试", "流程状态未知"],
        timeout_ms: 15000,
        poll_interval_ms: 1200,
        on_timeout: "流程状态未知",
        post_delay_ms: 2500,
      },
      流程成功: {
        recognition: {
          type: "or",
          any_of: [
            { type: "ocr", expected: "成功", roi: [150, 150, 930, 800] },
            { type: "ocr", expected: "已完成", roi: [150, 150, 930, 800] },
            { type: "ocr", expected: "完成", roi: [150, 150, 930, 800] },
            { type: "ocr", expected: "下一步", roi: [100, 1700, 1080, 2300] },
          ],
        },
        action: { type: "none" },
        finding: { severity: "info", message: "样例流程本轮执行成功" },
        next: [],
      },
      流程失败: {
        recognition: { type: "ocr", expected: "失败", roi: [100, 150, 980, 900] },
        action: { type: "none" },
        finding: { severity: "warning", message: "样例流程本轮判负" },
        next: [],
      },
      流程状态未知: {
        recognition: { type: "always" },
        action: { type: "none" },
        finding: { severity: "warning", message: "闭环一轮后画面停在未知状态" },
        next: ["流程成功", "流程失败", "主操作重试", "冒烟收尾"],
        timeout_ms: 15000,
        poll_interval_ms: 1500,
        on_timeout: "冒烟收尾",
      },
      冒烟收尾: {
        recognition: { type: "always" },
        action: { type: "none" },
        finding: { severity: "error", message: "流程状态无法判定，收尾交还 runner" },
        next: [],
      },
    },
  };
}

/** 一条 10 节点直链，节点参数逐个变多——单列场景最容易暴露高度估算不足。 */
function chainDoc(): TaskDoc {
  const nodes: TaskDoc["nodes"] = {};
  for (let i = 0; i < 10; i++) {
    const params: Record<string, unknown> = {};
    for (let p = 0; p < i; p++) params[`p${p}`] = p * 100;
    nodes[`N${i}`] = {
      recognition: { type: "ocr", expected: `文案${i}`, roi: [0, 0, 1080, 400], threshold: 0.8 },
      action: { type: "custom", name: "step", params },
      next: i < 9 ? [`N${i + 1}`] : [],
      timeout_ms: 5000,
      poll_interval_ms: 500,
      post_delay_ms: 200,
    };
  }
  return { entry: "N0", nodes };
}

/**
 * 样例任务 `sample_boot`：按真实启动任务的规模构造的合并态（合并 includes 后的展示态，
 * 18 节点 = 3 个自有 + 15 个 include 引入），节点数与边结构未做简化。
 *
 * 选它是因为这类图触发过 `TypeError: Reduce of empty array with no initial value`：
 * 这份图**不连通**——「打开GM面板 / GM面板不可用 / GM面板就绪 / GM铺垫跳过」与主启动链之间
 * 没有任何 next/on_timeout 边。图不连通时 dagre 会加 `_root` 虚拟节点桥接各连通分量，
 * 若此时图里还存在小数权重的边（老实现给 on_timeout 弱边设了 `weight: 0.1`），
 * network-simplex 的割值会攒出 -2.8e-17 的浮点噪声被误判为负割值，
 * 随后 `enterEdge` 找不到任何可替换边 → 对空数组 `reduce` 抛错 → 页面卡在「加载任务中…」。
 */
function sampleBoot(): TaskDoc {
  const always = { type: "always" as const };
  const none = { type: "none" as const };
  /** finding 兜底节点：识别 always + 动作 none + 一条 finding */
  const dead = (
    message: string,
    severity: "info" | "warning" | "error",
    next: string[] = [],
  ) => ({
    recognition: always,
    action: none,
    finding: { severity, message },
    next,
  });
  return {
    entry: "冷启动应用",
    max_steps: 30,
    back_fallback: false,
    _merge: {
      includes: ["common/boot_to_home.json", "common/gm_boot.json"],
      conflicts: ["登录页确认"],
      include_map: {
        冷启动应用: "common/boot_to_home.json",
        登录页确认: "common/gm_boot.json",
        登录页未出现: "common/boot_to_home.json",
        勾选跳过引导: "common/boot_to_home.json",
        跳过引导选项未找到: "common/boot_to_home.json",
        点击登录: "common/boot_to_home.json",
        登录按钮未找到: "common/boot_to_home.json",
        主页确认: "common/boot_to_home.json",
        主页加载慢: "common/boot_to_home.json",
        主页二次确认: "common/boot_to_home.json",
        主页未进入: "common/boot_to_home.json",
        开启GM模式: "common/gm_boot.json",
        GM开关未找到: "common/gm_boot.json",
        打开GM面板: "common/gm_boot.json",
        GM面板不可用: "common/gm_boot.json",
        用例开始: "<task>",
        GM面板就绪: "<task>",
        GM铺垫跳过: "<task>",
      },
    },
    nodes: {
      冷启动应用: {
        recognition: always,
        action: {
          type: "custom",
          name: "launch_app",
          params: { package: "com.example.app", force_stop: true, settle_ms: 2000 },
        },
        next: ["登录页确认"],
        post_delay_ms: 3000,
      },
      登录页确认: {
        recognition: { type: "ocr", expected: "登录", roi: [300, 2150, 950, 2448] },
        action: none,
        next: ["开启GM模式"],
        timeout_ms: 60000,
        poll_interval_ms: 2500,
        on_timeout: "登录页未出现",
      },
      登录页未出现: dead("登录页未出现，改走主页确认", "info", ["主页确认"]),
      勾选跳过引导: {
        recognition: { type: "ocr", expected: "跳过新手引导", roi: [40, 1920, 360, 1990] },
        action: {
          type: "custom",
          name: "ensure_checkbox",
          params: {
            probe: { x: 45, y: 1958 },
            tap: { x: 68, y: 1952 },
            checked_rgb: [85, 149, 52],
            tolerance: 60,
          },
        },
        next: ["点击登录"],
        timeout_ms: 8000,
        poll_interval_ms: 1000,
        on_timeout: "跳过引导选项未找到",
        post_delay_ms: 800,
      },
      跳过引导选项未找到: dead("登录页未找到『是否跳过新手引导』选项", "warning", ["点击登录"]),
      点击登录: {
        recognition: { type: "ocr", expected: "登录", roi: [300, 2150, 950, 2448] },
        action: { type: "click", target: "recognized" },
        next: ["主页确认"],
        timeout_ms: 15000,
        poll_interval_ms: 1500,
        on_timeout: "登录按钮未找到",
        post_delay_ms: 4000,
      },
      登录按钮未找到: dead("点击时未再识别到底部『登录』按钮", "warning", ["主页确认"]),
      主页确认: {
        recognition: { type: "ocr", expected: "主页", roi: [0, 2280, 1080, 2448] },
        action: none,
        next: ["用例开始"],
        timeout_ms: 60000,
        poll_interval_ms: 3000,
        on_timeout: "主页加载慢",
      },
      主页加载慢: dead("登录后 60 秒内未进入主页", "warning", ["主页二次确认"]),
      主页二次确认: {
        recognition: { type: "ocr", expected: "主页", roi: [0, 2280, 1080, 2448] },
        action: none,
        next: ["用例开始"],
        timeout_ms: 60000,
        poll_interval_ms: 3000,
        on_timeout: "主页未进入",
      },
      主页未进入: dead("登录后约 120 秒仍未进入主页", "error"),
      开启GM模式: {
        recognition: { type: "ocr", expected: "GM", roi: [0, 90, 280, 230] },
        action: { type: "click", target: "recognized" },
        next: ["勾选跳过引导"],
        timeout_ms: 8000,
        poll_interval_ms: 1000,
        on_timeout: "GM开关未找到",
        post_delay_ms: 1500,
      },
      GM开关未找到: dead("未识别到 GM 开关", "warning", ["勾选跳过引导"]),
      // ↓ 以下 4 个节点与上面的主启动链之间没有任何边 —— 正是「图不连通」的来源
      打开GM面板: {
        recognition: { type: "ocr", expected: "GM", roi: [0, 90, 280, 230] },
        action: { type: "click", target: "recognized" },
        next: ["GM面板就绪"],
        timeout_ms: 8000,
        poll_interval_ms: 1000,
        on_timeout: "GM面板不可用",
        post_delay_ms: 1200,
      },
      GM面板不可用: dead("GM 面板打不开", "error", ["GM铺垫跳过"]),
      用例开始: { recognition: always, action: none, next: [] },
      GM面板就绪: { recognition: always, action: none, next: [] },
      GM铺垫跳过: dead("GM 铺垫整体跳过", "warning"),
    },
  };
}

/**
 * 上面那份图的最小化形态（对 sample_boot 逐节点删剩下的 4 个节点，老实现同样抛错）：
 * 一个完全孤立的节点 + 一个「弱边（on_timeout）打头」的小分量。
 */
function minimalDisconnected(): TaskDoc {
  const bare = { recognition: { type: "always" as const }, action: { type: "none" as const } };
  return {
    entry: "起点",
    nodes: {
      孤立: { ...bare, next: [] },
      起点: { ...bare, next: [], on_timeout: "超时兜底" },
      超时兜底: { ...bare, next: ["收尾"] },
      收尾: { ...bare, next: [] },
    },
  };
}

interface Rect {
  name: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

function rects(doc: TaskDoc, detailed: boolean): Rect[] {
  const layout = autoLayout(doc, { detailed });
  const heights = estimateNodeHeights(doc, { detailed });
  return Object.keys(doc.nodes).map((name) => ({
    name,
    x: layout[name].x,
    y: layout[name].y,
    w: NODE_WIDTH,
    h: heights[name],
  }));
}

/** 两矩形是否真的相交（共边不算）。 */
function overlaps(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function expectNoOverlap(list: Rect[]): void {
  const bad: string[] = [];
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      if (overlaps(list[i], list[j])) bad.push(`${list[i].name} × ${list[j].name}`);
    }
  }
  expect(bad).toEqual([]);
}

describe("estimateNodeHeight", () => {
  it("参数行多的节点估算高度显著大于简洁节点", () => {
    const doc = sampleStageLoop();
    const heavy = estimateNodeHeight(doc.nodes.主操作执行); // 识别 2 行 + 动作 11 行 + 其他 3 行
    const light = estimateNodeHeight(doc.nodes.冒烟收尾); // 其他 1 行（finding），无 next
    expect(heavy).toBeGreaterThan(light * 2);
    expect(heavy).toBeGreaterThan(300);
    expect(light).toBeLessThan(150);
  });

  it("行数单调增长时估算高度单调增长", () => {
    const doc = chainDoc();
    const hs = Object.keys(doc.nodes).map((n) => estimateNodeHeight(doc.nodes[n]));
    for (let i = 1; i < hs.length; i++) {
      // 最后一个节点没有 next（少一截 footer），只比较前 9 个同构节点
      if (i < hs.length - 1) expect(hs[i]).toBeGreaterThan(hs[i - 1]);
    }
  });

  it("详情组为空的节点按简洁高度估算（TaskNode 回退两行摘要）", () => {
    // always + none 且无时序字段 → nodeDetailRows 返回空数组
    const bare = { recognition: { type: "always" as const }, action: { type: "none" as const } };
    expect(estimateNodeHeight(bare)).toBe(COMPACT_NODE_HEIGHT);
    // 有 next 时多一条 footer
    expect(estimateNodeHeight({ ...bare, next: ["X"] })).toBeGreaterThan(COMPACT_NODE_HEIGHT);
  });

  it("简洁模式所有节点统一用紧凑高度", () => {
    const doc = sampleStageLoop();
    const hs = estimateNodeHeights(doc, { detailed: false });
    expect(new Set(Object.values(hs))).toEqual(new Set([COMPACT_NODE_HEIGHT]));
    expect(COMPACT_NODE_HEIGHT).toBeLessThan(130); // 老的固定 180 之下，画布更紧凑
  });

  it("默认 detailed=true（对齐 uiStore.showNodeDetails 默认值）", () => {
    const doc = sampleStageLoop();
    expect(estimateNodeHeights(doc)).toEqual(estimateNodeHeights(doc, { detailed: true }));
    expect(estimateNodeHeights(doc).主操作执行).toBeGreaterThan(COMPACT_NODE_HEIGHT);
  });
});

describe("实测尺寸覆盖估算（opts.sizes）", () => {
  it("有实测值的节点用实测值，估算被完全绕开", () => {
    const doc = sampleStageLoop();
    const estimated = estimateNodeHeights(doc, { detailed: true });
    const sizes = resolveNodeSizes(doc, {
      detailed: true,
      sizes: { 主操作执行: { width: 300, height: 512 } },
    });
    expect(sizes.主操作执行).toEqual({ width: 300, height: 512 });
    expect(sizes.主操作执行.height).not.toBe(estimated.主操作执行);
  });

  it("部分覆盖：没给实测的节点仍走估算", () => {
    const doc = sampleStageLoop();
    const estimated = estimateNodeHeights(doc, { detailed: true });
    const sizes = resolveNodeSizes(doc, {
      detailed: true,
      sizes: { 主操作执行: { width: 300, height: 512 } },
    });
    expect(sizes.主操作执行.height).toBe(512);
    for (const name of Object.keys(doc.nodes)) {
      if (name === "主操作执行") continue;
      expect(sizes[name]).toEqual({ width: NODE_WIDTH, height: estimated[name] });
    }
  });

  it("不合法的实测值（0 / NaN / 缺字段）回退估算", () => {
    const doc = chainDoc();
    const estimated = estimateNodeHeights(doc, { detailed: true });
    const sizes = resolveNodeSizes(doc, {
      detailed: true,
      sizes: {
        N0: { width: 0, height: 0 },
        N1: { width: 260, height: Number.NaN },
        N2: { width: 260, height: 999 },
      },
    });
    expect(sizes.N0).toEqual({ width: NODE_WIDTH, height: estimated.N0 });
    expect(sizes.N1).toEqual({ width: NODE_WIDTH, height: estimated.N1 });
    expect(sizes.N2).toEqual({ width: 260, height: 999 });
  });

  it("虚拟节点 __task__ 不参与布局（sizes 里带上也一样）", () => {
    const doc = sampleStageLoop();
    const opts = {
      detailed: true,
      sizes: { __task__: { width: 180, height: 60 }, 冒烟收尾: { width: 260, height: 200 } },
    };
    expect(resolveNodeSizes(doc, opts).__task__).toBeUndefined();
    const layout = autoLayout(doc, opts);
    expect(layout.__task__).toBeUndefined();
    expect(Object.keys(layout).sort()).toEqual(Object.keys(doc.nodes).sort());
    // 传了不存在的键也不影响真实节点的布局结果
    expect(layout).toEqual(
      autoLayout(doc, { detailed: true, sizes: { 冒烟收尾: { width: 260, height: 200 } } }),
    );
  });

  it("autoLayout 按实测高度拉开间距，并用实测尺寸回推左上角", () => {
    const doc = chainDoc();
    const MEASURED = { width: 260, height: 520 };
    const sizes = Object.fromEntries(Object.keys(doc.nodes).map((n) => [n, MEASURED]));
    const layout = autoLayout(doc, { detailed: true, sizes });
    const ys = Object.keys(doc.nodes)
      .map((n) => layout[n].y)
      .sort((a, b) => a - b);
    for (let i = 1; i < ys.length; i++) {
      expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(MEASURED.height);
    }
    // 估算高度远小于 520 ⇒ 若还在用估算，间距会明显更小
    const estimated = autoLayout(doc, { detailed: true });
    const eys = Object.keys(doc.nodes)
      .map((n) => estimated[n].y)
      .sort((a, b) => a - b);
    expect(ys[ys.length - 1] - ys[0]).toBeGreaterThan(eys[eys.length - 1] - eys[0]);
  });
});

describe("autoLayout 无重叠", () => {
  it("样例任务 sample_flow：详情模式两两节点矩形不相交", () => {
    expectNoOverlap(rects(sampleStageLoop(), true));
  });

  it("样例任务 sample_flow：简洁模式同样不相交", () => {
    expectNoOverlap(rects(sampleStageLoop(), false));
  });

  it("10 节点直链：同一列相邻节点垂直间距 ≥ 上节点估算高度", () => {
    const list = rects(chainDoc(), true);
    const byColumn = new Map<number, Rect[]>();
    for (const r of list) {
      const col = byColumn.get(r.x) ?? [];
      col.push(r);
      byColumn.set(r.x, col);
    }
    let checked = 0;
    for (const col of byColumn.values()) {
      col.sort((a, b) => a.y - b.y);
      for (let i = 1; i < col.length; i++) {
        expect(col[i].y - col[i - 1].y).toBeGreaterThanOrEqual(col[i - 1].h);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(0); // 确认真的比对过相邻对，不是空跑
    expectNoOverlap(list);
  });

  it("输出的左上角 y 用节点自己的高度回推，不是统一常量", () => {
    // A → {重节点 B, 轻节点 C}：B/C 同一 rank，dagre 给它们同一个中心 y
    const doc: TaskDoc = {
      entry: "A",
      nodes: {
        A: { recognition: { type: "always" }, action: { type: "none" }, next: ["B", "C"] },
        B: {
          recognition: { type: "ocr", expected: "x", roi: [0, 0, 1, 1], threshold: 0.9 },
          action: { type: "custom", name: "f", params: { a: 1, b: 2, c: 3, d: 4, e: 5 } },
          timeout_ms: 1000,
          poll_interval_ms: 200,
          post_delay_ms: 100,
          next: [],
        },
        C: { recognition: { type: "always" }, action: { type: "none" }, next: [] },
      },
    };
    const layout = autoLayout(doc, { detailed: true });
    const h = estimateNodeHeights(doc, { detailed: true });
    expect(h.B).toBeGreaterThan(h.C);
    // 同 rank 中心线对齐 ⇒ 两者 top 必然不同（若统一用 NODE_HEIGHT/2 回推则会相等）
    expect(layout.B.y + h.B / 2).toBeCloseTo(layout.C.y + h.C / 2, 6);
    expect(layout.B.y).not.toBe(layout.C.y);
  });
});

/**
 * 回归：不连通的图 + on_timeout 弱边曾让 dagre 的 network-simplex 抛
 * `Reduce of empty array with no initial value`，整个编辑器卡在「加载任务中…」。
 * 这一组用例锁的是「任何形态都必须还回一份有限有效的坐标，且绝不抛异常」。
 */
describe("不连通图 / 退化形态不抛异常", () => {
  /**
   * `autoLayout` 里 dagre 罢工会被 try/catch 兜成网格排布，只断言「不抛」是抓不到回归的
   * ——所以这里同时盯住 `console.warn`：样例任务必须由 dagre 正常算完，一次兜底都不许有。
   */
  let warn: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => {
    warn.mockRestore();
  });

  function expectFiniteLayout(
    doc: TaskDoc,
    detailed: boolean,
  ): Record<string, { x: number; y: number }> {
    let layout: Record<string, { x: number; y: number }> = {};
    expect(() => {
      layout = autoLayout(doc, { detailed });
    }).not.toThrow();
    expect(warn).not.toHaveBeenCalled(); // dagre 没走兜底
    expect(Object.keys(layout).sort()).toEqual(Object.keys(doc.nodes).sort());
    for (const [name, p] of Object.entries(layout)) {
      expect(Number.isFinite(p.x), `${name}.x=${p.x}`).toBe(true);
      expect(Number.isFinite(p.y), `${name}.y=${p.y}`).toBe(true);
    }
    return layout;
  }

  /** 每条 next 边的目标必须落在来源下方（DAG 分层的最基本保证，网格兜底达不到） */
  function expectNextFlowsDownward(doc: TaskDoc, layout: Record<string, { x: number; y: number }>) {
    const h = estimateNodeHeights(doc, { detailed: true });
    let checked = 0;
    for (const [name, def] of Object.entries(doc.nodes)) {
      for (const target of def.next ?? []) {
        if (!(target in doc.nodes) || target === name) continue;
        const from = layout[name].y + h[name] / 2;
        const to = layout[target].y + h[target] / 2;
        expect(to, `${name} → ${target}`).toBeGreaterThan(from);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(0);
  }

  it("样例任务 sample_boot（3 自有 + 15 include，两个互不相连的子图）：详情模式正常算完", () => {
    const doc = sampleBoot();
    expect(Object.keys(doc.nodes)).toHaveLength(18);
    expectNextFlowsDownward(doc, expectFiniteLayout(doc, true));
  });

  it("样例任务 sample_boot：简洁模式同样正常算完", () => {
    expectFiniteLayout(sampleBoot(), false);
  });

  it("样例任务 sample_boot：include 来源节点照常参与布局（不因只读被漏掉）", () => {
    const doc = sampleBoot();
    const layout = autoLayout(doc, { detailed: true });
    // 15 个 include 节点 + 3 个自有节点全部有坐标
    expect(Object.keys(layout)).toHaveLength(18);
    expect(layout["冷启动应用"]).toBeDefined(); // include 来源
    expect(layout["用例开始"]).toBeDefined(); // <task> 自有
  });

  it("样例任务 sample_boot：详情模式两两节点矩形不相交", () => {
    expectNoOverlap(rects(sampleBoot(), true));
  });

  it("最小复现形态：孤立节点 + on_timeout 弱边打头的分量", () => {
    expectFiniteLayout(minimalDisconnected(), true);
    expectFiniteLayout(minimalDisconnected(), false);
  });

  it("on_timeout 弱边仍然生效：孤立的超时目标被排到 source 下方", () => {
    const layout = autoLayout(minimalDisconnected(), { detailed: true });
    // 弱边只是权重低，不是不建边——超时兜底必须落在起点的下一层
    expect(layout["超时兜底"].y).toBeGreaterThan(layout["起点"].y);
    expect(layout["收尾"].y).toBeGreaterThan(layout["超时兜底"].y);
  });

  it("全部节点互不相连（每个节点自成一个连通分量）", () => {
    const bare = { recognition: { type: "always" as const }, action: { type: "none" as const } };
    const nodes: TaskDoc["nodes"] = {};
    for (let i = 0; i < 12; i++) nodes[`孤${i}`] = { ...bare, next: [] };
    const layout = expectFiniteLayout({ entry: "孤0", nodes }, true);
    // 退化形态也不能把所有节点摞在同一个点上
    expect(new Set(Object.values(layout).map((p) => `${p.x},${p.y}`)).size).toBe(12);
  });

  it("空 doc（没有任何节点）返回空布局，不抛", () => {
    expect(autoLayout({ entry: "", nodes: {} }, { detailed: true })).toEqual({});
  });

  it("实测尺寸下同样不抛（工具条重排走的是这条路径）", () => {
    const doc = sampleBoot();
    const sizes = Object.fromEntries(
      Object.keys(doc.nodes).map((n, i) => [n, { width: 260, height: 140 + i * 7 }]),
    );
    const layout = autoLayout(doc, { detailed: true, sizes });
    expect(Object.keys(layout)).toHaveLength(18);
    for (const p of Object.values(layout)) {
      expect(Number.isFinite(p.x) && Number.isFinite(p.y)).toBe(true);
    }
  });
});
