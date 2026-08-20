"""Built-in custom actions: deterministic multi-step helpers."""

from __future__ import annotations

import io
import re
import subprocess
import time
from typing import Dict, List, Optional

from core.adb_timeout import adb_timeout_s
from task.custom_actions import CustomActionContext, register

DEFAULT_MAX_SWIPES = 5
DEFAULT_SETTLE_MS = 800
DEFAULT_LAUNCH_SETTLE_MS = 3000

DEFAULT_GM_SETTLE_MS = 1200
KEYCODE_BACK = 4
KEYCODE_DEL = 67
KEYCODE_MOVE_END = 123
KEYCODE_WAKEUP = 224
KEYCODE_MENU = 82
WAKE_SETTLE_MS = 600

# ROMs print the power state as `mWakefulness=Awake` (AOSP) or, on a few
# vendor builds, `mWakefulness: Awake`; anything else means "unknown".
_WAKEFULNESS_RE = re.compile(r"mWakefulness\s*[=:]\s*(\w+)")


def _wakefulness(device_id: str) -> Optional[str]:
    """Read the screen power state from `dumpsys power`.

    Returns the raw state word (Awake / Asleep / Dozing / Dreaming), or None
    when the state cannot be determined (dumpsys failed, or the ROM does not
    print an mWakefulness field at all).
    """
    res = _adb_shell(device_id, "dumpsys", "power")
    if res["ok"] != "True":
        return None
    match = _WAKEFULNESS_RE.search(res["stdout"])
    return match.group(1) if match else None


def _ensure_screen_awake(device_id: str, logger) -> List[Dict]:
    """Wake + unlock the screen before launching, when it is not already awake.

    A dark/locked device still accepts adb injection and still answers
    screencap — with all-black frames — so a whole task run can "succeed"
    against a black screen and produce a pile of bogus findings (see
    ai-docs/pitfalls.md). Guard the entry point instead: WAKEUP turns the panel
    on, MENU dismisses the swipe-to-unlock keyguard.

    Only acts when the state is not Awake, so an already-lit device pays a
    single dumpsys and gets zero injected keyevents (no foreground disturbance).
    An unknown state is treated as "possibly off": the wake pair is idempotent,
    so the conservative path is cheap and harmless.

    Best-effort: never fails the node. If the screen is still not awake
    afterwards we only warn — the blank_screen watchdog is the second line of
    defence and will report the black frames as a finding.
    """
    state = _wakefulness(device_id)
    if state == "Awake":
        logger.debug("launch_app: screen already awake, no wake keyevents sent")
        return []
    if state is None:
        logger.warning(
            "launch_app: cannot determine screen power state (no mWakefulness in "
            "`dumpsys power`); assuming the screen may be off and sending "
            "WAKEUP+MENU, which is idempotent on an awake device"
        )
    else:
        logger.info(
            "launch_app: screen is %s, sending WAKEUP+MENU before launching", state
        )

    _adb_shell(device_id, f"input keyevent {KEYCODE_WAKEUP}")
    time.sleep(WAKE_SETTLE_MS / 1000)
    _adb_shell(device_id, f"input keyevent {KEYCODE_MENU}")
    time.sleep(WAKE_SETTLE_MS / 1000)

    after = _wakefulness(device_id)
    if after == "Awake":
        logger.info("launch_app: screen awake after WAKEUP+MENU")
        return [{"ok": "True", "stdout": f"woke screen (was {state}), now Awake", "stderr": ""}]
    logger.warning(
        "launch_app: screen still not awake after WAKEUP+MENU (state=%s); continuing "
        "anyway — a black screen will be reported by the blank_screen watchdog",
        after,
    )
    return [
        {
            "ok": "True",
            "stdout": f"wake attempted (was {state}), state now {after}",
            "stderr": "",
        }
    ]


@register("launch_app")
def launch_app(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Launch (optionally cold-start) an app by package name.

    Wakes and unlocks the screen first when it is not already awake — a task
    run against a dark device injects fine but sees only black frames, which
    turns a whole run into false findings.

    params:
      package: Android package name (required)
      force_stop: kill the app first for a cold start (default False)
      settle_ms: wait after launching before the node completes (default 3000)
    """
    package = params.get("package")
    if not isinstance(package, str) or not package:
        raise ValueError("launch_app requires a 'package' string param")
    force_stop = bool(params.get("force_stop", False))
    settle_ms = int(params.get("settle_ms", DEFAULT_LAUNCH_SETTLE_MS))

    def adb_shell(*args: str) -> Dict:
        cmd = ["adb", "-s", ctx.device_id, "shell", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=adb_timeout_s())
        except subprocess.TimeoutExpired:
            return {"ok": "False", "stdout": "", "stderr": f"timeout: {' '.join(cmd)}"}
        ok = proc.returncode == 0 and "monkey aborted" not in (proc.stdout + proc.stderr).lower()
        return {"ok": str(ok), "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}

    results: List[Dict] = _ensure_screen_awake(ctx.device_id, ctx.logger)
    if force_stop:
        results.append(adb_shell("am", "force-stop", package))
        if results[-1]["ok"] != "True":
            return results
    results.append(
        adb_shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    )
    if results[-1]["ok"] == "True" and settle_ms:
        time.sleep(settle_ms / 1000)
    return results


def _adb_shell(device_id: str, *args: str) -> Dict:
    """Run `adb -s <dev> shell <args...>` and return an executor-style result.

    Pass the whole device-side command as a single arg to keep shell
    metacharacters (e.g. the ';' in GM commands) literal, e.g.
    _adb_shell(dev, "input text 'AddItem;1001;10'").
    """
    cmd = ["adb", "-s", device_id, "shell", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=adb_timeout_s())
    except subprocess.TimeoutExpired:
        return {"ok": "False", "stdout": "", "stderr": f"timeout: {' '.join(cmd)}"}
    ok = proc.returncode == 0
    return {"ok": str(ok), "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def _detect_latin_ime(device_id: str) -> Optional[str]:
    """Pick an enabled IME that commits ascii verbatim (id contains 'latin').

    Covers AOSP (com.android.inputmethod.latin) and Gboard
    (com.google.android.inputmethod.latin) without hard-coding either, so a
    pinyin/CJK default IME can be swapped out on whatever device runs the task.
    """
    res = _adb_shell(device_id, "ime", "list", "-s")
    if res["ok"] != "True":
        return None
    for line in res["stdout"].splitlines():
        ime = line.strip()
        if ime and "latin" in ime.lower():
            return ime
    return None


def _screen_size(device_id: str) -> Optional[tuple]:
    """Parse `wm size` -> (w, h); used to scale normalized tap points."""
    res = _adb_shell(device_id, "wm", "size")
    if res["ok"] != "True":
        return None
    for token in res["stdout"].replace("Override size:", "Physical size:").split():
        if "x" in token:
            try:
                w, h = token.split("x")
                return int(w), int(h)
            except ValueError:
                continue
    return None


def _resolve_point(ctx: CustomActionContext, spec, screen):
    """Resolve a tap-point spec -> ((x, y), None) or (None, error_message).

    A spec is one of, in priority order:
      * a recognition spec  {"type": "ocr"/"ui_text"/..., "expected": ...} —
        located on screen each run, so it survives resolution/layout changes;
      * a normalized point  {"nx": 0..1, "ny": 0..1} — scaled by screen size;
      * an absolute point   {"x": px, "y": px} — last resort, device-specific.
    """
    if not isinstance(spec, dict):
        return None, "point spec must be an object"
    if "type" in spec:
        hit = ctx.hub.recognize(ctx.device_id, spec)
        if hit and hit.get("center"):
            cx, cy = hit["center"]
            return (int(cx), int(cy)), None
        return None, f"recognition '{spec.get('expected', spec['type'])}' not found"
    if "nx" in spec and "ny" in spec:
        if not screen:
            return None, "normalized point needs screen size (wm size failed)"
        return (round(float(spec["nx"]) * screen[0]), round(float(spec["ny"]) * screen[1])), None
    if "x" in spec and "y" in spec:
        return (int(spec["x"]), int(spec["y"])), None
    return None, "point spec needs a recognition type, {nx, ny}, or {x, y}"


@register("gm_command")
def gm_command(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Open the in-game GM panel, run a parametrized command, close the panel.

    Encapsulates a flow the generic input_text node cannot do, because of two
    device quirks observed on real builds under test:
      * a pinyin/CJK default IME (e.g. Sogou) composes latin input and never
        commits it — we switch to a Latin IME for the injection and restore the
        original IME afterwards;
      * semicolons in the command get split by the on-device shell — we send the
        text single-quoted so sh keeps them literal.

    Kept device-agnostic on purpose (no hard-coded coords/IME in the task):
      * open / input_box / exec_button take recognition specs and are located by
        on-screen text every run (the panel's input box shows placeholder text);
      * the Latin IME is auto-detected from the device's enabled IME list;
      * only the textless bottom-left return button needs a position — give it as
        a normalized {nx, ny} so it scales across resolutions.

    params:
      command: GM command string, e.g. "AddItem;1001;10" (required;
               no single quotes)
      open: point spec for the GM badge that opens the panel (required)
      input_box: point spec for the command input box (required)
      exec_button: recognition spec for the execute button (required)
      close: point spec for the panel's bottom-left return button (required)
      latin_ime: override IME id for ascii injection (default: auto-detect)
      settle_ms: wait between steps (default 1200)
    """
    command = params.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError("gm_command requires a 'command' string param")
    if "'" in command:
        raise ValueError("gm_command 'command' must not contain single quotes")
    for key in ("open", "input_box", "exec_button", "close"):
        if not isinstance(params.get(key), dict):
            raise ValueError(f"gm_command requires a '{key}' spec object")
    exec_button = params["exec_button"]
    if "type" not in exec_button:
        raise ValueError("gm_command 'exec_button' must be a recognition spec")
    settle_ms = int(params.get("settle_ms", DEFAULT_GM_SETTLE_MS))

    needs_screen = any(
        isinstance(params.get(k), dict) and "nx" in params[k]
        for k in ("open", "input_box", "close")
    )
    screen = _screen_size(ctx.device_id) if needs_screen else None

    def settle() -> None:
        if settle_ms:
            time.sleep(settle_ms / 1000)

    def tap(point) -> Dict:
        return ctx.executor.execute(
            ctx.device_id,
            {"type": "click", "params": {"x": int(point[0]), "y": int(point[1])}},
            ctx.tracer,
        )

    results: List[Dict] = []

    def resolve_or_fail(key) -> Optional[tuple]:
        point, err = _resolve_point(ctx, params[key], screen)
        if err:
            results.append({"ok": "False", "stdout": "", "stderr": f"gm_command {key}: {err}"})
        return point

    # Open the GM panel and focus the command input box.
    open_pt = resolve_or_fail("open")
    if open_pt is None:
        return results
    results.append(tap(open_pt))
    settle()
    box_pt = resolve_or_fail("input_box")
    if box_pt is None:
        return results
    results.append(tap(box_pt))
    settle()

    # Pick a Latin IME so the command commits verbatim; remember the old one.
    latin_ime = params.get("latin_ime") or _detect_latin_ime(ctx.device_id)
    if not latin_ime:
        results.append(
            {"ok": "False", "stdout": "", "stderr": "gm_command: no Latin IME found (pass latin_ime)"}
        )
        return results
    original = _adb_shell(ctx.device_id, "settings", "get", "secure", "default_input_method")
    original_ime = original["stdout"] if original["ok"] == "True" else ""
    results.append(_adb_shell(ctx.device_id, "ime", "set", str(latin_ime)))
    settle()

    # Inject the command single-quoted so the on-device shell keeps ';' literal.
    results.append(_adb_shell(ctx.device_id, f"input text '{command}'"))
    settle()

    # Hide the keyboard (back collapses the IME, not the panel) before tapping
    # the execute button, which sits behind it.
    results.append(
        ctx.executor.execute(
            ctx.device_id, {"type": "key", "params": {"keycode": KEYCODE_BACK}}, ctx.tracer
        )
    )
    settle()

    exec_pt, exec_err = _resolve_point(ctx, exec_button, screen)
    if exec_err:
        # Restore the IME before bailing so we don't leave the device on Latin.
        if original_ime:
            _adb_shell(ctx.device_id, "ime", "set", original_ime)
        results.append({"ok": "False", "stdout": "", "stderr": f"gm_command exec_button: {exec_err}"})
        return results
    results.append(tap(exec_pt))
    settle()

    # Restore the user's IME and close the panel via its bottom-left return.
    if original_ime:
        results.append(_adb_shell(ctx.device_id, "ime", "set", original_ime))
    close_pt = resolve_or_fail("close")
    if close_pt is None:
        return results
    results.append(tap(close_pt))
    settle()
    return results


def _screencap_image(device_id: str):
    """Grab the current screen as a PIL RGB image (None on failure)."""
    cmd = ["adb", "-s", device_id, "exec-out", "screencap", "-p"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=adb_timeout_s())
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        from PIL import Image

        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except Exception:
        return None


@register("ensure_checkbox")
def ensure_checkbox(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Tap a checkbox only when it is not already checked (idempotent).

    Reads the checked state from a probe pixel (the green checkmark) instead of
    blindly toggling, so re-running the task never flips an already-checked box
    off. Login-page options like '是否跳过新手引导' persist across cold starts
    but reset on a fresh install, so a replay must set them without toggling.

    Best-effort: a failed screencap is reported but does not fail the node (the
    downstream recognition gate is the real assertion).

    params:
      probe: {"x", "y"} pixel sampled for the checkmark color (required)
      tap: point spec to toggle the box when unchecked (required)
      checked_rgb: [r, g, b] expected when checked (default [115, 206, 66])
      tolerance: per-channel max distance to count as checked (default 60)
      settle_ms: wait after a toggle (default 1200)
    """
    probe = params.get("probe")
    tap_spec = params.get("tap")
    if not isinstance(probe, dict) or "x" not in probe or "y" not in probe:
        raise ValueError("ensure_checkbox requires a 'probe' {x, y} param")
    if not isinstance(tap_spec, dict):
        raise ValueError("ensure_checkbox requires a 'tap' point spec")
    target = params.get("checked_rgb", [115, 206, 66])
    tol = int(params.get("tolerance", 60))
    settle_ms = int(params.get("settle_ms", DEFAULT_GM_SETTLE_MS))

    img = _screencap_image(ctx.device_id)
    if img is None:
        return [{"ok": "True", "stdout": "ensure_checkbox: screencap failed, skipped", "stderr": ""}]
    px = img.getpixel((int(probe["x"]), int(probe["y"])))
    checked = all(abs(int(px[i]) - int(target[i])) <= tol for i in range(3))
    if checked:
        return [{"ok": "True", "stdout": f"already checked (rgb={px})", "stderr": ""}]

    screen = _screen_size(ctx.device_id) if "nx" in tap_spec else None
    point, err = _resolve_point(ctx, tap_spec, screen)
    if err:
        return [{"ok": "False", "stdout": "", "stderr": f"ensure_checkbox tap: {err}"}]
    res = ctx.executor.execute(
        ctx.device_id,
        {"type": "click", "params": {"x": int(point[0]), "y": int(point[1])}},
        ctx.tracer,
    )
    if settle_ms:
        time.sleep(settle_ms / 1000)
    return [res, {"ok": "True", "stdout": f"toggled on (was rgb={px})", "stderr": ""}]


@register("set_text_field")
def set_text_field(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Focus a text field, clear it, and type text verbatim (idempotent).

    Switches to a Latin IME for the injection so a pinyin/CJK default IME does
    not compose the input (an account box otherwise mangles letters,
    e.g. 'qa' -> '强啊'), then restores the original IME. Clears the field
    first so re-runs replace the value instead of appending to a persisted one.
    Hides the soft keyboard with a single BACK while the IME is still up (the
    open keyboard consumes it, so it never reaches the app's exit dialog).

    Best-effort: if no Latin IME is enabled the step is skipped (ok) rather than
    failing the task — the downstream login/main-scene gate is the real check.

    params:
      field: point spec to tap/focus the field (required)
      text: string to enter (required, no single quotes)
      clear: DEL presses to clear existing content first (default 40)
      latin_ime: override IME id for the injection (default: auto-detect)
      settle_ms: wait between steps (default 1200)
    """
    field = params.get("field")
    text = params.get("text")
    if not isinstance(field, dict):
        raise ValueError("set_text_field requires a 'field' point spec")
    if not isinstance(text, str) or not text:
        raise ValueError("set_text_field requires a non-empty 'text'")
    if "'" in text:
        raise ValueError("set_text_field 'text' must not contain single quotes")
    clear = int(params.get("clear", 40))
    settle_ms = int(params.get("settle_ms", DEFAULT_GM_SETTLE_MS))

    def settle() -> None:
        if settle_ms:
            time.sleep(settle_ms / 1000)

    def tap(point) -> Dict:
        return ctx.executor.execute(
            ctx.device_id,
            {"type": "click", "params": {"x": int(point[0]), "y": int(point[1])}},
            ctx.tracer,
        )

    screen = _screen_size(ctx.device_id) if "nx" in field else None
    point, err = _resolve_point(ctx, field, screen)
    if err:
        return [{"ok": "False", "stdout": "", "stderr": f"set_text_field field: {err}"}]

    results: List[Dict] = [tap(point)]
    settle()

    latin_ime = params.get("latin_ime") or _detect_latin_ime(ctx.device_id)
    if not latin_ime:
        return results + [
            {"ok": "True", "stdout": "set_text_field: no Latin IME, skipped", "stderr": ""}
        ]
    original = _adb_shell(ctx.device_id, "settings", "get", "secure", "default_input_method")
    original_ime = original["stdout"] if original["ok"] == "True" else ""
    results.append(_adb_shell(ctx.device_id, "ime", "set", str(latin_ime)))
    settle()

    # Re-focus after the IME swap, move to the field end, delete back to clear.
    results.append(tap(point))
    settle()
    _adb_shell(ctx.device_id, f"input keyevent {KEYCODE_MOVE_END}")
    if clear > 0:
        results.append(
            _adb_shell(ctx.device_id, "input keyevent " + " ".join([str(KEYCODE_DEL)] * clear))
        )
    # Inject verbatim (single-quoted so the on-device shell keeps it literal).
    results.append(_adb_shell(ctx.device_id, f"input text '{text}'"))
    settle()

    # Hide the keyboard while the IME is still active (consumes the BACK), then
    # restore the user's IME with the field unfocused.
    results.append(
        ctx.executor.execute(
            ctx.device_id, {"type": "key", "params": {"keycode": KEYCODE_BACK}}, ctx.tracer
        )
    )
    settle()
    if original_ime:
        results.append(_adb_shell(ctx.device_id, "ime", "set", original_ime))
    return results


@register("swipe_until")
def swipe_until(ctx: CustomActionContext, params: Dict) -> List[Dict]:
    """Swipe repeatedly until a recognition spec hits (list/scroll search).

    params:
      recognition: node-style recognition spec to look for (required)
      swipe: {"x1", "y1", "x2", "y2", "duration_ms"?} one swipe gesture (required)
      max_swipes: attempts before giving up (default 5)
      settle_ms: wait after each swipe before recognizing (default 800)
    """
    recognition = params.get("recognition")
    swipe = params.get("swipe")
    if not isinstance(recognition, dict) or not isinstance(swipe, dict):
        raise ValueError("swipe_until requires 'recognition' and 'swipe' param objects")
    missing = [k for k in ("x1", "y1", "x2", "y2") if k not in swipe]
    if missing:
        raise ValueError(f"swipe_until swipe params missing {missing}")
    max_swipes = int(params.get("max_swipes", DEFAULT_MAX_SWIPES))
    settle_ms = int(params.get("settle_ms", DEFAULT_SETTLE_MS))

    if ctx.hub.recognize(ctx.device_id, recognition) is not None:
        return [{"ok": "True", "stdout": "found before swiping", "stderr": ""}]

    results: List[Dict] = []
    for attempt in range(1, max_swipes + 1):
        result = ctx.executor.execute(ctx.device_id, {"type": "drag", "params": dict(swipe)}, ctx.tracer)
        results.append(result)
        if result.get("ok") != "True":
            return results
        if settle_ms:
            time.sleep(settle_ms / 1000)
        if ctx.hub.recognize(ctx.device_id, recognition) is not None:
            ctx.logger.info("swipe_until: found after %d swipe(s)", attempt)
            results.append({"ok": "True", "stdout": f"found after {attempt} swipe(s)", "stderr": ""})
            return results

    raise ValueError(f"swipe_until: target not found after {max_swipes} swipes")
