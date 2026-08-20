"""历史报告 / findings：扫描 outputs/findings/<date>/<device>/<run_id>/。"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import backend.autoplayqa  # noqa: F401
from backend.autoplayqa import FINDINGS_DIR

router = APIRouter(prefix="/api", tags=["reports"])


def _run_dir(date: str, device: str, run_id: str):
    path = (FINDINGS_DIR / date / device / run_id).resolve()
    if not path.is_relative_to(FINDINGS_DIR):
        raise HTTPException(400, "路径越界")
    if not path.is_dir():
        raise HTTPException(404, "报告不存在")
    return path


@router.get("/reports")
def api_list_reports(date: Optional[str] = None, device: Optional[str] = None,
                     limit: int = 100) -> List[Dict]:
    if not FINDINGS_DIR.is_dir():
        return []
    out: List[Dict] = []
    for date_dir in sorted(FINDINGS_DIR.iterdir(), reverse=True):
        if not date_dir.is_dir() or (date and date_dir.name != date):
            continue
        for device_dir in sorted(date_dir.iterdir()):
            if not device_dir.is_dir() or (device and device_dir.name != device):
                continue
            for run_dir in sorted(device_dir.iterdir(), reverse=True):
                report_path = run_dir / "report.json"
                if not report_path.is_file():
                    continue
                entry: Dict = {
                    "date": date_dir.name,
                    "device": device_dir.name,
                    "run_id": run_dir.name,
                    "has_html": (run_dir / "report.html").is_file(),
                    "mtime": report_path.stat().st_mtime,
                }
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    findings = report.get("findings") or []
                    severity_counts: Dict[str, int] = {}
                    for f in findings:
                        sev = (f or {}).get("severity", "error")
                        severity_counts[sev] = severity_counts.get(sev, 0) + 1
                    entry.update({
                        "task": report.get("task") or report.get("task_name"),
                        "status": report.get("status"),
                        "finding_count": len(findings),
                        "severity_counts": severity_counts,
                    })
                except (json.JSONDecodeError, OSError):
                    entry["task"] = None
                out.append(entry)
                if len(out) >= limit:
                    return out
    return out


@router.get("/reports/{date}/{device}/{run_id}")
def api_get_report(date: str, device: str, run_id: str) -> Dict:
    run_dir = _run_dir(date, device, run_id)
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        raise HTTPException(404, "report.json 不存在")
    return json.loads(report_path.read_text(encoding="utf-8"))


@router.get("/reports/{date}/{device}/{run_id}/html")
def api_report_html(date: str, device: str, run_id: str):
    run_dir = _run_dir(date, device, run_id)
    html = run_dir / "report.html"
    if not html.is_file():
        raise HTTPException(404, "report.html 不存在")
    return FileResponse(html, media_type="text/html")


@router.get("/reports/{date}/{device}/{run_id}/files/{path:path}")
def api_report_file(date: str, device: str, run_id: str, path: str):
    run_dir = _run_dir(date, device, run_id)
    target = (run_dir / path).resolve()
    if not target.is_relative_to(run_dir):
        raise HTTPException(400, "路径越界")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@router.get("/reports/{date}/{device}/{run_id}/{path:path}")
def api_report_relative(date: str, device: str, run_id: str, path: str):
    """兜底：report.html 内的相对引用（证据截图/录屏/logcat）落到这里。

    report.html 是自包含相对路径的（report_html.py 的约定），iframe 里
    加载时相对路径解析为本前缀下的文件名——与 /files/ 等价，但无需改写
    报告内容。放在最后注册，/html 与 /files/ 等具体路由优先匹配。
    """
    return api_report_file(date, device, run_id, path)
