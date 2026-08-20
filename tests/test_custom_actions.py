from __future__ import annotations

import importlib
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from task import custom_actions
from task.custom_actions import (
    CustomActionContext,
    get_handler,
    register,
    registered_names,
    unregister,
)
from task.custom_actions.builtins import (
    ensure_checkbox,
    gm_command,
    launch_app,
    set_text_field,
    swipe_until,
)
from tests.test_task_engine import FakeExecutor, FakeHub, hit, make_engine


class SequenceHub:
    """recognize() returns scripted results in call order; None once exhausted."""

    def __init__(self, results: List[Optional[Dict]]):
        self.results = list(results)
        self.calls = 0

    def recognize(self, device_id: str, spec: Dict) -> Optional[Dict]:
        self.calls += 1
        return self.results.pop(0) if self.results else None


def make_ctx(hub, executor=None) -> CustomActionContext:
    return CustomActionContext(
        device_id="dev1",
        executor=executor or FakeExecutor(),
        hub=hub,
        hit={"center": None, "text": "", "score": 1.0, "channel": "always"},
        logger=logging.getLogger("test"),
    )


SWIPE = {"x1": 360, "y1": 800, "x2": 360, "y2": 400}
FIND_GUILD = {"type": "ui_text", "expected": "公会"}


# ---------- registry ----------


def test_register_get_unregister_roundtrip():
    @register("tmp_action")
    def tmp(ctx, params):
        return []

    try:
        assert get_handler("tmp_action") is tmp
        assert "tmp_action" in registered_names()
    finally:
        unregister("tmp_action")
    assert get_handler("tmp_action") is None


def test_duplicate_registration_rejected():
    with pytest.raises(ValueError, match="already registered"):
        register("swipe_until")(lambda ctx, params: [])


def test_builtins_registered_on_import():
    assert "swipe_until" in registered_names()


@contextmanager
def _handler_module(body: str, stem: str):
    """Drop a throwaway handler module into the package, then clean it up.

    Writes into the real package directory on purpose: that is exactly what
    "add task/custom_actions/<name>.py" means, and it is the only way to prove
    discovery needs no edit to __init__.py.
    """
    path = Path(custom_actions.__file__).resolve().parent / f"{stem}.py"
    path.write_text(body, encoding="utf-8")
    importlib.invalidate_caches()  # the new file must be visible to the finder
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        for cached in path.parent.glob(f"__pycache__/{stem}.*.pyc"):
            cached.unlink(missing_ok=True)
        sys.modules.pop(f"task.custom_actions.{stem}", None)
        importlib.invalidate_caches()


def test_new_module_is_discovered_without_touching_init():
    """A new handler module registers itself with zero wiring changes."""
    module_body = (
        "from task.custom_actions import register\n"
        "\n"
        "\n"
        '@register("discovered_probe")\n'
        "def probe(ctx, params):\n"
        "    return []\n"
    )

    with _handler_module(module_body, "zz_discovery_probe"):
        try:
            custom_actions._import_handler_modules()
            assert "discovered_probe" in registered_names()
            assert get_handler("discovered_probe") is not None
        finally:
            unregister("discovered_probe")

    # builtins were imported once and are not re-registered by a second scan
    custom_actions._import_handler_modules()
    assert "swipe_until" in registered_names()


def test_broken_module_raises_instead_of_being_skipped():
    """Fail-fast: a silently skipped module would only surface mid-run on a device."""
    with _handler_module("raise RuntimeError('broken handler module')\n", "zz_broken_probe"):
        with pytest.raises(RuntimeError, match="broken handler module"):
            custom_actions._import_handler_modules()


# ---------- engine dispatch ----------


def custom_task(name: str = "test_probe", params: Optional[Dict] = None) -> Dict:
    return {
        "entry": "start",
        "nodes": {
            "start": {
                "recognition": {"type": "always"},
                "action": {"type": "custom", "name": name, "params": params or {"k": "v"}},
                "next": [],
                "timeout_ms": 0,
            },
        },
    }


def test_engine_dispatches_custom_handler_with_context():
    seen: Dict = {}

    @register("test_probe")
    def probe(ctx: CustomActionContext, params: Dict) -> List[Dict]:
        seen["device_id"] = ctx.device_id
        seen["params"] = params
        seen["hit_channel"] = ctx.hit["channel"]
        seen["has_executor"] = ctx.executor is not None and ctx.hub is not None
        return [{"ok": "True", "stdout": "did the thing", "stderr": ""}]

    try:
        result = make_engine(FakeHub({})).run("dev1", custom_task())
    finally:
        unregister("test_probe")

    assert result["status"] == "completed"
    assert seen == {
        "device_id": "dev1",
        "params": {"k": "v"},
        "hit_channel": "always",
        "has_executor": True,
    }
    assert result["steps"][0]["results"][0]["stdout"] == "did the thing"


def test_custom_handler_exception_fails_node():
    @register("test_boom")
    def boom(ctx, params):
        raise RuntimeError("boom")

    try:
        result = make_engine(FakeHub({})).run("dev1", custom_task("test_boom"))
    finally:
        unregister("test_boom")

    assert result["status"] == "failed"
    assert "start" in result["error"] and "boom" in result["error"]


def test_custom_handler_failed_result_fails_node():
    @register("test_sad")
    def sad(ctx, params):
        return [{"ok": "False", "stdout": "", "stderr": "nah"}]

    try:
        result = make_engine(FakeHub({})).run("dev1", custom_task("test_sad"))
    finally:
        unregister("test_sad")

    assert result["status"] == "failed"
    assert "nah" in result["error"]


def test_unregistered_custom_action_fails_at_runtime():
    result = make_engine(FakeHub({})).run("dev1", custom_task("ghost"))

    assert result["status"] == "failed"
    assert "Unregistered custom action 'ghost'" in result["error"]


# ---------- builtin: swipe_until ----------


def test_swipe_until_found_on_second_swipe():
    hub = SequenceHub([None, None, hit()])  # before swiping, after 1st, after 2nd
    executor = FakeExecutor()

    results = swipe_until(
        make_ctx(hub, executor),
        {"recognition": FIND_GUILD, "swipe": SWIPE, "settle_ms": 0},
    )

    assert [a["type"] for a in executor.executed] == ["drag", "drag"]
    assert executor.executed[0]["params"] == SWIPE
    assert results[-1]["ok"] == "True"
    assert "after 2 swipe" in results[-1]["stdout"]


def test_swipe_until_found_without_swiping():
    executor = FakeExecutor()

    results = swipe_until(
        make_ctx(SequenceHub([hit()]), executor),
        {"recognition": FIND_GUILD, "swipe": SWIPE, "settle_ms": 0},
    )

    assert executor.executed == []
    assert results == [{"ok": "True", "stdout": "found before swiping", "stderr": ""}]


def test_swipe_until_gives_up_after_max_swipes():
    with pytest.raises(ValueError, match="not found after 2 swipes"):
        swipe_until(
            make_ctx(SequenceHub([])),
            {"recognition": FIND_GUILD, "swipe": SWIPE, "max_swipes": 2, "settle_ms": 0},
        )


def test_swipe_until_stops_on_swipe_failure():
    executor = FakeExecutor(fail_types={"drag"})

    results = swipe_until(
        make_ctx(SequenceHub([]), executor),
        {"recognition": FIND_GUILD, "swipe": SWIPE, "settle_ms": 0},
    )

    assert len(results) == 1
    assert results[0]["ok"] == "False"


def test_swipe_until_validates_params():
    with pytest.raises(ValueError, match="requires 'recognition' and 'swipe'"):
        swipe_until(make_ctx(SequenceHub([])), {"swipe": SWIPE})
    with pytest.raises(ValueError, match="missing"):
        swipe_until(
            make_ctx(SequenceHub([])),
            {"recognition": FIND_GUILD, "swipe": {"x1": 1}},
        )


# ---------- builtin: launch_app ----------


AWAKE = "Display Power: state=ON\n  mWakefulness=Awake\n  mWakefulnessChanging=false"
ASLEEP = "Display Power: state=OFF\n  mWakefulness=Asleep\n  mWakefulnessChanging=false"
DOZING = "  mWakefulness=Dozing"
NO_FIELD = "Power Manager State:\n  mIsPowered=true"  # ROM without the field


def fake_run(calls, fail_on=None, power=(AWAKE,)):
    """adb stub for launch_app; `power` feeds consecutive `dumpsys power` outputs.

    The last entry repeats, so a single-element tuple pins the state; a None
    entry simulates a failing dumpsys.
    """
    states = list(power)

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        code = 1 if fail_on and fail_on in cmd else 0
        out = "Events injected: 1"
        if cmd[4:6] == ["dumpsys", "power"]:
            out = states.pop(0) if len(states) > 1 else states[0]
            if out is None:
                code, out = 1, ""

        class Proc:
            returncode = code
            stdout = out
            stderr = ""

        return Proc()

    return runner


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the wake settle waits so the tests stay instant."""
    monkeypatch.setattr("task.custom_actions.builtins.time.sleep", lambda _s: None)


def _keyevents(calls: List) -> List[str]:
    return [a[0] for a in _shell_args(calls) if a and a[0].startswith("input keyevent")]


def test_launch_app_cold_start(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", fake_run(calls))

    results = launch_app(
        make_ctx(SequenceHub([])),
        {"package": "com.example.game", "force_stop": True, "settle_ms": 0},
    )

    assert [r["ok"] for r in results] == ["True", "True"]
    assert _shell_args(calls)[0] == ["dumpsys", "power"]  # power check comes first
    assert calls[1] == ["adb", "-s", "dev1", "shell", "am", "force-stop", "com.example.game"]
    assert calls[2][:5] == ["adb", "-s", "dev1", "shell", "monkey"]
    assert "com.example.game" in calls[2]


def test_launch_app_warm_start_skips_force_stop(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", fake_run(calls))

    results = launch_app(make_ctx(SequenceHub([])), {"package": "com.example.game", "settle_ms": 0})

    assert len(calls) == 2 and "monkey" in calls[1]
    assert results[-1]["ok"] == "True"


def test_launch_app_awake_screen_sends_no_keyevents(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", fake_run(calls))

    results = launch_app(make_ctx(SequenceHub([])), {"package": "com.example.game", "settle_ms": 0})

    # an already-lit device pays one dumpsys and nothing else - no injected
    # keyevents that could disturb the foreground screen
    assert _keyevents(calls) == []
    assert [a for a in _shell_args(calls) if a[:2] == ["dumpsys", "power"]] == [["dumpsys", "power"]]
    assert len(results) == 1 and results[0]["ok"] == "True"


@pytest.mark.parametrize("state", [ASLEEP, DOZING])
def test_launch_app_wakes_dark_screen_then_launches(monkeypatch, no_sleep, state):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", fake_run(calls, power=(state, AWAKE))
    )

    results = launch_app(make_ctx(SequenceHub([])), {"package": "com.example.game", "settle_ms": 0})

    # WAKEUP lights the panel, MENU dismisses the swipe-to-unlock keyguard
    assert _keyevents(calls) == ["input keyevent 224", "input keyevent 82"]
    # state is re-read after waking, and the app is launched as usual
    assert [a[:2] for a in _shell_args(calls)].count(["dumpsys", "power"]) == 2
    assert any("monkey" in c for c in calls)
    assert all(r["ok"] == "True" for r in results)
    assert "now Awake" in results[0]["stdout"]


@pytest.mark.parametrize("power", [(NO_FIELD, AWAKE), (None, AWAKE)])
def test_launch_app_unknown_power_state_wakes_conservatively(
    monkeypatch, no_sleep, caplog, power
):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", fake_run(calls, power=power)
    )

    with caplog.at_level(logging.WARNING, logger="test"):
        results = launch_app(
            make_ctx(SequenceHub([])), {"package": "com.example.game", "settle_ms": 0}
        )

    # unknown == "possibly off": the idempotent wake pair is cheap insurance
    assert _keyevents(calls) == ["input keyevent 224", "input keyevent 82"]
    assert any("cannot determine screen power state" in r.message for r in caplog.records)
    assert any("monkey" in c for c in calls)
    assert all(r["ok"] == "True" for r in results)


def test_launch_app_warns_but_continues_when_still_dark(monkeypatch, no_sleep, caplog):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", fake_run(calls, power=(ASLEEP,))
    )

    with caplog.at_level(logging.WARNING, logger="test"):
        results = launch_app(
            make_ctx(SequenceHub([])), {"package": "com.example.game", "settle_ms": 0}
        )

    assert _keyevents(calls) == ["input keyevent 224", "input keyevent 82"]
    assert any("still not awake" in r.message for r in caplog.records)
    # not fatal: the node proceeds and blank_screen watchdog reports the black frames
    assert any("monkey" in c for c in calls)
    assert all(r["ok"] == "True" for r in results)


def test_launch_app_stops_when_force_stop_fails(monkeypatch):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", fake_run(calls, fail_on="force-stop")
    )

    results = launch_app(
        make_ctx(SequenceHub([])),
        {"package": "com.example.game", "force_stop": True, "settle_ms": 0},
    )

    assert len(results) == 1
    assert results[0]["ok"] == "False"
    assert len(calls) == 2 and "monkey" not in str(calls)  # dumpsys + failed force-stop


def test_launch_app_validates_package():
    with pytest.raises(ValueError, match="requires a 'package'"):
        launch_app(make_ctx(SequenceHub([])), {})
    assert "launch_app" in registered_names()


# ---------- builtin: gm_command ----------


GM_PARAMS = {
    "command": "AddItem;1001;10",
    "open": {"type": "ocr", "expected": "GM"},
    "input_box": {"type": "ocr", "expected": "请输入指令"},
    "exec_button": {"type": "ocr", "expected": "执行"},
    "close": {"nx": 0.065, "ny": 0.981},
    "settle_ms": 0,
}

LATIN = "com.android.inputmethod.latin/.LatinIME"
SOGOU = "com.sohu.inputmethod.sogou.moto/com.sohu.inputmethod.sogou.SogouIME"


def _shell_args(calls: List) -> List[List[str]]:
    """Strip the ['adb', '-s', dev, 'shell'] prefix from recorded adb calls."""
    return [c[4:] for c in calls]


def gm_fake_run(calls, ime_list=LATIN + "\n" + SOGOU):
    """adb stub that answers the device queries gm_command makes."""

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        shell = cmd[4:]
        out = ""
        if shell[:3] == ["ime", "list", "-s"]:
            out = ime_list
        elif shell[:1] == ["settings"]:
            out = SOGOU
        elif shell[:2] == ["wm", "size"]:
            out = "Physical size: 1080x2400"

        class Proc:
            returncode = 0
            stdout = out
            stderr = ""

        return Proc()

    return runner


def test_gm_command_happy_path(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", gm_fake_run(calls))
    executor = FakeExecutor()
    hub = SequenceHub([hit(76, 373), hit(211, 2160), hit(643, 411)])  # open, input_box, exec

    results = gm_command(make_ctx(hub, executor), GM_PARAMS)

    assert all(r["ok"] == "True" for r in results)
    # taps via executor: open, input box, hide-keyboard (back), exec center, close
    assert [a["type"] for a in executor.executed] == ["click", "click", "key", "click", "click"]
    assert executor.executed[0]["params"] == {"x": 76, "y": 373}
    assert executor.executed[1]["params"] == {"x": 211, "y": 2160}
    assert executor.executed[2]["params"] == {"keycode": 4}
    assert executor.executed[3]["params"] == {"x": 643, "y": 411}
    # normalized close scaled by wm size: 0.065*1080->70, 0.981*2400->2354
    assert executor.executed[4]["params"] == {"x": 70, "y": 2354}
    # adb side: detect Latin IME, switch to it, inject quoted command (';' kept)
    shell = _shell_args(calls)
    assert ["ime", "list", "-s"] in shell
    assert ["wm", "size"] in shell  # needed because close is normalized
    assert ["ime", "set", LATIN] in shell
    assert ["input text 'AddItem;1001;10'"] in shell


def test_gm_command_restores_ime_when_exec_button_missing(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", gm_fake_run(calls))
    executor = FakeExecutor()
    hub = SequenceHub([hit(76, 373), hit(211, 2160)])  # open, input_box; exec -> None

    results = gm_command(make_ctx(hub, executor), GM_PARAMS)

    assert results[-1]["ok"] == "False"
    assert "exec_button" in results[-1]["stderr"]
    # bailed before tapping the execute/close points
    assert [a["type"] for a in executor.executed] == ["click", "click", "key"]
    # IME was set twice: to Latin, then restored to the captured original
    ime_sets = [args for args in _shell_args(calls) if args[:2] == ["ime", "set"]]
    assert len(ime_sets) == 2 and ime_sets[-1] == ["ime", "set", SOGOU]


def test_gm_command_fails_when_no_latin_ime(monkeypatch):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run",
        gm_fake_run(calls, ime_list=SOGOU + "\ncom.netease.nie.yosemite/.ime.ImeService"),
    )
    executor = FakeExecutor()
    hub = SequenceHub([hit(76, 373), hit(211, 2160)])

    results = gm_command(make_ctx(hub, executor), GM_PARAMS)

    assert results[-1]["ok"] == "False"
    assert "no Latin IME" in results[-1]["stderr"]
    # never switched IME or injected text when no safe IME exists
    shell = _shell_args(calls)
    assert not any(args[:2] == ["ime", "set"] for args in shell)
    assert not any(args and args[0].startswith("input text") for args in shell)


def test_gm_command_validates_params():
    with pytest.raises(ValueError, match="requires a 'command'"):
        gm_command(make_ctx(SequenceHub([])), {})
    with pytest.raises(ValueError, match="single quotes"):
        gm_command(make_ctx(SequenceHub([])), {**GM_PARAMS, "command": "Bond'1"})
    with pytest.raises(ValueError, match="'input_box'"):
        gm_command(
            make_ctx(SequenceHub([])),
            {"command": "c", "open": {"type": "ocr", "expected": "GM"}},
        )
    with pytest.raises(ValueError, match="recognition spec"):
        gm_command(
            make_ctx(SequenceHub([])),
            {
                "command": "c",
                "open": {"type": "ocr", "expected": "GM"},
                "input_box": {"type": "ocr", "expected": "x"},
                "close": {"nx": 0.1, "ny": 0.1},
                "exec_button": {"x": 1, "y": 2},
            },
        )


def test_gm_command_registered_on_import():
    assert "gm_command" in registered_names()


# ---------- builtin: ensure_checkbox ----------


class FakeImage:
    """Minimal PIL stand-in: getpixel returns a fixed RGB and records the probe."""

    def __init__(self, rgb):
        self.rgb = rgb
        self.probed = None

    def getpixel(self, xy):
        self.probed = xy
        return self.rgb


CHECKED_RGB = (115, 206, 66)  # the green checkmark ensure_checkbox samples for
CHECKBOX_PARAMS = {"probe": {"x": 96, "y": 1180}, "tap": {"x": 96, "y": 1180}, "settle_ms": 0}


def test_ensure_checkbox_skips_when_already_checked(monkeypatch):
    monkeypatch.setattr(
        "task.custom_actions.builtins._screencap_image", lambda dev: FakeImage(CHECKED_RGB)
    )
    executor = FakeExecutor()

    results = ensure_checkbox(make_ctx(SequenceHub([]), executor), CHECKBOX_PARAMS)

    assert executor.executed == []  # never toggles an already-checked box off
    assert len(results) == 1 and results[0]["ok"] == "True"
    assert "already checked" in results[0]["stdout"]


def test_ensure_checkbox_treats_near_color_as_checked(monkeypatch):
    # within the default tolerance of 60 on every channel -> still "checked"
    monkeypatch.setattr(
        "task.custom_actions.builtins._screencap_image", lambda dev: FakeImage((160, 180, 100))
    )
    executor = FakeExecutor()

    results = ensure_checkbox(make_ctx(SequenceHub([]), executor), CHECKBOX_PARAMS)

    assert executor.executed == []
    assert "already checked" in results[0]["stdout"]


def test_ensure_checkbox_taps_when_unchecked(monkeypatch):
    img = FakeImage((255, 255, 255))  # far from the checked color
    monkeypatch.setattr("task.custom_actions.builtins._screencap_image", lambda dev: img)
    executor = FakeExecutor()

    results = ensure_checkbox(make_ctx(SequenceHub([]), executor), CHECKBOX_PARAMS)

    assert [a["type"] for a in executor.executed] == ["click"]
    assert executor.executed[0]["params"] == {"x": 96, "y": 1180}
    assert results[-1]["ok"] == "True" and "toggled on" in results[-1]["stdout"]
    assert img.probed == (96, 1180)  # sampled the configured probe pixel


def test_ensure_checkbox_reports_tap_resolution_failure(monkeypatch):
    monkeypatch.setattr(
        "task.custom_actions.builtins._screencap_image", lambda dev: FakeImage((255, 255, 255))
    )
    executor = FakeExecutor()
    params = {
        "probe": {"x": 96, "y": 1180},
        "tap": {"type": "ocr", "expected": "跳过"},
        "settle_ms": 0,
    }

    results = ensure_checkbox(make_ctx(SequenceHub([]), executor), params)  # recognize -> None

    assert executor.executed == []
    assert results[-1]["ok"] == "False"
    assert "ensure_checkbox tap" in results[-1]["stderr"]


def test_ensure_checkbox_skips_when_screencap_fails(monkeypatch):
    monkeypatch.setattr("task.custom_actions.builtins._screencap_image", lambda dev: None)
    executor = FakeExecutor()

    results = ensure_checkbox(make_ctx(SequenceHub([]), executor), CHECKBOX_PARAMS)

    assert executor.executed == []  # best-effort: gate downstream, don't fail here
    assert results[0]["ok"] == "True" and "screencap failed" in results[0]["stdout"]


def test_ensure_checkbox_validates_params():
    with pytest.raises(ValueError, match="'probe'"):
        ensure_checkbox(make_ctx(SequenceHub([])), {"tap": {"x": 1, "y": 2}})
    with pytest.raises(ValueError, match="'tap'"):
        ensure_checkbox(make_ctx(SequenceHub([])), {"probe": {"x": 1, "y": 2}})


def test_ensure_checkbox_registered_on_import():
    assert "ensure_checkbox" in registered_names()


# ---------- builtin: set_text_field ----------


STF_PARAMS = {"field": {"x": 540, "y": 800}, "text": "qa123", "clear": 3, "settle_ms": 0}


def stf_fake_run(calls, ime_list=LATIN + "\n" + SOGOU, original=SOGOU):
    """adb stub answering the device queries set_text_field makes."""

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        shell = cmd[4:]
        out = ""
        if shell[:3] == ["ime", "list", "-s"]:
            out = ime_list
        elif shell[:1] == ["settings"]:
            out = original

        class Proc:
            returncode = 0
            stdout = out
            stderr = ""

        return Proc()

    return runner


def test_set_text_field_happy_path(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", stf_fake_run(calls))
    executor = FakeExecutor()

    results = set_text_field(make_ctx(SequenceHub([]), executor), STF_PARAMS)

    assert all(r["ok"] == "True" for r in results)
    # taps via executor: focus field, re-focus after IME swap, BACK to hide keyboard
    assert [a["type"] for a in executor.executed] == ["click", "click", "key"]
    assert executor.executed[0]["params"] == {"x": 540, "y": 800}
    assert executor.executed[1]["params"] == {"x": 540, "y": 800}
    assert executor.executed[2]["params"] == {"keycode": 4}
    shell = _shell_args(calls)
    assert ["ime", "list", "-s"] in shell  # detect a Latin IME
    assert ["settings", "get", "secure", "default_input_method"] in shell  # capture original
    assert ["input keyevent 123"] in shell  # move cursor to field end before clearing
    assert ["input keyevent 67 67 67"] in shell  # clear=3 backspaces
    assert ["input text 'qa123'"] in shell  # quoted, injected verbatim
    # swapped to Latin, then restored to the captured original, in that order
    ime_sets = [a for a in shell if a[:2] == ["ime", "set"]]
    assert ime_sets == [["ime", "set", LATIN], ["ime", "set", SOGOU]]


def test_set_text_field_skips_without_latin_ime(monkeypatch):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", stf_fake_run(calls, ime_list=SOGOU)
    )
    executor = FakeExecutor()

    results = set_text_field(make_ctx(SequenceHub([]), executor), STF_PARAMS)

    # focused the field once, then bailed before any IME swap or injection
    assert [a["type"] for a in executor.executed] == ["click"]
    assert results[-1]["ok"] == "True" and "no Latin IME" in results[-1]["stdout"]
    shell = _shell_args(calls)
    assert not any(a[:2] == ["ime", "set"] for a in shell)
    assert not any(a and a[0].startswith("input text") for a in shell)


def test_set_text_field_uses_explicit_latin_ime(monkeypatch):
    calls: List = []
    monkeypatch.setattr(
        "task.custom_actions.builtins.subprocess.run", stf_fake_run(calls, ime_list=SOGOU)
    )
    executor = FakeExecutor()

    results = set_text_field(make_ctx(SequenceHub([]), executor), {**STF_PARAMS, "latin_ime": LATIN})

    assert all(r["ok"] == "True" for r in results)
    shell = _shell_args(calls)
    assert ["ime", "list", "-s"] not in shell  # explicit IME skips detection
    assert ["ime", "set", LATIN] in shell
    assert ["input text 'qa123'"] in shell


def test_set_text_field_reports_field_resolution_failure(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", stf_fake_run(calls))
    executor = FakeExecutor()

    results = set_text_field(
        make_ctx(SequenceHub([]), executor),  # recognize -> None
        {"field": {"type": "ocr", "expected": "账号"}, "text": "qa", "settle_ms": 0},
    )

    assert executor.executed == []
    assert len(results) == 1 and results[0]["ok"] == "False"
    assert "set_text_field field" in results[0]["stderr"]
    assert calls == []  # bailed before touching adb


def test_set_text_field_clear_zero_skips_backspaces(monkeypatch):
    calls: List = []
    monkeypatch.setattr("task.custom_actions.builtins.subprocess.run", stf_fake_run(calls))
    executor = FakeExecutor()

    set_text_field(make_ctx(SequenceHub([]), executor), {**STF_PARAMS, "clear": 0})

    shell = _shell_args(calls)
    assert ["input keyevent 123"] in shell  # still moves cursor to end
    assert not any(a and a[0].startswith("input keyevent 67") for a in shell)  # no backspaces
    assert ["input text 'qa123'"] in shell


def test_set_text_field_validates_params():
    with pytest.raises(ValueError, match="'field'"):
        set_text_field(make_ctx(SequenceHub([])), {"text": "x"})
    with pytest.raises(ValueError, match="non-empty 'text'"):
        set_text_field(make_ctx(SequenceHub([])), {"field": {"x": 1, "y": 2}, "text": ""})
    with pytest.raises(ValueError, match="single quotes"):
        set_text_field(make_ctx(SequenceHub([])), {"field": {"x": 1, "y": 2}, "text": "qa'x"})


def test_set_text_field_registered_on_import():
    assert "set_text_field" in registered_names()
