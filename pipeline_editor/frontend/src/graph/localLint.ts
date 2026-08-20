/** 前端轻量同步提示（不复刻后端校验，只做即时可见的编辑辅助）。
 *
 * - 悬空 next / on_timeout 引用
 * - and/or 组合里 ≥2 个 ui_text 子识别（真机每次 uiautomator dump ~4.33s）
 * - and/or 嵌套超过 2 层
 */
import type { Recognition, TaskDoc } from "../types/task";

export interface LocalLintWarning {
  node: string | null;
  level: "warning" | "error";
  message: string;
}

function countUiTextSubs(rec: Recognition): number {
  let count = 0;
  const walk = (r: Recognition) => {
    if (r.type === "ui_text") count += 1;
    for (const sub of r.all_of ?? []) walk(sub);
    for (const sub of r.any_of ?? []) walk(sub);
  };
  for (const sub of rec.all_of ?? []) walk(sub);
  for (const sub of rec.any_of ?? []) walk(sub);
  return count;
}

function comboDepth(rec: Recognition): number {
  if (rec.type !== "and" && rec.type !== "or") return 0;
  const subs = [...(rec.all_of ?? []), ...(rec.any_of ?? [])];
  return 1 + Math.max(0, ...subs.map(comboDepth));
}

export function localLint(doc: TaskDoc): LocalLintWarning[] {
  const out: LocalLintWarning[] = [];
  const nodes = doc.nodes ?? {};

  if (doc.entry && !(doc.entry in nodes)) {
    out.push({ node: null, level: "error", message: `入口节点 "${doc.entry}" 不存在` });
  }
  if (doc.on_finding && !(doc.on_finding in nodes)) {
    out.push({
      node: null,
      level: "error",
      message: `on_finding 引用了不存在的节点 "${doc.on_finding}"`,
    });
  }

  for (const [name, def] of Object.entries(nodes)) {
    for (const target of def.next ?? []) {
      if (!(target in nodes)) {
        out.push({
          node: name,
          level: "error",
          message: `next 引用了不存在的节点 "${target}"`,
        });
      }
    }
    if (def.on_timeout && !(def.on_timeout in nodes)) {
      out.push({
        node: name,
        level: "error",
        message: `on_timeout 引用了不存在的节点 "${def.on_timeout}"`,
      });
    }
    const rec = def.recognition;
    if (rec) {
      const uiTextCount = countUiTextSubs(rec);
      if (uiTextCount >= 2) {
        out.push({
          node: name,
          level: "warning",
          message: `组合识别含 ${uiTextCount} 个 ui_text 子识别——每个都会触发一次 uiautomator dump（真机约 4.3s/次），考虑改用 ocr+roi`,
        });
      }
      if (comboDepth(rec) > 2) {
        out.push({
          node: name,
          level: "error",
          message: "and/or 组合嵌套超过 2 层（引擎会拒绝加载）",
        });
      }
    }
  }

  (doc.watchdogs ?? []).forEach((w, i) => {
    if (w.skip_to && !(w.skip_to in nodes)) {
      out.push({
        node: null,
        level: "error",
        message: `watchdog #${i + 1} 的 skip_to 引用了不存在的节点 "${w.skip_to}"`,
      });
    }
  });

  return out;
}
