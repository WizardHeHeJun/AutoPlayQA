import { describe, expect, it } from "vitest";

import type { TaskDoc } from "../../types/task";
import {
  OWN_WRITE_WINDOW_MS,
  canonicalJson,
  isOwnWrite,
  taskFingerprint,
  withinOwnWriteWindow,
} from "../ownWrite";

function sampleDoc(): TaskDoc {
  return {
    entry: "A",
    nodes: {
      A: {
        recognition: { type: "ocr", expected: "开始" },
        action: { type: "click", target: "recognized" },
        next: ["B"],
      },
      B: { recognition: { type: "always" }, action: { type: "none" }, next: [] },
      INC: { recognition: { type: "always" }, action: { type: "none" }, next: [] },
    },
    _merge: {
      includes: ["D:/x/common/frag.json"],
      conflicts: [],
      include_map: { A: "<task>", B: "<task>", INC: "common/frag.json" },
    },
    _steps: { A: "1", B: "2", INC: "?" },
  } as TaskDoc;
}

describe("canonicalJson", () => {
  it("键序不同但内容相同 → 指纹相同", () => {
    expect(canonicalJson({ a: 1, b: { c: 2, d: [1, 2] } })).toBe(
      canonicalJson({ b: { d: [1, 2], c: 2 }, a: 1 }),
    );
  });

  it("数组顺序是内容的一部分 → 指纹不同", () => {
    expect(canonicalJson({ next: ["A", "B"] })).not.toBe(canonicalJson({ next: ["B", "A"] }));
  });

  it("嵌套 null / 布尔 / 中文原样参与比对", () => {
    expect(canonicalJson({ landing: null, on: true, t: "开始" })).toBe(
      '{"landing":null,"on":true,"t":"开始"}',
    );
  });
});

describe("taskFingerprint", () => {
  it("合并态 doc 与其写盘形态指纹一致（剔生成键与 include 节点后比）", () => {
    const merged = sampleDoc();
    // 后端写盘的原始态：没有 _merge/_steps，也没有 include 来的 INC 节点
    const raw = {
      entry: "A",
      nodes: {
        A: {
          recognition: { type: "ocr", expected: "开始" },
          action: { type: "click", target: "recognized" },
          next: ["B"],
        },
        B: { recognition: { type: "always" }, action: { type: "none" }, next: [] },
      },
    } as TaskDoc;
    expect(taskFingerprint(merged)).toBe(taskFingerprint(raw));
  });

  it("节点内容变了 → 指纹变（AI 的改动能被认出来）", () => {
    const before = taskFingerprint(sampleDoc());
    const after = sampleDoc();
    after.nodes.A.next = ["B", "INC"];
    expect(taskFingerprint(after)).not.toBe(before);
  });

  it("只改 include 节点或生成键 → 指纹不变（这些本来就不写盘）", () => {
    const before = taskFingerprint(sampleDoc());
    const doc = sampleDoc();
    doc.nodes.INC.next = ["A"];
    doc._steps = { A: "9" };
    expect(taskFingerprint(doc)).toBe(before);
  });
});

describe("withinOwnWriteWindow", () => {
  const now = 1_000_000;

  it("没有自写记录 → 不在窗口内", () => {
    expect(withinOwnWriteWindow(null, now)).toBe(false);
  });

  it("窗口内 / 窗口外", () => {
    expect(withinOwnWriteWindow({ at: now - 1, fingerprint: "x" }, now)).toBe(true);
    expect(
      withinOwnWriteWindow({ at: now - OWN_WRITE_WINDOW_MS, fingerprint: "x" }, now),
    ).toBe(false);
  });
});

describe("isOwnWrite", () => {
  const now = 1_000_000;
  const record = { at: now - 500, fingerprint: "FP" };

  it("窗口内且内容一致 → 自写（忽略事件，不清 undo）", () => {
    expect(isOwnWrite(record, "FP", now)).toBe(true);
  });

  it("窗口内但内容不一致 → 外部写入（不能吞）", () => {
    expect(isOwnWrite(record, "OTHER", now)).toBe(false);
  });

  it("窗口内但磁盘内容拉不到 → 保守当外部写入", () => {
    expect(isOwnWrite(record, null, now)).toBe(false);
  });

  it("窗口外即使内容一致也按外部写入处理", () => {
    expect(isOwnWrite({ at: now - OWN_WRITE_WINDOW_MS - 1, fingerprint: "FP" }, "FP", now)).toBe(
      false,
    );
  });

  it("fingerprint=null（重排步号这类后端整文件重写）→ 窗口内一律自写", () => {
    expect(isOwnWrite({ at: now - 500, fingerprint: null }, "ANY", now)).toBe(true);
    expect(isOwnWrite({ at: now - 500, fingerprint: null }, null, now)).toBe(true);
    expect(isOwnWrite({ at: now - 9999, fingerprint: null }, "ANY", now)).toBe(false);
  });

  it("没有自写记录（从没保存过）→ 一律外部写入", () => {
    expect(isOwnWrite(null, "FP", now)).toBe(false);
  });
});
