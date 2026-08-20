"""Smoke-suite runner: log in once, run many cases, restart only when broken.

`resume_after`/`case_entry`/`landing` are declared per suite, with no
framework default (`validate_suite` checks this before a run starts). The
usual layout: a suite lives in `task_definitions/suites/<suite>.json` and
every case task starts with a shared boot skeleton (e.g.
`task_definitions/common/boot_to_home.json`): cold start -> login -> the
landing screen. Running the cases one by one therefore pays that cold-start
toll (measured ~55s on real hardware) once per case. The skeleton's success
exits all converge on the case-body entry node (`用例开始` in that example),
so resuming with `start_after` set to the suite's `resume_after` node
(`主界面确认`) starts polling exactly that entry — the boot chain is skipped
with no engine change at all.

The suite layer is pure orchestration: it decides *which* run to launch and
what to do when one ends badly. It never reaches into the state machine —
recognition gating, findings evidence, bug-skip and the fallback chains all
stay exactly as they are inside `TaskEngine.run`, and every case keeps its own
findings run directory / report.json (so the smoke-report flow is unchanged).

Recovery state machine (one flag, `needs_boot`):

    needs_boot = True                      # nothing is running yet
    for case in cases:
        attempt = 1
        loop:
            run(case, start_after = None if needs_boot else resume_after)
            needs_boot = True              # pessimistic until proven landed
            if completed and landed on the suite's landing screen:
                needs_boot = False; case OK; break
            if agent_required:             # not a bug: a human/agent step is due
                case suspended; break      # next case cold-starts
            # failed / crashed / landed somewhere else:
            policy abort           -> stop the suite, rest marked "skipped"
            policy restart_retry   -> retry this case (attempt <= max_retries),
                                      and because needs_boot is set the retry
                                      is a full cold start + login
            policy restart_continue-> give up on this case, next one cold-starts

`needs_boot` is what makes recovery free: the "restart" is not a separate boot
run, it is the next run being allowed to walk its own boot chain again.

Not every task belongs in a suite: a case containing an `agent` handoff step is
recorded as suspended and the suite moves on — it is never resumed mid-suite,
so that case only ever runs half way.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from task.task_loader import (
    DEFAULT_TASK_DIR,
    SUITE_FAILURE_POLICIES,
    SuiteValidationError,
    get_task_path,
    load_task,
    validate_suite,
)

#: `resume_after` (boot-skip node), `case_entry` (case-body entry node) and
#: `landing` (landing-screen recognition) are app-specific — there is no
#: framework default for any of them, so every suite must declare its own
#: (validated by task_loader.validate_suite, called at the top of `run`).

#: Generic fallback for a `landing` spec's own `timeout_ms` / `poll_interval_ms`
#: when the suite leaves them unset; not app-specific, just a timing default.
DEFAULT_LANDING_TIMEOUT_MS = 8000
DEFAULT_LANDING_POLL_MS = 1500

#: What to do when a case ends failed / off-scene. Single source of truth lives
#: in task_loader (which validates suite files against it); re-exported here so
#: the runner and the validator can never drift apart.
FAILURE_POLICIES = SUITE_FAILURE_POLICIES
DEFAULT_FAILURE_POLICY = "restart_retry"


class SuiteRunner:
    """Runs a suite of cases on one device, re-booting only when needed.

    engine: a TaskEngine (its `hub` is reused for the between-case landing
    check). findings_recorder: optional, only to zip each case's evidence when
    `export_to` is given — the same FindingsRecorder the engine already writes
    through, so nothing about evidence ownership changes.
    """

    def __init__(self, engine, logger, findings_recorder=None,
                 task_dir=DEFAULT_TASK_DIR):
        self.engine = engine
        self.logger = logger
        self.recorder = findings_recorder
        self.task_dir = task_dir

    # ---------- public API ----------

    def run(self, device_id: str, suite: Dict, export_to: Optional[str] = None,
            on_progress: Optional[Callable[[Dict], None]] = None) -> Dict:
        """Run every case of `suite` on `device_id`; return a structured result.

        Raises SuiteValidationError before touching the device: first if the
        suite definition itself is malformed — including a missing required
        field, since `resume_after`/`case_entry`/`landing` have no app-neutral
        default and every suite must declare its own — then if any case is
        missing, unloadable, or cannot be resumed at the suite's resume node.
        A typo should not surface fifteen minutes into a run.
        """
        validate_suite(suite, self.task_dir)
        cases: List[str] = list(suite["cases"])
        policy = suite.get("on_case_failure", DEFAULT_FAILURE_POLICY)
        max_retries = int(suite.get("max_retries", 1))
        resume_after = suite["resume_after"]
        case_entry = suite["case_entry"]
        landing = self._landing_spec(suite)
        # Cases whose setup lives in the boot chain itself (GM debug mode is
        # armed on the login page and resets on every cold start), so skipping
        # the boot would silently degrade their coverage instead of failing.
        full_boot_cases = set(suite.get("full_boot_cases") or [])

        if policy not in FAILURE_POLICIES:
            raise SuiteValidationError(
                f"Unknown on_case_failure '{policy}' (allowed: {FAILURE_POLICIES})"
            )
        tasks = self._preflight(cases, resume_after, case_entry)

        started = time.monotonic()
        started_at = datetime.now().isoformat(timespec="seconds")
        records: List[Dict] = []
        needs_boot = True
        aborted_at: Optional[str] = None

        # A suite is the long-running shape (tens of minutes, many cases), and
        # until now only its final summary was logged: between "started" and
        # "finished" the terminal showed engine lines with no idea which case
        # they belonged to. `on_progress` stays the machine-readable channel;
        # these are for whoever is watching the run.
        self.logger.info(
            "Suite '%s' started on %s (%d cases, policy=%s)",
            suite.get("name"), device_id, len(cases), policy,
        )

        for index, case in enumerate(cases, start=1):
            if aborted_at is not None:
                records.append(self._skipped_record(case, index, aborted_at))
                continue

            attempt = 1
            while True:
                boot_mode = "full" if (needs_boot or case in full_boot_cases) else "resume"
                self._emit(on_progress, {
                    "event": "case_start", "case": case, "index": index,
                    "total": len(cases), "attempt": attempt, "boot": boot_mode,
                })
                self.logger.info(
                    "Suite case %d/%d '%s' starting (boot=%s, attempt=%d)",
                    index, len(cases), case, boot_mode, attempt,
                )
                record = self._run_case(
                    device_id, case, tasks[case], index,
                    start_after=None if boot_mode == "full" else resume_after,
                    boot_mode=boot_mode, attempt=attempt, case_entry=case_entry,
                    landing=landing, export_to=export_to, on_progress=on_progress,
                )
                # Pessimistic by default: only a completed run that provably
                # landed back on the suite's landing screen earns a boot skip.
                needs_boot = not record["ok"]
                retrying = (
                    not record["ok"]
                    and record["status"] != "agent_required"
                    and policy == "restart_retry"
                    and attempt <= max_retries
                )
                record["retried"] = retrying
                records.append(record)
                self._emit(on_progress, {
                    "event": "case_end", "case": case, "index": index,
                    "total": len(cases), "status": record["status"],
                    "duration_s": record["duration_s"], "landed": record["landed"],
                    "findings": record["findings_count"], "error": record["error"],
                    "will_retry": retrying,
                })
                self.logger.info(
                    "Suite case %d/%d '%s' ended: status=%s landed=%s duration=%.1fs "
                    "findings=%d%s",
                    index, len(cases), case, record["status"], record["landed"],
                    record["duration_s"], record["findings_count"],
                    " (will retry)" if retrying else "",
                )
                if record["ok"] or record["status"] == "agent_required":
                    break
                if policy == "abort":
                    aborted_at = case
                    self.logger.error(
                        "Suite '%s' aborting after case '%s' failed (on_case_failure=abort)",
                        suite.get("name"), case,
                    )
                    break
                if retrying:
                    self.logger.warning(
                        "Case '%s' failed; restarting the app and retrying (attempt %d/%d)",
                        case, attempt + 1, max_retries + 1,
                    )
                    attempt += 1
                    continue
                break

        result = {
            # "every case eventually passed" — a case that only passed on the
            # retry is still ok here, but shows up as flaky in the summary and
            # keeps its failed attempt (with its own report) in `cases`.
            "ok": _all_cases_passed(records),
            "suite": suite.get("name"),
            "device_id": device_id,
            "policy": policy,
            "max_retries": max_retries,
            "resume_after": resume_after,
            "started_at": started_at,
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - started, 1),
            "aborted_at": aborted_at,
            "cases": records,
        }
        result["summary"] = self._summarize(records, result["duration_s"])
        self._emit(on_progress, {"event": "suite_end", "summary": result["summary"]})
        self.logger.info(
            "Suite '%s' finished: %s", suite.get("name"), result["summary"]
        )
        return result

    # ---------- one case ----------

    def _run_case(self, device_id: str, case: str, task: Dict, index: int,
                  start_after: Optional[str], boot_mode: str, attempt: int,
                  case_entry: str, landing: Optional[Dict],
                  export_to: Optional[str],
                  on_progress: Optional[Callable[[Dict], None]]) -> Dict:
        """Run one case; never lets an engine exception kill the whole suite.

        A raised exception is logged with its traceback and recorded as a
        status="error" case (with the message), so it stays visible and drives
        the same restart/retry recovery as a failed run — it is reported, not
        swallowed.
        """
        started = time.monotonic()
        boot_done_at: Dict[str, Optional[float]] = {"t": None}

        def on_step(node: str) -> None:
            # The boot chain is over once the case-body entry is recognized;
            # timing it is what makes the saved-time estimate a measurement
            # rather than a guess.
            if node == case_entry and boot_done_at["t"] is None:
                boot_done_at["t"] = time.monotonic() - started
            self._emit(on_progress, {"event": "node", "case": case, "node": node})

        error: Optional[str] = None
        result: Optional[Dict] = None
        try:
            result = self.engine.run(
                device_id, task, start_after=start_after, task_name=case, on_step=on_step
            )
        except Exception as exc:
            self.logger.exception("Case '%s' raised during the run", case)
            error = f"{type(exc).__name__}: {exc}"

        duration = round(time.monotonic() - started, 1)
        if result is None:
            status = "error"
            findings: List[Dict] = []
            report: Dict = {}
            steps = 0
        else:
            status = result.get("status", "failed")
            findings = result.get("findings") or []
            report = result.get("report") or {}
            steps = len(result.get("steps") or [])
            error = result.get("error")

        landed, landing_reason = self._check_landing(device_id, landing, status)
        export_path = self._export(export_to, findings, case)

        return {
            "case": case,
            "index": index,
            "attempt": attempt,
            "boot": boot_mode,
            "status": status,
            # A case is only "ok" if the engine completed it AND the device is
            # back on the suite's landing screen — the next case's precondition.
            "ok": status == "completed" and landed,
            "landed": landed,
            "landing_reason": landing_reason,
            "duration_s": duration,
            "boot_s": round(boot_done_at["t"], 1) if boot_done_at["t"] is not None else None,
            "steps": steps,
            "error": error,
            "findings_count": len(findings),
            "severity_counts": _count_severities(findings),
            "findings": [
                {
                    "type": f.get("type"),
                    "severity": f.get("severity"),
                    "node": f.get("node"),
                    "message": f.get("message"),
                }
                for f in findings
            ],
            "report_path": report.get("report_path"),
            "report_html_path": report.get("report_html_path"),
            "export_path": export_path or report.get("export_path"),
            "handoff": (result or {}).get("handoff"),
            "retried": False,
        }

    # ---------- helpers ----------

    def _preflight(self, cases: List[str], resume_after: str,
                   case_entry: str) -> Dict[str, Dict]:
        """Load every case up front and prove it is safe to resume mid-flow.

        Skipping the boot chain is only safe because the case re-verifies where
        it is before its body runs, so the entry gate contract is enforced here
        rather than left to authoring discipline: an entry that is `always`
        (an unconditional hit) would start clicking blind after a previous case
        left a panel open — exactly what this feature exists to prevent.
        """
        if not cases:
            raise SuiteValidationError("Suite has no cases")
        tasks: Dict[str, Dict] = {}
        for case in cases:
            if case in tasks:
                continue
            tasks[case] = load_task(get_task_path(case, self.task_dir))
            self._check_resumable(case, tasks[case]["nodes"], resume_after, case_entry)
        return tasks

    def _check_resumable(self, case: str, nodes: Dict, resume_after: str,
                         case_entry: str) -> None:
        if resume_after not in nodes:
            raise SuiteValidationError(
                f"Case '{case}' has no node '{resume_after}', so the suite cannot "
                f"skip its boot chain; include the shared boot fragment or set the "
                f"suite's 'resume_after' to a node the case defines"
            )
        if case_entry not in nodes:
            raise SuiteValidationError(
                f"Case '{case}' has no case-body entry node '{case_entry}' "
                f"(the node the boot skeleton's exits converge on)"
            )
        entry = nodes[case_entry]
        if (entry.get("recognition") or {}).get("type") == "always":
            raise SuiteValidationError(
                f"Case '{case}' entry node '{case_entry}' recognizes 'always', which "
                f"verifies nothing: resuming past the boot chain would start the case "
                f"blind. Gate it on the landing screen (e.g. ocr/ui_text on a stable "
                f"anchor + roi)"
            )
        recovery = entry.get("on_timeout")
        if not recovery:
            raise SuiteValidationError(
                f"Case '{case}' entry node '{case_entry}' has no on_timeout branch, so "
                f"a case that starts on the wrong screen would just fail the run instead "
                f"of reporting and recovering"
            )
        # The recovery branch should report itself (QA findings are the product),
        # but a missing one is an authoring smell, not a reason to refuse to run.
        if not (nodes.get(recovery) or {}).get("finding"):
            self.logger.warning(
                "Case '%s': entry recovery node '%s' has no `finding`, so a wrong-screen "
                "start will not be reported as a QA finding", case, recovery,
            )

    @staticmethod
    def _landing_spec(suite: Dict) -> Optional[Dict]:
        """Between-case landing check spec; `"landing": null` (or `{}`) disables it.

        `landing` is a required suite field (enforced by validate_suite, called
        at the top of `run`), so the key is always present by the time this runs.
        """
        landing = suite.get("landing")
        if not landing:
            return None
        return dict(landing)

    def _check_landing(self, device_id: str, landing: Optional[Dict],
                       status: str) -> tuple:
        """Is the device back on the suite's landing screen?

        Only meaningful after a completed run — a failed one is already known
        bad and gets a restart regardless, so the extra polling is skipped.

        This is a precondition probe for the *next* case, not a QA assertion:
        the case's own closing nodes already report an off-scene ending as a
        finding with evidence. So a miss here is logged and surfaced in the
        suite result (`landed: false`), never recorded as a finding against a
        run whose report is already sealed.
        """
        if status != "completed":
            return False, f"status={status}"
        if landing is None:
            return True, "landing check disabled"
        hub = getattr(self.engine, "hub", None)
        if hub is None:
            return True, "no recognizer hub"

        spec = {k: v for k, v in landing.items()
                if k not in ("timeout_ms", "poll_interval_ms")}
        timeout_ms = int(landing.get("timeout_ms", DEFAULT_LANDING_TIMEOUT_MS))
        interval_ms = int(landing.get("poll_interval_ms", DEFAULT_LANDING_POLL_MS))
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                hit = hub.recognize(device_id, spec)
            except Exception as exc:
                # An unmeasurable screen must not trigger a pointless reboot;
                # log it and give the benefit of the doubt.
                self.logger.warning("Landing check failed to run: %s", exc)
                return True, f"check error: {exc}"
            if hit is not None:
                return True, "landed"
            if time.monotonic() >= deadline:
                self.logger.warning(
                    "Case ended off the landing screen (%s not recognized within %dms); "
                    "the next case will cold-start", spec.get("expected") or spec.get("type"),
                    timeout_ms,
                )
                return False, "landing anchor not recognized"
            time.sleep(interval_ms / 1000)

    def _export(self, export_to: Optional[str], findings: List[Dict],
                case: str) -> Optional[str]:
        if not export_to or not findings or self.recorder is None:
            return None
        try:
            return self.recorder.export_run(export_to)
        except Exception as exc:
            self.logger.warning("Export of case '%s' evidence failed: %s", case, exc)
            return None

    @staticmethod
    def _skipped_record(case: str, index: int, aborted_at: str) -> Dict:
        return {
            "case": case, "index": index, "attempt": 0, "boot": None,
            "status": "skipped", "ok": False, "landed": False,
            "landing_reason": None, "duration_s": 0.0, "boot_s": None, "steps": 0,
            "error": f"suite aborted at case '{aborted_at}'",
            "findings_count": 0, "severity_counts": {}, "findings": [],
            "report_path": None, "report_html_path": None, "export_path": None,
            "handoff": None, "retried": False,
        }

    @staticmethod
    def _summarize(records: List[Dict], duration_s: float) -> Dict:
        """Suite totals plus the measured cost of the boots that were skipped."""
        boot_times = [r["boot_s"] for r in records
                      if r["boot"] == "full" and r["boot_s"] is not None]
        boot_avg = round(sum(boot_times) / len(boot_times), 1) if boot_times else None
        resumed = sum(1 for r in records if r["boot"] == "resume")
        final = _final_records(records)
        return {
            "cases": len(final),
            "cases_passed": sum(1 for r in final.values() if r["ok"]),
            # Cases that only passed after a restart: passing, but unstable.
            "flaky": sum(1 for r in final.values() if r["ok"] and r["attempt"] > 1),
            "total": len(records),
            "runs": sum(1 for r in records if r["status"] != "skipped"),
            "completed": sum(1 for r in records if r["ok"]),
            "failed": sum(1 for r in records
                          if r["status"] in ("failed", "error")
                          or (r["status"] == "completed" and not r["landed"])),
            "agent_required": sum(1 for r in records if r["status"] == "agent_required"),
            "skipped": sum(1 for r in records if r["status"] == "skipped"),
            "retries": sum(1 for r in records if r["attempt"] > 1),
            "findings": sum(r["findings_count"] for r in records),
            "severity_counts": _merge_severities(records),
            "full_boots": sum(1 for r in records if r["boot"] == "full"),
            "boots_skipped": resumed,
            "boot_s_avg": boot_avg,
            "estimated_saved_s": round(resumed * boot_avg, 1) if boot_avg is not None else None,
            "duration_s": duration_s,
        }

    def _emit(self, on_progress: Optional[Callable[[Dict], None]], event: Dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(event)
        except Exception as exc:  # a broken progress sink must not fail the suite
            self.logger.warning("Suite progress callback failed on %s: %s",
                                event.get("event"), exc)


def _final_records(records: List[Dict]) -> Dict[str, Dict]:
    """Last record per case (retries overwrite the attempt before them)."""
    final: Dict[str, Dict] = {}
    for record in records:
        final[record["case"]] = record
    return final


def _all_cases_passed(records: List[Dict]) -> bool:
    final = _final_records(records)
    return bool(final) and all(r["ok"] for r in final.values())


def _count_severities(findings: List[Dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        severity = f.get("severity") or "unknown"
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _merge_severities(records: List[Dict]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for record in records:
        for severity, count in (record.get("severity_counts") or {}).items():
            merged[severity] = merged.get(severity, 0) + count
    return merged


def format_suite_report(result: Dict) -> str:
    """Human-readable summary table (CLI / hand-off text)."""
    lines: List[str] = []
    summary = result["summary"]
    header = (
        f"Suite '{result.get('suite')}' on {result.get('device_id')}: "
        f"{'OK' if result.get('ok') else 'PROBLEMS'} "
        f"({summary['cases_passed']}/{summary['cases']} cases passed, "
        f"{summary['total']} run(s), {result['duration_s']}s)"
    )
    lines.append(header)
    lines.append(f"  {'#':<3} {'case':<22} {'boot':<7} {'status':<14} {'time':>7}  findings")
    for record in result.get("cases", []):
        status = record["status"]
        if status == "completed" and not record["landed"]:
            status = "completed*"  # completed but ended off the landing screen
        lines.append(
            f"  {record['index']:<3} {record['case']:<22} {str(record['boot'] or '-'):<7} "
            f"{status:<14} {record['duration_s']:>6}s  {record['findings_count']}"
            + (f"  (attempt {record['attempt']})" if record["attempt"] > 1 else "")
        )
        if record["error"]:
            lines.append(f"      error: {record['error']}")
    lines.append(
        f"  totals: passed={summary['cases_passed']}/{summary['cases']} "
        f"flaky={summary['flaky']} failed_runs={summary['failed']} "
        f"agent_required={summary['agent_required']} skipped={summary['skipped']} "
        f"retries={summary['retries']} findings={summary['findings']}"
    )
    if summary.get("estimated_saved_s") is not None:
        lines.append(
            f"  boot: {summary['full_boots']} full ({summary['boot_s_avg']}s avg), "
            f"{summary['boots_skipped']} skipped -> ~{summary['estimated_saved_s']}s saved"
        )
    return "\n".join(lines)
