/** fetch 封装：统一错误规约（HTTPException detail → Error.message）。 */

export class ApiError extends Error {
  status: number;
  /** 原始 detail（后端结构化错误，如 409 冲突的 {conflict, current_mtime_ns}）。 */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

/** HTTPException 的 detail 可以是字符串，也可以是带 message 的结构化对象。 */
function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
    return JSON.stringify(detail);
  }
  return fallback;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail: unknown;
    let message = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail;
      message = detailMessage(body.detail, resp.statusText);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(resp.status, message, detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, body?: unknown) =>
    request<T>(url, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(url: string, body?: unknown) =>
    request<T>(url, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  delete: <T>(url: string) => request<T>(url, { method: "DELETE" }),
};
