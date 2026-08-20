from __future__ import annotations

import re
from typing import Dict, List, Optional

from perception.screenshot_capturer import ScreenshotCapturer
from perception.ui_dump_matcher import UiDumpMatcher
from utils.debug_tracer import DebugTracer
from utils.image_annotator import draw_candidate_boxes


class UIDetector:
	"""LLM-free on-screen text locating: uiautomator dump first, local OCR second.

	Anything these two channels cannot resolve is the external agent's job
	(Claude/Codex looks at the screenshot itself via MCP).
	"""

	def __init__(
		self,
		logger,
		screenshot_capturer: ScreenshotCapturer,
		dump_matcher: Optional[UiDumpMatcher] = None,
		ocr_engine=None,
	):
		self.logger = logger
		self.capturer = screenshot_capturer
		self.matcher = dump_matcher or UiDumpMatcher(logger)
		self.ocr_engine = ocr_engine

	def infer_actions_from_screen(
		self, device_id: str, user_text: str, tracer: Optional["DebugTracer"] = None
	) -> List[Dict]:
		# Capture before-screenshot once for both debug annotation and the OCR pass.
		before_bytes: Optional[bytes] = None
		if tracer and tracer.enabled:
			try:
				before_bytes = self.capturer.capture_png_bytes(device_id)
				tracer.save_image("before.png", before_bytes)
			except Exception as exc:
				self.logger.warning("Debug: before-screenshot failed: %s", exc)

		# First pass: deterministic hierarchy matching with generic scoring.
		dump_actions = self._infer_actions_from_ui_dump(device_id, user_text, tracer, before_bytes)
		if dump_actions:
			return dump_actions

		# Second pass: local OCR text matching (free; works on game surfaces where
		# uiautomator sees nothing). Reuse the debug screenshot when present,
		# otherwise grab a PIL Image directly (no PNG round trip).
		screen = before_bytes if before_bytes is not None else self.capturer.capture_image(device_id)
		return self._infer_actions_from_ocr(user_text, screen, tracer)

	def _infer_actions_from_ui_dump(
		self,
		device_id: str,
		user_text: str,
		tracer: Optional["DebugTracer"] = None,
		before_bytes: Optional[bytes] = None,
	) -> List[Dict]:
		intent = self._parse_intent(user_text)
		xml_text = self.matcher.dump_ui_xml(device_id)
		if not xml_text:
			if tracer and tracer.enabled:
				tracer.record(
					input_text=user_text, parsed_intent=intent, found=False,
					top_candidates=[], chosen_candidate=None,
					uiautomator_error="dump_empty",
				)
			return []

		nodes = self.matcher.extract_nodes(xml_text)
		if not nodes:
			if tracer and tracer.enabled:
				tracer.record(
					input_text=user_text, parsed_intent=intent, found=False,
					top_candidates=[], chosen_candidate=None,
					uiautomator_error="no_nodes",
				)
			return []

		picked, score, top_candidates = self.matcher.pick_best_node(nodes, intent)
		if top_candidates:
			self.logger.info("UI candidates(top3): %s", top_candidates)

		# Confidence gate prevents accidental taps on low-quality matches.
		passed_gate = bool(picked) and (not intent["target"] or score >= 0.65)
		if intent["target"] and picked and score < 0.65:
			self.logger.warning("Low confidence UI match score=%.3f target=%s", score, intent["target"])

		# --- Debug recording ---
		if tracer and tracer.enabled:
			# Serialize tuples → lists for JSON
			candidates_json = [
				{
					"rank": i + 1,
					"score": c["score"],
					"text": c["text"],
					"desc": c["desc"],
					"class": c["class"],
					"center": list(c["center"]),
					"bounds": list(c["bounds"]),
				}
				for i, c in enumerate(top_candidates)
			]
			chosen_json = None
			if picked and passed_gate:
				chosen_json = {
					"text": picked["text"],
					"desc": picked["desc"],
					"center": list(picked["center"]),
					"bounds": list(picked["bounds"]),
					"score": round(score, 3),
				}
			tracer.record(
				input_text=user_text,
				parsed_intent=intent,
				found=passed_gate,
				top_candidates=candidates_json,
				chosen_candidate=chosen_json,
			)
			# Draw candidate bounding boxes on the before screenshot
			if tracer.annotate and before_bytes and top_candidates:
				try:
					annotated = draw_candidate_boxes(before_bytes, candidates_json)
					tracer.save_image("before_annotated.png", annotated)
				except Exception as exc:
					self.logger.warning("Debug: annotation failed: %s", exc)
		# --- End debug recording ---

		if not passed_gate:
			return []

		x, y = picked["center"]

		if intent["action"] == "input_text" and intent["input_text"]:
			return [
				{"type": "click", "params": {"x": x, "y": y}},
				{"type": "input_text", "params": {"text": intent["input_text"]}},
			]

		return [{"type": "click", "params": {"x": x, "y": y}}]

	def _infer_actions_from_ocr(
		self,
		user_text: str,
		screen,  # PNG bytes or PIL Image
		tracer: Optional["DebugTracer"] = None,
	) -> List[Dict]:
		if not self.ocr_engine or not self.ocr_engine.available():
			return []

		intent = self._parse_intent(user_text)
		if not intent["target"]:
			return []

		try:
			ocr_items = self.ocr_engine.recognize(screen)
		except Exception as exc:
			self.logger.warning("OCR pass failed: %s", exc)
			return []

		best_item = None
		best_score = 0.0
		for item in ocr_items:
			score = self.matcher.text_similarity(intent["target"], item["text"])
			if score > best_score:
				best_item, best_score = item, score

		if tracer and tracer.enabled:
			tracer.record(
				ocr_target=intent["target"],
				ocr_best_score=round(best_score, 3),
				ocr_best_text=best_item["text"] if best_item else None,
			)

		# Same confidence gate as the dump pass.
		if not best_item or best_score < 0.65:
			return []

		x, y = best_item["center"]
		self.logger.info("OCR matched '%s' score=%.3f center=(%d,%d)", best_item["text"], best_score, x, y)

		if intent["action"] == "input_text" and intent["input_text"]:
			return [
				{"type": "click", "params": {"x": x, "y": y}},
				{"type": "input_text", "params": {"text": intent["input_text"]}},
			]

		return [{"type": "click", "params": {"x": x, "y": y}}]

	def _parse_intent(self, user_text: str) -> Dict[str, str]:
		text = user_text.strip()
		action = "input_text" if re.search(r"输入|填入|键入|写入|\binput\b|\btype\b", text, re.IGNORECASE) else "click"

		# Try extracting target phrase around natural-language patterns.
		target = ""
		patterns = [
			r"(?:点击|点一下|点)\s*(.+?)(?:按钮|控件|$)",
			r"(?:在|到)?\s*(.+?)(?:中|里|内)?\s*(?:输入|填入|键入|写入)",
			r"(?:click|tap)\s+(.+)$",
		]
		for p in patterns:
			m = re.search(p, text, re.IGNORECASE)
			if m:
				target = m.group(1).strip(" \"'“”")
				break

		input_text = ""
		m_input = re.search(r"(?:输入|填入|键入|写入)\s*[\"“]?([^\"”]+)[\"”]?", text)
		if m_input:
			input_text = m_input.group(1).strip()
		else:
			m_input_en = re.search(r"(?:input|type)\s+[\"']?([^\"']+)[\"']?", text, re.IGNORECASE)
			if m_input_en:
				input_text = m_input_en.group(1).strip()

		direction = ""
		if any(k in text for k in ["下方", "下面", "底部", "下边"]):
			direction = "down"
		elif any(k in text for k in ["上方", "上面", "顶部", "上边"]):
			direction = "up"
		elif any(k in text for k in ["左侧", "左边"]):
			direction = "left"
		elif any(k in text for k in ["右侧", "右边"]):
			direction = "right"
		elif any(k in text for k in ["中间", "中央", "中心"]):
			direction = "center"

		return {
			"action": action,
			"target": target,
			"input_text": input_text,
			"direction": direction,
		}
