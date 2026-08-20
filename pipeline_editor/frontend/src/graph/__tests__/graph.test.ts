import { describe, expect, it } from "vitest";

import type { TaskDoc } from "../../types/task";
import { autoLayout } from "../autoLayout";
import { localLint } from "../localLint";
import { applyRename, renameImpact } from "../rename";
import { serializeForSave } from "../serialize";
import { taskToGraph } from "../taskToGraph";

function sampleDoc(): TaskDoc {
  return {
    entry: "A",
    _comment: "用户注释要保留",
    on_finding: "C",
    watchdogs: [{ type: "ocr", expected: "网络错误", skip_to: "C" }],
    nodes: {
      A: {
        recognition: { type: "ocr", expected: "开始" },
        action: { type: "click", target: "recognized" },
        next: ["B", "C"],
        on_timeout: "C",
      },
      B: {
        recognition: { type: "template", template: "btn" },
        action: { type: "none" },
        next: [],
      },
      C: {
        recognition: { type: "ui_text", expected: "主界面" },
        action: { type: "none" },
        next: [],
        _comment: "节点注释",
      },
      INC: {
        recognition: { type: "always" },
        action: { type: "none" },
        next: [],
      },
    },
    includes: ["common/frag.json"],
    _merge: {
      includes: ["D:/x/common/frag.json"],
      conflicts: [],
      include_map: { A: "<task>", B: "<task>", C: "<task>", INC: "common/frag.json" },
    },
    _steps: { A: "1", B: "2", C: "1.1", INC: "?" },
  };
}

describe("serializeForSave", () => {
  it("剔除 include 节点与生成键，保留 includes 引用和 _comment", () => {
    const out = serializeForSave(sampleDoc());
    expect(out.nodes).not.toHaveProperty("INC");
    expect(out.nodes).toHaveProperty("A");
    expect(out).not.toHaveProperty("_merge");
    expect(out).not.toHaveProperty("_steps");
    expect(out.includes).toEqual(["common/frag.json"]);
    expect(out._comment).toBe("用户注释要保留");
    expect(out.nodes.C._comment).toBe("节点注释");
  });

  it("无 _merge 时全部节点视为主文件", () => {
    const doc = sampleDoc();
    delete doc._merge;
    const out = serializeForSave(doc);
    expect(Object.keys(out.nodes)).toEqual(["A", "B", "C", "INC"]);
  });
});

describe("taskToGraph", () => {
  it("生成三类边并保留 next 顺序", () => {
    const doc = sampleDoc();
    const { nodes, edges } = taskToGraph(doc, { layout: {}, steps: doc._steps });
    expect(nodes).toHaveLength(4);
    const nextEdges = edges.filter((e) => e.type === "nextEdge");
    expect(nextEdges.map((e) => `${e.source}→${e.target}`)).toEqual(["A→B", "A→C"]);
    expect(nextEdges[0].data?.order).toBe(0);
    expect(nextEdges[1].data?.order).toBe(1);
    expect(edges.filter((e) => e.type === "timeoutEdge")).toHaveLength(1);
    const nodeB = nodes.find((n) => n.id === "B")!;
    expect(nodeB.data.isTerminal).toBe(true);
    const inc = nodes.find((n) => n.id === "INC")!;
    expect(inc.data.readonly).toBe(true);
    expect(inc.data.includeFrom).toBe("common/frag.json");
  });

  it("跳转层：on_finding + watchdog skip_to 汇成任务级节点", () => {
    const { nodes, edges } = taskToGraph(sampleDoc(), {
      layout: {},
      showJumpEdges: true,
    });
    expect(nodes.some((n) => n.id === "__task__")).toBe(true);
    const jumps = edges.filter((e) => e.type === "jumpEdge");
    expect(jumps).toHaveLength(1); // C 同时被 on_finding 和 watchdog 引用，合并为一条
    expect(jumps[0].target).toBe("C");
  });
});

describe("rename", () => {
  it("级联 entry / next / on_timeout / on_finding / watchdog.skip_to / include_map", () => {
    const doc = sampleDoc();
    const impact = renameImpact(doc, "C");
    expect(impact.onFinding).toBe(true);
    expect(impact.nextRefs).toEqual(["A"]);
    expect(impact.timeoutRefs).toEqual(["A"]);
    expect(impact.watchdogRefs).toEqual([0]);

    applyRename(doc, "C", "主界面确认");
    expect(doc.nodes).toHaveProperty("主界面确认");
    expect(doc.nodes).not.toHaveProperty("C");
    expect(doc.nodes.A.next).toEqual(["B", "主界面确认"]);
    expect(doc.nodes.A.on_timeout).toBe("主界面确认");
    expect(doc.on_finding).toBe("主界面确认");
    expect(doc.watchdogs?.[0].skip_to).toBe("主界面确认");
    expect(doc._merge?.include_map["主界面确认"]).toBe("<task>");
  });

  it("改名保持 nodes 键顺序", () => {
    const doc = sampleDoc();
    applyRename(doc, "B", "B2");
    expect(Object.keys(doc.nodes)).toEqual(["A", "B2", "C", "INC"]);
  });
});

describe("localLint", () => {
  it("悬空引用报 error", () => {
    const doc = sampleDoc();
    doc.nodes.A.next = ["B", "不存在"];
    const warnings = localLint(doc);
    expect(warnings.some((w) => w.level === "error" && w.node === "A")).toBe(true);
  });

  it("≥2 个 ui_text 子识别报黄色警告", () => {
    const doc = sampleDoc();
    doc.nodes.A.recognition = {
      type: "or",
      any_of: [
        { type: "ui_text", expected: "x" },
        { type: "ui_text", expected: "y" },
      ],
    };
    const warnings = localLint(doc);
    expect(warnings.some((w) => w.level === "warning" && w.node === "A")).toBe(true);
  });

  it("嵌套超 2 层报 error", () => {
    const doc = sampleDoc();
    doc.nodes.A.recognition = {
      type: "and",
      all_of: [
        {
          type: "or",
          any_of: [
            { type: "and", all_of: [{ type: "ocr", expected: "x" }] },
          ],
        },
      ],
    };
    const warnings = localLint(doc);
    expect(warnings.some((w) => w.level === "error" && w.node === "A")).toBe(true);
  });
});

describe("autoLayout", () => {
  it("为每个节点给出坐标", () => {
    const layout = autoLayout(sampleDoc());
    expect(Object.keys(layout).sort()).toEqual(["A", "B", "C", "INC"].sort());
    for (const pos of Object.values(layout)) {
      expect(Number.isFinite(pos.x)).toBe(true);
      expect(Number.isFinite(pos.y)).toBe(true);
    }
  });
});
