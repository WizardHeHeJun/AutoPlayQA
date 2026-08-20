from __future__ import annotations

import io
from typing import List, Dict

from PIL import Image, ImageDraw, ImageFont

# Colour per rank: 1st=green, 2nd=yellow, 3rd=orange
_RANK_COLORS = ["#00FF00", "#FFFF00", "#FF8800"]

# CJK-capable font search order; first successful load wins.
_CJK_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",                          # Microsoft YaHei (Windows)
    "C:/Windows/Fonts/simhei.ttf",                        # SimHei (Windows)
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",      # WenQuanYi (Linux)
    "/System/Library/Fonts/PingFang.ttc",                 # PingFang (macOS)
]


def _cjk_font(size: int = 20):
    """Return a CJK-capable TrueType font, or PIL's built-in default if none is found."""
    for path in _CJK_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def draw_candidate_boxes(png_bytes: bytes, candidates: List[Dict]) -> bytes:
    """Draw coloured bounding boxes for top UI candidates on a PNG screenshot.

    Args:
        png_bytes: Raw PNG bytes of the screenshot.
        candidates: List of candidate dicts, each containing at least
                    ``bounds`` ([x1, y1, x2, y2]), ``score`` (float),
                    and ``text`` (str).  Only the first 3 are drawn.

    Returns:
        PNG bytes with annotations drawn.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)

    for i, c in enumerate(candidates[: len(_RANK_COLORS)]):
        bounds = c.get("bounds")
        if not bounds or len(bounds) != 4:
            continue
        x1, y1, x2, y2 = bounds
        color = _RANK_COLORS[i]
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        label = f"#{i + 1} {c.get('score', 0):.2f} {c.get('text', '')}"
        draw.text((x1, max(y1 - 18, 0)), label, fill=color)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def draw_set_of_marks(
    image: "Image.Image",
    elements: List[Dict],
    scale: float = 1.0,
) -> "Image.Image":
    """Overlay numbered Set-of-Marks badges so an agent can tap by index.

    Each element gets a filled circular badge at its center carrying the
    element's 1-based ``index``; clickable elements are tinted differently from
    plain text so the agent can tell controls from labels at a glance. When an
    element carries ``bounds`` ([x1, y1, x2, y2]) a thin box is traced too.

    Works on a PIL Image (not PNG bytes) to stay on the raw capture path with no
    encode/decode round trip; returns a new annotated RGB Image, leaving the
    input untouched.

    **Coordinate-system contract**: ``elements`` are always in *absolute device
    pixels* — the space the ``click`` action, ``click_index`` and the returned
    element table live in. ``scale`` is the ratio between ``image`` and that
    device space (1.0 when ``image`` is the native capture, 720/1080 when it has
    been downscaled for the preview), and is applied to centers and bounds while
    drawing. So the element table never moves; only the canvas does. Badges are
    drawn *after* the resize, at a radius derived from the canvas width, which
    keeps the digits crisp instead of resampling annotated pixels.

    Args:
        image: Canvas to draw on (native capture or an already-downscaled copy).
        elements: List of dicts each with ``index`` (int), ``center`` ([x, y]),
                  optional ``bounds`` ([x1, y1, x2, y2]) and ``clickable`` (bool),
                  all in device pixels.
        scale: canvas-pixels / device-pixels ratio for the coordinates above.

    Returns:
        A new annotated RGB PIL Image.
    """
    img = image.convert("RGB").copy()
    draw = ImageDraw.Draw(img)

    # Badge size scales with the frame so marks stay legible across resolutions.
    radius = max(14, img.width // 60)
    font = _cjk_font(int(radius * 1.2))
    clickable_fill, text_fill = "#E53935", "#1E88E5"  # red = control, blue = label
    placed: List[List[int]] = []  # badge boxes already drawn, for overlap nudging

    for el in elements:
        center = el.get("center")
        if not center or len(center) != 2:
            continue
        cx, cy = int(center[0] * scale), int(center[1] * scale)

        bounds = el.get("bounds")
        if bounds and len(bounds) == 4:
            draw.rectangle([int(v * scale) for v in bounds], outline="#FFEB3B", width=2)

        # Nudge the badge upward while it would overlap an already-placed one so
        # clustered elements stay individually readable.
        bx, by = cx, cy
        for _ in range(6):
            box = [bx - radius, by - radius, bx + radius, by + radius]
            if not any(_boxes_overlap(box, p) for p in placed):
                break
            by -= int(radius * 1.6)
        box = [bx - radius, by - radius, bx + radius, by + radius]
        placed.append(box)

        fill = clickable_fill if el.get("clickable") else text_fill
        draw.ellipse(box, fill=fill, outline="#FFFFFF", width=2)
        label = str(el.get("index", "?"))
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((bx - tw / 2 - tb[0], by - th / 2 - tb[1]), label, fill="#FFFFFF", font=font)

    return img


def _boxes_overlap(a: List[int], b: List[int]) -> bool:
    """True when two [x1, y1, x2, y2] boxes intersect."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def draw_vision_clicks(png_bytes: bytes, click_pts: List[Dict]) -> bytes:
    """Draw blue crosshairs on vision-model proposed click points.

    Args:
        png_bytes: Raw PNG bytes.
        click_pts: List of dicts with 'x' and 'y' keys (absolute pixel coords).

    Returns:
        Annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    r = 24  # crosshair radius

    for i, pt in enumerate(click_pts):
        x, y = int(pt["x"]), int(pt["y"])
        draw.ellipse([x - r, y - r, x + r, y + r], outline="#0088FF", width=3)
        draw.line([x - r, y, x + r, y], fill="#0088FF", width=2)
        draw.line([x, y - r, x, y + r], fill="#0088FF", width=2)
        label = f"V{i + 1} ({x},{y})"
        draw.text((x + r + 4, y - 10), label, fill="#0088FF")

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def draw_movement_elements(png_bytes: bytes, detection: Dict) -> bytes:
    """Annotate joystick center, player character, and movement targets on a screenshot.

    Draws colored shapes for each detected element:
    - Green crosshair circle : joystick center (\u6447\u6746\u4e2d\u5fc3)
    - Blue circle            : player character (\u89d2\u8272)
    - Yellow numbered circles: movement targets in visit order (\u76ee\u6807 1, 2, \u2026)

    Any element the model did not detect is listed as "\u672a\u8bc6\u522b" in the top-left overlay.

    Args:
        png_bytes: Raw PNG bytes.
        detection: Dict with keys 'joystick', 'character', 'targets' as returned by
                   UIDetector._detect_movement_elements() (coordinates in absolute pixels).

    Returns:
        Annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _cjk_font(20)
    r = 30  # annotation circle radius
    not_found: List[str] = []

    # --- Joystick: green crosshair circle ---
    js = detection.get("joystick", {})
    if js.get("found"):
        x, y = int(js["x"]), int(js["y"])
        draw.ellipse([x - r, y - r, x + r, y + r], outline="#00FF00", width=4)
        draw.line([x - r, y, x + r, y], fill="#00FF00", width=2)
        draw.line([x, y - r, x, y + r], fill="#00FF00", width=2)
        draw.text((x + r + 4, y - 12), "\u6447\u6746\u4e2d\u5fc3", fill="#00FF00", font=font)
    else:
        not_found.append("\u6447\u6746\u4e2d\u5fc3: \u672a\u8bc6\u522b")

    # --- Character: blue circle ---
    ch = detection.get("character", {})
    if ch.get("found"):
        x, y = int(ch["x"]), int(ch["y"])
        draw.ellipse([x - r, y - r, x + r, y + r], outline="#00AAFF", width=4)
        draw.text((x + r + 4, y - 12), "\u89d2\u8272", fill="#00AAFF", font=font)
    else:
        not_found.append("\u79fb\u52a8\u89d2\u8272: \u672a\u8bc6\u522b")

    # --- Movement targets: yellow numbered circles ---
    targets = detection.get("targets", [])
    if not targets:
        not_found.append("\u79fb\u52a8\u76ee\u7684\u5730: \u672a\u8bc6\u522b")
    for i, t in enumerate(targets):
        if t.get("found"):
            x, y = int(t["x"]), int(t["y"])
            draw.ellipse([x - r, y - r, x + r, y + r], outline="#FFDD00", width=4)
            label = t.get("label") or f"\u76ee\u6807{i + 1}"
            draw.text((x + r + 4, y - 12), label, fill="#FFDD00", font=font)
        else:
            not_found.append(f"\u76ee\u6807{i + 1}: \u672a\u8bc6\u522b")

    # --- Overlay undetected elements at top-left corner ---
    for j, line in enumerate(not_found):
        draw.text((10, 10 + j * 28), line, fill="#FF4444", font=font)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
