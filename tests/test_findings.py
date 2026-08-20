"""QA-findings pipeline: recorder, logcat monitor, blank_screen channel, engine integration."""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from perception.logcat_monitor import EvidencePolicy, LogcatMonitor, annotate_lines
from perception.screen_recorder import RollingScreenRecorder
from task.findings import FindingsRecorder, prune_old_runs
from task.recognizers import RecognizerHub
from task.report_html import render_report_html
from task.task_engine import TaskEngine
from task.task_loader import TaskValidationError, validate_task

LOGGER = logging.getLogger("test")


class FakeCapturer:
    def __init__(self, png: bytes = b"\x89PNG-fake", stream_enabled: bool = False):
        self.png = png
        self.calls = 0
        self.exact_calls = 0
        self.stream_enabled = stream_enabled

    def capture_png_bytes(self, device_id: str, exact: bool = False) -> bytes:
        self.calls += 1
        if exact:
            self.exact_calls += 1
        return self.png

    def encode_png(self, image) -> bytes:
        return self.png

    def capture_image(self, device_id: str):
        from PIL import Image

        self.calls += 1
        return Image.open(io.BytesIO(self.png))


class FailingCapturer:
    def capture_png_bytes(self, device_id: str) -> bytes:
        raise RuntimeError("no device")


class FakeDumpMatcher:
    def dump_ui_xml(self, device_id: str) -> str:
        return "<?xml version='1.0'?><hierarchy/>"


class FakeHub:
    """Scripted recognizer: maps expected-text -> hit dict (None = miss)."""

    def __init__(self, hits: Dict[str, Optional[Dict]]):
        self.hits = hits

    def recognize(self, device_id: str, spec: Dict, **kwargs) -> Optional[Dict]:
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        hit = self.hits.get(spec["expected"])
        return dict(hit) if hit else None


class FakeExecutor:
    def __init__(self, fail_types: Optional[set] = None):
        self.executed: List[Dict] = []
        self.fail_types = fail_types or set()

    def execute(self, device_id: str, action: Dict, tracer=None) -> Dict:
        self.executed.append(action)
        if action["type"] in self.fail_types:
            return {"ok": "False", "stdout": "", "stderr": "boom"}
        return {"ok": "True", "stdout": "", "stderr": ""}


class FakeLogcat:
    def __init__(self, batches: List[List[Dict]]):
        self.batches = list(batches)
        self.started_on: Optional[str] = None

    def start(self, device_id: str) -> None:
        self.started_on = device_id

    def poll(self, device_id: str) -> List[Dict]:
        return self.batches.pop(0) if self.batches else []


def hit(x: int = 100, y: int = 200, text: str = "t") -> Dict:
    return {"center": (x, y), "text": text, "score": 0.9, "channel": "ui_text"}


class FakeTailMonitor:
    """Monitor stub that only serves tail() — the in-game log fragment."""

    def __init__(self, lines: List[str]):
        self.lines = lines

    def tail(self, device_id: str, seconds: int = 60, max_lines: int = 300):
        return list(self.lines)[-max_lines:]


def make_recorder(tmp_path, capturer=None, matcher=None, monitor=None, **kwargs) -> FindingsRecorder:
    return FindingsRecorder(
        LOGGER,
        FakeCapturer() if capturer is None else capturer,
        FakeDumpMatcher() if matcher is None else matcher,
        output_dir=tmp_path / "findings",
        logcat_monitor=monitor,
        **kwargs,
    )


def make_engine(hub, executor=None, recorder=None, logcat=None, **kwargs) -> TaskEngine:
    return TaskEngine(
        hub, executor or FakeExecutor(), LOGGER,
        findings_recorder=recorder, logcat_monitor=logcat, **kwargs,
    )


# ---------- FindingsRecorder ----------


def test_recorder_writes_evidence_and_report(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev:1", task_name="demo")

    finding = recorder.record("watchdog", "error", "出现错误弹窗", node="主界面", ui_dump=True)
    findings, summary = recorder.finalize(status="completed")

    assert finding["evidence"]["screenshot"].endswith("01_watchdog.png")
    assert finding["evidence"]["ui_dump"].endswith("01_watchdog.xml")
    # ":" sanitized out of the device segment
    assert "dev_1" in finding["evidence"]["screenshot"]
    assert len(findings) == 1
    assert summary["counts"] == {"error": 1}

    report = json.loads(open(summary["report_path"], encoding="utf-8").read())
    assert report["task"] == "demo"
    assert report["device"] == "dev:1"
    assert report["status"] == "completed"
    assert report["findings"][0]["message"] == "出现错误弹窗"


def test_recorder_fresh_evidence_uses_exact_capture(tmp_path):
    # A finding without a pinned frame (persistent state) takes a lossless
    # screencap, bypassing the lossy stream backend.
    cap = FakeCapturer()
    recorder = make_recorder(tmp_path, capturer=cap)
    recorder.open_run("dev1")

    finding = recorder.record("crash", "critical", "FATAL EXCEPTION")

    assert cap.exact_calls == 1
    assert finding["evidence"]["screenshot"].endswith("01_crash.png")
    assert "screenshot_exact" not in finding["evidence"]


def test_recorder_pinned_frame_adds_exact_shot_under_stream(tmp_path):
    # Under the lossy stream backend, a pinned transient frame is kept (it shows
    # the toast) and an exact screencap of the settled state is added.
    cap = FakeCapturer(stream_enabled=True)
    recorder = make_recorder(tmp_path, capturer=cap)
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "一闪而过的报错", image=b"pinned-toast-frame")

    evidence = finding["evidence"]
    assert evidence["screenshot"].endswith("01_watchdog.png")
    assert evidence["screenshot_exact"].endswith("01_watchdog_exact.png")
    assert cap.exact_calls == 1


def test_recorder_pinned_frame_no_exact_shot_on_screencap(tmp_path):
    # With the screencap backend the pinned frame is already exact; no extra shot.
    cap = FakeCapturer(stream_enabled=False)
    recorder = make_recorder(tmp_path, capturer=cap)
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "msg", image=b"pinned")

    assert "screenshot_exact" not in finding["evidence"]
    assert cap.exact_calls == 0


def test_recorder_force_exact_adds_the_shot_on_screencap_too(tmp_path):
    # force_exact is for callers whose pinned frame is untrustworthy for reasons
    # the backend knows nothing about (the sentinel pins downscaled 720p monitor
    # frames): the lossless companion is taken whatever the backend says.
    cap = FakeCapturer(stream_enabled=False)
    recorder = make_recorder(tmp_path, capturer=cap)
    recorder.open_run("dev1")

    finding = recorder.record("sentinel_blank_screen", "warning", "blank",
                              image=b"pinned-720p-frame", force_exact=True)

    evidence = finding["evidence"]
    assert evidence["screenshot"].endswith("01_sentinel_blank_screen.png")
    assert evidence["screenshot_exact"].endswith("01_sentinel_blank_screen_exact.png")
    assert cap.exact_calls == 1


def test_recorder_force_exact_failure_still_keeps_the_finding(tmp_path):
    # The extra shot is a bonus; a device that will not answer must not cost the
    # finding or its pinned frame.
    class NoExactCapturer(FakeCapturer):
        def capture_png_bytes(self, device_id, exact=False):
            raise RuntimeError("device gone")

    recorder = make_recorder(tmp_path, capturer=NoExactCapturer())
    recorder.open_run("dev1")

    finding = recorder.record("sentinel_blank_screen", "warning", "blank",
                              image=b"pinned", force_exact=True)

    assert finding["evidence"]["screenshot"].endswith("01_sentinel_blank_screen.png")
    assert "screenshot_exact" not in finding["evidence"]


def test_recorder_failure_appends_task_failure_with_scene(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")

    findings, summary = recorder.finalize(status="failed", error="Action failed at node 'x'")

    assert [f["type"] for f in findings] == ["task_failure"]
    evidence = findings[0]["evidence"]
    assert "screenshot" in evidence and "ui_dump" in evidence
    assert summary["report_path"] is not None


def test_recorder_clean_run_writes_nothing(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")

    findings, summary = recorder.finalize(status="completed")

    assert findings == []
    assert summary == {
        "counts": {},
        "run_dir": None,
        "report_path": None,
        "report_html_path": None,
        "export_path": None,
    }
    assert not (tmp_path / "findings").exists()


def test_recorder_degrades_when_evidence_capture_fails(tmp_path):
    recorder = make_recorder(tmp_path, capturer=FailingCapturer())
    recorder.open_run("dev1")

    finding = recorder.record("crash", "critical", "FATAL EXCEPTION")

    assert finding["evidence"] == {}
    assert recorder.findings[0]["severity"] == "critical"


def test_recorder_unknown_severity_falls_back_to_warning(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "fatal", "msg", screenshot=False)

    assert finding["severity"] == "warning"


# ---------- save_context_image (evidence that is NOT a finding) ----------


def test_save_context_image_creates_the_run_dir_and_returns_a_relative_path(tmp_path):
    # The run dir is normally created lazily by the first finding; a context
    # image has to be able to make it on its own.
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")
    assert not recorder.run_dir.exists()

    rel = recorder.save_context_image("popup_01_促销弹窗关闭X", object())

    assert rel == "popup_01_促销弹窗关闭X.png"
    assert (recorder.run_dir / rel).read_bytes() == b"\x89PNG-fake"


def test_save_context_image_is_not_a_finding(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")

    recorder.save_context_image("popup_01_x", object())
    findings, summary = recorder.finalize(status="completed")

    # A swept popup is expected noise: the frame is kept for auditing, but it
    # must not show up as a problem or inflate the severity counts.
    assert findings == []
    assert summary["counts"] == {}
    assert summary["report_path"] is None


def test_save_context_image_encodes_the_frame_handed_in(tmp_path):
    """The caller's frame is what lands on disk — no re-capture from the device."""
    capturer = FakeCapturer(png=b"\x89PNG-pinned")
    recorder = make_recorder(tmp_path, capturer=capturer)
    recorder.open_run("dev1")

    rel = recorder.save_context_image("popup_01_x", object())

    assert (recorder.run_dir / rel).read_bytes() == b"\x89PNG-pinned"
    assert capturer.calls == 0  # nothing was grabbed from the device


def test_save_context_image_degrades_instead_of_raising(tmp_path):
    class BrokenEncoder(FakeCapturer):
        def encode_png(self, image):
            raise RuntimeError("encoder blew up")

    recorder = make_recorder(tmp_path, capturer=BrokenEncoder())
    recorder.open_run("dev1")

    assert recorder.save_context_image("popup_01_x", object()) is None
    # No frame and no capturer are equally harmless.
    assert recorder.save_context_image("popup_02_x", None) is None


# ---------- LogcatMonitor ----------


def install_fake_adb(monkeypatch, date_out: str, logcat_out: str):
    commands: List[str] = []

    def fake_run(cmd, **kwargs):
        shell_cmd = cmd[-1]
        commands.append(shell_cmd)
        out = date_out if shell_cmd.startswith("date") else logcat_out
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("perception.logcat_monitor.subprocess.run", fake_run)
    return commands


CRASH_LOG = (
    "06-12 10:01:00.000 I/foo(1): normal line\n"
    "06-12 10:01:01.000 E/AndroidRuntime(123): FATAL EXCEPTION: main\n"
    "06-12 10:01:01.001 E/AndroidRuntime(123): java.lang.NullPointerException\n"
    "06-12 10:01:01.002 E/AndroidRuntime(123):     at com.game.Main.run\n"
    "06-12 10:02:00.000 E/ActivityManager(456): ANR in com.game/.MainActivity\n"
)


def test_logcat_detects_crash_and_anr_with_excerpt(monkeypatch):
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", CRASH_LOG)
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")

    events = monitor.poll("dev1")

    assert [e["type"] for e in events] == ["crash", "anr"]
    assert events[0]["severity"] == "critical"
    assert events[1]["severity"] == "error"
    assert "NullPointerException" in events[0]["excerpt"][1]


def test_logcat_dedupes_across_polls(monkeypatch):
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", CRASH_LOG)
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")

    assert len(monitor.poll("dev1")) == 2
    assert monitor.poll("dev1") == []


def test_logcat_uses_device_clock_marker(monkeypatch):
    commands = install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", "")
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")
    monitor.poll("dev1")

    assert any("-T '06-12 10:00:00.000'" in c for c in commands)


def test_logcat_falls_back_to_tail_without_clock(monkeypatch):
    commands = install_fake_adb(monkeypatch, "garbage\n", "")
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")
    monitor.poll("dev1")

    assert any("-t 2000" in c for c in commands)


def test_logcat_disables_after_adb_failure(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise FileNotFoundError("adb")

    monkeypatch.setattr("perception.logcat_monitor.subprocess.run", fake_run)
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")

    assert monitor.poll("dev1") == []
    polls_so_far = len(calls)
    assert monitor.poll("dev1") == []
    assert len(calls) == polls_so_far  # disabled: no further adb calls


# ---------- rolling screen recorder (video evidence) ----------


class FakePopen:
    def __init__(self):
        self._rc = None

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def kill(self):
        self._rc = -9


def install_fake_screenrecord(monkeypatch):
    state = {"popen": [], "run": []}

    def fake_popen(cmd, **kwargs):
        state["popen"].append(cmd)
        proc = FakePopen()
        state.setdefault("procs", []).append(proc)
        return proc

    def fake_run(cmd, **kwargs):
        state["run"].append(cmd)
        if "pull" in cmd:
            local = Path(cmd[-1])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"mp4-data")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("perception.screen_recorder.subprocess.Popen", fake_popen)
    monkeypatch.setattr("perception.screen_recorder.subprocess.run", fake_run)
    monkeypatch.setattr("perception.screen_recorder.STOP_SETTLE_S", 0)
    return state


def test_screen_recorder_starts_first_segment(monkeypatch):
    state = install_fake_screenrecord(monkeypatch)
    rec = RollingScreenRecorder(LOGGER)
    rec.start("dev1")

    assert len(state["popen"]) == 1
    cmd = state["popen"][0]
    assert "screenrecord" in cmd and "/sdcard/ga_rec_0.mp4" in cmd
    # leftover files from a previous run were cleaned first
    assert any("rm -f" in c[-1] for c in state["run"])


def test_screen_recorder_tick_rotates_when_segment_is_old(monkeypatch):
    state = install_fake_screenrecord(monkeypatch)
    rec = RollingScreenRecorder(LOGGER, segment_s=60)
    rec.start("dev1")

    rec.tick()  # fresh segment: nothing happens
    assert len(state["popen"]) == 1

    rec._segment_started -= 61
    rec.tick()

    assert len(state["popen"]) == 2  # rotated into a new segment
    assert any("pkill" in c[-1] for c in state["run"])  # previous one stopped cleanly
    assert rec._segments == ["/sdcard/ga_rec_0.mp4", "/sdcard/ga_rec_1.mp4"]


def test_screen_recorder_collect_pulls_once_and_resumes(monkeypatch, tmp_path):
    state = install_fake_screenrecord(monkeypatch)
    rec = RollingScreenRecorder(LOGGER)
    rec.start("dev1")

    first = rec.collect(tmp_path / "video")

    assert [Path(p).name for p in first] == ["ga_rec_0.mp4"]
    assert Path(first[0]).read_bytes() == b"mp4-data"
    assert len(state["popen"]) == 2  # recording resumed

    second = rec.collect(tmp_path / "video")

    assert [Path(p).name for p in second] == ["ga_rec_0.mp4", "ga_rec_1.mp4"]
    pulls = [c for c in state["run"] if "pull" in c]
    assert len(pulls) == 2  # ga_rec_0 reused, not pulled twice


def test_screen_recorder_disabled_when_screenrecord_unavailable(monkeypatch):
    def broken_popen(cmd, **kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr("perception.screen_recorder.subprocess.Popen", broken_popen)
    monkeypatch.setattr(
        "perception.screen_recorder.subprocess.run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    rec = RollingScreenRecorder(LOGGER)
    rec.start("dev1")

    assert rec._broken is True
    rec.tick()
    assert rec.collect("anywhere") == []


class FakeScreenRecorder:
    """collect()-only stub used by FindingsRecorder tests."""

    def __init__(self, files: int = 1, fail: bool = False):
        self.files = files
        self.fail = fail
        self.collect_calls = 0

    def collect(self, target_dir) -> List[str]:
        if self.fail:
            raise RuntimeError("recording forbidden")
        self.collect_calls += 1
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(self.files):
            p = target / f"seg_{self.collect_calls}_{i}.mp4"
            p.write_bytes(b"v")
            paths.append(str(p))
        return paths


def test_recorder_video_replaces_frame_history(tmp_path):
    recorder = make_recorder(tmp_path, screen_recorder=FakeScreenRecorder())
    recorder.open_run("dev1")
    recorder.snapshot_history()

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert len(finding["evidence"]["video"]) == 1
    assert Path(finding["evidence"]["video"][0]).is_file()
    assert "history" not in finding["evidence"]  # mp4 supersedes frames


# ---------- pcap (protocol snapshot) evidence, opt-in alongside video ----------


class FakePcapRecorder:
    """collect()-only stub mirroring FakeScreenRecorder, for pcap evidence."""

    def __init__(self, files: int = 1, fail: bool = False):
        self.files = files
        self.fail = fail
        self.collect_calls = 0

    def collect(self, target_dir) -> List[str]:
        if self.fail:
            raise RuntimeError("no tcpdump")
        self.collect_calls += 1
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(self.files):
            p = target / f"pcap_{self.collect_calls}_{i}.pcap"
            p.write_bytes(b"p")
            paths.append(str(p))
        return paths


def test_recorder_attaches_pcap_alongside_video(tmp_path):
    pcap = FakePcapRecorder()
    recorder = make_recorder(
        tmp_path, screen_recorder=FakeScreenRecorder(), pcap_recorder=pcap
    )
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    # pcap sits next to video, not instead of it.
    assert len(finding["evidence"]["video"]) == 1
    assert len(finding["evidence"]["pcap"]) == 1
    assert Path(finding["evidence"]["pcap"][0]).is_file()
    assert pcap.collect_calls == 1


def test_recorder_records_finding_when_pcap_absent(tmp_path):
    recorder = make_recorder(tmp_path)  # no pcap recorder wired
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert "pcap" not in finding["evidence"]  # opt-in, absent by default
    assert len(recorder.findings) == 1  # finding still recorded


def test_recorder_records_finding_when_pcap_broken(tmp_path):
    recorder = make_recorder(tmp_path, pcap_recorder=FakePcapRecorder(fail=True))
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    # A pcap collect failure must never drop or break the finding.
    assert "pcap" not in finding["evidence"]
    assert len(recorder.findings) == 1
    assert finding["message"] == "msg"


def test_recorder_falls_back_to_frames_when_video_unavailable(tmp_path):
    recorder = make_recorder(tmp_path, screen_recorder=FakeScreenRecorder(fail=True))
    recorder.open_run("dev1")
    recorder.snapshot_history()

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert "video" not in finding["evidence"]
    assert len(finding["evidence"]["history"]) == 1


class FakeLifecycleScreen(FakeScreenRecorder):
    """Adds the engine-facing lifecycle to the collect() stub."""

    def __init__(self):
        super().__init__(files=1)
        self.started_on = None
        self.ticks = 0
        self.stopped = 0

    def start(self, device_id):
        self.started_on = device_id

    def tick(self):
        self.ticks += 1

    def stop(self):
        self.stopped += 1


def test_engine_failure_video_then_recording_stopped(tmp_path, sample_task):
    screen = FakeLifecycleScreen()
    recorder = make_recorder(tmp_path, screen_recorder=screen)
    engine = make_engine(
        FakeHub({"设置": hit()}), executor=FakeExecutor(fail_types={"click"}),
        recorder=recorder, screen_recorder=screen,
    )

    result = engine.run("dev1", sample_task)

    assert result["status"] == "failed"
    failure = result["findings"][-1]
    assert failure["evidence"]["video"]  # crash moment captured as mp4
    assert screen.started_on == "dev1"
    assert screen.stopped == 1  # cleaned up after finalize pulled the video


def test_engine_ticks_video_between_steps(sample_task):
    screen = FakeLifecycleScreen()
    engine = make_engine(FakeHub({"设置": hit()}), screen_recorder=screen)

    result = engine.run("dev1", sample_task)

    assert result["status"] == "completed"
    assert screen.ticks == 2  # once per executed step
    assert screen.stopped == 1


class FakeLifecyclePcap:
    """Engine-facing lifecycle stub for the pcap recorder (start/tick/stop)."""

    def __init__(self):
        self.started_on = None
        self.ticks = 0
        self.stopped = 0

    def start(self, device_id):
        self.started_on = device_id

    def tick(self):
        self.ticks += 1

    def stop(self):
        self.stopped += 1

    def collect(self, target_dir):
        return []


def test_engine_drives_pcap_lifecycle(sample_task):
    pcap = FakeLifecyclePcap()
    engine = make_engine(FakeHub({"设置": hit()}), pcap_recorder=pcap)

    result = engine.run("dev1", sample_task)

    assert result["status"] == "completed"
    assert pcap.started_on == "dev1"  # started with the run
    assert pcap.ticks == 2  # ticked once per executed step, same as video
    assert pcap.stopped == 1  # stopped after finalize


# ---------- portable report & export ----------


def evidence_paths(finding: Dict) -> List[str]:
    paths: List[str] = []
    for value in (finding.get("evidence") or {}).values():
        paths.extend(value if isinstance(value, list) else [value])
    return paths


def test_report_paths_are_relative_and_resolvable(tmp_path):
    recorder = make_recorder(tmp_path, monitor=FakeTailMonitor(["06-12 10:00:00.000 I/g: x"]))
    recorder.open_run("dev1", "demo")
    recorder.add_timeline("node_recognized", node="a")
    recorder.snapshot_history()
    recorder.record("watchdog", "error", "msg", ui_dump=True)

    findings, summary = recorder.finalize("completed")

    run_dir = Path(summary["run_dir"])
    # in-memory findings keep absolute paths for the caller...
    assert findings[0]["evidence"]["screenshot"].startswith(str(run_dir))
    # ...while report.json is self-contained: relative, forward-slash, resolvable
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    for finding in report["findings"]:
        for rel in evidence_paths(finding):
            assert not rel.startswith(str(run_dir)) and "\\" not in rel
            assert (run_dir / rel).is_file()


def test_finalize_auto_exports_when_configured(tmp_path):
    export_target = tmp_path / "deliver"
    recorder = make_recorder(tmp_path, export_dir=str(export_target))
    recorder.open_run("dev:1", "登录流程")
    recorder.record("watchdog", "error", "出错")

    _, summary = recorder.finalize("failed", error="boom")

    dest = Path(summary["export_path"])
    assert dest.parent == export_target
    assert dest.suffix == ".zip"
    assert "登录流程" in dest.name and "dev_1" in dest.name and "failed" in dest.stem
    # the exported zip is self-contained: report.json at the root, evidence resolvable
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert "report.json" in names
        report = json.loads(zf.read("report.json").decode("utf-8"))
        for finding in report["findings"]:
            for rel in evidence_paths(finding):
                assert rel in names


def test_export_collision_appends_suffix(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1", "t")
    recorder.record("watchdog", "error", "x", screenshot=False)
    recorder.finalize("completed")

    first = recorder.export_run(tmp_path / "out")
    second = recorder.export_run(tmp_path / "out")

    assert first != second
    assert Path(first).is_file() and Path(second).is_file()
    assert Path(first).suffix == ".zip" and Path(second).suffix == ".zip"


def test_clean_run_not_exported(tmp_path):
    export_target = tmp_path / "deliver"
    recorder = make_recorder(tmp_path, export_dir=str(export_target))
    recorder.open_run("dev1")

    _, summary = recorder.finalize("completed")

    assert summary["export_path"] is None
    assert not export_target.exists()


# ---------- run-summary notifiers ----------


class FakeNotifier:
    """Stands in for a core.notifier Notifier: records what it was handed."""

    def __init__(self, min_findings: int = 1, on_status: Optional[List[str]] = None, boom: bool = False):
        self.min_findings = min_findings
        self.on_status = on_status or []
        self.boom = boom
        self.pushed: List[Dict] = []

    def should_notify(self, status: Optional[str], finding_count: int) -> bool:
        if finding_count < self.min_findings:
            return False
        return not self.on_status or status in self.on_status

    def notify_run(self, summary: Dict) -> bool:
        if self.boom:
            raise RuntimeError("webhook down")
        self.pushed.append(summary)
        return True


def test_finalize_pushes_one_summary_per_run(tmp_path):
    notifier = FakeNotifier()
    recorder = make_recorder(tmp_path, export_dir=str(tmp_path / "deliver"), notifiers=[notifier])
    recorder.open_run("dev:1", "登录流程")
    recorder.record("watchdog", "error", "出错弹窗", node="主界面")
    recorder.record("popup_branch", "warning", "网络异常", screenshot=False)

    _, summary = recorder.finalize("failed", error="boom")

    assert len(notifier.pushed) == 1  # one per run, never one per finding
    pushed = notifier.pushed[0]
    assert pushed["task"] == "登录流程" and pushed["device"] == "dev:1"
    assert pushed["status"] == "failed" and pushed["error"] == "boom"
    assert pushed["counts"] == {"error": 2, "warning": 1}  # incl. the task_failure finding
    assert [f["type"] for f in pushed["findings"]] == ["watchdog", "popup_branch", "task_failure"]
    assert pushed["findings"][0] == {
        "type": "watchdog", "severity": "error", "message": "出错弹窗"
    }
    # the report/export the message points at are the ones finalize just wrote
    assert pushed["report_path"] == summary["report_path"]
    assert pushed["export_path"] == summary["export_path"]
    assert Path(pushed["export_path"]).is_file()


def test_finalize_previews_at_most_three_findings(tmp_path):
    notifier = FakeNotifier()
    recorder = make_recorder(tmp_path, notifiers=[notifier])
    recorder.open_run("dev1", "t")
    for i in range(5):
        recorder.record("watchdog", "warning", f"msg{i}", screenshot=False)

    recorder.finalize("completed")

    assert [f["message"] for f in notifier.pushed[0]["findings"]] == ["msg0", "msg1", "msg2"]
    assert notifier.pushed[0]["counts"] == {"warning": 5}  # counts still cover all of them


def test_finalize_applies_each_notifiers_filter(tmp_path):
    quiet = FakeNotifier(min_findings=2)          # one finding is below its bar
    only_failed = FakeNotifier(on_status=["failed"])
    always = FakeNotifier()
    recorder = make_recorder(tmp_path, notifiers=[quiet, only_failed, always])
    recorder.open_run("dev1", "t")
    recorder.record("watchdog", "warning", "x", screenshot=False)

    recorder.finalize("completed")

    assert quiet.pushed == [] and only_failed.pushed == []
    assert len(always.pushed) == 1


def test_clean_run_pushes_nothing(tmp_path):
    notifier = FakeNotifier()
    recorder = make_recorder(tmp_path, notifiers=[notifier])
    recorder.open_run("dev1", "t")

    _, summary = recorder.finalize("completed")

    assert notifier.pushed == []
    assert summary["report_path"] is None


def test_notifier_failure_does_not_affect_the_run(tmp_path):
    broken = FakeNotifier(boom=True)
    healthy = FakeNotifier()
    recorder = make_recorder(tmp_path, notifiers=[broken, healthy])
    recorder.open_run("dev1", "t")
    recorder.record("watchdog", "error", "x", screenshot=False)

    findings, summary = recorder.finalize("failed", error="boom")

    # the push blew up, the run's own product is untouched
    assert len(findings) == 2 and summary["report_path"]
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert len(report["findings"]) == 2
    assert len(healthy.pushed) == 1  # and a later notifier still got its message


def test_no_notifiers_configured_is_a_noop(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1", "t")
    recorder.record("watchdog", "error", "x", screenshot=False)

    findings, summary = recorder.finalize("completed")

    assert recorder.notifiers == []
    assert len(findings) == 1 and summary["report_path"]


# ---------- human-readable HTML report ----------


def test_html_report_written_beside_json_with_run_meta(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev:1", task_name="登录流程")
    recorder.record("watchdog", "error", "出现错误弹窗", node="主界面")

    _, summary = recorder.finalize("completed")

    html_path = Path(summary["report_html_path"])
    assert html_path.name == "report.html"
    assert html_path.parent == Path(summary["report_path"]).parent
    page = html_path.read_text(encoding="utf-8")
    assert page.startswith("<!DOCTYPE html>") and page.rstrip().endswith("</html>")
    for token in ("登录流程", "dev:1", "completed", "出现错误弹窗", "主界面", "error"):
        assert token in page
    assert "<style>" in page  # styling is inlined
    assert "http://" not in page and "https://" not in page  # no external asset


def test_html_report_escapes_dynamic_text(tmp_path):
    recorder = make_recorder(tmp_path, monitor=FakeTailMonitor(["E/g: a < b && c > d"]))
    recorder.open_run("dev1", task_name="<b>task</b>")
    recorder.record("watchdog", "error", "<script>alert('x')</script> & more", screenshot=False)

    _, summary = recorder.finalize("failed", error="boom <img src=x>")

    page = Path(summary["report_html_path"]).read_text(encoding="utf-8")
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; more" in page
    assert "&lt;b&gt;task&lt;/b&gt;" in page
    assert "boom &lt;img src=x&gt;" in page
    assert "a &lt; b &amp;&amp; c &gt; d" in page


def test_html_report_embeds_evidence_by_relative_path(tmp_path):
    recorder = make_recorder(
        tmp_path,
        monitor=FakeTailMonitor(["06-12 10:00:00.000 I/game(1): tail"]),
        screen_recorder=FakeScreenRecorder(),
    )
    recorder.open_run("dev1", "demo")
    recorder.add_timeline("node_recognized", node="主界面")
    recorder.record("watchdog", "error", "msg", node="主界面", ui_dump=True)

    _, summary = recorder.finalize("completed")

    run_dir = Path(summary["run_dir"])
    page = Path(summary["report_html_path"]).read_text(encoding="utf-8")
    # screenshot as <img>, video as <video>, both relative and resolvable
    assert '<img src="01_watchdog.png"' in page
    assert (run_dir / "01_watchdog.png").is_file()
    assert '<video controls preload="metadata" src="video/seg_1_0.mp4">' in page
    assert (run_dir / "video/seg_1_0.mp4").is_file()
    # non-media evidence degrades to links, still relative
    assert '<a href="01_watchdog.xml">' in page
    assert '<a href="01_watchdog_logcat.log">' in page
    assert '<a href="01_watchdog_timeline.json">' in page
    # no absolute path from this machine leaked into the page
    assert str(run_dir) not in page


def test_html_report_inlines_log_and_flow_as_details(tmp_path):
    lines = [f"06-12 10:00:{i:02d}.000 I/game(1): line {i}" for i in range(3)]
    recorder = make_recorder(tmp_path, monitor=FakeTailMonitor(lines))
    recorder.open_run("dev1", "demo")
    recorder.add_timeline("node_recognized", node="主界面", channel="ui_text")
    recorder.record("watchdog", "error", "msg", screenshot=False)

    _, summary = recorder.finalize("completed")

    page = Path(summary["report_html_path"]).read_text(encoding="utf-8")
    assert page.count("<details>") >= 2
    assert "logcat 片段（问题前）" in page and "流程时间线（问题前）" in page
    assert "line 2" in page
    assert "node_recognized" in page and "主界面" in page


def test_html_report_goes_into_export_zip(tmp_path):
    export_target = tmp_path / "deliver"
    recorder = make_recorder(tmp_path, export_dir=str(export_target))
    recorder.open_run("dev1", "demo")
    recorder.record("watchdog", "error", "出错")

    _, summary = recorder.finalize("failed", error="boom")

    with zipfile.ZipFile(summary["export_path"]) as zf:
        names = set(zf.namelist())
        assert {"report.json", "report.html"} <= names
        page = zf.read("report.html").decode("utf-8")
        # every media/link reference in the page resolves inside the archive
        for rel in re.findall(r'(?:src|href)="([^"]+)"', page):
            assert rel in names


def test_html_render_failure_leaves_report_json_intact(tmp_path, monkeypatch):
    def boom(report):
        raise RuntimeError("render blew up")

    monkeypatch.setattr("task.findings.render_report_html", boom)
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1", "demo")
    recorder.record("watchdog", "error", "msg", screenshot=False)

    findings, summary = recorder.finalize("completed")

    assert summary["report_html_path"] is None
    assert len(findings) == 1
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert report["findings"][0]["message"] == "msg"


def test_render_report_html_handles_empty_run():
    page = render_report_html(
        {
            "task": "冒烟",
            "device": "dev1",
            "started_at": "2026-08-05T10:00:00",
            "finished_at": "2026-08-05T10:01:00",
            "status": "completed",
            "error": None,
            "counts": {},
            "findings": [],
        }
    )

    assert "没有记录到任何测试发现" in page
    assert "冒烟" in page and "dev1" in page and "2026-08-05T10:00:00" in page
    assert "<img" not in page and "<video" not in page


def test_render_report_html_shows_node_health_table():
    page = render_report_html(
        {
            "task": "冒烟",
            "status": "completed",
            "counts": {},
            "findings": [],
            "node_stats": {
                "健康节点": {"poll_rounds": 1, "direct_hits": 1, "popup_assisted_hits": 0,
                             "back_assisted_hits": 0, "recovery_hits": 0,
                             "timeout_recoveries": 0, "drift_count": 0, "drift_px": []},
                "腐烂节点": {"poll_rounds": 9, "direct_hits": 0, "popup_assisted_hits": 0,
                             "back_assisted_hits": 1, "recovery_hits": 0,
                             "timeout_recoveries": 3, "drift_count": 2, "drift_px": [80.0]},
            },
        }
    )

    assert "节点健康度" in page
    assert "健康节点" in page and "腐烂节点" in page
    # Shaky nodes are highlighted and sorted to the top.
    assert page.index("腐烂节点") < page.index("健康节点")
    assert '<tr class="rot">' in page


def test_render_report_html_without_node_stats_skips_the_table():
    page = render_report_html({"status": "completed", "findings": []})

    assert "节点健康度" not in page


def test_render_report_html_survives_sparse_finding():
    # A finding missing optional keys must not break rendering.
    page = render_report_html({"status": "failed", "findings": [{"message": "只有消息"}]})

    assert "只有消息" in page
    assert "</html>" in page


# ---------- findings retention (prune old day-folders) ----------

def test_prune_old_runs_removes_stale_day_folders(tmp_path):
    base = tmp_path / "findings"
    today = date.today()
    fresh = today.strftime("%Y%m%d")
    stale = (today - timedelta(days=30)).strftime("%Y%m%d")
    for day in (fresh, stale):
        (base / day / "dev1" / "run").mkdir(parents=True)
        (base / day / "dev1" / "run" / "report.json").write_text("{}", encoding="utf-8")

    removed = prune_old_runs(base, retention_days=14)

    assert removed == 1
    assert (base / fresh).is_dir()
    assert not (base / stale).exists()


def test_prune_old_runs_disabled_and_ignores_non_date_dirs(tmp_path):
    base = tmp_path / "findings"
    old = (date.today() - timedelta(days=99)).strftime("%Y%m%d")
    (base / old / "dev1").mkdir(parents=True)
    (base / "exports").mkdir(parents=True)  # not a date folder, must be left alone

    assert prune_old_runs(base, retention_days=0) == 0  # disabled
    assert (base / old).is_dir()

    assert prune_old_runs(base, retention_days=7) == 1
    assert not (base / old).exists()
    assert (base / "exports").is_dir()


def test_prune_old_runs_missing_dir_is_noop(tmp_path):
    assert prune_old_runs(tmp_path / "nope", retention_days=14) == 0


# ---------- flight recorder: log fragment / timeline / screen history ----------


def test_recorder_attaches_log_fragment(tmp_path):
    lines = [f"06-12 10:00:{i:02d}.000 I/game(1): line {i}" for i in range(50)]
    recorder = make_recorder(tmp_path, monitor=FakeTailMonitor(lines))
    recorder.open_run("dev1")

    finding = recorder.record("watchdog", "error", "出错了")

    assert finding["log_excerpt"] == lines[-40:]  # inline cap
    log_file = finding["evidence"]["logcat"]
    assert log_file.endswith("01_watchdog_logcat.log")
    assert open(log_file, encoding="utf-8").read().splitlines() == lines


def test_recorder_attaches_timeline_window_only(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")
    recorder.add_timeline("node_recognized", node="旧节点")
    recorder._timeline[0]["mono"] -= 120  # age it out of the 60s window
    recorder.add_timeline("node_recognized", node="新节点", channel="ui_text")
    recorder.add_timeline("action", node="新节点", type="click", ok=True)

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert len(finding["recent_flow"]) == 2
    assert "新节点" in finding["recent_flow"][0] and "旧节点" not in str(finding["recent_flow"])
    timeline = json.loads(open(finding["evidence"]["timeline"], encoding="utf-8").read())
    assert [e["event"] for e in timeline["events"]] == ["node_recognized", "action"]


def test_recorder_history_frames_written_once_and_shared(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1")
    recorder.snapshot_history()
    recorder.snapshot_history()

    first = recorder.record("watchdog", "error", "a", screenshot=False)
    second = recorder.record("anomaly_node", "warning", "b", screenshot=False)

    assert len(first["evidence"]["history"]) == 2
    assert first["evidence"]["history"] == second["evidence"]["history"]
    for path in first["evidence"]["history"]:
        assert open(path, "rb").read() == FakeCapturer().png


def test_recorder_history_disabled_by_config(tmp_path):
    recorder = make_recorder(tmp_path, history=False)
    recorder.open_run("dev1")
    recorder.snapshot_history()

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert "history" not in finding["evidence"]


def test_recorder_history_degrades_on_capture_failure(tmp_path):
    recorder = make_recorder(tmp_path, capturer=FailingCapturer())
    recorder.open_run("dev1")
    recorder.snapshot_history()
    recorder.snapshot_history()  # already broken: no retry storm

    finding = recorder.record("watchdog", "error", "msg", screenshot=False)

    assert "history" not in finding["evidence"]


def test_logcat_tail_filters_to_recent_window(monkeypatch):
    tail_log = (
        "06-12 10:00:00.000 I/old(1): ancient\n"
        "06-12 10:01:00.000 I/old(1): too old\n"
        "06-12 10:01:40.000 I/game(2): recent A\n"
        "stack continuation without timestamp\n"
        "06-12 10:02:30.000 I/game(2): recent B\n"
    )
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", tail_log)
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60)

    assert lines == [
        "06-12 10:01:40.000 I/game(2): recent A",
        "stack continuation without timestamp",
        "06-12 10:02:30.000 I/game(2): recent B",
    ]


def test_logcat_tail_caps_lines_and_handles_no_timestamps(monkeypatch):
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", "alpha\nbeta\ngamma\n")
    monitor = LogcatMonitor(LOGGER)

    assert monitor.tail("dev1", max_lines=2) == ["beta", "gamma"]


def test_logcat_tail_none_when_disabled(monkeypatch):
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", "x\n")
    monitor = LogcatMonitor(LOGGER)
    monitor._disabled = True

    assert monitor.tail("dev1") is None


# ---------- evidence log: game-side W/E errors must survive ROM noise ----------


def install_fake_adb_channels(monkeypatch, date_out: str, context_out: str, priority_out):
    """Fake adb that answers the `*:W` priority fetch differently from the plain one.

    priority_out=None simulates the priority channel failing (adb non-zero).
    """
    commands: List[str] = []

    def fake_run(cmd, **kwargs):
        shell_cmd = cmd[-1]
        commands.append(shell_cmd)
        if shell_cmd.startswith("date"):
            return SimpleNamespace(returncode=0, stdout=date_out, stderr="")
        if "'*:W'" in shell_cmd:
            if priority_out is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout=priority_out, stderr="")
        return SimpleNamespace(returncode=0, stdout=context_out, stderr="")

    monkeypatch.setattr("perception.logcat_monitor.subprocess.run", fake_run)
    return commands


GAME_ERROR = (
    "06-12 10:01:00.000 E/[mygame] (16634): [ed.Rpc] happened error: "
    '{["code"] = 7202, ["msg"] = "[Code]: PlayerNotFound . Module: Battle"}'
)
GAME_WARN = "06-12 10:01:00.100 W/[mygame] (16634): battle check failed"


def noise_lines(start_ms: int, count: int) -> List[str]:
    """ROM chatter (I/D/V) — the stuff that used to evict the game's error."""
    levels = "IDV"
    return [
        f"06-12 10:01:{(start_ms + i) // 1000 % 60:02d}."
        f"{(start_ms + i) % 1000:03d} {levels[i % 3]}/KERNEL(0): noise {i}"
        for i in range(count)
    ]


def test_logcat_tail_keeps_game_error_under_noise_burst(monkeypatch):
    # 200 lines of ROM spam after the game's E/W lines: a plain last-N tail
    # would drop both (this is the 2026-08-11 evidence gap).
    context = "\n".join([GAME_ERROR, GAME_WARN, *noise_lines(1000, 200)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=20)

    assert len(lines) == 20
    assert GAME_ERROR in lines
    assert GAME_WARN in lines
    assert lines.index(GAME_ERROR) < lines.index(GAME_WARN)  # chronological order kept
    assert lines[-1].endswith("noise 199")  # newest context still there


def test_logcat_tail_merges_priority_channel_beyond_context_depth(monkeypatch):
    # The error is older than the context channel reaches; only the `*:W`
    # channel still carries it, and it must be merged back in chronologically.
    context = "\n".join(noise_lines(1000, 50)) + "\n"
    install_fake_adb_channels(
        monkeypatch, "06-12 10:00:00.000\n", context, GAME_ERROR + "\n" + GAME_WARN + "\n"
    )
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=300)

    assert GAME_ERROR in lines
    assert lines[0] == GAME_ERROR  # oldest timestamp sorts first
    assert lines[1] == GAME_WARN
    assert len([l for l in lines if "noise" in l]) == 50  # context untouched


def test_logcat_tail_does_not_duplicate_lines_present_in_both_channels(monkeypatch):
    context = "\n".join([GAME_ERROR, *noise_lines(1000, 5)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, GAME_ERROR + "\n")
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=300)

    assert lines.count(GAME_ERROR) == 1


def test_logcat_tail_degrades_when_priority_channel_fails(monkeypatch):
    context = "\n".join([GAME_ERROR, *noise_lines(1000, 5)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, None)
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=300)

    assert lines is not None and GAME_ERROR in lines  # context-only fallback


def test_logcat_tail_asks_for_a_priority_channel(monkeypatch):
    commands = install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", GAME_ERROR + "\n", "")
    monitor = LogcatMonitor(LOGGER)
    monitor.tail("dev1")

    assert any("'*:W'" in c for c in commands)
    assert any(c.startswith("logcat -d -v time -t") and "*:" not in c for c in commands)


def test_logcat_crash_detection_unaffected_by_mixed_levels(monkeypatch):
    mixed = "\n".join([*noise_lines(1000, 30), GAME_ERROR, GAME_WARN, CRASH_LOG]) + "\n"
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", mixed)
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")

    events = monitor.poll("dev1")

    # Business errors are evidence, not crashes: still exactly the crash + ANR.
    assert [e["type"] for e in events] == ["crash", "anr"]


def test_finding_inline_log_excerpt_keeps_game_error(tmp_path):
    monitor = FakeTailMonitor([GAME_ERROR, *noise_lines(1000, 200)])
    recorder = make_recorder(tmp_path, monitor=monitor)
    recorder.open_run("dev1")

    finding = recorder.record("anomaly_node", "error", "battle not started", screenshot=False)

    assert GAME_ERROR in finding["log_excerpt"]
    assert len(finding["log_excerpt"]) <= 40


# ---------- evidence log: E-level ROM/engine bursts must not evict the game ----------
#
# Second gap found on 2026-08-11: "drop V/D/I first" is useless when the spam is
# itself E level. Measured on real smoke-run evidence, an E/Unity
# avatar-skeleton burst filled all 300 lines with 5.5s of log and 21 distinct
# messages, and `E/[mygame]` never survived a file that contained such a burst.


def unity_burst(second: int, repeats: int) -> List[str]:
    """E-level engine spam: one message plus a stack, emitted over and over."""
    body = [
        "The input bones do not match the skeleton of the Avatar(char_npc_03).",
        "Please check if the Avatar is generated in optimized mode.",
        " #0 0x6da78801cc (libunity.so) ? 0x0",
        " #1 0x6da7e1812c (libunity.so) ? 0x0",
    ]
    return [
        f"06-12 10:01:{second:02d}.{(r * 4 + i) % 1000:03d} E/Unity   (28274): {msg}"
        for r in range(repeats)
        for i, msg in enumerate(body)
    ]


def codec_noise(second: int, count: int) -> List[str]:
    """E-level vendor codec chatter — a known-noise tag."""
    return [
        f"06-12 10:01:{second:02d}.{i % 1000:03d} E/QC2Interface(719): "
        f"config failed for param 0x{i:04x}"
        for i in range(count)
    ]


def test_evidence_keeps_business_error_under_e_level_burst(monkeypatch):
    context = "\n".join([GAME_ERROR, GAME_WARN, *unity_burst(10, 60)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=100)

    assert len(lines) == 100  # quotas re-rank, they never shrink the delivered budget
    assert GAME_ERROR in lines and GAME_WARN in lines
    assert lines.index(GAME_ERROR) < lines.index(GAME_WARN)


def test_evidence_repeat_quota_rescues_a_rare_message_from_its_own_tag_burst(monkeypatch):
    # `Unity` carries both the avatar spam and the game's own Debug.LogError, so
    # it must not be blacklisted as a tag. The per-message quota is what makes
    # the rare line win: the old level-only trim kept the newest 30 E lines,
    # which are all burst.
    rare = "06-12 10:01:05.000 E/Unity   (28274): LuaException: index a nil value (field 'reward')"
    context = "\n".join([rare, *unity_burst(10, 60)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=30)

    assert len(lines) == 30
    assert rare in lines
    assert len(set(lines)) >= 5  # every distinct message of the burst, not 30 copies of two


def test_evidence_known_noise_tag_ranks_below_unknown_tag(monkeypatch):
    unknown = "06-12 10:01:05.000 E/BrandNewSubsystem(42): shader compile failed"
    context = "\n".join([unknown, *codec_noise(10, 200)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    monitor = LogcatMonitor(LOGGER)

    lines = monitor.tail("dev1", seconds=60, max_lines=30)

    # An unlisted tag falls back to its level and outranks known ROM noise:
    # a stale noise list costs precision, never evidence.
    assert unknown in lines


def test_evidence_noise_tags_are_configurable(monkeypatch):
    chatty = [
        f"06-12 10:01:10.{i:03d} E/HouseKeeper(9): sweep {i}" for i in range(200)
    ]
    game = "06-12 10:01:05.000 E/BrandNewSubsystem(42): shader compile failed"
    context = "\n".join([game, *chatty]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    policy = EvidencePolicy.from_config({"extra_noise_tags": ["HouseKeeper"]})
    monitor = LogcatMonitor(LOGGER, evidence_policy=policy)

    lines = monitor.tail("dev1", seconds=60, max_lines=20)

    assert game in lines
    assert len(lines) == 20


def test_evidence_business_tags_are_configurable(monkeypatch):
    ours = "06-12 10:01:05.000 I/[myco](42): checkout flow entered"
    context = "\n".join([ours, *unity_burst(10, 60)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    policy = EvidencePolicy.from_config({"business_tags": ["[myco]"]})
    monitor = LogcatMonitor(LOGGER, evidence_policy=policy)

    # Even at I level a declared game tag outranks an E-level burst.
    assert ours in monitor.tail("dev1", seconds=60, max_lines=30)


def test_evidence_crash_marker_survives_an_e_level_burst(monkeypatch):
    fatal = "06-12 10:01:05.000 E/AndroidRuntime(123): FATAL EXCEPTION: main"
    context = "\n".join([fatal, *unity_burst(10, 60)]) + "\n"
    install_fake_adb_channels(monkeypatch, "06-12 10:00:00.000\n", context, "")
    monitor = LogcatMonitor(LOGGER)

    assert fatal in monitor.tail("dev1", seconds=60, max_lines=30)


def test_logcat_crash_detection_unaffected_by_e_level_burst(monkeypatch):
    mixed = "\n".join([*unity_burst(10, 60), *codec_noise(11, 50), CRASH_LOG]) + "\n"
    install_fake_adb(monkeypatch, "06-12 10:00:00.000\n", mixed)
    monitor = LogcatMonitor(LOGGER)
    monitor.start("dev1")

    events = monitor.poll("dev1")

    assert [e["type"] for e in events] == ["crash", "anr"]


def test_evidence_tag_parsing_handles_slashes_and_brackets():
    lines = [
        "06-12 10:01:00.000 E/QCNEJ/WlanStaInfoRelay(719): relay down",
        "06-12 10:01:00.001 E/[mygame] (16634): server code 7202",
        "    continuation without its own header",
    ]

    assert [item[3] for item in annotate_lines(lines)] == [
        "QCNEJ/WlanStaInfoRelay", "[mygame]", "[mygame]",  # continuation inherits
    ]


def test_finding_inline_excerpt_survives_e_level_burst(tmp_path):
    monitor = FakeTailMonitor([GAME_ERROR, *unity_burst(10, 60)])
    recorder = make_recorder(tmp_path, monitor=monitor)
    recorder.open_run("dev1")

    finding = recorder.record("anomaly_node", "error", "battle not started", screenshot=False)

    assert GAME_ERROR in finding["log_excerpt"]
    assert len(finding["log_excerpt"]) <= 40


def test_engine_failure_carries_flight_recorder_context(tmp_path):
    task = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "click", "params": {"x": 1, "y": 2}},
                "next": ["boom"],
                "timeout_ms": 0,
            },
            "boom": {
                "recognition": {"type": "ui_text", "expected": "崩"},
                "action": {"type": "key", "params": {"keycode": 4}},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    game_log = [f"06-12 10:00:{i:02d}.000 E/game(1): err {i}" for i in range(3)]
    recorder = make_recorder(tmp_path, monitor=FakeTailMonitor(game_log))
    engine = make_engine(
        FakeHub({"崩": hit()}), executor=FakeExecutor(fail_types={"key"}), recorder=recorder
    )

    result = engine.run("dev1", task, task_name="flight")

    assert result["status"] == "failed"
    failure = result["findings"][-1]
    assert failure["type"] == "task_failure"
    # in-game log fragment travels inline and on disk
    assert failure["log_excerpt"] == game_log
    assert failure["evidence"]["logcat"].endswith("_logcat.log")
    # the last minute of flow: both nodes and both actions, in order
    events = [line.split(" ", 1)[1].split(" ")[0] for line in failure["recent_flow"]]
    assert events == ["node_recognized", "action", "node_recognized", "action"]
    assert "ok=False" in failure["recent_flow"][-1]
    # one settled frame buffered after the successful first step
    assert len(failure["evidence"]["history"]) == 1
    assert failure["evidence"]["timeline"].endswith("_timeline.json")


# ---------- blank_screen recognition channel ----------


def png_of(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_blank_screen_hits_on_uniform_frame():
    from PIL import Image

    capturer = FakeCapturer(png_of(Image.new("L", (40, 40), color=12)))
    hub = RecognizerHub(FakeDumpMatcher(), None, capturer, LOGGER)

    result = hub.recognize("dev1", {"type": "blank_screen"})

    assert result is not None
    assert result["channel"] == "blank_screen"
    assert result["score"] < 8.0


def test_blank_screen_misses_on_busy_frame():
    from PIL import Image

    img = Image.new("L", (40, 40))
    img.putdata([(i * 37) % 256 for i in range(40 * 40)])
    hub = RecognizerHub(FakeDumpMatcher(), None, FakeCapturer(png_of(img)), LOGGER)

    assert hub.recognize("dev1", {"type": "blank_screen"}) is None


# ---------- engine integration ----------


def on_timeout_task() -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "目标"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
                "on_timeout": "recover",
            },
            "recover": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_engine_records_timeout_recovery_finding(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"目标": None}), recorder=recorder)

    result = engine.run("dev1", on_timeout_task(), task_name="demo")

    assert result["status"] == "completed"
    # The authored on_timeout owns the stall, so the BACK fallback never fires:
    # the recovery is the only finding.
    assert [f["type"] for f in result["findings"]] == ["timeout_recovery"]
    finding = result["findings"][0]
    assert finding["severity"] == "warning"
    assert finding["node"] == "start"
    assert "screenshot" in finding["evidence"]
    assert result["report"]["counts"] == {"warning": 1}
    report = json.loads(open(result["report"]["report_path"], encoding="utf-8").read())
    assert report["task"] == "demo"


def test_engine_writes_node_stats_into_report(tmp_path):
    recorder = make_recorder(tmp_path)
    task = on_timeout_task()
    engine = make_engine(FakeHub({"目标": None}), recorder=recorder)

    result = engine.run("dev1", task, task_name="demo")

    assert result["node_stats"]["start"]["timeout_recoveries"] == 1
    report = json.loads(open(result["report"]["report_path"], encoding="utf-8").read())
    # Health telemetry is a top-level report field, not a finding.
    assert report["node_stats"]["start"]["timeout_recoveries"] == 1
    assert report["node_stats"]["recover"]["recovery_hits"] == 1
    assert all(f["type"] != "node_stats" for f in report["findings"])


def rot_task() -> Dict:
    """A node whose anchor never matches, looping through its timeout escape.

    Its on_timeout also keeps the BACK fallback out of the picture, so the run's
    findings are purely about the rotting anchor.
    """
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["target"],
                "timeout_ms": 0,
            },
            "target": {
                "recognition": {"type": "ui_text", "expected": "目标"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
                "on_timeout": "recover",
            },
            "recover": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["target"],
                "timeout_ms": 0,
            },
        },
    }


def test_engine_flags_anchor_rot_suspect_once_per_node(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"目标": None}), recorder=recorder, max_steps=4)

    result = engine.run("dev1", rot_task(), task_name="demo")

    assert result["node_stats"]["target"]["timeout_recoveries"] >= 2
    suspects = [f for f in result["findings"] if f["type"] == "anchor_rot_suspect"]
    # One per node per run, no matter how many times it stalled.
    assert len(suspects) == 1
    assert suspects[0]["node"] == "target"
    assert suspects[0]["severity"] == "warning"
    assert suspects[0]["extra"]["timeout_recoveries"] >= 2


def test_anchor_rot_suspect_threshold_is_configurable(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(
        FakeHub({"目标": None}), recorder=recorder, max_steps=4,
        engine_config={"rot_suspect_timeouts": 0},  # check disabled
    )

    result = engine.run("dev1", rot_task(), task_name="demo")

    assert all(f["type"] != "anchor_rot_suspect" for f in result["findings"])
    # Statistics are still collected even with the verdict turned off.
    assert result["node_stats"]["target"]["timeout_recoveries"] >= 2


def test_engine_records_anomaly_node_finding(tmp_path):
    task = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["popup"],
                "timeout_ms": 0,
            },
            "popup": {
                "recognition": {"type": "ui_text", "expected": "广告"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
                "finding": "意外广告弹窗出现",
            },
        },
    }
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"广告": hit()}), recorder=recorder)

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    assert [f["type"] for f in result["findings"]] == ["anomaly_node"]
    assert result["findings"][0]["message"] == "意外广告弹窗出现"
    assert result["findings"][0]["node"] == "popup"


def watchdog_task(fail_task: bool) -> Dict:
    return {
        "entry": "start",
        "watchdogs": [
            {"type": "ui_text", "expected": "错误", "severity": "error", "fail_task": fail_task}
        ],
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["finish"],
                "timeout_ms": 0,
            },
            "finish": {
                "recognition": {"type": "ui_text", "expected": "完成"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_engine_watchdog_records_once_and_continues(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"完成": hit(), "错误": hit(50, 60)}), recorder=recorder)

    result = engine.run("dev1", watchdog_task(fail_task=False))

    assert result["status"] == "completed"
    # hit after both steps, but recorded only once per run
    assert [f["type"] for f in result["findings"]] == ["watchdog"]
    assert result["findings"][0]["severity"] == "error"


def test_engine_watchdog_fail_task_aborts(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"完成": hit(), "错误": hit()}), recorder=recorder)

    result = engine.run("dev1", watchdog_task(fail_task=True))

    assert result["status"] == "failed"
    assert "Watchdog triggered at node 'start'" in result["error"]
    assert [f["type"] for f in result["findings"]] == ["watchdog", "task_failure"]


def test_engine_watchdog_explains_recognition_timeout(tmp_path):
    task = {
        "entry": "start",
        "watchdogs": [{"type": "ui_text", "expected": "错误", "fail_task": True}],
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "目标"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"目标": None, "错误": hit()}), recorder=recorder)

    result = engine.run("dev1", task)

    assert result["status"] == "failed"
    # the watchdog diagnosis replaces the generic timeout message
    assert result["error"].startswith("Watchdog triggered")
    assert "watchdog" in [f["type"] for f in result["findings"]]


class SequencedWatchdogHub:
    """always-hits flow nodes; the watchdog text follows a present/absent
    schedule across successive checks, simulating a transient toast."""

    def __init__(self, flow_hits: Dict[str, Optional[Dict]], expected: str, schedule: List[bool]):
        self.flow_hits = flow_hits
        self.expected = expected
        self.schedule = list(schedule)
        self.wd_calls = 0
        self.capturer = FakeCapturer()

    def recognize(self, device_id: str, spec: Dict, **kwargs) -> Optional[Dict]:
        if spec.get("type") == "always":
            return {"center": None, "text": "", "score": 1.0, "channel": "always"}
        exp = spec.get("expected")
        if exp == self.expected:
            present = self.schedule[self.wd_calls] if self.wd_calls < len(self.schedule) else False
            self.wd_calls += 1
            return hit(50, 60) if present else None
        scripted = self.flow_hits.get(exp)
        return dict(scripted) if scripted else None


def test_engine_watchdog_two_shot_catches_late_toast(tmp_path):
    # Absent on the transient shot, present on the settled shot: the old single
    # check (transient only) would miss it; the second shot catches it.
    hub = SequencedWatchdogHub({"完成": hit()}, expected="错误", schedule=[False, True])
    recorder = make_recorder(tmp_path)
    engine = make_engine(hub, recorder=recorder)

    result = engine.run("dev1", watchdog_task(fail_task=False))

    assert result["status"] == "completed"
    assert [f["type"] for f in result["findings"]] == ["watchdog"]
    assert hub.wd_calls == 4  # two shots per step over two steps


class PinningCapturer:
    """Hands out a uniform 'toast' frame for capture_image (the detection /
    evidence frame) and a distinct payload for capture_png_bytes (a stale
    re-capture), so a test can prove the finding pins the detection frame."""

    def __init__(self):
        from PIL import Image

        self.detect_frame = Image.new("L", (8, 8), color=3)  # uniform -> blank_screen hit
        self.encoded: Optional[bytes] = None
        self.png_calls = 0

    def capture_image(self, device_id: str):
        return self.detect_frame

    def capture_png_bytes(self, device_id: str) -> bytes:
        self.png_calls += 1
        return b"STALE-RECAPTURE"

    def encode_png(self, image) -> bytes:
        self.encoded = png_of(image)
        return self.encoded


def test_engine_watchdog_evidence_pins_detection_frame(tmp_path):
    cap = PinningCapturer()
    task = {
        "entry": "start",
        "watchdogs": [{"type": "blank_screen", "threshold": 8.0}],
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    hub = RecognizerHub(FakeDumpMatcher(), None, cap, LOGGER)
    recorder = make_recorder(tmp_path, capturer=cap, history=False)
    engine = make_engine(hub, recorder=recorder)

    result = engine.run("dev1", task)

    assert result["status"] == "completed"
    finding = result["findings"][0]
    assert finding["type"] == "watchdog"
    # evidence is the exact frame that tripped the check, not a stale re-capture
    assert open(finding["evidence"]["screenshot"], "rb").read() == cap.encoded
    assert cap.png_calls == 0


def test_engine_failure_preserves_scene_evidence(tmp_path, sample_task):
    recorder = make_recorder(tmp_path)
    engine = make_engine(
        FakeHub({"设置": hit()}), executor=FakeExecutor(fail_types={"click"}), recorder=recorder
    )

    result = engine.run("dev1", sample_task)

    assert result["status"] == "failed"
    failure = result["findings"][-1]
    assert failure["type"] == "task_failure"
    assert "screenshot" in failure["evidence"] and "ui_dump" in failure["evidence"]
    for path in failure["evidence"].values():
        assert open(path, "rb").read()


def test_engine_logcat_events_become_findings(tmp_path, sample_task):
    recorder = make_recorder(tmp_path)
    crash_event = {
        "type": "crash",
        "severity": "critical",
        "line": "FATAL EXCEPTION: main",
        "excerpt": ["FATAL EXCEPTION: main", "java.lang.NullPointerException"],
    }
    logcat = FakeLogcat([[crash_event]])
    engine = make_engine(FakeHub({"设置": hit()}), recorder=recorder, logcat=logcat)

    result = engine.run("dev1", sample_task)

    assert logcat.started_on == "dev1"
    assert result["status"] == "completed"
    crash = [f for f in result["findings"] if f["type"] == "crash"][0]
    assert crash["severity"] == "critical"
    assert crash["node"] == "start"  # attributed to the step it was detected after
    assert crash["extra"]["excerpt"][1] == "java.lang.NullPointerException"


def test_engine_without_recorder_keeps_result_shape(sample_task):
    engine = make_engine(FakeHub({"设置": hit()}))

    result = engine.run("dev1", sample_task)

    assert result["status"] == "completed"
    assert result["findings"] == []
    assert result["report"] is None


# ---------- loader validation ----------


def valid_qa_task() -> Dict:
    return {
        "entry": "start",
        "watchdogs": [
            {"type": "ui_text", "expected": "错误", "severity": "critical", "fail_task": True},
            {"type": "blank_screen", "threshold": 6.0},
        ],
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "finding": {"severity": "warning", "message": "异常分支"},
            },
        },
    }


def test_loader_accepts_watchdogs_and_finding():
    validate_task(valid_qa_task())

    task = valid_qa_task()
    task["nodes"]["start"]["finding"] = "字符串形式也可以"
    validate_task(task)


def test_loader_accepts_skip_to_and_on_finding():
    task = valid_qa_task()
    task["on_finding"] = "start"
    task["watchdogs"][0]["skip_to"] = "start"
    validate_task(task)


def test_loader_accepts_blank_screen_node_recognition():
    task = valid_qa_task()
    task["nodes"]["start"]["recognition"] = {"type": "blank_screen", "threshold": 5}
    validate_task(task)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda t: t.update(watchdogs="nope"), "must be a list"),
        (lambda t: t["watchdogs"].append({"type": "always"}), "unsupported type"),
        (lambda t: t["watchdogs"].append({"type": "ui_text"}), "requires non-empty 'expected'"),
        (lambda t: t["watchdogs"].append({"type": "ocr", "expected": "x", "severity": "fatal"}), "invalid severity"),
        (lambda t: t["watchdogs"].append({"type": "ocr", "expected": "x", "fail_task": "yes"}), "must be a boolean"),
        (lambda t: t["watchdogs"].append({"type": "ocr", "expected": "x", "roi": [1, 2]}), "roi"),
        (lambda t: t["nodes"]["start"].update(finding=123), "must be a string or object"),
        (lambda t: t["nodes"]["start"].update(finding=""), "must be non-empty"),
        (lambda t: t["nodes"]["start"].update(finding={}), "requires non-empty 'message'"),
        (lambda t: t["nodes"]["start"].update(finding={"message": "x", "severity": "huge"}), "invalid severity"),
        (lambda t: t["watchdogs"].append({"type": "ocr", "expected": "x", "skip_to": "ghost"}), "unknown skip_to node 'ghost'"),
        (lambda t: t["watchdogs"].append({"type": "ocr", "expected": "x", "skip_to": ""}), "must be a non-empty node name"),
        (lambda t: t.update(on_finding="ghost"), "'on_finding' references unknown node 'ghost'"),
    ],
)
def test_loader_rejects_invalid_qa_fields(mutate, fragment):
    task = valid_qa_task()
    mutate(task)
    with pytest.raises(TaskValidationError, match=fragment):
        validate_task(task)


# ---------- bug-skip recovery (jump only after a reported bug, never on a stall) ----------


def skip_task(watchdog_extra=None, on_finding=None, watchdogs=True) -> Dict:
    """start --(normal)--> normal_finish, with a 'recovery' detour. The watchdog
    text '错误' (when present in the hub) is the reported bug; 'normal_finish'
    is only reachable when nothing trips."""
    wd = {"type": "ui_text", "expected": "错误", "severity": "error"}
    if watchdog_extra:
        wd.update(watchdog_extra)
    task: Dict = {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["normal_finish"],
                "timeout_ms": 0,
            },
            "normal_finish": {
                "recognition": {"type": "ui_text", "expected": "正常完成"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
            "recovery": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }
    if watchdogs:
        task["watchdogs"] = [wd]
    if on_finding:
        task["on_finding"] = on_finding
    return task


def test_engine_watchdog_skip_to_recovers_past_bug(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"错误": hit(50, 60)}), recorder=recorder)

    result = engine.run("dev1", skip_task(watchdog_extra={"skip_to": "recovery"}))

    assert result["status"] == "completed"
    # jumped to recovery after reporting the bug, never reached normal_finish
    assert [s["node"] for s in result["steps"]] == ["start", "recovery"]
    assert [f["type"] for f in result["findings"]] == ["watchdog"]


def test_engine_on_finding_global_recovers(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"错误": hit(50, 60)}), recorder=recorder)

    # watchdog has no skip_to; the task-level on_finding is the fallback target
    result = engine.run("dev1", skip_task(on_finding="recovery"))

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "recovery"]
    assert [f["type"] for f in result["findings"]] == ["watchdog"]


def test_engine_skip_to_overrides_fail_task(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"错误": hit(50, 60)}), recorder=recorder)

    # both set: recover (skip_to) wins over abort (fail_task)
    result = engine.run(
        "dev1", skip_task(watchdog_extra={"skip_to": "recovery", "fail_task": True})
    )

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "recovery"]
    assert [f["type"] for f in result["findings"]] == ["watchdog"]


def test_engine_logcat_crash_skips_to_on_finding(tmp_path):
    recorder = make_recorder(tmp_path)
    crash_event = {
        "type": "crash",
        "severity": "critical",
        "line": "FATAL EXCEPTION: main",
        "excerpt": ["FATAL EXCEPTION: main"],
    }
    logcat = FakeLogcat([[crash_event]])
    engine = make_engine(
        FakeHub({}), recorder=recorder, logcat=logcat,
    )

    result = engine.run("dev1", skip_task(on_finding="recovery", watchdogs=False))

    assert result["status"] == "completed"
    assert [s["node"] for s in result["steps"]] == ["start", "recovery"]
    assert [f["type"] for f in result["findings"]] == ["crash"]


def test_engine_stall_without_bug_never_skips(tmp_path):
    # The core guarantee: a bare recognition timeout (no watchdog/crash) does
    # NOT skip, even with on_finding set — a stall alone is not a reported bug.
    recorder = make_recorder(tmp_path)
    task = skip_task(on_finding="recovery", watchdogs=False)
    task["nodes"]["start"]["next"] = ["target"]
    task["nodes"]["target"] = {
        "recognition": {"type": "ui_text", "expected": "目标"},  # never hits -> timeout
        "action": {"type": "none"},
        "next": [],
        "timeout_ms": 0,
    }
    engine = make_engine(FakeHub({}), recorder=recorder)

    result = engine.run("dev1", task)

    assert result["status"] == "failed"
    assert "Recognition timeout" in result["error"]
    # never diverted to the recovery node
    assert [s["node"] for s in result["steps"]] == ["start"]
    assert all(f["type"] != "bug_skip" for f in result["findings"])


# ---------- live progress: timeline tail + per-run run.log ----------


def test_timeline_tail_returns_the_most_recent_events(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.open_run("dev1", task_name="demo")
    for i in range(5):
        recorder.add_timeline("node_recognized", node=f"n{i}")

    tail = recorder.timeline_tail(2)

    assert [e["detail"]["node"] for e in tail] == ["n3", "n4"]
    # Shallow copies: mutating the caller's dict must not corrupt the recorder.
    tail[0]["event"] = "tampered"
    assert recorder.timeline_tail(2)[0]["event"] == "node_recognized"
    assert recorder.timeline_tail(0) == []


def test_engine_recent_events_delegates_to_the_recorder(tmp_path, sample_task):
    recorder = make_recorder(tmp_path)
    engine = make_engine(FakeHub({"设置": hit()}), recorder=recorder)

    engine.run("dev1", sample_task, task_name="demo")

    events = engine.recent_events(3)
    assert events and all("event" in e for e in events)
    assert any(e["event"] == "node_recognized" for e in events)


def _run_log_task() -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "ui_text", "expected": "设置"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
                "timeout_ms": 0,
                "post_delay_ms": 1,  # produces a DEBUG line
            },
        },
    }


def _run_log_logger(name: str) -> logging.Logger:
    """A DEBUG-level logger of its own, so the file handler sees DEBUG records."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    return logger


def test_engine_writes_run_log_next_to_the_report(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = TaskEngine(
        FakeHub({"设置": hit(612, 388)}), FakeExecutor(),
        _run_log_logger("run_log_on"), findings_recorder=recorder,
    )

    engine.run("dev1", _run_log_task(), task_name="demo")

    run_log = recorder.run_dir / "run.log"
    assert run_log.is_file()
    text = run_log.read_text(encoding="utf-8")
    assert "[step 1] node 'start' recognized via ui_text" in text
    assert "[step 1] action click (612, 388) ok" in text
    # DEBUG detail the console never showed still lands in the file.
    assert "DEBUG" in text and "post_delay 1ms" in text
    # The handler is detached again: a later log line must not reopen the file.
    assert not any(isinstance(h, logging.FileHandler) for h in engine.logger.handlers)


def test_run_log_can_be_switched_off(tmp_path):
    recorder = make_recorder(tmp_path)
    engine = TaskEngine(
        FakeHub({"设置": hit()}), FakeExecutor(),
        _run_log_logger("run_log_off"), findings_recorder=recorder, run_log=False,
    )

    engine.run("dev1", _run_log_task(), task_name="demo")

    assert not (recorder.run_dir / "run.log").exists()


def test_run_log_is_skipped_without_a_recorder(tmp_path):
    """No findings recorder = no run folder to put it in; never an error."""
    engine = TaskEngine(FakeHub({"设置": hit()}), FakeExecutor(), _run_log_logger("run_log_none"))

    result = engine.run("dev1", _run_log_task(), task_name="demo")

    assert result["status"] == "completed"
