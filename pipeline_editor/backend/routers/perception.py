"""感知辅助：截图底图 / OCR / 模板试匹配 / 模板裁剪保存。

全部触发 get_runtime()（需要 adb 设备）。同步 def —— FastAPI 自动丢线程池,
阻塞的 adb 往返不会冻结事件循环（与 mcp_server 的 anyio.to_thread 同构）。
"""
from __future__ import annotations

import base64
import io
from typing import Dict, List, Optional

from fastapi import APIRouter, Body

import backend.autoplayqa  # noqa: F401
from backend.autoplayqa import get_runtime

router = APIRouter(prefix="/api", tags=["perception"])


@router.post("/screenshot")
def api_screenshot(body: Dict = Body(...)) -> Dict:
    """全分辨率截图，base64 PNG 直接返回（ROI 框选 / 模板裁剪的底图）。"""
    device_id = body["device_id"]
    rt = get_runtime()
    image = rt.capturer.capture_image(device_id)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {
        "width": image.width,
        "height": image.height,
        "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


@router.post("/ocr")
def api_ocr(body: Dict = Body(...)) -> List[Dict]:
    device_id = body["device_id"]
    roi = body.get("roi")
    rt = get_runtime()
    return rt.ocr.recognize(rt.capturer.capture_image(device_id), roi=roi)


@router.post("/find-text")
def api_find_text(body: Dict = Body(...)) -> Dict:
    device_id = body["device_id"]
    text = body["text"]
    rt = get_runtime()
    node, score = rt.dump_matcher.match_text(device_id, text)
    if node and score >= 0.65:
        return {"found": True, "center": list(node["center"]), "score": round(score, 3),
                "channel": "ui_text", "matched_text": node["text"] or node["desc"]}
    if rt.ocr.available():
        image = rt.capturer.capture_image(device_id)
        best, best_score = None, 0.0
        for item in rt.ocr.recognize(image):
            s = rt.dump_matcher.text_similarity(text, item["text"])
            if s > best_score:
                best, best_score = item, s
        if best and best_score >= 0.65:
            return {"found": True, "center": list(best["center"]),
                    "score": round(best_score, 3), "channel": "ocr",
                    "matched_text": best["text"]}
    return {"found": False, "center": None, "score": 0.0, "channel": None,
            "matched_text": None}


@router.post("/find-template")
def api_find_template(body: Dict = Body(...)) -> Dict:
    device_id = body["device_id"]
    template = body["template"]
    threshold = body.get("threshold", 0.8)
    roi = body.get("roi")
    scales = body.get("scales")
    multi = body.get("multi", False)
    rt = get_runtime()
    if not rt.template_matcher.available():
        return {"found": False, "error": "opencv 未安装", "matches": [], "count": 0}
    image = rt.capturer.capture_image(device_id)
    try:
        matches = rt.template_matcher.match_all(
            image, template, threshold=threshold, roi=roi, scales=scales,
            max_results=20 if multi else 1,
        )
    except FileNotFoundError as exc:
        return {"found": False, "error": str(exc), "matches": [], "count": 0}
    if not matches:
        return {"found": False, "count": 0, "matches": [], "center": None, "score": 0.0}
    best = matches[0]
    return {"found": True, "count": len(matches), "matches": matches,
            "center": best["center"], "score": best["score"], "bbox": best["bbox"]}


@router.post("/capture-template")
def api_capture_template(body: Dict = Body(...)) -> Dict:
    device_id = body["device_id"]
    name = body["name"]
    region = body["region"]
    rt = get_runtime()
    image = rt.capturer.capture_image(device_id)
    path = rt.template_matcher.save_template(image, name, region=region)
    stem = name[:-4] if name.lower().endswith(".png") else name
    return {"ok": True, "name": stem, "path": path, "region": region}
