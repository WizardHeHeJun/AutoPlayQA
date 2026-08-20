from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from task.suite_runner import SuiteRunner, format_suite_report
from task.task_loader import SuiteValidationError, load_suite, validate_suite

LOGGER = logging.getLogger("test")


# ---------- fakes (no device, no engine internals) ----------

class FakeHub:
    """Landing check stand-in: `landed` decides whether the landing anchor hits."""

    def __init__(self, landed: bool = True):
        self.landed = landed
        self.calls: List[Dict] = []

    def recognize(self, device_id: str, spec: Dict, **kwargs):
        self.calls.append(spec)
        if callable(self.landed):
            return {"center": (1, 2), "score": 1.0} if self.landed() else None
        return {"center": (1, 2), "score": 1.0} if self.landed else None


class FakeEngine:
    """Scripted TaskEngine: each run() pops the next outcome for that case."""

    def __init__(self, outcomes: Dict[str, List[Dict]], hub: Optional[FakeHub] = None,
                 nodes_seen: Optional[List[str]] = None):
        self.outcomes = {k: list(v) for k, v in outcomes.items()}
        self.hub = hub or FakeHub()
        self.calls: List[Dict] = []
        self.nodes_seen = nodes_seen or ["用例开始"]

    def run(self, device_id, task, tracer=None, start_after=None, task_name=None, on_step=None):
        self.calls.append({"device_id": device_id, "task_name": task_name,
                           "start_after": start_after})
        if on_step:
            for node in self.nodes_seen:
                on_step(node)
        queue = self.outcomes.get(task_name)
        if not queue:
            return completed()
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def completed(findings=None):
    return {"ok": True, "status": "completed", "steps": [{"node": "n"}],
            "error": None, "handoff": None, "findings": findings or [],
            "report": {"report_path": "outputs/findings/x/report.json"}}


def failed(error="boom", findings=None):
    return {"ok": False, "status": "failed", "steps": [], "error": error,
            "handoff": None, "findings": findings or [], "report": {}}


def suspended():
    return {"ok": False, "status": "agent_required", "steps": [], "error": None,
            "handoff": {"node": "手动步骤", "instruction": "do it"},
            "findings": [], "report": {}}


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    """Two minimal case tasks that both define the suite's resume node."""
    task_dir = tmp_path / "task_definitions"
    task_dir.mkdir()
    for name in ("case_a", "case_b", "case_c"):
        task = {
            "entry": "主场景确认",
            "nodes": {
                "主场景确认": {
                    "recognition": {"type": "ocr", "expected": "主界面"},
                    "action": {"type": "none"},
                    "next": ["用例开始"],
                },
                "用例开始": {
                    "recognition": {"type": "ocr", "expected": "主界面"},
                    "action": {"type": "none"},
                    "next": [],
                    "on_timeout": "起点不对",
                },
                "起点不对": {
                    "recognition": {"type": "always"},
                    "action": {"type": "none"},
                    "finding": {"severity": "warning", "message": "不在主场景"},
                    "next": [],
                },
            },
        }
        (task_dir / f"{name}.json").write_text(
            json.dumps(task, ensure_ascii=False), encoding="utf-8"
        )
    return task_dir


def make_suite(cases, **overrides) -> Dict:
    """A minimal valid suite dict.

    `resume_after` / `case_entry` / `landing` are required fields (no framework
    default), filled in here with the values the old production defaults used
    to supply so tests that don't care about them are unaffected; pass an
    override or `del` a key on the returned dict to exercise one specific field.
    """
    suite = {
        "name": "test_suite",
        "cases": list(cases),
        "resume_after": "主场景确认",
        "case_entry": "用例开始",
        "landing": {
            "type": "ocr", "expected": "主界面", "roi": [0, 2280, 1080, 2448],
            "timeout_ms": 8000, "poll_interval_ms": 1500,
        },
    }
    suite.update(overrides)
    return suite


def make_runner(engine, case_dir) -> SuiteRunner:
    return SuiteRunner(engine, LOGGER, task_dir=case_dir)


# ---------- boot skipping ----------

def test_first_case_boots_then_rest_resume(case_dir):
    engine = FakeEngine({"case_a": [completed()], "case_b": [completed()],
                         "case_c": [completed()]})
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b", "case_c"])
    )

    assert result["ok"] is True
    # first run walks the whole boot chain, the rest resume past it
    assert [c["start_after"] for c in engine.calls] == [None, "主场景确认", "主场景确认"]
    assert [r["boot"] for r in result["cases"]] == ["full", "resume", "resume"]
    assert result["summary"]["full_boots"] == 1
    assert result["summary"]["boots_skipped"] == 2


def test_full_boot_cases_always_cold_start(case_dir):
    engine = FakeEngine({})
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b", "case_c"], full_boot_cases=["case_b"])
    )

    assert [c["start_after"] for c in engine.calls] == [None, None, "主场景确认"]
    assert [r["boot"] for r in result["cases"]] == ["full", "full", "resume"]


def test_custom_resume_node_is_used(case_dir):
    engine = FakeEngine({})
    make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b"], resume_after="主场景确认")
    )
    assert engine.calls[1]["start_after"] == "主场景确认"


# ---------- recovery ----------

def test_failed_case_restarts_and_retries_then_continues(case_dir):
    engine = FakeEngine({"case_a": [failed("stuck"), completed()],
                         "case_b": [completed()]})
    result = make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "case_b"]))

    # attempt 1 (full boot) failed -> retry cold-starts -> then case_b resumes
    assert [c["start_after"] for c in engine.calls] == [None, None, "主场景确认"]
    statuses = [(r["case"], r["attempt"], r["status"]) for r in result["cases"]]
    assert statuses == [("case_a", 1, "failed"), ("case_a", 2, "completed"),
                        ("case_b", 1, "completed")]
    assert result["summary"]["retries"] == 1
    # every case eventually passed, but the retry is surfaced as flaky and the
    # failed attempt keeps its own record (and its own findings report)
    assert result["ok"] is True
    assert result["summary"]["flaky"] == 1
    assert result["summary"]["cases_passed"] == 2
    assert result["summary"]["failed"] == 1


def test_retry_exhausted_skips_to_next_case_with_full_boot(case_dir):
    engine = FakeEngine({"case_a": [failed(), failed()], "case_b": [completed()]})
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b"], max_retries=1)
    )

    assert [c["task_name"] for c in engine.calls] == ["case_a", "case_a", "case_b"]
    # after giving up on case_a the device state is unknown -> next case cold-starts
    assert engine.calls[2]["start_after"] is None
    assert result["cases"][-1]["status"] == "completed"
    assert result["summary"]["failed"] == 2
    # case_a never passed -> the suite is not ok even though case_b was fine
    assert result["ok"] is False
    assert result["summary"]["cases_passed"] == 1 and result["summary"]["cases"] == 2


def test_restart_continue_never_retries(case_dir):
    engine = FakeEngine({"case_a": [failed()], "case_b": [completed()]})
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b"], on_case_failure="restart_continue")
    )

    assert [c["task_name"] for c in engine.calls] == ["case_a", "case_b"]
    assert engine.calls[1]["start_after"] is None
    assert result["summary"]["retries"] == 0


def test_abort_policy_stops_and_marks_the_rest_skipped(case_dir):
    engine = FakeEngine({"case_a": [completed()], "case_b": [failed("dead")]})
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b", "case_c"], on_case_failure="abort")
    )

    assert [c["task_name"] for c in engine.calls] == ["case_a", "case_b"]
    assert [r["status"] for r in result["cases"]] == ["completed", "failed", "skipped"]
    assert result["aborted_at"] == "case_b"
    assert result["summary"]["skipped"] == 1


def test_engine_exception_is_recorded_not_swallowed(case_dir):
    engine = FakeEngine({"case_a": [RuntimeError("adb died"), completed()],
                         "case_b": [completed()]})
    result = make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "case_b"]))

    crashed = result["cases"][0]
    assert crashed["status"] == "error"
    assert "adb died" in crashed["error"]
    # and it recovers like any other failure: cold start + retry
    assert engine.calls[1]["start_after"] is None
    assert result["cases"][1]["status"] == "completed"


def test_agent_required_is_not_retried_but_forces_a_reboot(case_dir):
    engine = FakeEngine({"case_a": [suspended()], "case_b": [completed()]})
    result = make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "case_b"]))

    assert [c["task_name"] for c in engine.calls] == ["case_a", "case_b"]
    assert result["cases"][0]["status"] == "agent_required"
    assert result["cases"][0]["handoff"]["node"] == "手动步骤"
    assert engine.calls[1]["start_after"] is None
    assert result["summary"]["agent_required"] == 1


# ---------- landing check ----------

def test_case_ending_off_the_landing_screen_counts_as_failure(case_dir):
    hub = FakeHub(landed=False)
    engine = FakeEngine({"case_a": [completed(), completed()], "case_b": [completed()]},
                        hub=hub)
    suite = make_suite(
        ["case_a"], landing={"type": "ocr", "expected": "主界面", "timeout_ms": 0,
                             "poll_interval_ms": 0}
    )
    result = make_runner(engine, case_dir).run("dev1", suite)

    record = result["cases"][0]
    assert record["status"] == "completed" and record["landed"] is False
    assert record["ok"] is False
    # a bad landing recovers exactly like a failure: cold start + retry
    assert [c["start_after"] for c in engine.calls] == [None, None]
    # the landing spec reaches the hub without the polling knobs
    assert hub.calls[0] == {"type": "ocr", "expected": "主界面"}


def test_landing_check_can_be_disabled(case_dir):
    hub = FakeHub(landed=False)
    engine = FakeEngine({}, hub=hub)
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b"], landing=None)
    )

    assert hub.calls == []
    assert result["ok"] is True
    assert engine.calls[1]["start_after"] == "主场景确认"


def test_landing_check_error_does_not_force_a_reboot(case_dir):
    class BoomHub(FakeHub):
        def recognize(self, device_id, spec, **kwargs):
            raise RuntimeError("no capturer")

    engine = FakeEngine({}, hub=BoomHub())
    result = make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "case_b"]))

    assert result["ok"] is True
    assert engine.calls[1]["start_after"] == "主场景确认"


# ---------- summary / reporting ----------

def test_summary_counts_findings_and_measures_saved_boot_time(case_dir):
    findings = [{"type": "watchdog", "severity": "error", "node": "n", "message": "m"},
                {"type": "anomaly_node", "severity": "warning", "node": "n2", "message": "m2"}]
    engine = FakeEngine(
        {"case_a": [completed(findings)], "case_b": [completed()], "case_c": [completed()]},
        nodes_seen=["主场景确认", "用例开始"],
    )
    result = make_runner(engine, case_dir).run(
        "dev1", make_suite(["case_a", "case_b", "case_c"])
    )

    summary = result["summary"]
    assert summary["total"] == 3 and summary["completed"] == 3
    assert summary["findings"] == 2
    assert summary["severity_counts"] == {"error": 1, "warning": 1}
    assert summary["boots_skipped"] == 2
    # boot cost was measured (case entry seen via on_step) and extrapolated
    assert summary["boot_s_avg"] is not None
    assert summary["estimated_saved_s"] == pytest.approx(2 * summary["boot_s_avg"], abs=0.1)
    assert result["cases"][0]["findings"][0]["severity"] == "error"
    assert "case_a" in format_suite_report(result)


def test_per_case_findings_export_is_called_once_per_case_with_findings(case_dir):
    class FakeRecorder:
        def __init__(self):
            self.exports: List[str] = []

        def export_run(self, target_dir):
            self.exports.append(target_dir)
            return f"{target_dir}/run.zip"

    recorder = FakeRecorder()
    findings = [{"type": "watchdog", "severity": "error", "node": "n", "message": "m"}]
    engine = FakeEngine({"case_a": [completed(findings)], "case_b": [completed()]})
    runner = SuiteRunner(engine, LOGGER, findings_recorder=recorder, task_dir=case_dir)

    result = runner.run("dev1", make_suite(["case_a", "case_b"]), export_to="out/zips")

    assert recorder.exports == ["out/zips"]
    assert result["cases"][0]["export_path"] == "out/zips/run.zip"
    assert result["cases"][1]["export_path"] is None


# ---------- suite definition validation ----------

def test_unknown_case_is_rejected(case_dir):
    with pytest.raises(SuiteValidationError, match="unknown case"):
        validate_suite(make_suite(["case_a", "nope"]), task_dir=case_dir)


def test_missing_resume_after_is_rejected(case_dir):
    suite = make_suite(["case_a"])
    del suite["resume_after"]
    with pytest.raises(SuiteValidationError, match="resume_after"):
        validate_suite(suite, task_dir=case_dir)


def test_missing_case_entry_is_rejected(case_dir):
    suite = make_suite(["case_a"])
    del suite["case_entry"]
    with pytest.raises(SuiteValidationError, match="case_entry"):
        validate_suite(suite, task_dir=case_dir)


def test_missing_landing_is_rejected(case_dir):
    suite = make_suite(["case_a"])
    del suite["landing"]
    with pytest.raises(SuiteValidationError, match="landing"):
        validate_suite(suite, task_dir=case_dir)


def test_bad_policy_and_retries_rejected(case_dir):
    with pytest.raises(SuiteValidationError, match="on_case_failure"):
        validate_suite(make_suite(["case_a"], on_case_failure="explode"), task_dir=case_dir)
    with pytest.raises(SuiteValidationError, match="max_retries"):
        validate_suite(make_suite(["case_a"], max_retries=-1), task_dir=case_dir)


def test_empty_cases_rejected(case_dir):
    with pytest.raises(SuiteValidationError, match="cases"):
        validate_suite(make_suite([]), task_dir=case_dir)


def test_full_boot_cases_must_be_in_cases(case_dir):
    with pytest.raises(SuiteValidationError, match="full_boot_cases"):
        validate_suite(make_suite(["case_a"], full_boot_cases=["case_b"]), task_dir=case_dir)


def test_load_suite_reads_file_and_defaults_the_name(tmp_path, case_dir):
    suite_dir = tmp_path / "suites"
    suite_dir.mkdir()
    # "name" is deliberately omitted (that's what this test checks); the three
    # required fields are not, since they have no default to fall back to.
    minimal = make_suite(["case_a"])
    del minimal["name"]
    (suite_dir / "mini.json").write_text(json.dumps(minimal), encoding="utf-8")
    suite = load_suite("mini", suite_dir=suite_dir, task_dir=case_dir)
    assert suite["name"] == "mini" and suite["cases"] == ["case_a"]

    with pytest.raises(SuiteValidationError, match="not found"):
        load_suite("missing", suite_dir=suite_dir, task_dir=case_dir)


def _write_case(case_dir: Path, name: str, entry_node: Dict, extra: Optional[Dict] = None):
    nodes = {
        "主场景确认": {"recognition": {"type": "ocr", "expected": "主界面"},
                       "action": {"type": "none"}, "next": ["用例开始"]},
        "用例开始": entry_node,
    }
    nodes.update(extra or {})
    (case_dir / f"{name}.json").write_text(
        json.dumps({"entry": "主场景确认", "nodes": nodes}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_blind_always_entry_is_refused_before_the_device_is_touched(case_dir):
    """The whole point of the gate: no case may start blind after a boot skip."""
    _write_case(case_dir, "blind", {"recognition": {"type": "always"},
                                    "action": {"type": "none"}, "next": []})
    engine = FakeEngine({})
    with pytest.raises(SuiteValidationError, match="always"):
        make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "blind"]))
    assert engine.calls == []


def test_entry_without_on_timeout_is_refused(case_dir):
    _write_case(case_dir, "norecovery", {"recognition": {"type": "ocr", "expected": "主界面"},
                                         "action": {"type": "none"}, "next": []})
    with pytest.raises(SuiteValidationError, match="on_timeout"):
        make_runner(FakeEngine({}), case_dir).run("dev1", make_suite(["norecovery"]))


def test_recovery_branch_without_finding_only_warns(case_dir, caplog):
    _write_case(
        case_dir, "silent",
        {"recognition": {"type": "ocr", "expected": "主界面"}, "action": {"type": "none"},
         "next": [], "on_timeout": "起点不对"},
        extra={"起点不对": {"recognition": {"type": "always"},
                            "action": {"type": "none"}, "next": []}},
    )
    with caplog.at_level(logging.WARNING):
        result = make_runner(FakeEngine({}), case_dir).run("dev1", make_suite(["silent"]))
    assert result["ok"] is True  # runs, but the smell is logged
    assert "not be reported as a QA finding" in caplog.text


def test_unknown_failure_policy_is_refused(case_dir):
    with pytest.raises(SuiteValidationError, match="on_case_failure"):
        make_runner(FakeEngine({}), case_dir).run(
            "dev1", make_suite(["case_a"], on_case_failure="explode")
        )


def test_case_without_the_resume_node_fails_preflight(tmp_path, case_dir):
    (case_dir / "standalone.json").write_text(
        json.dumps({
            "entry": "开始",
            "nodes": {"开始": {"recognition": {"type": "always"},
                               "action": {"type": "none"}, "next": []}},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    engine = FakeEngine({})
    with pytest.raises(SuiteValidationError, match="主场景确认"):
        make_runner(engine, case_dir).run("dev1", make_suite(["case_a", "standalone"]))
    # pre-flight fails before the device is touched
    assert engine.calls == []


def test_shipped_smoke_all_suite_is_valid():
    """The suite that ships with the repo must load against the real tasks."""
    suite_path = Path("task/task_definitions/suites/smoke_all.json")
    if not suite_path.is_file():  # task definitions are local assets
        pytest.skip("smoke_all suite not present")
    suite = load_suite("smoke_all")
    assert suite["cases"]
    assert suite["on_case_failure"] == "restart_retry"


def test_smoke_all_cases_gate_their_entry_on_the_landing_screen():
    """The suite contract: resuming lands on a REAL gate, never on `always`.

    Skipping the boot chain is only safe because every case re-verifies where it
    is before its body runs — a case whose 用例开始 is an unconditional hit would
    start clicking blind after a previous case left a panel open.
    """
    from task.task_loader import get_task_path, load_task

    suite_path = Path("task/task_definitions/suites/smoke_all.json")
    if not suite_path.is_file():  # task definitions are local assets
        pytest.skip("smoke_all suite not present")
    suite = load_suite("smoke_all")
    resume_after = suite.get("resume_after", "主场景确认")
    case_entry = suite.get("case_entry", "用例开始")

    for case in suite["cases"]:
        nodes = load_task(get_task_path(case))["nodes"]
        assert resume_after in nodes, case
        assert nodes[resume_after]["next"] == [case_entry], case
        entry = nodes[case_entry]
        assert entry["recognition"]["type"] != "always", f"{case}: {case_entry} is a blind gate"
        assert entry.get("on_timeout"), f"{case}: {case_entry} has no recovery branch"
        # the recovery branch must report itself as a QA finding
        assert nodes[entry["on_timeout"]].get("finding"), case


# ---------- progress on the terminal ----------
#
# A suite runs for tens of minutes. `on_progress` is the machine-readable
# channel, but a human watching the log used to see nothing between "started"
# and the final summary — engine lines with no case attached.

def test_suite_logs_start_and_one_line_per_case(case_dir, caplog):
    engine = FakeEngine({"case_a": [completed()], "case_b": [failed("boom")]})

    with caplog.at_level(logging.INFO, logger="test"):
        make_runner(engine, case_dir).run(
            "dev1", make_suite(["case_a", "case_b"], on_case_failure="restart_continue")
        )

    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Suite 'test_suite' started on dev1 (2 cases") for m in lines)
    assert any(m.startswith("Suite case 1/2 'case_a' starting (boot=full") for m in lines)
    assert any("Suite case 1/2 'case_a' ended: status=completed" in m for m in lines)
    assert any(m.startswith("Suite case 2/2 'case_b' starting (boot=resume") for m in lines)
    assert any("Suite case 2/2 'case_b' ended: status=failed" in m for m in lines)


def test_case_end_line_marks_a_retry_before_it_happens(case_dir, caplog):
    engine = FakeEngine({"case_a": [failed("boom"), completed()]})

    with caplog.at_level(logging.INFO, logger="test"):
        make_runner(engine, case_dir).run(
            "dev1", make_suite(["case_a"], on_case_failure="restart_retry", max_retries=1)
        )

    ends = [r.getMessage() for r in caplog.records if "' ended:" in r.getMessage()]
    assert ends[0].endswith("(will retry)")
    assert not ends[1].endswith("(will retry)")
