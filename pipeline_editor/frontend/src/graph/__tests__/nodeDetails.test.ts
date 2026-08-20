import { describe, expect, it } from "vitest";

import type { TaskDoc, TaskNodeDef } from "../../types/task";
import {
  CUSTOM_PARAM_HINT,
  FIELD_HINTS,
  nodeDetailRows,
  type DetailRow,
  type DetailSection,
} from "../nodeDetails";
import { taskToGraph } from "../taskToGraph";

function rowsOf(sections: DetailSection[], kind: DetailSection["kind"]): Record<string, string> {
  const sec = sections.find((s) => s.kind === kind);
  if (!sec) return {};
  return Object.fromEntries(sec.rows.map((r) => [r.label, r.value]));
}

function rowsFull(
  sections: DetailSection[],
  kind: DetailSection["kind"],
): Record<string, DetailRow> {
  const sec = sections.find((s) => s.kind === kind);
  if (!sec) return {};
  return Object.fromEntries(sec.rows.map((r) => [r.label, r]));
}

describe("nodeDetailRows", () => {
  it("template 识别 + click 动作：分组标题与参数行", () => {
    const def: TaskNodeDef = {
      recognition: {
        type: "template",
        template: "btn_start.png",
        roi: [0, 0.5, 1, 1],
        threshold: 0.85,
        scales: [0.9, 1, 1.1],
        grayscale: false,
      },
      action: { type: "click", target: "recognized" },
      next: ["下一步"],
    };
    const sections = nodeDetailRows(def);
    expect(sections.map((s) => s.kind)).toEqual(["rec", "act"]); // 无 timeout 等 → 无「其他」组
    expect(sections[0].title).toBe("识别 · template");
    expect(sections[1].title).toBe("动作 · click");

    const rec = rowsOf(sections, "rec");
    expect(rec.template).toBe("btn_start.png");
    expect(rec.roi).toBe("[0,0.5,1,1]"); // 数组紧凑格式
    expect(rec.threshold).toBe("0.85");
    expect(rec.scales).toBe("[0.9,1,1.1]");
    expect(rec.grayscale).toBe("false"); // 显式 false 也是「已设置」

    expect(rowsOf(sections, "act")).toEqual({ target: "命中处" });
  });

  it("click 非命中处时输出 params 坐标；未知 params 键照实显示", () => {
    const sections = nodeDetailRows({
      recognition: { type: "always" },
      action: { type: "click", params: { x: 540, y: 1200, duration_ms: 80 } },
    });
    expect(rowsOf(sections, "act")).toEqual({ x: "540", y: "1200", duration_ms: "80" });
    // always 且无 roi → 识别组无行，整组不输出
    expect(sections.map((s) => s.kind)).toEqual(["act"]);
  });

  it("drag / key / wait 的 params 逐行输出", () => {
    const drag = rowsOf(
      nodeDetailRows({
        recognition: { type: "always" },
        action: { type: "drag", params: { x1: 100, y1: 200, x2: 300, y2: 400, duration_ms: 500 } },
      }),
      "act",
    );
    expect(drag).toEqual({ x1: "100", y1: "200", x2: "300", y2: "400", duration_ms: "500" });

    const key = nodeDetailRows({
      recognition: { type: "always" },
      action: { type: "key", params: { keycode: "BACK" } },
    });
    expect(key[0].title).toBe("动作 · key");
    expect(rowsOf(key, "act")).toEqual({ keycode: "BACK" });
  });

  it("and 组合：每个子识别一行（复用 recSummary），box_index 有值才显示", () => {
    const sections = nodeDetailRows({
      recognition: {
        type: "and",
        all_of: [
          { type: "ui_text", expected: "确认", roi: [0, 0, 1, 1] },
          { type: "template", template: "icon.png" },
        ],
        box_index: 1,
      },
      action: { type: "none" },
    });
    const rec = rowsOf(sections, "rec");
    expect(sections[0].title).toBe("识别 · and");
    expect(rec["子 1"]).toBe('ui_text "确认" roi✓');
    expect(rec["子 2"]).toBe("template [icon.png]");
    expect(rec.box_index).toBe("1");
    // action none 无行 → 动作组不输出
    expect(sections.map((s) => s.kind)).toEqual(["rec"]);
  });

  it("or 组合：box_index 不显示，子识别按 any_of 展开", () => {
    const rec = rowsOf(
      nodeDetailRows({
        recognition: {
          type: "or",
          any_of: [{ type: "ocr", expected: "A" }, { type: "yolo", label: "btn" }],
          box_index: 2,
        },
        action: { type: "none" },
      }),
      "rec",
    );
    expect(Object.keys(rec)).toEqual(["子 1", "子 2"]);
    expect(rec.box_index).toBeUndefined();
  });

  it("llm 动作按 agent 展示，text 截断", () => {
    const long =
      "请在弹窗里找到并点击那个非常长的确认按钮然后返回主界面继续执行后续步骤直到任务结束为止，中间遇到任何广告都直接关闭";
    expect(long.length).toBeGreaterThan(40);
    const sections = nodeDetailRows({
      recognition: { type: "always" },
      action: { type: "llm", text: long },
    });
    expect(sections[0].title).toBe("动作 · agent");
    const value = rowsOf(sections, "act").text;
    expect(value.endsWith("…")).toBe(true);
    expect(value.length).toBe(41); // 40 字符 + 省略号
    expect(long.startsWith(value.slice(0, -1))).toBe(true);
  });

  it("其他组：只显示节点上显式设置的时序字段，defaults 绝不渗入", () => {
    const doc: TaskDoc = {
      entry: "A",
      defaults: { timeout_ms: 9999, poll_interval_ms: 777, post_delay_ms: 555 },
      nodes: {
        A: {
          recognition: { type: "ocr", expected: "x" },
          action: { type: "none" },
          post_delay_ms: 300,
        },
      },
    };
    const sections = nodeDetailRows(doc.nodes.A);
    const misc = rowsOf(sections, "misc");
    expect(misc).toEqual({ post_delay_ms: "300" }); // 只有节点自己写的那一行
    expect(misc.timeout_ms).toBeUndefined();
    expect(misc.poll_interval_ms).toBeUndefined();
    expect(JSON.stringify(sections)).not.toContain("9999");
  });

  it("其他组：wait_still 摘要成一行，finding 带 severity 且截断", () => {
    const misc = rowsOf(
      nodeDetailRows({
        recognition: { type: "always" },
        action: { type: "none" },
        timeout_ms: 5000,
        wait_still: { timeout_ms: 3000, interval_ms: 500 },
        finding: {
          severity: "warning",
          message:
            "这里出现了一个非常长的问题描述用于验证截断行为是否生效，后面这一段文字纯粹是为了把长度顶过阈值",
        },
      }),
      "misc",
    );
    expect(misc.timeout_ms).toBe("5000");
    expect(misc.wait_still).toBe("timeout 3000 / interval 500");
    expect(misc.finding.startsWith("warning · 这里出现了")).toBe(true);
    expect(misc.finding.endsWith("…")).toBe(true);
  });

  it("finding 为字符串时也出行", () => {
    const misc = rowsOf(
      nodeDetailRows({
        recognition: { type: "always" },
        action: { type: "none" },
        finding: "登录失败",
      }),
      "misc",
    );
    expect(misc.finding).toBe("登录失败");
  });

  it("expected 超长字符串截断加省略号", () => {
    const rec = rowsOf(
      nodeDetailRows({
        recognition: { type: "ui_text", expected: "x".repeat(80) },
        action: { type: "none" },
      }),
      "rec",
    );
    expect(rec.expected).toBe(`${"x".repeat(40)}…`);
  });

  it("full 字段：截断时保留原文（供 Tooltip 全文），短值不设置 full，full 本身再超 200 也截断", () => {
    const short = "短文本，不需要截断";
    const shortRow = rowsFull(
      nodeDetailRows({
        recognition: { type: "ui_text", expected: short },
        action: { type: "none" },
      }),
      "rec",
    ).expected;
    expect(shortRow.value).toBe(short);
    expect(shortRow.full).toBeUndefined();

    const mid = "y".repeat(80); // > 40（value 截断）但 <= 200（full 不再截断）
    const midRow = rowsFull(
      nodeDetailRows({ recognition: { type: "ui_text", expected: mid }, action: { type: "none" } }),
      "rec",
    ).expected;
    expect(midRow.value).toBe(`${"y".repeat(40)}…`);
    expect(midRow.full).toBe(mid); // 完整原文，未再截断

    const long = "z".repeat(500); // > 200，full 自己也要截断
    const longRow = rowsFull(
      nodeDetailRows({ recognition: { type: "ui_text", expected: long }, action: { type: "none" } }),
      "rec",
    ).expected;
    expect(longRow.value).toBe(`${"z".repeat(40)}…`);
    expect(longRow.full).toBe(`${"z".repeat(200)}…`);
    expect(longRow.full?.length).toBe(201);
  });

  it("常用字段带中文说明 hint（label 的 Tooltip 用它）", () => {
    const sections = nodeDetailRows({
      recognition: { type: "template", template: "btn.png", roi: [0, 0, 1, 1], threshold: 0.9 },
      action: { type: "click", target: "recognized" },
      timeout_ms: 8000,
    });
    const hintOf = (kind: DetailSection["kind"], label: string): string | undefined =>
      sections.find((s) => s.kind === kind)?.rows.find((r) => r.label === label)?.hint;
    expect(hintOf("rec", "roi")).toContain("识别区域");
    expect(hintOf("rec", "template")).toContain("模板图路径");
    // 时序字段口径与 NodeInspector 的时序调参一致
    expect(hintOf("misc", "timeout_ms")).toBe(FIELD_HINTS.timeout_ms);
    expect(FIELD_HINTS.timeout_ms).toContain("识别轮询总预算");
  });

  it("custom 的未知 params 统一给「透传」说明，已收录的键用自己的说明", () => {
    const sections = nodeDetailRows({
      recognition: { type: "always" },
      action: {
        type: "custom",
        // max_swipes 是 swipe_until 的私有参数（表里没收录）；duration_ms 是已收录的通用字段
        name: "swipe_until",
        params: { max_swipes: 30, duration_ms: 500 },
      },
    });
    const act = sections.find((s) => s.kind === "act")!;
    const byLabel = Object.fromEntries(act.rows.map((r) => [r.label, r.hint]));
    expect(byLabel.max_swipes).toBe(CUSTOM_PARAM_HINT);
    expect(byLabel.duration_ms).toBe(FIELD_HINTS.duration_ms);
    expect(byLabel.name).toBe(FIELD_HINTS.name);
    // 非 custom 动作的未知 params 不硬塞说明
    const drag = nodeDetailRows({
      recognition: { type: "always" },
      action: { type: "drag", params: { x1: 1, y1: 2 } },
    }).find((s) => s.kind === "act")!;
    expect(drag.rows.every((r) => r.hint === undefined)).toBe(true);
  });

  it("custom 动作显示 name", () => {
    expect(
      rowsOf(
        nodeDetailRows({
          recognition: { type: "always" },
          action: { type: "custom", name: "restart_app" },
        }),
        "act",
      ),
    ).toEqual({ name: "restart_app" });
  });
});

describe("taskToGraph 详情开关", () => {
  const doc: TaskDoc = {
    entry: "A",
    nodes: {
      A: {
        recognition: { type: "ui_text", expected: "开始" },
        action: { type: "click", target: "recognized" },
        next: ["B"],
      },
      B: { recognition: { type: "always" }, action: { type: "none" }, next: [] },
    },
  };

  it("showNodeDetails 关闭（默认）时不派生 details", () => {
    const { nodes } = taskToGraph(doc, { layout: {} });
    const a = nodes.find((n) => n.id === "A")!;
    expect(a.data.showDetails).toBe(false);
    expect(a.data.details).toEqual([]);
    expect(a.data.recSummary).toBe('ui_text "开始"'); // 简洁模式摘要仍在
  });

  it("showNodeDetails 打开时派生分组行与 next 目标", () => {
    const { nodes } = taskToGraph(doc, { layout: {}, showNodeDetails: true });
    const a = nodes.find((n) => n.id === "A")!;
    expect(a.data.showDetails).toBe(true);
    expect(a.data.details.map((s) => s.kind)).toEqual(["rec", "act"]);
    expect(a.data.nextTargets).toEqual(["B"]);
    const b = nodes.find((n) => n.id === "B")!;
    expect(b.data.nextTargets).toEqual([]);
  });
});
