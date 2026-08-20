#!/usr/bin/env python3
"""冒烟汇报生成器：bugs.json -> 证据包 + 报告 HTML + 报告 MD。

固定「冒烟汇报」这条流程里**所有机械环节**，把判断留给智能体：

    智能体负责（写进 bugs.json）        脚本负责（本文件）
    ---------------------------        --------------------------------
    哪条 finding 是真产品缺陷           扫 outputs/findings 统计回放轮次
    严重级别 / 归属 / 根因读法          按缺陷清单抽证据文件到 evidence/
    人工可照做的复现步骤                渲染固定版式的 HTML（自包含、可离线）
    哪些断言没有文件证据                渲染同内容 Markdown（贴飞书/复制用）
                                       校验所有证据链接可解析，断链即报错

设计约束：
  - 只用标准库，不引入依赖；不 import 项目内任何模块（报告可脱离运行环境生成）。
  - 证据一律相对路径（正斜杠），整个输出目录可打包外发。
  - 统计数字只来自落盘 report.json，不接受 bugs.json 手填（防止人工数字与证据打架）。

用法：
    python build_report.py <bugs.json> [--out DIR] [--validate] [--stats-only]

    --validate    只校验 bugs.json 与证据文件是否齐全，不写任何文件
    --stats-only  只打印回放执行汇总表（Markdown），用于起草阶段先看数据
    --out DIR     输出目录，默认取 bugs.json 所在目录

退出码：0 成功 / 1 校验失败（缺字段、证据文件不存在、渲染后断链）
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

SEVERITY_ORDER = ("S1", "S2", "S3", "S4")
SEVERITY_LABELS = {
    "S1": "S1 致命 / Blocker",
    "S2": "S2 严重",
    "S3": "S3 一般",
    "S4": "S4 轻微",
}
SEVERITY_CLASS = {"S1": "s1", "S2": "s2", "S3": "s3", "S4": "s3"}

SOURCE_BADGES = {
    "file": ("b-ok", "有文件证据"),
    "observed": ("b-warn", "未捕获到直接证据，为执行期观察"),
    "inferred": ("b-warn", "为推断"),
}

# 证据文件默认拷贝范围（相对 run 目录的 glob），可被 run 条目的 include/exclude 覆盖
DEFAULT_INCLUDE = ["*.png", "*.log", "*.json", "*.html", "video/*"]

STATUS_LABELS = {
    "completed": "完成",
    "failed": "失败",
    "agent_required": "待交接",
    "running": "运行中",
}


class BuildError(Exception):
    """校验失败：缺字段、证据缺失、路径不合法。"""


# ---------------------------------------------------------------- 工具


def _rel(path: str) -> str:
    return str(path).replace("\\", "/")


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(round(seconds)), 60)
    return f"{minutes}m{sec:02d}s"


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------- 回放统计


def scan_runs(findings_root: Path, period: Optional[Tuple[str, str]] = None) -> Dict:
    """扫描 findings 目录下所有 report.json，聚合成回放执行汇总。

    period 为 ("YYYY-MM-DD", "YYYY-MM-DD") 闭区间，按日期文件夹名过滤；None 表示全量。
    统计只认落盘数据：轮次、状态分布、finding 级别计数、平均时长。
    """
    if not findings_root.is_dir():
        raise BuildError(f"findings 目录不存在：{findings_root}")

    lo, hi = None, None
    if period:
        lo = period[0].replace("-", "")
        hi = period[1].replace("-", "")

    per_task: Dict[str, Dict] = defaultdict(
        lambda: {"runs": 0, "levels": Counter(), "status": Counter(), "dates": set(), "durations": []}
    )
    totals = {"runs": 0, "levels": Counter(), "status": Counter(), "dates": set()}

    for report in sorted(findings_root.glob("*/*/*/report.json")):
        day = report.parents[2].name
        if lo and not (lo <= day <= hi):
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 跳过无法解析的 report.json: {report} ({exc})", file=sys.stderr)
            continue

        task = data.get("task") or "(unknown)"
        entry = per_task[task]
        entry["runs"] += 1
        entry["levels"].update(data.get("counts") or {})
        entry["status"][data.get("status") or "unknown"] += 1
        started = data.get("started_at") or ""
        if started:
            entry["dates"].add(started[:10])
            totals["dates"].add(started[:10])
        finished = data.get("finished_at") or ""
        if started and finished:
            try:
                delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                entry["durations"].append(delta.total_seconds())
            except ValueError:
                pass

        totals["runs"] += 1
        totals["levels"].update(data.get("counts") or {})
        totals["status"][data.get("status") or "unknown"] += 1

    if not totals["runs"]:
        raise BuildError(f"{findings_root} 下没有匹配的 report.json（period={period}）")

    rows = []
    for task, entry in sorted(per_task.items(), key=lambda kv: -kv[1]["runs"]):
        durations = entry["durations"]
        rows.append(
            {
                "task": task,
                "runs": entry["runs"],
                "dates": sorted(entry["dates"]),
                "avg_duration": _fmt_duration(sum(durations) / len(durations)) if durations else "—",
                "status": dict(entry["status"]),
                "levels": dict(entry["levels"]),
            }
        )
    return {
        "rows": rows,
        "total_runs": totals["runs"],
        "total_tasks": len(per_task),
        "total_levels": dict(totals["levels"]),
        "total_status": dict(totals["status"]),
        "dates": sorted(totals["dates"]),
    }


def _status_text(status: Dict[str, int]) -> str:
    parts = [f"{STATUS_LABELS.get(k, k)} {v}" for k, v in sorted(status.items(), key=lambda kv: -kv[1])]
    return " / ".join(parts) if parts else "—"


def stats_markdown(stats: Dict, task_labels: Dict[str, str], bug_index: Dict[str, List[Dict]]) -> str:
    lines = [
        "| 用例脚本 | 模块 | 轮次 | 日期 | 平均时长 | 运行状态分布 | error | warning | 产出缺陷 |",
        "| --- | --- | ---: | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for row in stats["rows"]:
        dates = row["dates"]
        span = dates[0] if len(dates) <= 1 else f"{dates[0]} ~ {dates[-1]}"
        bugs = "、".join(b["id"] for b in bug_index.get(row["task"], [])) or "—"
        lines.append(
            f"| `{row['task']}` | {task_labels.get(row['task'], '—')} | {row['runs']} | {span} "
            f"| {row['avg_duration']} | {_status_text(row['status'])} "
            f"| {row['levels'].get('error', 0)} | {row['levels'].get('warning', 0)} | {bugs} |"
        )
    total_dates = stats["dates"]
    span = f"{total_dates[0]} ~ {total_dates[-1]}" if total_dates else "—"
    lines.append(
        f"| **合计** | {stats['total_tasks']} 个脚本 | **{stats['total_runs']}** | {span} | — "
        f"| {_status_text(stats['total_status'])} | {stats['total_levels'].get('error', 0)} "
        f"| {stats['total_levels'].get('warning', 0)} | {sum(len(v) for v in bug_index.values())} 条 |"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 证据抽取


def _match_globs(run_dir: Path, patterns: List[str]) -> List[Path]:
    found: List[Path] = []
    for pattern in patterns:
        for path in sorted(run_dir.glob(pattern)):
            if path.is_file() and path not in found:
                found.append(path)
    return found


def collect_evidence(spec: Dict, out_dir: Path, project_root: Path, do_copy: bool) -> Dict[str, Dict]:
    """把每条缺陷引用的 run 目录抽进 <out>/evidence/<BUG-ID>/<run 名>/。

    返回 {bug_id: {run_key: {"dir": 包内相对目录, "src": 原始目录, "files": [...]}}}，
    供渲染阶段把 {"run": key, "file": name} 解析成包内相对路径。
    """
    layout: Dict[str, Dict] = {}
    for bug in spec["bugs"]:
        bug_id = bug["id"]
        layout[bug_id] = {}
        for key, run in (bug.get("runs") or {}).items():
            raw = run["path"] if isinstance(run, dict) else run
            src = (project_root / raw).resolve()
            if not src.is_dir():
                raise BuildError(f"{bug_id} 的 run『{key}』目录不存在：{raw}")
            include = run.get("include", DEFAULT_INCLUDE) if isinstance(run, dict) else DEFAULT_INCLUDE
            exclude = run.get("exclude", []) if isinstance(run, dict) else []
            excluded = {p.name for p in _match_globs(src, exclude)}

            dest_rel = f"evidence/{bug_id}/{src.name}"
            dest = out_dir / dest_rel
            copied: List[str] = []
            for path in _match_globs(src, include):
                if path.name in excluded:
                    continue
                sub = path.relative_to(src)
                if do_copy:
                    target = dest / sub
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                copied.append(_rel(sub))
            layout[bug_id][key] = {
                "dir": dest_rel,
                "src": _rel(raw),
                "files": copied,
            }
    return layout


def resolve_ref(bug_id: str, item: Dict, layout: Dict[str, Dict], out_dir: Path) -> str:
    """把 {"run": key, "file": name} 或 {"ref": 包内相对路径} 解析成包内相对路径并校验存在。

    校验对象是 collect_evidence 匹配出的文件清单而非磁盘，因此 --validate（不拷贝）
    与正式生成走同一条判断，不会因为「还没拷」而误报断链。
    """
    if "ref" in item:
        rel = _rel(item["ref"])
        for runs in layout.values():
            for run in runs.values():
                prefix = run["dir"] + "/"
                if rel.startswith(prefix) and rel[len(prefix):] in run["files"]:
                    return rel
        raise BuildError(f"{bug_id} 的 ref 指向包内不存在的文件：{rel}（该文件需先被某条缺陷的 run 抽进包里）")

    run_key = item.get("run")
    runs = layout.get(bug_id, {})
    if run_key not in runs:
        raise BuildError(f"{bug_id} 引用了未声明的 run『{run_key}』（可用：{list(runs)}）")
    name = _rel(item["file"])
    if name not in runs[run_key]["files"]:
        raise BuildError(
            f"{bug_id} 的证据文件未被抽进包内：{run_key}/{name}"
            f"（检查该 run 的 include 是否覆盖它；原始目录 {runs[run_key]['src']}）"
        )
    return f"{runs[run_key]['dir']}/{name}"


# ---------------------------------------------------------------- 块渲染


def blocks_to_html(blocks: List[Dict]) -> str:
    """渲染自由段落块：p / ul / ol / code / note / quote / caption。"""
    out: List[str] = []
    for block in blocks or []:
        if "p" in block:
            out.append(f"<p>{_inline(block['p'])}</p>")
        elif "ul" in block:
            items = "".join(f"<li>{_inline(i)}</li>" for i in block["ul"])
            out.append(f'<ul class="tight">{items}</ul>')
        elif "ol" in block:
            items = "".join(f"<li>{_inline(i)}</li>" for i in block["ol"])
            out.append(f'<ol class="steps">{items}</ol>')
        elif "code" in block:
            out.append(f"<pre><code>{_esc(block['code'])}</code></pre>")
        elif "note" in block:
            out.append(f'<div class="note">{_inline(block["note"])}</div>')
        elif "quote" in block:
            out.append(f"<blockquote><p>{_inline(block['quote'])}</p></blockquote>")
        elif "caption" in block:
            out.append(f'<p style="font-size:13px;color:var(--muted)">{_inline(block["caption"])}</p>')
        else:
            raise BuildError(f"未知的内容块：{list(block)}（可用 p/ul/ol/code/note/quote/caption）")
    return "\n".join(out)


def blocks_to_md(blocks: List[Dict]) -> str:
    out: List[str] = []
    for block in blocks or []:
        if "p" in block:
            out.append(block["p"])
        elif "ul" in block:
            out.append("\n".join(f"- {i}" for i in block["ul"]))
        elif "ol" in block:
            out.append("\n".join(f"{n}. {i}" for n, i in enumerate(block["ol"], 1)))
        elif "code" in block:
            out.append("```text\n" + block["code"] + "\n```")
        elif "note" in block:
            out.append(f"> ⚠️ {block['note']}")
        elif "quote" in block:
            out.append(f"> {block['quote']}")
        elif "caption" in block:
            out.append(f"（{block['caption']}）")
    return "\n\n".join(out)


def _inline(text: str) -> str:
    """极简行内标记：**粗体**、`代码`；其余转义。链接写成 [文字](路径)。"""
    escaped = _esc(text)
    out, i = [], 0
    while i < len(escaped):
        if escaped.startswith("**", i):
            end = escaped.find("**", i + 2)
            if end != -1:
                out.append(f"<b>{escaped[i + 2:end]}</b>")
                i = end + 2
                continue
        if escaped[i] == "`":
            end = escaped.find("`", i + 1)
            if end != -1:
                out.append(f"<code>{escaped[i + 1:end]}</code>")
                i = end + 1
                continue
        if escaped[i] == "[":
            close = escaped.find("](", i)
            end = escaped.find(")", close + 2) if close != -1 else -1
            if close != -1 and end != -1:
                label, href = escaped[i + 1:close], escaped[close + 2:end]
                out.append(f'<a href="{href}">{label}</a>')
                i = end + 1
                continue
        out.append(escaped[i])
        i += 1
    return "".join(out)


def _source_badge(source: Optional[str], note: str = "") -> str:
    if not source:
        return _inline(note)
    cls, label = SOURCE_BADGES.get(source, ("b-warn", source))
    tail = f" {_inline(note)}" if note else ""
    return f'<span class="b {cls}">{label}</span>{tail}'


def _source_md(source: Optional[str], note: str = "") -> str:
    if not source:
        return note
    label = SOURCE_BADGES.get(source, ("", source))[1]
    return f"**{label}**{('　' + note) if note else ''}"


# ---------------------------------------------------------------- HTML 模板

STYLE = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --panel-2:#fbfcfd; --text:#1a1d21; --muted:#5c6470;
  --line:#e3e6ea; --line-strong:#cfd4da;
  --accent:#2f6feb; --accent-soft:#e8f0fe;
  --s1:#c8232c; --s1-bg:#fdecec; --s2:#c2620a; --s2-bg:#fdf1e3; --s3:#8a6d00; --s3-bg:#fbf5db;
  --ok:#1f7a4d; --ok-bg:#e6f5ec; --warn:#a15c00; --warn-bg:#fbf0df;
  --code-bg:#f2f4f7; --code-text:#232830;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Consolas,"Liberation Mono",Menlo,monospace;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14171b; --panel:#1b1f24; --panel-2:#20252b; --text:#e6e9ed; --muted:#9aa3ae;
    --line:#2b3138; --line-strong:#3a424b;
    --accent:#6f9bff; --accent-soft:#1e2a44;
    --s1:#ff7b7b; --s1-bg:#3a1f22; --s2:#ffab5e; --s2-bg:#3a2a19; --s3:#e6c74d; --s3-bg:#332e17;
    --ok:#63d19b; --ok-bg:#17301f; --warn:#e8b063; --warn-bg:#332818;
    --code-bg:#12151a; --code-text:#d4dae2;
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.75}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 80px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header.top{background:linear-gradient(135deg,var(--panel) 0%,var(--panel-2) 100%);
  border-bottom:1px solid var(--line);padding:34px 0 26px;margin-bottom:26px}
header.top .wrap{padding-bottom:0}
.eyebrow{font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
h1{font-size:27px;line-height:1.35;margin:8px 0 12px;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--muted);max-width:820px;margin:0}
.meta-grid{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:12.5px;color:var(--muted)}
.chip b{color:var(--text);font-weight:600}
nav.toc{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);margin-bottom:28px}
nav.toc .inner{max-width:1180px;margin:0 auto;padding:9px 20px;display:flex;flex-wrap:wrap;gap:6px 4px;overflow-x:auto}
nav.toc a{font-size:13px;padding:5px 11px;border-radius:7px;color:var(--muted);white-space:nowrap}
nav.toc a:hover{background:var(--accent-soft);color:var(--accent);text-decoration:none}
section{margin:0 0 40px}
h2{font-size:20px;margin:34px 0 14px;padding-bottom:9px;border-bottom:2px solid var(--line-strong);
  font-weight:700;letter-spacing:-.01em;scroll-margin-top:60px}
h3{font-size:16.5px;margin:26px 0 10px;font-weight:650;scroll-margin-top:60px}
h4{font-size:14px;margin:20px 0 8px;font-weight:650;color:var(--muted);letter-spacing:.02em}
p{margin:10px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:16px 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 6px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 16px}
.stat .n{font-size:29px;font-weight:700;line-height:1.15;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .l{font-size:12.5px;color:var(--muted);margin-top:3px}
.stat.red .n{color:var(--s1)} .stat.orange .n{color:var(--s2)} .stat.blue .n{color:var(--accent)}
.tbl-wrap{overflow-x:auto;margin:14px 0;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
th,td{padding:9px 13px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
thead th{background:var(--panel-2);font-weight:650;color:var(--muted);font-size:12.5px;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel-2)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.kv th{width:150px;white-space:nowrap;background:var(--panel-2)}
.b{display:inline-block;padding:1px 9px;border-radius:6px;font-size:12px;font-weight:650;white-space:nowrap;line-height:1.7}
.b-s1{background:var(--s1-bg);color:var(--s1)}
.b-s2{background:var(--s2-bg);color:var(--s2)}
.b-s3{background:var(--s3-bg);color:var(--s3)}
.b-ok{background:var(--ok-bg);color:var(--ok)}
.b-warn{background:var(--warn-bg);color:var(--warn)}
.b-neutral{background:var(--accent-soft);color:var(--accent)}
.bug{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin:22px 0 34px;overflow:hidden}
.bug-head{padding:16px 22px;border-bottom:1px solid var(--line);background:var(--panel-2)}
.bug-head .id{font-family:var(--mono);font-size:12.5px;color:var(--muted);letter-spacing:.04em}
.bug-head h3{margin:4px 0 0;font-size:17px;scroll-margin-top:60px}
.bug-head .tags{margin-top:9px;display:flex;gap:7px;flex-wrap:wrap}
.bug-body{padding:6px 22px 22px}
.bug.s1{border-left:4px solid var(--s1)}
.bug.s2{border-left:4px solid var(--s2)}
.bug.s3{border-left:4px solid var(--s3)}
ol.steps{margin:10px 0;padding-left:22px}
ol.steps li{margin:5px 0}
ul.tight{margin:10px 0;padding-left:20px}
ul.tight li{margin:5px 0}
blockquote{margin:14px 0;padding:11px 16px;border-left:3px solid var(--accent);
  background:var(--accent-soft);border-radius:0 8px 8px 0;color:var(--text)}
blockquote p{margin:4px 0}
.note{margin:14px 0;padding:11px 16px;border-left:3px solid var(--warn);
  background:var(--warn-bg);border-radius:0 8px 8px 0;font-size:13.5px}
pre{background:var(--code-bg);color:var(--code-text);border:1px solid var(--line);border-radius:9px;
  padding:14px 16px;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.65;margin:12px 0}
code{font-family:var(--mono);font-size:.9em;background:var(--code-bg);padding:1px 5px;border-radius:4px;border:1px solid var(--line)}
pre code{background:none;border:none;padding:0}
.shots{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:16px 0}
.shot{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--panel-2)}
.shot img{display:block;width:100%;height:auto;background:#000}
.shot video{display:block;width:100%;background:#000}
.shot .cap{padding:8px 11px;font-size:12px;color:var(--muted);line-height:1.55;border-top:1px solid var(--line)}
.shot .cap b{color:var(--text);font-weight:600;display:block;margin-bottom:2px}
footer{margin-top:50px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
@media(max-width:600px){h1{font-size:22px}.wrap{padding:0 14px 60px}.bug-body{padding:6px 14px 18px}.bug-head{padding:14px}}
@media print{nav.toc{display:none}body{background:#fff}.bug,.card,.stat{break-inside:avoid}}
"""

CN_NUM = "一二三四五六七八九十十一十二十三十四十五十六十七十八十九二十"


def _cn(n: int) -> str:
    return CN_NUM[n - 1] if n <= 10 else f"{n}"


# ---------------------------------------------------------------- 渲染 HTML


def render_html(spec: Dict, stats: Dict, layout: Dict, out_dir: Path) -> str:
    meta = spec["meta"]
    bugs = spec["bugs"]
    sev_count = Counter(b["severity"] for b in bugs)
    task_labels = meta.get("task_labels", {})
    bug_index = _bug_index(bugs)

    p: List[str] = []
    a = p.append

    # ---- head
    title = meta["title"]
    a(f'<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{_esc(title)}（{_esc(meta['date'])}）</title>")
    a(f"<style>{STYLE}</style>\n</head>\n<body>")

    # ---- header
    sev_line = " / ".join(f"{s}×{sev_count[s]}" for s in SEVERITY_ORDER if sev_count[s])
    a('<header class="top"><div class="wrap">')
    a(f'<div class="eyebrow">{_esc(meta.get("eyebrow", "AutoPlayQA · 自动化冒烟"))}</div>')
    a(f"<h1>{_esc(title)}</h1>")
    a(
        f'<p class="sub">试点期间在 {_esc(meta.get("device_count", 1))} 台真机上执行 <b>{stats["total_runs"]} 轮</b>'
        f'自动化回放，覆盖 <b>{stats["total_tasks"]} 个用例脚本</b>，产出 <b>{len(bugs)} 条产品缺陷</b>'
        f"（{_esc(sev_line)}）。{_inline(meta.get('subtitle_tail', ''))}</p>"
    )
    a('<div class="meta-grid">')
    for key, value in (meta.get("env") or {}).items():
        a(f'<span class="chip">{_esc(key)} <b>{_esc(value)}</b></span>')
    a("</div></div></header>")

    # ---- toc
    a('<nav class="toc"><div class="inner">')
    a('<a href="#summary">执行摘要</a><a href="#defects">缺陷一览</a>')
    a('<a href="#runs">回放执行汇总</a><a href="#method">方法与证据约定</a>')
    for bug in bugs:
        a(f'<a href="#{_esc(bug["id"].lower())}">{_esc(bug["id"])} {_esc(bug.get("module", ""))}</a>')
    a('<a href="#appendix-a">附录 A 证据包</a><a href="#appendix-b">附录 B 原始位置</a>')
    a('<a href="#appendix-c">附录 C 无文件证据</a></div></nav>')

    a('<div class="wrap">')

    # ---- 执行摘要
    a('<section id="summary"><h2>一、执行摘要</h2><div class="stats">')
    a(f'<div class="stat"><div class="n">{stats["total_runs"]}</div><div class="l">自动化回放轮次</div></div>')
    a(f'<div class="stat"><div class="n">{stats["total_tasks"]}</div><div class="l">用例脚本</div></div>')
    a(f'<div class="stat blue"><div class="n">{len(bugs)}</div><div class="l">产品缺陷</div></div>')
    for sev in SEVERITY_ORDER:
        if sev_count[sev]:
            cls = {"S1": " red", "S2": " orange"}.get(sev, "")
            a(
                f'<div class="stat{cls}"><div class="n">{sev_count[sev]}</div>'
                f'<div class="l">{_esc(SEVERITY_LABELS[sev])}</div></div>'
            )
    a("</div>")
    for card in meta.get("summary_cards") or []:
        a(f'<div class="card"><h4>{_esc(card["title"])}</h4>')
        a(blocks_to_html(card["blocks"]))
        a("</div>")
    a("</section>")

    # ---- 缺陷一览
    a('<section id="defects"><h2>二、缺陷一览</h2><div class="tbl-wrap"><table><thead><tr>')
    a("<th>编号</th><th>模块</th><th>标题</th><th>级别</th><th>归属建议</th><th>复现率</th><th>日期</th>")
    a("</tr></thead><tbody>")
    for bug in bugs:
        sev = bug["severity"]
        note = bug.get("severity_note", "")
        note_html = f'<br><span style="font-size:11.5px;color:var(--muted)">{_esc(note)}</span>' if note else ""
        a(
            f'<tr><td><a href="#{_esc(bug["id"].lower())}"><b>{_esc(bug["id"])}</b></a></td>'
            f'<td>{_esc(bug.get("module", ""))}</td><td>{_inline(bug["title"])}</td>'
            f'<td><span class="b b-{SEVERITY_CLASS[sev]}">{_esc(SEVERITY_LABELS[sev])}</span>{note_html}</td>'
            f'<td>{_inline(bug.get("owner", ""))}</td>'
            f'<td class="num">{_esc(bug.get("repro_rate", "—"))}</td><td>{_esc(bug.get("date", ""))}</td></tr>'
        )
    a("</tbody></table></div>")
    for block in meta.get("defects_notes") or []:
        a(blocks_to_html([block]))
    a("</section>")

    # ---- 回放执行汇总
    a('<section id="runs"><h2>三、回放执行汇总</h2>')
    a(
        f'<p>下表统计自 <code>{_esc(meta.get("findings_root", "outputs/findings"))}</code> 下 '
        f'{stats["total_runs"]} 份 <code>report.json</code> 的真实落盘记录。</p>'
    )
    a('<div class="tbl-wrap"><table><thead><tr>')
    a(
        "<th>用例脚本</th><th>模块</th><th class='num'>轮次</th><th>日期</th><th class='num'>平均时长</th>"
        "<th>运行状态分布</th><th class='num'>error</th><th class='num'>warning</th><th>产出缺陷</th>"
    )
    a("</tr></thead><tbody>")
    for row in stats["rows"]:
        dates = row["dates"]
        span = dates[0] if len(dates) <= 1 else f"{dates[0]} ~ {dates[-1]}"
        chips = "".join(
            f'<span class="b b-{SEVERITY_CLASS[b["severity"]]}">{_esc(b["id"])}</span> '
            for b in bug_index.get(row["task"], [])
        )
        a(
            f'<tr><td><code>{_esc(row["task"])}</code></td><td>{_esc(task_labels.get(row["task"], "—"))}</td>'
            f'<td class="num">{row["runs"]}</td><td>{_esc(span)}</td><td class="num">{_esc(row["avg_duration"])}</td>'
            f'<td>{_esc(_status_text(row["status"]))}</td>'
            f'<td class="num">{row["levels"].get("error", 0)}</td><td class="num">{row["levels"].get("warning", 0)}</td>'
            f"<td>{chips or '—'}</td></tr>"
        )
    total_dates = stats["dates"]
    span = f"{total_dates[0]} ~ {total_dates[-1]}" if total_dates else "—"
    a(
        f'<tr style="font-weight:650;background:var(--panel-2)"><td>合计</td><td>{stats["total_tasks"]} 个脚本</td>'
        f'<td class="num">{stats["total_runs"]}</td><td>{_esc(span)}</td><td class="num">—</td>'
        f'<td>{_esc(_status_text(stats["total_status"]))}</td>'
        f'<td class="num">{stats["total_levels"].get("error", 0)}</td>'
        f'<td class="num">{stats["total_levels"].get("warning", 0)}</td><td>{len(bugs)} 条</td></tr>'
    )
    a("</tbody></table></div>")
    a(
        '<div class="note"><b>读表注意：</b>表中 <code>error</code> / <code>warning</code> 是引擎记录的 '
        "<b>finding 计数</b>，<b>不等于产品缺陷数</b>。多数 warning 是自动化脚本自身的识别加固问题"
        "（按钮 OCR 未命中、锚点漂移、造数据内容不固定等），属工具侧噪音；经人工复核确认为产品缺陷的仅本报告收录的 "
        f"{len(bugs)} 条。{_inline(meta.get('runs_note_tail', ''))}</div></section>"
    )

    # ---- 方法与证据约定
    a('<section id="method"><h2>四、方法与证据约定</h2><div class="card">')
    a("<h4>复现方式</h4><p>AutoPlayQA 是<b>确定性回放工具</b>（截图 + OCR / 控件识别驱动点击），"
      "<b>不使用任何 AI 猜测</b>。因此下文「复现步骤」全部写成人类可手工照做的操作，开发 / 策划无需安装本工具即可复现。</p>")
    a("<h4>证据可信度约定</h4><p>本报告严格区分两类断言：</p><ul class='tight'>"
      '<li><span class="b b-ok">有文件证据</span> — 有落盘文件可核对（截图 / logcat / timeline / report.json）。</li>'
      '<li><span class="b b-warn">未捕获到直接证据</span> — 仅在执行期由测试人员实时观察，未落进证据文件。'
      "<b>请勿把后者当作已验证结论</b>，汇总见<a href=\"#appendix-c\">附录 C</a>。</li></ul>")
    a("<h4>查看方式</h4><p>本 HTML 与 <code>evidence/</code> 同级，所有链接均为相对路径，整个报告目录可直接打包发送。"
      "每轮回放另附 <code>report.html</code>，浏览器双击打开即可看到该轮全部 finding；"
      "<code>report.json</code> 为同内容的机器可读版本。</p></div></section>")

    # ---- 缺陷详情
    for idx, bug in enumerate(bugs, start=5):
        a(_render_bug_html(bug, idx, layout, out_dir))

    # ---- 附录
    a(_render_appendix_html(spec, layout))

    a(
        f'<footer>{_esc(title)} · 生成于 {_esc(meta["date"])} · 数据源：'
        f'<code>{_esc(meta.get("findings_root", "outputs/findings"))}</code> 下 {stats["total_runs"]} 份 report.json 与本目录 <code>evidence/</code><br>'
        "本页为自包含静态 HTML，所有证据链接均为相对路径；连同 <code>evidence/</code> 一起打包即可离线查阅。"
        "由 <code>.claude/skills/smoke-report/build_report.py</code> 从 <code>bugs.json</code> 生成。</footer>"
    )
    a("</div></body></html>")
    return "\n".join(p)


def _bug_index(bugs: List[Dict]) -> Dict[str, List[Dict]]:
    index: Dict[str, List[Dict]] = defaultdict(list)
    for bug in bugs:
        for task in bug.get("tasks") or []:
            index[task].append(bug)
    return index


def _render_bug_html(bug: Dict, section_no: int, layout: Dict, out_dir: Path) -> str:
    bug_id = bug["id"]
    sev = bug["severity"]
    cls = SEVERITY_CLASS[sev]
    p: List[str] = []
    a = p.append

    a(f'<section><h2 id="{_esc(bug_id.lower())}">{_cn(section_no)}、{_esc(bug_id)}'
      f'（{_esc(bug.get("owner", ""))}）{_inline(bug.get("headline", bug["title"]))}</h2>')
    a(f'<div class="bug {cls}"><div class="bug-head">')
    a(f'<div class="id">{_esc(bug_id)} · {_esc(bug.get("module", ""))} · {_esc(bug.get("date", ""))}</div>')
    a(f'<h3>{_inline(bug["title"])}</h3><div class="tags">')
    sev_label = SEVERITY_LABELS[sev] + (f"（{bug['severity_note']}）" if bug.get("severity_note") else "")
    a(f'<span class="b b-{cls}">{_esc(sev_label)}</span>')
    a(f'<span class="b b-neutral">归属：{_esc(bug.get("owner", ""))}</span>')
    a(f'<span class="b b-ok">复现率 {_esc(bug.get("repro_rate", "—"))}</span>')
    a('</div></div><div class="bug-body">')

    if bug.get("severity_reason"):
        a(f"<h4>级别理由</h4><p>{_inline(bug['severity_reason'])}</p>")
    if bug.get("owner_reason"):
        a(f"<h4>归属建议</h4><p>{_inline(bug['owner_reason'])}</p>")

    env = bug.get("env")
    if isinstance(env, str):
        a(f"<h4>环境</h4><p>{_inline(env)}</p>")
    elif env:
        a('<h4>环境</h4><div class="tbl-wrap"><table class="kv"><thead><tr><th>项</th><th>值</th><th>来源</th></tr></thead><tbody>')
        for row in env:
            a(
                f'<tr><th>{_esc(row["item"])}</th><td>{_inline(row["value"])}</td>'
                f'<td>{_source_badge(row.get("source"), row.get("source_note", ""))}</td></tr>'
            )
        a("</tbody></table></div>")

    if bug.get("preconditions"):
        a("<h4>前置条件</h4>" + blocks_to_html([{"ol": bug["preconditions"]}]))
    if bug.get("steps"):
        a("<h4>复现步骤（人工手动照做）</h4>" + blocks_to_html([{"ol": bug["steps"]}]))
    if bug.get("actual"):
        a("<h4>实际结果</h4>" + blocks_to_html(bug["actual"]))

    shots = bug.get("shots") or []
    if shots:
        a('<div class="shots">')
        for shot in shots:
            rel = resolve_ref(bug_id, shot, layout, out_dir)
            media = (
                f'<video controls preload="metadata" src="{_esc(rel)}"></video>'
                if rel.lower().endswith((".mp4", ".webm"))
                else f'<a href="{_esc(rel)}" target="_blank"><img src="{_esc(rel)}" alt="{_esc(shot.get("title", ""))}"></a>'
            )
            a(
                f'<figure class="shot" style="margin:0">{media}'
                f'<figcaption class="cap"><b>{_inline(shot.get("title", ""))}</b>{_inline(shot.get("caption", ""))}</figcaption></figure>'
            )
        a("</div>")

    if bug.get("expected"):
        a("<h4>期望结果</h4>" + blocks_to_html(bug["expected"]))
    if bug.get("repro_detail"):
        a("<h4>复现率</h4>" + blocks_to_html(bug["repro_detail"]))
    for extra in bug.get("sections") or []:
        a(f"<h4>{_esc(extra['title'])}</h4>" + blocks_to_html(extra["blocks"]))

    a('<h4>证据清单</h4><div class="tbl-wrap"><table><thead><tr><th>证据</th><th>路径</th><th>说明</th></tr></thead><tbody>')
    for item in bug.get("evidence") or []:
        links = []
        for ref in item.get("files") or [item]:
            rel = resolve_ref(bug_id, ref, layout, out_dir)
            links.append(f'<a href="{_esc(rel)}">{_esc(ref.get("label") or Path(rel).name)}</a>')
        a(
            f'<tr><td>{_inline(item.get("name", ""))}</td><td>{"、".join(links)}</td>'
            f'<td>{_inline(item.get("desc", ""))}</td></tr>'
        )
    a("</tbody></table></div>")

    for block in bug.get("footnotes") or []:
        a(blocks_to_html([block]))
    a("</div></div></section>")
    return "\n".join(p)


def _render_appendix_html(spec: Dict, layout: Dict) -> str:
    meta = spec["meta"]
    bugs = spec["bugs"]
    p: List[str] = []
    a = p.append

    a('<section id="appendix-a"><h2>附录 A · 证据包结构</h2><pre><code>')
    a(_esc(_package_tree(spec, layout)))
    a("</code></pre><p><b>查看方式</b>：任一 <code>report.html</code> 用浏览器双击打开即可看到该轮全部 finding"
      "（截图、录屏、日志片段、操作时间线内联在一页）；<code>report.json</code> 为同内容的机器可读版本。</p></section>")

    a('<section id="appendix-b"><h2>附录 B · 原始证据位置（项目内，便于回溯）</h2>')
    a('<div class="tbl-wrap"><table><thead><tr><th>缺陷</th><th>原始目录</th></tr></thead><tbody>')
    for bug in bugs:
        srcs = [run["src"] for run in layout.get(bug["id"], {}).values()]
        if not srcs:
            continue
        a(f'<tr><td>{_esc(bug["id"])}</td><td><code>{_esc("、".join(dict.fromkeys(srcs)))}</code></td></tr>')
    a("</tbody></table></div></section>")

    unverified = meta.get("unverified") or []
    a('<section id="appendix-c"><h2>附录 C · 本报告中「无文件证据」的断言汇总</h2>')
    if unverified:
        a(f"<p>接收方复现时请特别注意以下 {len(unverified)} 条，它们<b>不是</b>已验证结论：</p>")
        a('<div class="tbl-wrap"><table><thead><tr><th style="width:44px">#</th><th>断言</th><th>状态</th></tr></thead><tbody>')
        for n, row in enumerate(unverified, 1):
            a(
                f'<tr><td>{n}</td><td>{_inline(row["claim"])}</td>'
                f'<td>{_source_badge(row.get("source", "observed"), row.get("note", ""))}</td></tr>'
            )
        a("</tbody></table></div>")
    else:
        a("<p>本报告全部断言均有落盘文件可核对。</p>")
    a("</section>")
    return "\n".join(p)


def _package_tree(spec: Dict, layout: Dict) -> str:
    meta = spec["meta"]
    base = meta.get("package_root", "outputs/bug_reports")
    lines = [f"{base}/", f"├── {meta['basename']}.html       ← 本文件（汇总 + 缺陷报告）",
             f"├── {meta['basename']}.md         ← 同内容 Markdown 版",
             "├── bugs.json                     ← 报告数据源（可重新生成本报告）",
             "└── evidence/"]
    bugs = spec["bugs"]
    for i, bug in enumerate(bugs):
        last_bug = i == len(bugs) - 1
        lines.append(f"    {'└──' if last_bug else '├──'} {bug['id']}/{' ' * 6}{bug.get('module', '')}")
        runs = layout.get(bug["id"], {})
        for j, (key, run) in enumerate(runs.items()):
            last_run = j == len(runs) - 1
            prefix = "    " if last_bug else "│   "
            lines.append(f"    {prefix}{'└──' if last_run else '├──'} {Path(run['dir']).name}/  ({key}，{len(run['files'])} 个文件)")
    return "\n".join(lines)


# ---------------------------------------------------------------- 渲染 Markdown


def render_md(spec: Dict, stats: Dict, layout: Dict, out_dir: Path) -> str:
    meta = spec["meta"]
    bugs = spec["bugs"]
    sev_count = Counter(b["severity"] for b in bugs)
    sev_line = " / ".join(f"{s}×{sev_count[s]}" for s in SEVERITY_ORDER if sev_count[s])
    out: List[str] = [f"# {meta['title']}（{meta['date']}）", ""]

    out.append("## 一、执行摘要")
    out.append("")
    out.append(
        f"试点期间执行 **{stats['total_runs']} 轮**自动化回放，覆盖 **{stats['total_tasks']} 个用例脚本**，"
        f"产出 **{len(bugs)} 条产品缺陷**（{sev_line}）。"
    )
    env = meta.get("env") or {}
    if env:
        out.append("")
        out.append("| 项 | 值 |")
        out.append("| --- | --- |")
        out.extend(f"| {k} | {v} |" for k, v in env.items())
    for card in meta.get("summary_cards") or []:
        out.append("")
        out.append(f"### {card['title']}")
        out.append("")
        out.append(blocks_to_md(card["blocks"]))

    out.append("")
    out.append("## 二、缺陷一览")
    out.append("")
    out.append("| 编号 | 模块 | 标题 | 级别 | 归属建议 | 复现率 | 日期 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for bug in bugs:
        label = SEVERITY_LABELS[bug["severity"]] + (f"（{bug['severity_note']}）" if bug.get("severity_note") else "")
        out.append(
            f"| {bug['id']} | {bug.get('module', '')} | {bug['title']} | **{label}** "
            f"| {bug.get('owner', '')} | {bug.get('repro_rate', '—')} | {bug.get('date', '')} |"
        )
    for block in meta.get("defects_notes") or []:
        out.append("")
        out.append(blocks_to_md([block]))

    out.append("")
    out.append("## 三、回放执行汇总")
    out.append("")
    out.append(stats_markdown(stats, meta.get("task_labels", {}), _bug_index(bugs)))
    out.append("")
    out.append(
        f"> **读表注意**：`error` / `warning` 是引擎记录的 finding 计数，**不等于产品缺陷数**；"
        f"多数 warning 是自动化脚本自身的识别加固噪音，经复核确认的产品缺陷仅 {len(bugs)} 条。"
    )

    out.append("")
    out.append("## 四、方法与证据约定")
    out.append("")
    out.append(
        "AutoPlayQA 是确定性回放工具（截图 + OCR / 控件识别驱动点击），**不使用任何 AI 猜测**，"
        "因此复现步骤全部写成人工可照做的操作。报告严格区分两类断言：**有文件证据**（可核对落盘文件）与"
        "**未捕获到直接证据，为执行期观察**（勿当作已验证结论，汇总见附录 C）。"
    )

    for idx, bug in enumerate(bugs, start=5):
        out.append("")
        out.append(f"## {_cn(idx)}、{bug['id']}（{bug.get('owner', '')}）{bug.get('headline', bug['title'])}")
        out.append("")
        out.append("| 字段 | 内容 |")
        out.append("| --- | --- |")
        out.append(f"| **标题** | {bug['title']} |")
        label = SEVERITY_LABELS[bug["severity"]] + (f"（{bug['severity_note']}）" if bug.get("severity_note") else "")
        out.append(f"| **严重级别** | **{label}** |")
        if bug.get("severity_reason"):
            out.append(f"| **级别理由** | {bug['severity_reason']} |")
        if bug.get("owner_reason"):
            out.append(f"| **归属建议** | **{bug.get('owner', '')}**。{bug['owner_reason']} |")
        out.append(f"| **复现率** | {bug.get('repro_rate', '—')} |")

        env_rows = bug.get("env")
        if isinstance(env_rows, str):
            out.append("")
            out.append(f"**环境**：{env_rows}")
        elif env_rows:
            out.append("")
            out.append("**环境**")
            out.append("")
            out.append("| 项 | 值 | 来源 |")
            out.append("| --- | --- | --- |")
            for row in env_rows:
                out.append(f"| {row['item']} | {row['value']} | {_source_md(row.get('source'), row.get('source_note', ''))} |")

        for title, key in (("前置条件", "preconditions"), ("复现步骤（人工手动照做）", "steps")):
            if bug.get(key):
                out.append("")
                out.append(f"**{title}**")
                out.append("")
                out.append(blocks_to_md([{"ol": bug[key]}]))
        for title, key in (("实际结果", "actual"), ("期望结果", "expected"), ("复现率", "repro_detail")):
            if bug.get(key):
                out.append("")
                out.append(f"**{title}**")
                out.append("")
                out.append(blocks_to_md(bug[key]))
        for extra in bug.get("sections") or []:
            out.append("")
            out.append(f"**{extra['title']}**")
            out.append("")
            out.append(blocks_to_md(extra["blocks"]))

        out.append("")
        out.append("**证据清单**")
        out.append("")
        out.append("| 证据 | 相对路径 | 说明 |")
        out.append("| --- | --- | --- |")
        for item in bug.get("evidence") or []:
            paths = [resolve_ref(bug["id"], ref, layout, out_dir) for ref in (item.get("files") or [item])]
            out.append(f"| {item.get('name', '')} | {'、'.join('`' + p + '`' for p in paths)} | {item.get('desc', '')} |")
        for block in bug.get("footnotes") or []:
            out.append("")
            out.append(blocks_to_md([block]))

    out.append("")
    out.append("## 附录 A · 证据包结构")
    out.append("")
    out.append("```text\n" + _package_tree(spec, layout) + "\n```")
    out.append("")
    out.append("## 附录 B · 原始证据位置")
    out.append("")
    out.append("| 缺陷 | 原始目录 |")
    out.append("| --- | --- |")
    for bug in bugs:
        srcs = list(dict.fromkeys(run["src"] for run in layout.get(bug["id"], {}).values()))
        if srcs:
            out.append(f"| {bug['id']} | `{'、'.join(srcs)}` |")

    out.append("")
    out.append("## 附录 C · 本报告中「无文件证据」的断言汇总")
    out.append("")
    unverified = meta.get("unverified") or []
    if unverified:
        out.append("| # | 断言 | 状态 |")
        out.append("| --- | --- | --- |")
        for n, row in enumerate(unverified, 1):
            out.append(f"| {n} | {row['claim']} | {_source_md(row.get('source', 'observed'), row.get('note', ''))} |")
    else:
        out.append("本报告全部断言均有落盘文件可核对。")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 校验


REQUIRED_META = ("title", "date", "basename")
REQUIRED_BUG = ("id", "title", "severity", "module")


def validate_spec(spec: Dict) -> None:
    if "meta" not in spec or "bugs" not in spec:
        raise BuildError("bugs.json 顶层必须有 meta 与 bugs 两个键")
    for key in REQUIRED_META:
        if not spec["meta"].get(key):
            raise BuildError(f"meta 缺少必填字段：{key}")
    if not spec["bugs"]:
        raise BuildError("bugs 为空：没有缺陷就不需要出缺陷报告（可只跑 --stats-only 出回放汇总）")
    seen = set()
    for bug in spec["bugs"]:
        for key in REQUIRED_BUG:
            if not bug.get(key):
                raise BuildError(f"缺陷 {bug.get('id', '?')} 缺少必填字段：{key}")
        if bug["severity"] not in SEVERITY_LABELS:
            raise BuildError(f"{bug['id']} 的 severity 非法：{bug['severity']}（可用 {list(SEVERITY_LABELS)}）")
        if bug["id"] in seen:
            raise BuildError(f"缺陷编号重复：{bug['id']}")
        seen.add(bug["id"])
        if not bug.get("steps"):
            raise BuildError(f"{bug['id']} 没有复现步骤：报告的价值在于可复现，steps 必填")
        if not bug.get("evidence"):
            raise BuildError(f"{bug['id']} 没有证据清单：异常必须留证，evidence 必填")


def check_links(html_text: str, out_dir: Path) -> List[str]:
    """渲染后自检：所有指向 evidence/ 的 href/src 必须在包内存在。"""
    import re

    missing = []
    for rel in sorted(set(re.findall(r'(?:href|src)="(evidence/[^"]+)"', html_text))):
        if not (out_dir / rel).exists():
            missing.append(rel)
    return missing


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="冒烟汇报生成器：bugs.json -> 证据包 + HTML + MD")
    parser.add_argument("spec", help="bugs.json 路径")
    parser.add_argument("--out", help="输出目录，默认 bugs.json 所在目录")
    parser.add_argument("--project-root", default=".", help="项目根（解析 outputs/findings 相对路径），默认当前目录")
    parser.add_argument("--validate", action="store_true", help="只校验不写文件")
    parser.add_argument("--stats-only", action="store_true", help="只打印回放执行汇总表")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    spec_path = Path(args.spec).resolve()
    project_root = Path(args.project_root).resolve()
    out_dir = Path(args.out).resolve() if args.out else spec_path.parent

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[错误] 无法读取 {spec_path}: {exc}", file=sys.stderr)
        return 1

    try:
        meta = spec.get("meta", {})
        findings_root = project_root / meta.get("findings_root", "outputs/findings")
        period = meta.get("period")
        stats = scan_runs(findings_root, tuple(period) if period else None)

        if args.stats_only:
            print(stats_markdown(stats, meta.get("task_labels", {}), _bug_index(spec.get("bugs", []))))
            return 0

        validate_spec(spec)
        out_dir.mkdir(parents=True, exist_ok=True)
        layout = collect_evidence(spec, out_dir, project_root, do_copy=not args.validate)

        html_text = render_html(spec, stats, layout, out_dir)
        md_text = render_md(spec, stats, layout, out_dir)
        if not args.validate:
            missing = check_links(html_text, out_dir)
            if missing:
                raise BuildError("渲染后存在断链：\n  " + "\n  ".join(missing))
    except BuildError as exc:
        print(f"[校验失败] {exc}", file=sys.stderr)
        return 1

    if args.validate:
        print(f"[OK] 校验通过：{len(spec['bugs'])} 条缺陷，{stats['total_runs']} 轮回放，证据引用全部可解析")
        return 0

    html_path = out_dir / f"{spec['meta']['basename']}.html"
    md_path = out_dir / f"{spec['meta']['basename']}.md"
    html_path.write_text(html_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")

    files = sum(len(run["files"]) for bug in layout.values() for run in bug.values())
    print(f"[OK] 报告已生成")
    print(f"  HTML : {html_path}")
    print(f"  MD   : {md_path}")
    print(f"  证据 : {files} 个文件 -> {out_dir / 'evidence'}")
    print(f"  统计 : {stats['total_runs']} 轮 / {stats['total_tasks']} 个用例 / {len(spec['bugs'])} 条缺陷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
