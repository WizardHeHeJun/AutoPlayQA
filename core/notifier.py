"""Run-summary push: tell someone a finished run produced findings.

An unattended run (run_suite / cron) writes its findings to disk and stops —
nobody learns about them until a human goes looking. A notifier closes that
loop: `FindingsRecorder.finalize` hands the run's summary to every configured
notifier, which POSTs it to a chat robot or an in-house webhook.

Deliberately *one message per run*, not one per finding: a flaky screen can
produce a dozen findings in a minute and a per-finding push would be pure spam.
The summary carries the counts plus the first few messages; the report/export
paths in it are how the reader gets the rest.

Lives in core/ because it must not know anything about tasks or perception (the
layer rules forbid importing upwards) — it takes a plain dict and ships it.
The transport is an ordinary HTTP POST via `requests`, imported lazily so a
checkout without the package still boots (the factory logs once and yields no
notifiers). Nothing here may raise: a failed push is logged and reported as
False, never allowed to break the run it is reporting on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

#: Chat robots are best-effort; a slow one must not hold up a run's teardown.
DEFAULT_TIMEOUT_S = 5.0
#: Findings previewed inline in the text message (the rest live in the report).
PREVIEW_FINDINGS = 3
#: Per-message truncation — a stack-trace-sized finding must not fill the chat.
MAX_MESSAGE_CHARS = 160


def _post_json(url: str, payload: Dict, timeout_s: float, logger) -> Optional[Any]:
    """POST `payload` as JSON; return the response object, or None on failure.

    `requests` is imported here rather than at module import time so that the
    package stays optional (see the module docstring), and so tests can patch
    `requests.post` on the real module object.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - covered via build_notifiers
        logger.warning("Notifier skipped, `requests` is not installed: %s", exc)
        return None
    try:
        return requests.post(url, json=payload, timeout=timeout_s)
    except Exception as exc:
        logger.warning("Notifier POST to %s failed: %s", url, exc)
        return None


def _truncate(text: Optional[str], limit: int = MAX_MESSAGE_CHARS) -> str:
    value = "" if text is None else str(text).strip().replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


class Notifier:
    """One destination for run summaries.

    Subclasses only decide the payload shape; delivery, error handling and the
    filter config (`min_findings` / `on_status`, set by `build_notifiers` and
    applied by the caller) are shared.
    """

    name = "notifier"

    def __init__(self, logger, url: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.logger = logger
        self.url = url
        self.timeout_s = float(timeout_s)
        # Filter config travels with the notifier; the decision to apply it
        # belongs to whoever calls notify_run (see FindingsRecorder.finalize).
        self.min_findings: int = 1
        self.on_status: List[str] = []

    def notify_run(self, summary: Dict) -> bool:
        """Push one run summary. True when the destination accepted it."""
        raise NotImplementedError

    def should_notify(self, status: Optional[str], finding_count: int) -> bool:
        """Does this run clear this notifier's filter?"""
        if finding_count < self.min_findings:
            return False
        if self.on_status and status not in self.on_status:
            return False
        return True

    # ---------- delivery ----------

    def _post(self, payload: Dict) -> bool:
        resp = _post_json(self.url, payload, self.timeout_s, self.logger)
        if resp is None:
            return False
        if not self._ok(resp):
            return False
        self.logger.info("Run summary pushed via %s notifier", self.name)
        return True

    def _ok(self, resp) -> bool:
        """Did the destination accept the message? (HTTP status by default.)"""
        code = getattr(resp, "status_code", 200)
        try:
            accepted = int(code) < 400
        except (TypeError, ValueError):
            return True  # a stub response without a usable status: assume sent
        if not accepted:
            self.logger.warning("Notifier %s rejected the message: HTTP %s", self.url, code)
        return accepted


class WebhookNotifier(Notifier):
    """Generic integration point: POST the summary dict verbatim.

    No shaping at all, so an in-house dashboard/bot receives exactly the fields
    `finalize` produced and can evolve with them.
    """

    name = "webhook"

    def notify_run(self, summary: Dict) -> bool:
        return self._post(dict(summary))


class FeishuNotifier(Notifier):
    """Feishu (Lark) custom-robot webhook: a plain text card.

    The robot API only takes its own envelope, so the summary is rendered into
    a short human-readable block — counts first, then the first few findings,
    then where the evidence is.
    """

    name = "feishu"

    def notify_run(self, summary: Dict) -> bool:
        return self._post({"msg_type": "text", "content": {"text": self.render_text(summary)}})

    @staticmethod
    def render_text(summary: Dict) -> str:
        counts = summary.get("counts") or {}
        total = sum(counts.values()) if counts else 0
        lines = [
            "[AutoPlayQA] 运行结果",
            f"任务: {summary.get('task') or '-'}",
            f"设备: {summary.get('device') or '-'}",
            f"状态: {summary.get('status') or '-'}",
        ]
        detail = ", ".join(f"{sev}={counts[sev]}" for sev in sorted(counts))
        lines.append(f"发现: {total} 条" + (f" ({detail})" if detail else ""))
        if summary.get("error"):
            lines.append(f"错误: {_truncate(summary.get('error'))}")
        for i, finding in enumerate(summary.get("findings") or [], start=1):
            lines.append(
                f"{i}. [{finding.get('severity', '?')}] {finding.get('type', '?')}: "
                f"{_truncate(finding.get('message'))}"
            )
        if summary.get("report_path"):
            lines.append(f"报告: {summary['report_path']}")
        if summary.get("export_path"):
            lines.append(f"证据包: {summary['export_path']}")
        return "\n".join(lines)

    def _ok(self, resp) -> bool:
        # A wrong token still answers HTTP 200 — the real verdict is the body's
        # `code` field (0 = delivered). An unreadable body is not treated as a
        # failure: the message may well have gone through.
        if not super()._ok(resp):
            return False
        try:
            body = resp.json()
        except Exception:
            return True
        if isinstance(body, dict) and body.get("code"):
            self.logger.warning("Feishu webhook rejected the message: %s", body)
            return False
        return True


_TYPES = {"feishu": FeishuNotifier, "webhook": WebhookNotifier}


def build_notifiers(config_list: Optional[Sequence[Dict]], logger) -> List[Notifier]:
    """Build the notifiers declared under `findings.notifiers`.

    Each entry is `{type, url, min_findings?, on_status?, timeout_s?}`. Bad
    entries (unknown type, missing url) are logged and skipped rather than
    raising — a typo in an optional reporting channel must not stop the tool
    from running tests. Returns [] when nothing is configured or `requests` is
    missing, which makes the whole feature a no-op downstream.
    """
    if not config_list:
        return []
    try:
        import requests  # noqa: F401  (availability probe only)
    except ImportError:
        logger.warning(
            "findings.notifiers configured but `requests` is not installed; "
            "run-summary push disabled"
        )
        return []

    notifiers: List[Notifier] = []
    for index, item in enumerate(config_list):
        if not isinstance(item, dict):
            logger.warning("Ignoring findings.notifiers[%d]: expected a mapping", index)
            continue
        kind = str(item.get("type", "")).strip().lower()
        cls = _TYPES.get(kind)
        if cls is None:
            logger.warning(
                "Ignoring findings.notifiers[%d]: unknown type '%s' (known: %s)",
                index, item.get("type"), ", ".join(sorted(_TYPES)),
            )
            continue
        url = item.get("url")
        if not url:
            logger.warning("Ignoring findings.notifiers[%d] (%s): no url", index, kind)
            continue
        notifier = cls(logger, str(url), timeout_s=item.get("timeout_s", DEFAULT_TIMEOUT_S))
        try:
            notifier.min_findings = int(item.get("min_findings", 1))
        except (TypeError, ValueError):
            logger.warning(
                "findings.notifiers[%d]: bad min_findings %r, using 1", index, item.get("min_findings")
            )
        on_status = item.get("on_status") or []
        if isinstance(on_status, str):
            on_status = [on_status]
        notifier.on_status = [str(s) for s in on_status]
        notifiers.append(notifier)
    return notifiers
