"""RollingPcapRecorder: on-device tcpdump packet-capture evidence channel.

Mirrors tests/test_findings.py's screenrecord fakes — no real device, all
subprocess calls mocked. Covers preflight (su/direct/latch-off), segment
rotation, collect() pull de-dup, broken-state no-ops, BPF/snaplen command
construction, and stop() cleanup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from perception.pcap_recorder import RollingPcapRecorder

LOGGER = logging.getLogger("test")


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


def install_fake_tcpdump(monkeypatch, version_ok=True, su_ok=True, timeout_ok=True):
    """Patch subprocess in pcap_recorder; returns a state dict of recorded calls."""
    state = {"popen": [], "run": [], "procs": []}

    def fake_popen(cmd, **kwargs):
        state["popen"].append(cmd)
        proc = FakePopen()
        state["procs"].append(proc)
        return proc

    def fake_run(cmd, **kwargs):
        state["run"].append(cmd)
        shell_cmd = cmd[-1] if "shell" in cmd else ""
        if "echo ok" in shell_cmd:  # device-side `timeout` probe
            if timeout_ok:
                return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            return SimpleNamespace(returncode=127, stdout="", stderr="timeout: not found")
        if "--version" in shell_cmd:
            is_su = shell_cmd.startswith("su -c")
            if is_su and not su_ok:
                return SimpleNamespace(returncode=127, stdout="", stderr="su: not found")
            if version_ok:
                return SimpleNamespace(
                    returncode=0,
                    stdout="tcpdump version 4.99.1\nlibpcap version 1.10.1",
                    stderr="",
                )
            return SimpleNamespace(returncode=127, stdout="", stderr="tcpdump: not found")
        if "pull" in cmd:
            local = Path(cmd[-1])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"pcap-data")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("perception.pcap_recorder.subprocess.Popen", fake_popen)
    monkeypatch.setattr("perception.pcap_recorder.subprocess.run", fake_run)
    monkeypatch.setattr("perception.pcap_recorder.STOP_SETTLE_S", 0)
    return state


def _popen_shell(cmd):
    """The device command string of a captured Popen call (the last element)."""
    return cmd[-1]


# ---------- preflight ----------


def test_start_probes_su_first_and_launches_segment(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    assert rec._broken is False
    assert rec._su_prefix == "su"
    # First run call is the su version probe.
    assert any("--version" in c[-1] and c[-1].startswith("su -c") for c in state["run"])
    # Leftover pcap files cleaned before capture starts.
    assert any("rm -f" in c[-1] and ".pcap" in c[-1] for c in state["run"])
    # One capture segment launched, wrapped in su, writing segment 0.
    assert len(state["popen"]) == 1
    shell = _popen_shell(state["popen"][0])
    assert shell.startswith("su -c '") and "tcpdump" in shell
    assert "/data/local/tmp/ga_pcap_0.pcap" in shell


def test_start_falls_back_to_direct_when_no_root(monkeypatch):
    state = install_fake_tcpdump(monkeypatch, su_ok=False)
    rec = RollingPcapRecorder(LOGGER)  # su_mode defaults to auto
    rec.start("dev1")

    assert rec._broken is False
    assert rec._su_prefix == "direct"
    shell = _popen_shell(state["popen"][0])
    assert not shell.startswith("su -c")  # direct tcpdump, no su wrap
    assert shell.startswith("timeout ") and " tcpdump " in shell


def test_start_latches_off_when_tcpdump_missing(monkeypatch):
    install_fake_tcpdump(monkeypatch, version_ok=False, su_ok=False)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    assert rec._broken is True
    # No capture ever launched; all methods now no-op without raising.
    rec.tick()
    assert rec.collect("anywhere") == []
    rec.stop()


def test_su_mode_direct_skips_su_probe(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER, su_mode="direct")
    rec.start("dev1")

    assert rec._su_prefix == "direct"
    assert not any(c[-1].startswith("su -c") for c in state["run"])


def test_su_mode_su_only_latches_off_without_root(monkeypatch):
    install_fake_tcpdump(monkeypatch, su_ok=False)
    rec = RollingPcapRecorder(LOGGER, su_mode="su")
    rec.start("dev1")

    assert rec._broken is True  # su required but unavailable -> off, no direct try


# ---------- command construction ----------


def test_capture_command_carries_bpf_and_snaplen(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(
        LOGGER, snaplen=96, bpf_filter="tcp and not port 5555", su_mode="direct"
    )
    rec.start("dev1")

    shell = _popen_shell(state["popen"][0])
    assert "-U" in shell  # packet-buffered so segments are pullable any time
    assert "-s 96" in shell
    assert "-w /data/local/tmp/ga_pcap_0.pcap" in shell
    assert "tcp and not port 5555" in shell


def test_custom_tcpdump_path_used(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER, tcpdump_path="/data/local/tmp/tcpdump", su_mode="direct")
    rec.start("dev1")

    shell = _popen_shell(state["popen"][0])
    assert "/data/local/tmp/tcpdump -U" in shell


# ---------- self-limiting capture (orphan guard) ----------


def test_capture_self_limits_past_the_segment_length(monkeypatch):
    state = install_fake_tcpdump(monkeypatch, su_ok=False)  # direct, easier to read
    rec = RollingPcapRecorder(LOGGER, segment_s=60)
    rec.start("dev1")

    # A hard-killed harness leaves the device-side tcpdump behind; `timeout`
    # is what makes it die on its own.
    assert rec.hard_limit_s() > rec.segment_s  # rotation always wins in normal operation
    assert _popen_shell(state["popen"][0]).startswith(f"timeout {rec.hard_limit_s()} tcpdump ")


def test_self_limit_wraps_inside_the_su_quotes(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    shell = _popen_shell(state["popen"][0])
    # su -c '<timeout ... tcpdump ...>': the whole thing must reach the device as
    # one argument, or the root shell never sees the wrapper.
    assert shell.startswith("su -c 'timeout ") and shell.endswith("'")


def test_latches_off_when_device_has_no_timeout(monkeypatch):
    state = install_fake_tcpdump(monkeypatch, timeout_ok=False)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    # An unbounded root tcpdump that could outlive us is worse than no pcap.
    assert rec._broken is True
    assert state["popen"] == []
    rec.tick()
    assert rec.collect("anywhere") == []
    rec.stop()


def test_start_sweeps_leftover_tcpdump(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    sweeps = [c for c in state["run"] if "pkill -9 tcpdump" in c[-1]]
    assert len(sweeps) == 1  # leftovers from a hard-killed previous run
    assert sweeps[0][-1].startswith("su -c '")  # root capture needs a root kill


def test_empty_bpf_filter_omits_expression(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER, bpf_filter="", su_mode="direct")
    rec.start("dev1")

    shell = _popen_shell(state["popen"][0]).rstrip()
    assert shell.endswith("/data/local/tmp/ga_pcap_0.pcap")  # nothing after -w path


# ---------- rotation ----------


def test_tick_rotates_when_segment_is_old(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER, segment_s=60)
    rec.start("dev1")

    rec.tick()  # fresh segment: nothing happens
    assert len(state["popen"]) == 1

    rec._segment_started -= 61
    rec.tick()

    assert len(state["popen"]) == 2  # rotated into a new segment
    assert any("pkill" in c[-1] for c in state["run"])  # previous one flushed
    assert rec._segments == [
        "/data/local/tmp/ga_pcap_0.pcap",
        "/data/local/tmp/ga_pcap_1.pcap",
    ]


def test_tick_restarts_when_tcpdump_died(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    state["procs"][0]._rc = 1  # tcpdump exited unexpectedly
    rec.tick()

    assert len(state["popen"]) == 2  # a fresh segment took over


def test_old_segments_pruned_beyond_keep_window(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER, segment_s=1, keep_segments=1)
    rec.start("dev1")

    for _ in range(4):
        rec._segment_started -= 2
        rec.tick()

    # keep_segments + 1 = 2 live device files at most.
    assert len(rec._segments) <= 2
    assert any("rm -f /data/local/tmp/ga_pcap_0.pcap" in c[-1] for c in state["run"])


# ---------- collect ----------


def test_collect_pulls_once_and_resumes(monkeypatch, tmp_path):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    first = rec.collect(tmp_path / "pcap")

    assert [Path(p).name for p in first] == ["ga_pcap_0.pcap"]
    assert Path(first[0]).read_bytes() == b"pcap-data"
    assert len(state["popen"]) == 2  # capture resumed after the pull

    second = rec.collect(tmp_path / "pcap")

    assert [Path(p).name for p in second] == ["ga_pcap_0.pcap", "ga_pcap_1.pcap"]
    pulls = [c for c in state["run"] if "pull" in c]
    assert len(pulls) == 2  # ga_pcap_0 reused, not pulled twice


def test_collect_noop_when_broken(monkeypatch):
    install_fake_tcpdump(monkeypatch, version_ok=False, su_ok=False)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    assert rec.collect("anywhere") == []


def test_collect_survives_pull_failure(monkeypatch, tmp_path):
    state = install_fake_tcpdump(monkeypatch)

    def failing_run(cmd, **kwargs):
        if "pull" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="pull error")
        if "--version" in (cmd[-1] if "shell" in cmd else ""):
            return SimpleNamespace(returncode=0, stdout="libpcap", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")
    monkeypatch.setattr("perception.pcap_recorder.subprocess.run", failing_run)

    assert rec.collect(tmp_path / "pcap") == []  # nothing pulled, no exception


# ---------- adb failure resilience ----------


def test_adb_errors_do_not_raise(monkeypatch, tmp_path):
    install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    def boom(cmd, **kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr("perception.pcap_recorder.subprocess.run", boom)
    # collect / tick / stop must swallow the adb failure, never propagate.
    assert rec.collect(tmp_path / "pcap") == []
    rec.tick()
    rec.stop()


# ---------- stop ----------


def test_stop_flushes_and_cleans_device_files(monkeypatch):
    state = install_fake_tcpdump(monkeypatch)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    rec.stop()

    assert any("pkill" in c[-1] for c in state["run"])  # SIGINT flush
    assert any(
        "rm -f" in c[-1] and "ga_pcap_" in c[-1] for c in state["run"]
    )  # device files removed
    assert rec._proc is None


def test_broken_recorder_stop_is_noop(monkeypatch):
    state = install_fake_tcpdump(monkeypatch, version_ok=False, su_ok=False)
    rec = RollingPcapRecorder(LOGGER)
    rec.start("dev1")

    runs_before = len(state["run"])
    rec.stop()  # no raise, and no extra shell calls (nothing to flush/clean)

    assert rec._proc is None
    assert len(state["run"]) == runs_before
