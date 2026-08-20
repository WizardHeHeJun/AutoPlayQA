from __future__ import annotations

import re
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from utils.debug_tracer import DebugTracer


def parse_actions_fallback(user_text: str) -> List[Dict]:
    """Deterministic regex parsing for explicit coordinate / input commands."""
    click_match = re.search(r"(?:click|tap|点击)\s+(\d+)\s+(\d+)", user_text, re.IGNORECASE)
    if click_match:
        return [{"type": "click", "params": {"x": int(click_match.group(1)), "y": int(click_match.group(2))}}]

    drag_match = re.search(
        r"(?:drag|swipe|拖拽|滑动)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(\d+))?",
        user_text,
        re.IGNORECASE,
    )
    if drag_match:
        duration = int(drag_match.group(5)) if drag_match.group(5) else 500
        return [
            {
                "type": "drag",
                "params": {
                    "x1": int(drag_match.group(1)),
                    "y1": int(drag_match.group(2)),
                    "x2": int(drag_match.group(3)),
                    "y2": int(drag_match.group(4)),
                    "duration_ms": duration,
                },
            }
        ]

    input_match = re.search(r"(?:input|type)\s+[\"']?([^\"'\s]+)[\"']?", user_text, re.IGNORECASE)
    if input_match:
        return [{"type": "input_text", "params": {"text": input_match.group(1)}}]

    input_match_cn = re.search(r"(?:输入|键入|填入)\s*[\"“]?([0-9A-Za-z@._\-]+)[\"”]?", user_text)
    if input_match_cn:
        return [{"type": "input_text", "params": {"text": input_match_cn.group(1)}}]

    return []


class LocalTextResolver:
    """LLM-free replacement for the old LLMRouter.

    Resolution order: explicit-coordinate regex -> on-screen text locating
    (uiautomator dump -> local OCR via UIDetector). Anything beyond that is
    the external agent's job (Claude/Codex over MCP), so we fail loudly.
    """

    def __init__(self, ui_detector, logger):
        self.ui_detector = ui_detector
        self.logger = logger

    def generate_actions(
        self, user_text: str, device_id: Optional[str] = None, tracer: Optional["DebugTracer"] = None
    ) -> List[Dict]:
        actions = parse_actions_fallback(user_text)
        if actions:
            if tracer and tracer.enabled:
                tracer.record(route_used="regex_fallback", route_actions=actions)
            return actions

        if self.ui_detector and device_id:
            try:
                actions = self.ui_detector.infer_actions_from_screen(device_id, user_text, tracer)
            except Exception as exc:
                if tracer and tracer.enabled:
                    tracer.record(route_used="ui_detector_error", ui_detector_error=str(exc))
                self.logger.warning("UI detector failed: %s", str(exc))
                actions = []
            if actions:
                if tracer and tracer.enabled:
                    tracer.record(route_used="ui_detector", route_actions=actions)
                return actions

        raise RuntimeError(
            "Cannot resolve command locally (no explicit coordinates and no on-screen text match). "
            "Provide coordinates, or drive this step via Claude/Codex with the MCP tools."
        )
