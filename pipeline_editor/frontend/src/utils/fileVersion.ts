/** 保存的乐观并发控制：版本基线的维护与冲突识别（纯函数，可单测）。
 *
 * 背景：REST PUT 与 AI 的 MCP save 都是整文件覆盖。人 Ctrl+S 与 AI 保存交叉时，
 * 后写的静默赢。现在读通道（GET）给出文件版本令牌 `mtime_ns`，保存时回传
 * `base_mtime_ns`；与磁盘当前版本不符 → 后端 409，前端弹冲突横幅而不是覆盖。
 *
 * 基线（base）的生命周期只有三种推进方式，都收敛到这里：
 * 1. 载入 / 重载：用 GET 响应的 `mtime_ns`。
 * 2. 保存 / 重排成功：用响应回传的新 `mtime_ns`。
 * 3. 用户在冲突横幅上点「保持本地」：拉一次 GET 把基线推到磁盘当前版本
 *    —— 表示"我已知情，下次保存是有意覆盖"。不这么做的话基线永远过期，
 *    保存会一直 409，用户被锁死（见 `EditorPage` / `SuitesPage` 的 keepLocal）。
 */
import { ApiError } from "../api/client";
import type { FileVersion } from "../types/api";

/** 后端因版本冲突拒绝写盘（HTTP 409）——与网络错误、校验错误区分开。 */
export function isVersionConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

/**
 * 写盘成功后的新基线：服务端给了新版本就用它。
 *
 * 没给（老后端 / 字段缺失）→ `null`：放弃乐观锁退回 last-write-wins，
 * 而不是留着必然过期的旧基线把用户锁死在 409。
 */
export function advanceVersion(serverVersion: FileVersion | undefined): FileVersion {
  return typeof serverVersion === "string" ? serverVersion : null;
}

/** 409 冲突响应里的磁盘当前版本（拿不到就 null，调用方退回重新 GET）。 */
export function conflictVersion(error: unknown): FileVersion {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (detail && typeof detail === "object") {
    const current = (detail as { current_mtime_ns?: unknown }).current_mtime_ns;
    if (typeof current === "string") return current;
  }
  return null;
}
