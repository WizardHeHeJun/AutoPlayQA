import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { advanceVersion, conflictVersion, isVersionConflict } from "../fileVersion";

function conflictError(current?: string): ApiError {
  return new ApiError(409, "磁盘上的任务文件比你载入的版本更新（AI/MCP 或手改），已拒绝覆盖", {
    conflict: true,
    message: "磁盘上的任务文件比你载入的版本更新（AI/MCP 或手改），已拒绝覆盖",
    ...(current === undefined ? {} : { current_mtime_ns: current }),
  });
}

describe("isVersionConflict", () => {
  it("409 = 版本冲突", () => {
    expect(isVersionConflict(conflictError("1755498123456789100"))).toBe(true);
  });

  it("其它 HTTP 错误 / 普通异常都不是冲突", () => {
    expect(isVersionConflict(new ApiError(404, "任务不存在"))).toBe(false);
    expect(isVersionConflict(new ApiError(400, "非法任务名"))).toBe(false);
    expect(isVersionConflict(new Error("Failed to fetch"))).toBe(false);
    expect(isVersionConflict(null)).toBe(false);
  });
});

describe("advanceVersion", () => {
  it("服务端给了新版本 → 用它当下次保存的基线", () => {
    expect(advanceVersion("1755498123456789100")).toBe("1755498123456789100");
  });

  it("服务端没给（老后端/字段缺失）→ 作废基线，退回 last-write-wins 而不是被 409 锁死", () => {
    expect(advanceVersion(undefined)).toBeNull();
    expect(advanceVersion(null)).toBeNull();
  });

  it("版本是字符串（mtime_ns 超出 JS 安全整数，不能当数字收）", () => {
    const token = "1755498123456789100";
    expect(advanceVersion(token)).toBe(token);
    expect(String(Number(token))).not.toBe(token); // 一旦当数字就精度丢失
  });
});

describe("conflictVersion", () => {
  it("从 409 detail 里取磁盘当前版本", () => {
    expect(conflictVersion(conflictError("1755498123456789100"))).toBe(
      "1755498123456789100",
    );
  });

  it("detail 里没有版本 / 不是 ApiError → null（调用方退回重新 GET）", () => {
    expect(conflictVersion(conflictError())).toBeNull();
    expect(conflictVersion(new ApiError(409, "conflict"))).toBeNull();
    expect(conflictVersion(new Error("boom"))).toBeNull();
  });
});
