from __future__ import annotations

from typing import Dict, List, Optional

from agent.device_agent import DeviceAgent
from core.logger import log_event

#: Free-text commands are truncated to this many characters in log lines.
MAX_LOG_TEXT = 40


def _brief(text: str) -> str:
    """A free-text command as one short log token."""
    return text if len(text) <= MAX_LOG_TEXT else text[:MAX_LOG_TEXT] + "..."


class AgentPool:
    def __init__(self, logger, text_resolver, config, screenshot_capturer=None):
        self.logger = logger
        self.text_resolver = text_resolver
        self.config = config
        self.screenshot_capturer = screenshot_capturer
        self._agents: Dict[str, DeviceAgent] = {}
        self.selected = "all"
        self._last_tracers: Dict[str, Optional[object]] = {}

    def sync_from_devices(self, devices):
        previous = len(self._agents)
        self._agents = {
            d.device_id: DeviceAgent(d, self.logger, self.text_resolver, self.config, self.screenshot_capturer)
            for d in devices
        }
        # A device that silently dropped off between two commands is the usual
        # explanation for "the command did nothing"; the count makes it visible.
        self.logger.debug(
            "Agent pool synced: %d device(s) (was %d): %s",
            len(self._agents), previous, ",".join(sorted(self._agents)) or "-",
        )

    def list_agents(self) -> List[DeviceAgent]:
        return list(self._agents.values())

    def set_selected(self, selector: str) -> None:
        if selector == "all" or selector in self._agents:
            self.selected = selector
            return
        if selector.isdigit():
            index = int(selector)
            keys = sorted(self._agents.keys())
            if 1 <= index <= len(keys):
                self.selected = keys[index - 1]
                return
        raise ValueError(f"Unknown agent selector: {selector}")

    def get_last_tracers(self) -> Dict[str, Optional[object]]:
        return self._last_tracers

    def execute_text(
        self, text: str, debug_config: Optional[Dict] = None, recorder=None
    ) -> Dict[str, List[Dict]]:
        from utils.debug_tracer import DebugTracer

        self._last_tracers = {}
        results: Dict[str, List[Dict]] = {}
        targets = list(self._agents.keys()) if self.selected == "all" else [self.selected]

        for device_id in targets:
            tracer = None
            if debug_config and debug_config.get("enabled"):
                tracer = DebugTracer(device_id, debug_config)
            # One line per dispatch: with selector "all" the per-device results
            # interleave, and this says which device got what, in order.
            log_event(self.logger, "agent_dispatch", device=device_id, text=_brief(text))
            results[device_id] = self._agents[device_id].execute_text_command(text, tracer, recorder=recorder)
            self._last_tracers[device_id] = tracer

        return results

    def execute_actions(self, actions: List[Dict]) -> Dict[str, List[Dict]]:
        results: Dict[str, List[Dict]] = {}
        targets = list(self._agents.keys()) if self.selected == "all" else [self.selected]
        for device_id in targets:
            log_event(self.logger, "agent_dispatch", device=device_id, actions=len(actions))
            results[device_id] = self._agents[device_id].execute_actions(actions)
        return results
