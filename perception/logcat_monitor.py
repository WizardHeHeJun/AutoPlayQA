"""Poll-based crash/ANR detection over adb logcat, plus evidence log fragments.

No background threads: the task engine calls poll() between steps and once at
finish, so detection stays deterministic and tests can mock subprocess. start()
records the device clock as a -T marker so log noise from before the run is
excluded; if the clock is unavailable the monitor falls back to a tail window
plus run-scoped dedup (pre-run crashes may then surface once).

Two separate jobs, deliberately kept apart:

* poll()  — crash/ANR detection. Keyword rules over the run-scoped dump.
* tail()  — QA evidence. The recent log fragment attached to a finding; it must
  keep the game's own W/E business errors (server codes, Lua stack traces),
  which are the first thing a tester needs and are *not* crashes.

tail() therefore fetches two channels (all levels for context + a `*:W`
priority channel) and ranks the buffer before trimming it to the caller's line
budget (see select_evidence_lines: tag tiers + per-tag / per-repeat quotas, not
level alone — on this ROM the loudest spam is itself E-level).
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from core.logger import log_event

DEFAULT_RULES = (
    {"pattern": "FATAL EXCEPTION", "type": "crash", "severity": "critical", "context": 20},
    {"pattern": "Fatal signal", "type": "native_crash", "severity": "critical", "context": 12},
    {"pattern": "ANR in", "type": "anr", "severity": "error", "context": 10},
)

FALLBACK_TAIL_LINES = 2000
MAX_EVENTS_PER_POLL = 5
#: Characters of the offending log line kept in the detection's own log field
#: (the full line and its context ship with the finding).
LOG_LINE_PREVIEW = 120
# Context channel: every level. Chatty ROMs (kernel/SurfaceFlinger/thermal
# spam) burn hundreds of lines per second, so this has to be deep enough that a
# 60s window is not already truncated at fetch time.
TAIL_FETCH_LINES = 4000
# Priority channel: W and above only, so the same budget spans far more time.
PRIORITY_FETCH_LINES = 1500
PRIORITY_MIN_LEVEL = "W"
# Levels that count as QA signal and survive line-budget trimming.
IMPORTANT_LEVELS = frozenset({"W", "E", "F"})

# ---------------------------------------------------------------------------
# Evidence ranking policy
#
# Measured on 44 real evidence logs (2026-08-11, one vendor ROM, 8944 W/E/F
# lines): level alone is not a usable signal, because 3663 of them are E and
# 96% of those E lines come from ROM/engine spam — E/Unity avatar-skeleton
# bursts (42%), QC2*/FMQ codec chatter (28%), ANDR-PERF/SurfaceFlinger (21%).
# The game's own `E/[mygame]` business errors never appeared in the same file as
# a Unity burst: the burst had already evicted them. So evidence ranking is by
# *tag class* first, with quotas that stop any single tag (or any single
# repeated message) from eating the budget.
# ---------------------------------------------------------------------------

# The game's own logs — always top priority. A tag may end with `*` to match by
# prefix. "[mygame]" is the log tag of the game under test; override
# it per deployment via config (findings.logcat_evidence.business_tags).
DEFAULT_BUSINESS_TAGS: Tuple[str, ...] = ("[mygame]",)

# Known ROM / vendor / framework chatter. Demoted below unknown tags even at
# E level. This list is a shortcut, never a gate: an unknown tag still ranks by
# its level (see _evidence_tier), so a stale list degrades to the old behaviour
# instead of hiding a new error. Extend via config rather than editing here.
DEFAULT_NOISE_TAGS: Tuple[str, ...] = (
    # graphics / media / codec
    "SurfaceFlinger", "QC2*", "Codec2*", "FMQ", "MPEG4Writer", "GraphicBufferSource",
    "ColorUtils", "ResourceManagerService", "mediaserver", "AudioFlinger",
    # perf / thermal / kernel
    "ANDR-PERF-*", "vendor.qti.hardware.perf*", "KERNEL", "ThermalEngine", "FanService",
    "libc",
    # vendor telemetry / network
    "AsusRouteMonitor", "CNAsusAnalytics", "top2Dropbox", "TrafficStation",
    "TcpSocketMonitor", "NetworkScheduler", "WifiHAL", "QCNEJ*", "WorkSourceUtil",
    "DiscreteRegistry",
    # framework / storage chatter
    "ProcessState", "IPCThreadState", "FastPrintWriter", "MediaProvider",
    "PickerDbFacade", "SQLiteCastStore", "ContextImpl", "binder:*",
    # crash reporter's own aggregation echo (one line per merged engine error)
    "[CrashSightReport]",
)

# Quotas are expressed for a 300-line evidence budget and scale with max_lines,
# so the 40-line inline excerpt gets a proportionally tighter (more diverse)
# selection instead of the same absolute caps. <= 0 means "no quota".
QUOTA_REFERENCE_LINES = 300
DEFAULT_NOISE_TAG_QUOTA = 15   # lines per known-noise tag
DEFAULT_TAG_QUOTA = 60         # lines per unknown W/E/F tag (e.g. Unity)
DEFAULT_REPEAT_QUOTA = 3       # occurrences of one identical message per tag

# Crash/ANR markers must never lose their line to a quota, whatever the tag.
EVIDENCE_CRITICAL_PATTERNS: Tuple[str, ...] = tuple(r["pattern"] for r in DEFAULT_RULES)

_MARKER_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")
_LINE_TS_RE = re.compile(r"^(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})")
_LINE_LEVEL_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ([VDIWEFS])/")
# "MM-DD hh:mm:ss.mmm L/TAG( PID): message" — the tag may itself contain "/"
# (QCNEJ/WlanStaInfoRelay) or brackets ([mygame]), so it runs up to the pid.
_LINE_HEADER_RE = re.compile(
    r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ([VDIWEFS])/(.*?)\(\s*\d+\):\s?(.*)$"
)

Annotated = Tuple[str, Optional[float], Optional[str], Optional[str]]


class TagSet:
    """Logcat tag matcher: exact names plus `prefix*` wildcards."""

    __slots__ = ("exact", "prefixes")

    def __init__(self, names: Iterable[str]):
        self.exact: Set[str] = set()
        self.prefixes: List[str] = []
        for raw in names or ():
            name = str(raw).strip()
            if not name:
                continue
            if name.endswith("*"):
                self.prefixes.append(name[:-1])
            else:
                self.exact.add(name)

    def __contains__(self, tag: object) -> bool:
        if not isinstance(tag, str) or not tag:
            return False
        if tag in self.exact:
            return True
        return any(tag.startswith(prefix) for prefix in self.prefixes)


class EvidencePolicy:
    """How `select_evidence_lines` ranks a noisy buffer down to a line budget.

    `noise_tags` replaces the built-in list; `extra_noise_tags` appends to it —
    a per-device ROM only needs to name its own spammers instead of restating a
    list that will go stale.
    """

    __slots__ = ("business_tags", "noise_tags", "noise_tag_quota", "tag_quota", "repeat_quota")

    def __init__(self, business_tags: Optional[Iterable[str]] = None,
                 noise_tags: Optional[Iterable[str]] = None,
                 extra_noise_tags: Iterable[str] = (),
                 noise_tag_quota: int = DEFAULT_NOISE_TAG_QUOTA,
                 tag_quota: int = DEFAULT_TAG_QUOTA,
                 repeat_quota: int = DEFAULT_REPEAT_QUOTA):
        base = DEFAULT_NOISE_TAGS if noise_tags is None else tuple(noise_tags)
        self.business_tags = TagSet(
            DEFAULT_BUSINESS_TAGS if business_tags is None else business_tags
        )
        self.noise_tags = TagSet(tuple(base) + tuple(extra_noise_tags or ()))
        self.noise_tag_quota = int(noise_tag_quota)
        self.tag_quota = int(tag_quota)
        self.repeat_quota = int(repeat_quota)

    @classmethod
    def from_config(cls, config: Optional[Dict]) -> "EvidencePolicy":
        config = config or {}
        return cls(
            business_tags=config.get("business_tags"),
            noise_tags=config.get("noise_tags"),
            extra_noise_tags=config.get("extra_noise_tags", ()),
            noise_tag_quota=config.get("noise_tag_quota", DEFAULT_NOISE_TAG_QUOTA),
            tag_quota=config.get("tag_quota", DEFAULT_TAG_QUOTA),
            repeat_quota=config.get("repeat_quota", DEFAULT_REPEAT_QUOTA),
        )


DEFAULT_EVIDENCE_POLICY = EvidencePolicy()


def annotate_lines(lines: Sequence[str]) -> List[Annotated]:
    """(line, timestamp_seconds, level, tag) per line, "-v time" format.

    Continuation lines (wrapped stack traces without their own header) inherit
    the previous line's timestamp, level and tag, so they stay glued to the
    message they belong to during windowing and trimming.
    """
    out: List[Annotated] = []
    stamp: Optional[float] = None
    level: Optional[str] = None
    tag: Optional[str] = None
    for line in lines:
        line_stamp = _line_seconds(line)
        if line_stamp is not None:
            stamp = line_stamp
        header = _LINE_HEADER_RE.match(line)
        if header:
            level, tag = header.group(1), header.group(2).strip()
        else:
            match = _LINE_LEVEL_RE.match(line)
            if match:
                level, tag = match.group(1), None
        out.append((line, stamp, level, tag))
    return out


def _message_body(line: str) -> str:
    """The message after the "-v time" header; the whole line if there is none."""
    header = _LINE_HEADER_RE.match(line)
    return header.group(3).strip() if header else line.strip()


def _evidence_tier(line: str, level: Optional[str], tag: Optional[str],
                   policy: EvidencePolicy) -> int:
    """0 = crash/business, 1 = W/E/F from an unknown tag, 2 = W/E/F noise, 3 = context."""
    if any(pattern in line for pattern in EVIDENCE_CRITICAL_PATTERNS):
        return 0
    if tag in policy.business_tags:
        return 0
    if level in IMPORTANT_LEVELS:
        return 2 if tag in policy.noise_tags else 1
    return 3


def _scaled_quota(quota: int, max_lines: int) -> float:
    if quota <= 0:
        return float("inf")
    return max(1.0, round(quota * max_lines / QUOTA_REFERENCE_LINES))


def select_evidence_lines(lines: Sequence[str], max_lines: int,
                          policy: Optional[EvidencePolicy] = None) -> List[str]:
    """Trim to `max_lines` without sacrificing QA signal.

    A plain `lines[-max_lines:]` loses the game's error outright: on a noisy ROM
    the newest 300 lines can span barely a second. Keeping W/E/F over V/D/I (the
    first fix) is not enough either — the loudest spam here *is* E level, so an
    E-level burst still evicts the business error. Selection therefore runs in
    priority tiers, newest-first inside each tier:

    0. crash/ANR markers and the game's own tags — never quota'd;
    1. W/E/F from tags we have no opinion about (level-based fallback, so an
       unlisted new tag is treated as signal, not hidden);
    2. W/E/F from known ROM/vendor noise tags;
    3. everything else, as surrounding context.

    Inside tiers 1 and 2 two quotas apply: at most `tag_quota` /
    `noise_tag_quota` lines per tag, and at most `repeat_quota` copies of one
    identical message per tag — that is what defuses the E/Unity avatar burst
    (265 lines, 21 distinct messages) without blacklisting `Unity`, which also
    carries real game errors. Quotas only re-order priority: if budget is left
    over, the newest unselected lines top it back up, so the caller always gets
    `max_lines` lines. Chronological order is preserved.
    """
    lines = list(lines)
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines
    policy = policy or DEFAULT_EVIDENCE_POLICY
    annotated = annotate_lines(lines)
    tiers: List[List[int]] = [[], [], [], []]
    tags: List[str] = []
    for i, (line, _stamp, level, tag) in enumerate(annotated):
        tags.append(tag or "")
        tiers[_evidence_tier(line, level, tag, policy)].append(i)

    noise_quota = _scaled_quota(policy.noise_tag_quota, max_lines)
    tag_quota = _scaled_quota(policy.tag_quota, max_lines)
    repeat_quota = _scaled_quota(policy.repeat_quota, max_lines)

    keep: Set[int] = set()
    per_tag: Counter = Counter()
    per_repeat: Counter = Counter()
    for tier, indices in enumerate(tiers):
        if len(keep) >= max_lines:
            break
        for i in reversed(indices):
            if len(keep) >= max_lines:
                break
            if tier in (1, 2):
                tag = tags[i]
                if per_tag[tag] >= (noise_quota if tier == 2 else tag_quota):
                    continue
                signature = (tag, _message_body(lines[i]))
                if per_repeat[signature] >= repeat_quota:
                    continue
                per_tag[tag] += 1
                per_repeat[signature] += 1
            keep.add(i)
    for i in range(len(lines) - 1, -1, -1):  # top-up: quotas rank, they don't shrink
        if len(keep) >= max_lines:
            break
        keep.add(i)
    return [line for i, line in enumerate(lines) if i in keep]


def _line_seconds(line: str) -> Optional[float]:
    """Seconds-within-year of a "-v time" line timestamp (year-agnostic ordering)."""
    m = _LINE_TS_RE.match(line)
    if not m:
        return None
    month, day, hour, minute, sec, ms = (int(g) for g in m.groups())
    return (((month * 31 + day) * 24 + hour) * 3600) + minute * 60 + sec + ms / 1000


class LogcatMonitor:
    """Scans logcat for crash markers between start() and successive poll() calls.

    poll() returns new events only (dedup by matched line, which carries the
    log timestamp): [{"type", "severity", "line", "excerpt"}]. The first adb
    failure disables the monitor for the rest of the run — if adb is down the
    run is doomed anyway, and we avoid per-step retry latency.
    """

    def __init__(self, logger, rules=None, evidence_policy: Optional[EvidencePolicy] = None):
        self.logger = logger
        self.rules = [dict(r) for r in (rules if rules is not None else DEFAULT_RULES)]
        # Ranking policy for tail() only; poll()'s crash rules are untouched.
        self.evidence_policy = evidence_policy or DEFAULT_EVIDENCE_POLICY
        self._marker: Optional[str] = None
        self._seen: Set[str] = set()
        self._disabled = False

    def start(self, device_id: str) -> None:
        self._seen = set()
        self._disabled = False
        self._marker = self._device_time(device_id)
        if self._marker is None:
            self.logger.warning(
                "logcat monitor: device clock unavailable; using tail window "
                "(crashes from before the run may surface once)"
            )

    def poll(self, device_id: str) -> List[Dict]:
        if self._disabled:
            return []
        out = self._dump(device_id)
        if out is None:
            self._disabled = True
            self.logger.warning("logcat monitor: dump failed, disabled for the rest of this run")
            return []

        lines = out.splitlines()
        events: List[Dict] = []
        for i, line in enumerate(lines):
            for rule in self.rules:
                if rule["pattern"] not in line:
                    continue
                key = line.strip()
                if key not in self._seen:
                    self._seen.add(key)
                    context = int(rule.get("context", 10))
                    # The detection itself, logged where it happens. The engine
                    # turns this into a finding (and only warns when it has no
                    # recorder), so this stays DEBUG — no double WARNING.
                    log_event(
                        self.logger, "logcat_hit", type=rule["type"],
                        severity=rule["severity"], pattern=rule["pattern"],
                        line=key[:LOG_LINE_PREVIEW],
                    )
                    events.append(
                        {
                            "type": rule["type"],
                            "severity": rule["severity"],
                            "line": key,
                            "excerpt": [l.rstrip() for l in lines[i : i + context]],
                        }
                    )
                break
            if len(events) >= MAX_EVENTS_PER_POLL:
                break
        return events

    def tail(self, device_id: str, seconds: int = 60, max_lines: int = 300) -> Optional[List[str]]:
        """Recent log fragment for finding evidence: the last `seconds` of logcat.

        Two channels are fetched and merged chronologically:

        * context — every level, `TAIL_FETCH_LINES` deep, for the surrounding flow;
        * priority — `*:W` and above, so warnings/errors reach much further back
          than the ROM's per-second noise volume allows the context channel to.

        Trimming to `max_lines` then goes through `select_evidence_lines`, which
        ranks by tag class and caps per-tag / per-repeat volume, so the game's
        own error (server code, Lua traceback) survives a burst even when the
        burst is itself E level. The window is computed from the newest timestamp in the buffer ("-v time"
        format), so it works without the device clock marker; if no line carries
        a parseable timestamp the raw tail is returned instead. None on adb
        failure of the context channel or when the monitor is disabled; a failed
        priority channel only degrades to context-only.
        """
        if self._disabled:
            return None
        context = self._fetch_lines(device_id, max(TAIL_FETCH_LINES, max_lines))
        if context is None:
            return None
        if not context:
            return []
        priority = self._fetch_lines(
            device_id, max(PRIORITY_FETCH_LINES, max_lines), min_level=PRIORITY_MIN_LEVEL
        )
        if priority is None:
            self.logger.warning(
                "logcat monitor: priority (%s+) fetch failed; evidence log falls back to "
                "context lines only", PRIORITY_MIN_LEVEL
            )
            priority = []

        annotated = self._merge_by_time(context, priority)
        newest = max((item[1] for item in annotated if item[1] is not None), default=None)
        if newest is None:
            return select_evidence_lines(
                [item[0] for item in annotated], max_lines, self.evidence_policy
            )
        cutoff = newest - seconds
        windowed = [item[0] for item in annotated if item[1] is not None and item[1] >= cutoff]
        return select_evidence_lines(windowed, max_lines, self.evidence_policy)

    @staticmethod
    def _merge_by_time(context: Sequence[str], priority: Sequence[str]) -> List[Annotated]:
        """Merge the two channels into one chronological, duplicate-free stream.

        The priority channel is normally a subset of the context channel, so its
        lines are matched off against context occurrence-by-occurrence (a
        repeated identical line stays repeated); only the older W/E lines that
        the context channel no longer reaches get appended and sorted in.
        """
        merged = list(annotate_lines(context))
        remaining = Counter(item[0] for item in merged)
        for item in annotate_lines(priority):
            if remaining.get(item[0], 0) > 0:
                remaining[item[0]] -= 1
            else:
                merged.append(item)
        # Stable sort: lines before the first timestamp keep their leading spot,
        # and same-timestamp lines keep the order their channel emitted them in.
        merged.sort(key=lambda item: item[1] if item[1] is not None else float("-inf"))
        return merged

    def _fetch_lines(
        self, device_id: str, count: int, min_level: Optional[str] = None
    ) -> Optional[List[str]]:
        """Last `count` logcat lines, optionally filtered to `min_level` and above.

        The filterspec is single-quoted so the device shell does not glob `*`.
        """
        command = f"logcat -d -v time -t {count}"
        if min_level:
            command += f" '*:{min_level}'"
        out = self._adb_shell(device_id, command, timeout=15)
        if out is None:
            return None
        return [l.rstrip() for l in out.splitlines() if l.strip()]

    @staticmethod
    def _line_seconds(line: str) -> Optional[float]:
        """Seconds-within-year of a "-v time" line timestamp (year-agnostic ordering)."""
        return _line_seconds(line)

    def _device_time(self, device_id: str) -> Optional[str]:
        # Single shell string so the device-side quoting survives adb's arg join.
        out = self._adb_shell(device_id, "date '+%m-%d %H:%M:%S.000'", timeout=5)
        if not out:
            return None
        value = out.strip().splitlines()[-1].strip()
        return value if _MARKER_RE.match(value) else None

    def _dump(self, device_id: str) -> Optional[str]:
        if self._marker:
            command = f"logcat -d -v time -T '{self._marker}'"
        else:
            command = f"logcat -d -v time -t {FALLBACK_TAIL_LINES}"
        return self._adb_shell(device_id, command, timeout=15)

    def _adb_shell(self, device_id: str, command: str, timeout: int) -> Optional[str]:
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", command],
                check=False, capture_output=True,
                encoding="utf-8", errors="ignore", timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("logcat monitor adb failed: %s", exc)
            return None
        if result.returncode != 0:
            self.logger.warning("logcat monitor adb error: %s", (result.stderr or "").strip())
            return None
        return result.stdout
