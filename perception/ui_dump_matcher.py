from __future__ import annotations

import math
import re
import subprocess
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from core.adb_timeout import adb_timeout_s


class UiDumpMatcher:
    """uiautomator dump + text-similarity node matching.

    Shared by UIDetector (NL command path) and task recognizers (ui_text channel).
    """

    def __init__(self, logger):
        self.logger = logger

    _DEVICE_DUMP_PATH = "/sdcard/window_dump.xml"

    def dump_ui_xml(self, device_id: str) -> str:
        xml = self._dump_via_tty(device_id)
        if xml:
            return xml
        # Some ROMs ignore the /dev/tty trick and only
        # write the hierarchy to a file; fall back to dump-to-file + cat.
        return self._dump_via_file(device_id)

    def _dump_via_tty(self, device_id: str) -> str:
        out = self._run_adb(["-s", device_id, "shell", "uiautomator", "dump", "/dev/tty"])
        if out is None:
            return ""
        start = out.find("<?xml")
        return "" if start < 0 else out[start:]

    def _dump_via_file(self, device_id: str) -> str:
        dumped = self._run_adb(["-s", device_id, "shell", "uiautomator", "dump", self._DEVICE_DUMP_PATH])
        if dumped is None:
            return ""
        out = self._run_adb(["-s", device_id, "shell", "cat", self._DEVICE_DUMP_PATH])
        if out is None:
            return ""
        start = out.find("<?xml")
        return "" if start < 0 else out[start:]

    def _run_adb(self, args: List[str]) -> Optional[str]:
        """Run an adb command; returns stdout (utf-8) or None on failure.

        A timeout is treated like any other dump failure (None): the two-level
        fallback chain (tty dump -> file dump) still runs, and a caller that
        gets "" back falls through to OCR — never to an unbounded wait.
        """
        timeout = adb_timeout_s()
        try:
            result = subprocess.run(
                ["adb"] + args, check=False, capture_output=True,
                encoding="utf-8", errors="ignore", timeout=timeout,
            )
        except FileNotFoundError:
            self.logger.warning("adb not found when trying to dump UI hierarchy")
            return None
        except subprocess.TimeoutExpired:
            self.logger.warning(
                "adb %s timed out after %gs; treating the dump as empty",
                " ".join(args[1:3]), timeout,
            )
            return None
        if result.returncode != 0:
            self.logger.warning("adb %s failed: %s", " ".join(args[1:3]), result.stderr.strip())
            return None
        return result.stdout

    def extract_nodes(self, xml_text: str) -> List[Dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        nodes: List[Dict] = []
        for node in root.iter("node"):
            bounds = self.parse_bounds(node.attrib.get("bounds", ""))
            if not bounds:
                continue

            nodes.append(
                {
                    "text": (node.attrib.get("text") or "").strip(),
                    "desc": (node.attrib.get("content-desc") or "").strip(),
                    "resource_id": (node.attrib.get("resource-id") or "").strip(),
                    "class_name": (node.attrib.get("class") or "").strip(),
                    "clickable": node.attrib.get("clickable", "false") == "true",
                    "focusable": node.attrib.get("focusable", "false") == "true",
                    "enabled": node.attrib.get("enabled", "true") == "true",
                    "bounds": bounds,
                    "center": ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2),
                }
            )
        return nodes

    def pick_best_node(self, nodes: List[Dict], intent: Dict[str, str]) -> Tuple[Optional[Dict], float, List[Dict]]:
        max_x = max(n["bounds"][2] for n in nodes)
        max_y = max(n["bounds"][3] for n in nodes)
        screen_w = max(max_x, 1)
        screen_h = max(max_y, 1)

        scored: List[Tuple[float, Dict]] = []
        for n in nodes:
            if not n["enabled"]:
                continue

            score = 0.0
            target = intent["target"]
            if target:
                score += self.best_text_score(target, n)

            # Generic class/interactable preference.
            class_name = n["class_name"].lower()
            if intent["action"] == "click":
                if n["clickable"]:
                    score += 0.15
                if "button" in class_name or "imagebutton" in class_name:
                    score += 0.20
            else:
                if "edittext" in class_name or "textfield" in class_name:
                    score += 0.35
                if n["focusable"] or n["clickable"]:
                    score += 0.15

            score += self.direction_score(n["center"], screen_w, screen_h, intent["direction"])
            scored.append((score, n))

        if not scored:
            return None, 0.0, []

        scored.sort(key=lambda x: x[0], reverse=True)
        top3 = [
            {
                "score": round(s, 3),
                "center": n["center"],
                "bounds": n["bounds"],
                "text": n["text"],
                "desc": n["desc"],
                "class": n["class_name"],
            }
            for s, n in scored[:3]
        ]
        return scored[0][1], scored[0][0], top3

    def match_text(self, device_id: str, expected: str) -> Tuple[Optional[Dict], float]:
        """Find the node whose text/desc/resource_id best matches `expected`.

        Convenience entry for recognizers: dump + extract + pure text scoring
        (no class/direction bonuses). Returns (node, score) or (None, 0.0).
        """
        xml_text = self.dump_ui_xml(device_id)
        if not xml_text:
            return None, 0.0
        nodes = self.extract_nodes(xml_text)
        if not nodes:
            return None, 0.0

        best_node: Optional[Dict] = None
        best_score = 0.0
        for n in nodes:
            if not n["enabled"]:
                continue
            score = self.best_text_score(expected, n)
            if score > best_score:
                best_node, best_score = n, score
        return best_node, best_score

    def best_text_score(self, target: str, node: Dict) -> float:
        fields = [node["text"], node["desc"], node["resource_id"]]
        best = 0.0
        for field in fields:
            if not field:
                continue
            best = max(best, self.text_similarity(target, field))
        return best

    def text_similarity(self, target: str, field: str) -> float:
        t = target.lower().strip()
        f = field.lower().strip()
        if not t or not f:
            return 0.0
        if t in f:
            return 0.9
        return SequenceMatcher(None, t, f).ratio()

    def direction_score(self, center: Tuple[int, int], w: int, h: int, direction: str) -> float:
        if not direction:
            return 0.0
        nx = center[0] / w
        ny = center[1] / h
        if direction == "down":
            return 0.2 * ny
        if direction == "up":
            return 0.2 * (1 - ny)
        if direction == "left":
            return 0.2 * (1 - nx)
        if direction == "right":
            return 0.2 * nx
        if direction == "center":
            dist = math.dist((nx, ny), (0.5, 0.5))
            return 0.2 * max(0.0, 1 - dist / 0.707)
        return 0.0

    def parse_bounds(self, bounds: str) -> Optional[Tuple[int, int, int, int]]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not m:
            return None
        return tuple(int(g) for g in m.groups())
