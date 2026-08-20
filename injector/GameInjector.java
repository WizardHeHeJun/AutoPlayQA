package com.ga.injector;

import android.os.SystemClock;
import android.view.InputEvent;
import android.view.MotionEvent;
import android.view.MotionEvent.PointerCoords;
import android.view.MotionEvent.PointerProperties;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Method;

/**
 * No-root multi-touch injector.
 *
 * Launched from adb shell via app_process (same privilege path the platform
 * `input` command uses), so it can call the hidden InputManager.injectInputEvent
 * without root and without writing to /dev/input (which SELinux blocks for the
 * shell domain on modern MIUI/HyperOS).
 *
 * Protocol (one MotionEvent per stdin line, fields space-separated):
 *
 *     <delay_ms> <action> <changed_id> <count> [<id> <x> <y>]...
 *
 *   action      = DOWN | POINTER_DOWN | MOVE | POINTER_UP | UP
 *   changed_id  = pointer id this action concerns (-1 for MOVE)
 *   count + triples = the FULL pointer set at this instant, in stable order
 *                     (the pointer index for POINTER_DOWN/UP is the position of
 *                      changed_id within this list)
 *
 * Coordinates are display pixels (same space as `input tap`). The Python side
 * (motionevent_backend) performs pointer diffing and emits this sequence; this
 * class is intentionally mechanical.
 */
public final class GameInjector {

    private static final int SOURCE_TOUCHSCREEN = 0x00001002;
    private static final int TOOL_TYPE_FINGER = 1;
    // INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH — block until each event lands,
    // preserving gesture ordering.
    private static final int INJECT_MODE = 2;
    private static final int ACTION_POINTER_INDEX_SHIFT = 8;

    private static Object inputManager;
    private static Method injectMethod;
    private static long downTime = 0L;

    public static void main(String[] args) {
        try {
            resolveInputManager();
            BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                handleLine(line);
            }
            System.out.println("OK");
        } catch (Throwable t) {
            System.err.println("INJECT_ERROR: " + t);
            System.exit(1);
        }
    }

    private static void resolveInputManager() throws Exception {
        Object im = null;
        // Android 14 (API 34) moved the singleton to InputManagerGlobal.
        try {
            Class<?> c = Class.forName("android.hardware.input.InputManagerGlobal");
            im = c.getMethod("getInstance").invoke(null);
        } catch (Throwable ignore) {
            Class<?> c = Class.forName("android.hardware.input.InputManager");
            im = c.getMethod("getInstance").invoke(null);
        }
        inputManager = im;
        injectMethod = im.getClass().getMethod("injectInputEvent", InputEvent.class, int.class);
        injectMethod.setAccessible(true);
    }

    private static void handleLine(String line) throws Exception {
        String[] tok = line.split("\\s+");
        int idx = 0;
        long delayMs = Long.parseLong(tok[idx++]);
        String action = tok[idx++];
        int changedId = Integer.parseInt(tok[idx++]);
        int count = Integer.parseInt(tok[idx++]);

        int[] ids = new int[count];
        PointerProperties[] props = new PointerProperties[count];
        PointerCoords[] coords = new PointerCoords[count];
        int changedIndex = 0;
        for (int i = 0; i < count; i++) {
            int id = Integer.parseInt(tok[idx++]);
            float x = Float.parseFloat(tok[idx++]);
            float y = Float.parseFloat(tok[idx++]);
            ids[i] = id;
            if (id == changedId) {
                changedIndex = i;
            }
            PointerProperties pp = new PointerProperties();
            pp.id = id;
            pp.toolType = TOOL_TYPE_FINGER;
            props[i] = pp;
            PointerCoords pc = new PointerCoords();
            pc.x = x;
            pc.y = y;
            pc.pressure = 1.0f;
            pc.size = 1.0f;
            coords[i] = pc;
        }

        if (delayMs > 0) {
            SystemClock.sleep(delayMs);
        }

        long now = SystemClock.uptimeMillis();
        int actionCode = resolveAction(action, changedIndex);
        if ("DOWN".equals(action)) {
            downTime = now;
        }

        MotionEvent event = MotionEvent.obtain(
                downTime, now, actionCode, count, props, coords,
                0, 0, 1.0f, 1.0f, 0, 0, SOURCE_TOUCHSCREEN, 0);
        injectMethod.invoke(inputManager, event, INJECT_MODE);
        event.recycle();
    }

    private static int resolveAction(String action, int changedIndex) {
        switch (action) {
            case "DOWN":
                return MotionEvent.ACTION_DOWN;
            case "UP":
                return MotionEvent.ACTION_UP;
            case "MOVE":
                return MotionEvent.ACTION_MOVE;
            case "POINTER_DOWN":
                return MotionEvent.ACTION_POINTER_DOWN
                        | (changedIndex << ACTION_POINTER_INDEX_SHIFT);
            case "POINTER_UP":
                return MotionEvent.ACTION_POINTER_UP
                        | (changedIndex << ACTION_POINTER_INDEX_SHIFT);
            default:
                throw new IllegalArgumentException("unknown action: " + action);
        }
    }
}
