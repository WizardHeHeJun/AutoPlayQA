from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

from action.action_executor import ActionExecutor
from core.logger import log_event

if TYPE_CHECKING:
    from utils.debug_tracer import DebugTracer


class DeviceAgent:
    def __init__(self, profile, logger, text_resolver, config, screenshot_capturer=None):
        self.profile = profile
        self.logger = logger
        self.text_resolver = text_resolver
        self.config = config
        self.executor = ActionExecutor(logger, config)
        self.screenshot_capturer = screenshot_capturer

    @property
    def device_id(self) -> str:
        return self.profile.device_id

    def execute_actions(self, actions: List[Dict]) -> List[Dict]:
        results = []
        for action in actions:
            results.append(self.executor.execute(self.device_id, action))
        return results

    def execute_text_command(
        self, text: str, tracer: Optional["DebugTracer"] = None, recorder=None
    ) -> List[Dict]:
        actions = self.text_resolver.generate_actions(text, device_id=self.device_id, tracer=tracer)

        verify = (
            self.config.get("execution", {}).get("verify_steps", False)
            and len(actions) > 1
            and self.screenshot_capturer is not None
        )
        change_threshold = float(self.config.get("execution", {}).get("verify_change_threshold", 0.005))

        prev_frame = None  # PIL Image; PNG round trip skipped on the verify path
        if verify:
            try:
                prev_frame = self.screenshot_capturer.capture_image(self.device_id)
            except Exception as exc:
                self.logger.warning(
                    "verify_steps: before-screenshot failed on %s, disabling: %s",
                    self.device_id, exc,
                )
                verify = False

        results = []
        for index, action in enumerate(actions):
            results.append(self.executor.execute(self.device_id, action, tracer))

            # Screen-effect gate: a multi-step plan where a tap/drag changed nothing
            # means the plan is off-track; abort instead of chaining errors.
            if verify and action.get("type") in ("click", "drag") and index < len(actions) - 1:
                from utils.helpers import image_change_ratio

                try:
                    cur_frame = self.screenshot_capturer.capture_image(self.device_id)
                except Exception as exc:
                    self.logger.warning(
                        "verify_steps: screenshot failed on %s, disabling: %s",
                        self.device_id, exc,
                    )
                    verify = False
                    continue
                ratio = image_change_ratio(prev_frame, cur_frame)
                if tracer and tracer.enabled:
                    tracer.record(**{f"verify_step_{index}_change_ratio": round(ratio, 5)})
                if ratio < change_threshold:
                    message = (
                        f"Step {index + 1}/{len(actions)} ({action.get('type')}) produced no screen change "
                        f"(ratio={ratio:.5f}) on {self.device_id}; aborting remaining actions."
                    )
                    self.logger.warning(message)
                    results.append({"ok": "False", "stdout": "", "stderr": message})
                    break
                # The passing case used to exist only inside a DebugTracer that
                # is off by default, so a plan that barely cleared the threshold
                # looked identical to one that redrew the whole screen.
                log_event(self.logger, "verify_step", device=self.device_id,
                          index=index, change_ratio=round(ratio, 5))
                prev_frame = cur_frame

        if recorder is not None:
            recorder.add(self.device_id, text, actions, results)

        # Capture after-screenshot and flush trace record.
        if tracer and tracer.enabled:
            if tracer.capture_after and self.screenshot_capturer:
                try:
                    after_bytes = self.screenshot_capturer.capture_png_bytes(self.device_id)
                    tracer.save_image("after.png", after_bytes)
                except Exception as exc:
                    self.logger.warning("Debug: after-screenshot failed: %s", exc)
            tracer.flush()

        return results
