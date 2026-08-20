"""Run-summary notifiers: payload shape, failure containment, factory filtering.

No socket is ever opened here — `requests.post` is patched on the real module
object (core.notifier imports it lazily inside the call, so the patch lands).
"""

from __future__ import annotations

import logging
import sys
from typing import Dict, List

import pytest
import requests

from core.notifier import FeishuNotifier, Notifier, WebhookNotifier, build_notifiers

LOGGER = logging.getLogger("test")


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None):
        self.status_code = status_code
        self._body = {} if body is None else body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class PostSpy:
    """Records every POST and answers with a scripted response."""

    def __init__(self, response=None, exc: Exception = None):
        self.calls: List[Dict] = []
        self.response = response if response is not None else FakeResponse()
        self.exc = exc

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


@pytest.fixture
def spy(monkeypatch):
    spy = PostSpy()
    monkeypatch.setattr(requests, "post", spy)
    return spy


def summary(**over) -> Dict:
    base = {
        "task": "daily_check",
        "device": "emulator-5554",
        "status": "failed",
        "error": "Action failed at node 'shop'",
        "counts": {"error": 2, "warning": 1},
        "findings": [
            {"type": "popup_branch", "severity": "warning", "message": "网络异常弹窗"},
            {"type": "task_failure", "severity": "error", "message": "boom"},
        ],
        "report_path": "outputs/findings/20260818/dev/103000_ab12cd/report.json",
    }
    base.update(over)
    return base


# ---------- payload shape ----------

def test_feishu_posts_custom_robot_text_envelope(spy):
    assert FeishuNotifier(LOGGER, "https://open.feishu.cn/hook/x").notify_run(summary()) is True

    call = spy.calls[0]
    assert call["url"] == "https://open.feishu.cn/hook/x"
    assert call["timeout"] == 5.0
    payload = call["json"]
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert set(payload["content"]) == {"text"}
    for expected in ("daily_check", "emulator-5554", "failed", "error=2", "warning=1",
                     "网络异常弹窗", "popup_branch", "report.json"):
        assert expected in text


def test_feishu_text_truncates_long_messages_and_lists_export(spy):
    long_message = "x" * 500
    FeishuNotifier(LOGGER, "u").notify_run(
        summary(findings=[{"type": "t", "severity": "error", "message": long_message}],
                export_path="outputs/exports/run.zip")
    )
    text = spy.calls[0]["json"]["content"]["text"]
    assert "…" in text
    assert "x" * 500 not in text
    assert "outputs/exports/run.zip" in text


def test_feishu_preview_covers_only_the_findings_it_was_given(spy):
    FeishuNotifier(LOGGER, "u").notify_run(summary())
    text = spy.calls[0]["json"]["content"]["text"]
    assert "1. [warning] popup_branch" in text
    assert "2. [error] task_failure" in text
    assert "3. " not in text


def test_webhook_posts_the_summary_verbatim(spy):
    payload = summary()
    assert WebhookNotifier(LOGGER, "http://qa.local/hook").notify_run(payload) is True
    assert spy.calls[0]["json"] == payload
    assert spy.calls[0]["json"] is not payload  # a copy, so the caller's dict is safe


# ---------- failures never escape ----------

def test_network_error_returns_false_without_raising(monkeypatch):
    monkeypatch.setattr(requests, "post", PostSpy(exc=OSError("connection refused")))
    assert WebhookNotifier(LOGGER, "http://down").notify_run(summary()) is False
    assert FeishuNotifier(LOGGER, "http://down").notify_run(summary()) is False


def test_http_error_status_is_a_failure(monkeypatch):
    monkeypatch.setattr(requests, "post", PostSpy(response=FakeResponse(status_code=500)))
    assert WebhookNotifier(LOGGER, "http://x").notify_run(summary()) is False


def test_feishu_non_zero_body_code_is_a_failure(monkeypatch):
    # A wrong robot token still answers HTTP 200; the verdict is the body code.
    monkeypatch.setattr(
        requests, "post",
        PostSpy(response=FakeResponse(body={"code": 19021, "msg": "sign match fail"})),
    )
    assert FeishuNotifier(LOGGER, "http://x").notify_run(summary()) is False


def test_feishu_unreadable_body_still_counts_as_sent(monkeypatch):
    monkeypatch.setattr(requests, "post", PostSpy(response=FakeResponse(body=None)))
    assert FeishuNotifier(LOGGER, "http://x").notify_run(summary()) is True


def test_base_notifier_has_no_transport_of_its_own():
    with pytest.raises(NotImplementedError):
        Notifier(LOGGER, "http://x").notify_run(summary())


# ---------- factory ----------

def test_build_notifiers_builds_both_types_with_their_filters():
    built = build_notifiers(
        [
            {"type": "feishu", "url": "https://open.feishu.cn/hook/x", "on_status": ["failed"]},
            {"type": "webhook", "url": "http://qa.local/hook", "min_findings": 3, "timeout_s": 9},
        ],
        LOGGER,
    )
    assert [type(n) for n in built] == [FeishuNotifier, WebhookNotifier]
    assert built[0].min_findings == 1 and built[0].on_status == ["failed"]
    assert built[1].min_findings == 3 and built[1].on_status == []
    assert built[1].timeout_s == 9.0


def test_build_notifiers_skips_unknown_type_and_missing_url():
    built = build_notifiers(
        [
            {"type": "telegram", "url": "http://x"},
            {"type": "webhook"},
            "not-a-mapping",
            {"type": "webhook", "url": "http://good"},
        ],
        LOGGER,
    )
    assert [n.url for n in built] == ["http://good"]


def test_build_notifiers_empty_config_is_a_noop():
    assert build_notifiers([], LOGGER) == []
    assert build_notifiers(None, LOGGER) == []


def test_build_notifiers_returns_nothing_without_requests(monkeypatch):
    # `None` in sys.modules makes `import requests` raise ImportError, the same
    # as an environment that never installed it.
    monkeypatch.setitem(sys.modules, "requests", None)
    assert build_notifiers([{"type": "feishu", "url": "http://x"}], LOGGER) == []


def test_build_notifiers_tolerates_a_bad_min_findings():
    built = build_notifiers([{"type": "webhook", "url": "http://x", "min_findings": "many"}], LOGGER)
    assert built[0].min_findings == 1


def test_on_status_accepts_a_bare_string():
    built = build_notifiers([{"type": "webhook", "url": "http://x", "on_status": "failed"}], LOGGER)
    assert built[0].on_status == ["failed"]


# ---------- filter predicate ----------

@pytest.mark.parametrize(
    "min_findings,on_status,status,count,expected",
    [
        (1, [], "completed", 0, False),   # clean run: default filter pushes nothing
        (1, [], "completed", 1, True),
        (3, [], "failed", 2, False),
        (3, [], "failed", 3, True),
        (1, ["failed"], "completed", 5, False),
        (1, ["failed"], "failed", 5, True),
        (0, [], "completed", 0, True),    # opt-in "always tell me"
    ],
)
def test_should_notify(min_findings, on_status, status, count, expected):
    notifier = WebhookNotifier(LOGGER, "http://x")
    notifier.min_findings = min_findings
    notifier.on_status = on_status
    assert notifier.should_notify(status, count) is expected
