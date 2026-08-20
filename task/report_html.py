"""Human-readable rendering of a findings report: report.json -> report.html.

QA colleagues get a run folder (or an exported zip) whose report.json is machine
readable only. This module turns the very same dict into a self-contained single
HTML file written next to it: no external CSS/JS/font/CDN request, so it opens
offline by double-click and survives being zipped up and mailed around.

Evidence is referenced exactly as report.json stores it — run-dir-relative,
forward-slash paths — so the page keeps working from inside the folder or the
extracted archive: screenshots render as <img>, recordings as <video controls>,
the logcat fragment and flow timeline inline as collapsible <details>, anything
else as a plain link.

Pure standard library (str + html.escape), no template engine; every dynamic
value goes through escaping because game text and logcat lines carry <, > and &.
"""

from __future__ import annotations

import html
import json
from typing import Dict, List, Tuple

# Evidence keys the recorder writes, in the order they should be presented.
EVIDENCE_ORDER = (
    "screenshot",
    "screenshot_exact",
    "video",
    "history",
    "logcat",
    "timeline",
    "pcap",
    "ui_dump",
)

EVIDENCE_LABELS = {
    "screenshot": "证据截图",
    "screenshot_exact": "无损截图（settle 后）",
    "video": "录屏",
    "history": "问题前历史帧",
    "logcat": "logcat 片段（文件）",
    "timeline": "流程时间线（文件）",
    "pcap": "网络抓包（pcap）",
    "ui_dump": "UI 层级 dump",
}

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv")

SEVERITY_LABELS = {
    "info": "info",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
       line-height: 1.6; color: #1f2328; background: #f6f8fa; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 12px; }
.sub { color: #57606a; font-size: 13px; margin: 0 0 20px; }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
        padding: 16px 18px; margin-bottom: 16px; }
table.meta { border-collapse: collapse; font-size: 14px; }
table.meta th { text-align: left; padding: 4px 16px 4px 0; color: #57606a; font-weight: 500;
                white-space: nowrap; vertical-align: top; }
table.meta td { padding: 4px 0; word-break: break-all; }
.badges { margin: 12px 0 0; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px;
         margin-right: 6px; border: 1px solid transparent; }
.sev-info { background: #ddf4ff; color: #0969da; border-color: #b6e3ff; }
.sev-warning { background: #fff8c5; color: #7d4e00; border-color: #f5e6a8; }
.sev-error { background: #ffebe9; color: #cf222e; border-color: #ffcecb; }
.sev-critical { background: #cf222e; color: #fff; border-color: #a40e26; }
.sev-unknown { background: #eaeef2; color: #57606a; border-color: #d0d7de; }
.st-completed { background: #dafbe1; color: #1a7f37; border-color: #aceebb; }
.st-failed { background: #ffebe9; color: #cf222e; border-color: #ffcecb; }
.st-other { background: #eaeef2; color: #57606a; border-color: #d0d7de; }
.f-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin-bottom: 8px; }
.f-seq { font-weight: 600; }
.f-type { font-family: ui-monospace, Consolas, monospace; font-size: 13px; color: #0550ae; }
.f-node { font-size: 13px; color: #57606a; }
.f-time { font-size: 12px; color: #8c959f; margin-left: auto; }
.f-msg { font-size: 15px; margin: 0 0 12px; white-space: pre-wrap; word-break: break-word; }
.ev { margin-top: 12px; }
.ev-label { font-size: 12px; color: #57606a; margin-bottom: 4px; }
.ev img, .ev video { max-width: 100%; max-height: 460px; border: 1px solid #d0d7de;
                     border-radius: 6px; background: #eaeef2; vertical-align: top; }
.shots { display: flex; flex-wrap: wrap; gap: 10px; }
.shots figure { margin: 0; }
.shots figcaption { font-size: 11px; color: #8c959f; }
.thumb img { max-height: 150px; }
details { margin-top: 10px; border: 1px solid #d0d7de; border-radius: 6px; padding: 6px 10px;
          background: #f6f8fa; }
summary { cursor: pointer; font-size: 13px; color: #24292f; }
pre { margin: 8px 0 4px; padding: 10px; background: #0d1117; color: #e6edf3; border-radius: 6px;
      overflow-x: auto; font-family: ui-monospace, Consolas, monospace; font-size: 12px;
      line-height: 1.5; white-space: pre; }
table.stats { border-collapse: collapse; font-size: 13px; width: 100%; }
table.stats th, table.stats td { border-bottom: 1px solid #d0d7de; padding: 6px 10px;
                                 text-align: right; white-space: nowrap; }
table.stats th { color: #57606a; font-weight: 500; }
table.stats th.node, table.stats td.node { text-align: left; word-break: break-all;
                                           white-space: normal; }
table.stats tr.rot td { background: #fff8c5; }
.stats-wrap { overflow-x: auto; }
ul.links { list-style: none; margin: 6px 0 0; padding: 0; font-size: 13px; }
ul.links li { margin: 2px 0; }
a { color: #0969da; }
.empty { color: #57606a; font-size: 14px; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .card, details { background: #161b22; border-color: #30363d; }
  table.meta th, .sub, .f-node, .ev-label, .empty { color: #8b949e; }
  summary { color: #e6edf3; }
  .f-type { color: #79c0ff; }
}
"""


def render_report_html(report: Dict) -> str:
    """Render a findings report dict (the report.json payload) as one HTML page.

    Accepts a report with zero findings and renders the run metadata plus an
    explicit "no findings" note, so the page is always a valid stand-alone
    document. Never raises on odd/missing fields: unknown values degrade to
    placeholders rather than breaking the report.
    """
    report = report or {}
    findings = report.get("findings") or []
    title = "QA 运行报告 - {}".format(report.get("task") or "未命名任务")

    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f"<style>{STYLE}</style>",
        "</head>",
        "<body>",
        _render_header(report, len(findings)),
    ]
    if findings:
        parts.append(f"<h2>测试发现（{len(findings)}）</h2>")
        parts.extend(_render_finding(f) for f in findings)
    else:
        parts.append("<h2>测试发现</h2>")
        parts.append('<div class="card empty">本次运行没有记录到任何测试发现。</div>')
    parts.append(_render_node_stats(report.get("node_stats")))
    parts.extend(["</body>", "</html>", ""])
    return "\n".join(part for part in parts if part)


# ---------- sections ----------


def _render_header(report: Dict, total: int) -> str:
    status = str(report.get("status") or "unknown")
    rows = [
        ("任务", report.get("task")),
        ("设备", report.get("device")),
        ("运行目录", report.get("run_id")),
        ("开始时间", report.get("started_at")),
        ("结束时间", report.get("finished_at")),
        ("状态", None),  # rendered as a badge below
        ("错误", report.get("error")),
    ]
    cells: List[str] = []
    for label, value in rows:
        if label == "状态":
            cells.append(
                f"<tr><th>状态</th><td>"
                f'<span class="badge {_status_class(status)}">{_esc(status)}</span>'
                f"</td></tr>"
            )
            continue
        if value in (None, ""):
            continue
        cells.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")

    counts = report.get("counts") or {}
    badges = "".join(
        f'<span class="badge {_severity_class(sev)}">{_esc(sev)} × {_esc(num)}</span>'
        for sev, num in counts.items()
    )
    badge_block = f'<div class="badges">{badges}</div>' if badges else ""
    return (
        f"<h1>{_esc('QA 运行报告')}</h1>"
        f'<p class="sub">共 {total} 条测试发现 · 证据文件与本页同目录，可整包复制或解压后离线查看</p>'
        f'<div class="card"><table class="meta">{"".join(cells)}</table>{badge_block}</div>'
    )


#: node_stats key -> column header, in display order.
STAT_COLUMNS = (
    ("direct_hits", "直接命中"),
    ("popup_assisted_hits", "弹窗协助"),
    ("back_assisted_hits", "BACK 协助"),
    ("recovery_hits", "兜底命中"),
    ("timeout_recoveries", "超时兜底"),
    ("drift_count", "锚点移位"),
    ("poll_rounds", "识别轮次"),
)


def _render_node_stats(node_stats) -> str:
    """Per-node health table: how each node was reached this run.

    Rows that needed a timeout recovery or whose anchor moved are highlighted —
    those are the nodes whose anchors are going stale, which is what a QA
    engineer wants to see before the task starts failing outright.
    """
    if not isinstance(node_stats, dict) or not node_stats:
        return ""

    def sort_key(item):
        name, stat = item
        stat = stat if isinstance(stat, dict) else {}
        return (-(_num(stat.get("timeout_recoveries")) + _num(stat.get("drift_count"))), str(name))

    head = '<th class="node">节点</th>' + "".join(
        f"<th>{_esc(label)}</th>" for _, label in STAT_COLUMNS
    )
    rows: List[str] = []
    for name, stat in sorted(node_stats.items(), key=sort_key):
        stat = stat if isinstance(stat, dict) else {}
        shaky = _num(stat.get("timeout_recoveries")) or _num(stat.get("drift_count"))
        cells = "".join(f"<td>{_esc(stat.get(key, 0))}</td>" for key, _ in STAT_COLUMNS)
        rows.append(
            f'<tr class="{"rot" if shaky else ""}">'
            f'<td class="node">{_esc(name)}</td>{cells}</tr>'
        )
    return (
        "<h2>节点健康度</h2>"
        '<div class="card"><div class="stats-wrap">'
        f'<table class="stats"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="sub" style="margin:12px 0 0">高亮行 = 本次运行走过超时兜底或锚点移位，'
        "锚点可能正在腐烂，建议复核该节点的识别配置。</p></div>"
    )


def _num(value) -> int:
    """Best-effort int for a stat cell (report.json may carry anything)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _render_finding(finding: Dict) -> str:
    finding = finding if isinstance(finding, dict) else {}
    severity = str(finding.get("severity") or "unknown")
    head = [
        f'<span class="f-seq">#{_esc(finding.get("seq", "?"))}</span>',
        f'<span class="badge {_severity_class(severity)}">{_esc(severity)}</span>',
        f'<span class="f-type">{_esc(finding.get("type") or "unknown")}</span>',
    ]
    if finding.get("node"):
        head.append(f'<span class="f-node">节点：{_esc(finding["node"])}</span>')
    if finding.get("time"):
        head.append(f'<span class="f-time">{_esc(finding["time"])}</span>')

    body = [
        '<div class="card">',
        f'<div class="f-head">{"".join(head)}</div>',
        f'<p class="f-msg">{_esc(finding.get("message") or "")}</p>',
        _render_evidence(finding.get("evidence") or {}),
        _render_lines_block("logcat 片段（问题前）", finding.get("log_excerpt")),
        _render_lines_block("流程时间线（问题前）", finding.get("recent_flow")),
        _render_extra(finding.get("extra")),
        "</div>",
    ]
    return "".join(part for part in body if part)


def _render_evidence(evidence: Dict) -> str:
    if not isinstance(evidence, dict) or not evidence:
        return ""
    keys = [k for k in EVIDENCE_ORDER if k in evidence]
    keys += [k for k in evidence if k not in EVIDENCE_ORDER]

    blocks: List[str] = []
    links: List[str] = []
    for key in keys:
        label = EVIDENCE_LABELS.get(key, str(key))
        paths = _as_paths(evidence.get(key))
        if not paths:
            continue
        media, leftovers = _render_media(label, paths, thumb=key == "history")
        if media:
            blocks.append(media)
        links.extend(f'<li>{_esc(label)}：<a href="{_esc(p)}">{_esc(p)}</a></li>' for p in leftovers)

    if links:
        blocks.append(f'<ul class="links">{"".join(links)}</ul>')
    return f'<div class="ev">{"".join(blocks)}</div>' if blocks else ""


def _render_media(label: str, paths: List[str], thumb: bool = False) -> Tuple[str, List[str]]:
    """Split paths into renderable media (img/video figures) and plain links."""
    figures: List[str] = []
    leftovers: List[str] = []
    for path in paths:
        lower = path.lower()
        name = path.rsplit("/", 1)[-1]
        if lower.endswith(IMAGE_SUFFIXES):
            figures.append(
                f"<figure><a href=\"{_esc(path)}\">"
                f'<img src="{_esc(path)}" alt="{_esc(label)}" loading="lazy"></a>'
                f"<figcaption>{_esc(name)}</figcaption></figure>"
            )
        elif lower.endswith(VIDEO_SUFFIXES):
            figures.append(
                f'<figure><video controls preload="metadata" src="{_esc(path)}"></video>'
                f"<figcaption>{_esc(name)}</figcaption></figure>"
            )
        else:
            leftovers.append(path)
    if not figures:
        return "", leftovers
    cls = "shots thumb" if thumb else "shots"
    block = (
        f'<div class="ev-label">{_esc(label)}</div>'
        f'<div class="{cls}">{"".join(figures)}</div>'
    )
    return block, leftovers


def _render_lines_block(label: str, lines) -> str:
    if not lines:
        return ""
    items = [lines] if isinstance(lines, str) else [str(line) for line in lines]
    text = "\n".join(items)
    return (
        f"<details><summary>{_esc(label)}（{len(items)} 行）</summary>"
        f"<pre>{_esc(text)}</pre></details>"
    )


def _render_extra(extra) -> str:
    if not extra:
        return ""
    try:
        text = json.dumps(extra, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(extra)
    return f"<details><summary>附加数据</summary><pre>{_esc(text)}</pre></details>"


# ---------- helpers ----------


def _as_paths(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def _severity_class(severity: str) -> str:
    return f"sev-{severity}" if severity in SEVERITY_LABELS else "sev-unknown"


def _status_class(status: str) -> str:
    if status == "completed":
        return "st-completed"
    if status == "failed":
        return "st-failed"
    return "st-other"


def _esc(value) -> str:
    """HTML-escape any value (game text / logcat lines carry <, > and &)."""
    return html.escape("" if value is None else str(value), quote=True)
