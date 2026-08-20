import { describe, expect, it } from "vitest";

import type { CustomActionParam } from "../../../types/api";
import {
  asBool,
  asNumber,
  asRoi,
  asString,
  isSet,
  parseJsonLine,
  placeholderOf,
  setParam,
  stepOf,
  toJsonLine,
  unknownParamKeys,
} from "../customParams";

function param(over: Partial<CustomActionParam> & { key: string }): CustomActionParam {
  return {
    type: "json",
    default: null,
    default_unresolved: false,
    choices: null,
    required: false,
    ...over,
  };
}

const SCHEMA: CustomActionParam[] = [
  param({ key: "kills_target", type: "int", default: 6 }),
  param({ key: "gun", type: "enum", choices: ["auto", "rifle"] }),
  param({ key: "counter_roi", type: "roi", default: [10, 135, 560, 260] }),
];

describe("unknownParamKeys", () => {
  it("列出 schema 没描述的键，schema 内的键不重复列出", () => {
    expect(unknownParamKeys({ kills_target: 3, legacy_key: 1 }, SCHEMA)).toEqual([
      "legacy_key",
    ]);
  });

  it("空 params / 空 schema 都不炸", () => {
    expect(unknownParamKeys(undefined, SCHEMA)).toEqual([]);
    expect(unknownParamKeys({ a: 1, b: 2 }, [])).toEqual(["a", "b"]);
  });
});

describe("isSet / setParam（只写显式设置的键）", () => {
  it("显式 undefined 之外的值都算已设置，包括 null 与 false", () => {
    expect(isSet({ a: null }, "a")).toBe(true);
    expect(isSet({ a: false }, "a")).toBe(true);
    expect(isSet({ a: 1 }, "b")).toBe(false);
    expect(isSet(undefined, "a")).toBe(false);
  });

  it("写入不改原对象", () => {
    const before = { a: 1 };
    expect(setParam(before, "b", 2)).toEqual({ a: 1, b: 2 });
    expect(before).toEqual({ a: 1 });
  });

  it("undefined = 删键；删空后整个 params 消失", () => {
    expect(setParam({ a: 1, b: 2 }, "a", undefined)).toEqual({ b: 2 });
    expect(setParam({ a: 1 }, "a", undefined)).toBeUndefined();
    expect(setParam(undefined, "a", undefined)).toBeUndefined();
  });

  it("默认值绝不会被自动灌进 params", () => {
    // 表单只在用户操作时调 setParam；schema 默认值只经 placeholderOf 展示。
    let params = setParam(undefined, "kills_target", 3);
    expect(params).toEqual({ kills_target: 3 });
    params = setParam(params, "kills_target", undefined);
    expect(params).toBeUndefined();
  });
});

describe("placeholderOf", () => {
  it("有默认值就显示默认值", () => {
    expect(placeholderOf(param({ key: "k", type: "int", default: 6 }))).toBe("默认 6");
    expect(placeholderOf(param({ key: "k", type: "str", default: "burst" }))).toBe(
      "默认 burst",
    );
    expect(placeholderOf(param({ key: "k", type: "roi", default: [1, 2, 3, 4] }))).toBe(
      "默认 [1,2,3,4]",
    );
  });

  it("没默认值 / 求不出 / 必填 各有明确说法", () => {
    expect(placeholderOf(param({ key: "k" }))).toBe("未设置（handler 默认）");
    expect(placeholderOf(param({ key: "k", default_unresolved: true }))).toContain("未静态求出");
    expect(placeholderOf(param({ key: "k", required: true }))).toBe("必填");
  });
});

describe("控件取值（形态不符一律 undefined，不硬转）", () => {
  it("asNumber", () => {
    expect(asNumber(3)).toBe(3);
    expect(asNumber("3")).toBeUndefined();
    expect(asNumber(NaN)).toBeUndefined();
  });

  it("asString / asBool", () => {
    expect(asString("a")).toBe("a");
    expect(asString(1)).toBeUndefined();
    expect(asBool(false)).toBe(false);
    expect(asBool("true")).toBeUndefined();
  });

  it("asRoi 只接受 4 个数字", () => {
    expect(asRoi([1, 2, 3, 4])).toEqual([1, 2, 3, 4]);
    expect(asRoi([1, 2, 3])).toBeUndefined();
    expect(asRoi([1, 2, 3, "4"])).toBeUndefined();
    expect(asRoi("1,2,3,4")).toBeUndefined();
  });
});

describe("JSON 行", () => {
  it("undefined ↔ 空串", () => {
    expect(toJsonLine(undefined)).toBe("");
    expect(parseJsonLine("")).toEqual({ ok: true, value: undefined });
    expect(parseJsonLine("   ")).toEqual({ ok: true, value: undefined });
  });

  it("round-trip 保值", () => {
    for (const v of [1, "a", true, null, [1, 2], { a: 1 }]) {
      const parsed = parseJsonLine(toJsonLine(v));
      expect(parsed).toEqual({ ok: true, value: v });
    }
  });

  it("坏 JSON 报错而不是写坏值", () => {
    const parsed = parseJsonLine("{a:");
    expect(parsed.ok).toBe(false);
  });
});

describe("stepOf", () => {
  it("float 小数给细步进，int 给 1", () => {
    expect(stepOf(param({ key: "k", type: "float", default: 4.197 }))).toBe(0.01);
    expect(stepOf(param({ key: "k", type: "float", default: 27000 }))).toBe(0.1);
    expect(stepOf(param({ key: "k", type: "int", default: 6 }))).toBe(1);
  });
});
