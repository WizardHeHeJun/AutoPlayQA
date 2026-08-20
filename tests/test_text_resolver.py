from __future__ import annotations

import logging

import pytest

from core.text_resolver import LocalTextResolver, parse_actions_fallback


class FakeDetector:
    def __init__(self, actions=None, raise_exc=False):
        self.actions = actions or []
        self.raise_exc = raise_exc
        self.requests = []

    def infer_actions_from_screen(self, device_id, user_text, tracer=None):
        self.requests.append((device_id, user_text))
        if self.raise_exc:
            raise RuntimeError("detector boom")
        return self.actions


def make_resolver(detector=None):
    return LocalTextResolver(detector, logging.getLogger("test"))


def test_explicit_click_regex():
    actions = parse_actions_fallback("click 100 200")
    assert actions == [{"type": "click", "params": {"x": 100, "y": 200}}]


def test_explicit_click_chinese():
    actions = parse_actions_fallback("点击 300 400")
    assert actions == [{"type": "click", "params": {"x": 300, "y": 400}}]


def test_explicit_drag_with_duration():
    actions = parse_actions_fallback("drag 1 2 3 4 800")
    assert actions[0]["params"]["duration_ms"] == 800


def test_input_chinese():
    actions = parse_actions_fallback("输入 88809123")
    assert actions == [{"type": "input_text", "params": {"text": "88809123"}}]


def test_resolver_prefers_regex_over_detector():
    detector = FakeDetector(actions=[{"type": "click", "params": {"x": 9, "y": 9}}])
    resolver = make_resolver(detector)

    actions = resolver.generate_actions("click 1 2", device_id="dev1")

    assert actions == [{"type": "click", "params": {"x": 1, "y": 2}}]
    assert detector.requests == []


def test_resolver_delegates_to_detector():
    expected = [{"type": "click", "params": {"x": 5, "y": 6}}]
    detector = FakeDetector(actions=expected)
    resolver = make_resolver(detector)

    actions = resolver.generate_actions("点击设置按钮", device_id="dev1")

    assert actions == expected
    assert detector.requests == [("dev1", "点击设置按钮")]


def test_resolver_raises_when_unresolvable():
    resolver = make_resolver(FakeDetector(actions=[]))
    with pytest.raises(RuntimeError, match="MCP"):
        resolver.generate_actions("打开设置然后调亮度", device_id="dev1")


def test_resolver_raises_when_detector_errors():
    resolver = make_resolver(FakeDetector(raise_exc=True))
    with pytest.raises(RuntimeError, match="MCP"):
        resolver.generate_actions("点击设置按钮", device_id="dev1")


def test_resolver_without_device_id_skips_detector():
    detector = FakeDetector(actions=[{"type": "click", "params": {"x": 1, "y": 1}}])
    resolver = make_resolver(detector)
    with pytest.raises(RuntimeError):
        resolver.generate_actions("点击设置按钮")
    assert detector.requests == []
