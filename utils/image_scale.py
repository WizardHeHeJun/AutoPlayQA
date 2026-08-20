"""Preview downscaling for frames that leave the process (MCP return face).

Why this exists: an agent reading a 1080x2400 screenshot pays for it in image
tokens on every look, and it almost never needs native pixels — it needs to see
*what is on screen*. Normalising the short edge to 720 cuts the token bill
roughly in half while leaving the frame perfectly legible.

Scope discipline: this is a **return-face** concern only. The recognition
channels (OCR, template/feature/YOLO matching, pixel diff, blank detection) and
the findings evidence chain keep working on the untouched capture — nothing in
here may be wired into `capture_image()` hot paths or `screenshot_exact`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps PIL off the import path
    from PIL import Image as PILImage

# Short edge (min of width/height) the MCP return face normalises to.
PREVIEW_SHORT_EDGE = 720


def preview_scale(size: Tuple[int, int], max_short_edge: int = PREVIEW_SHORT_EDGE) -> float:
    """Ratio to shrink ``size`` by so its short edge is at most ``max_short_edge``.

    Returns 1.0 when the frame is already small enough (or the cap is disabled
    with a non-positive value), so callers can treat 1.0 as "no resampling".
    """
    width, height = size
    short_edge = min(width, height)
    if max_short_edge <= 0 or short_edge <= max_short_edge or short_edge <= 0:
        return 1.0
    return max_short_edge / float(short_edge)


def downscale_short_edge(
    image: "PILImage.Image",
    max_short_edge: int = PREVIEW_SHORT_EDGE,
) -> Tuple["PILImage.Image", float]:
    """Return ``(image, scale)`` with the short edge capped, aspect ratio kept.

    Uses LANCZOS so small on-screen text stays readable after the shrink. When
    no resampling is needed the *original* image object comes back untouched
    (scale 1.0) — callers may rely on that identity to skip a copy.
    """
    from PIL import Image

    scale = preview_scale(image.size, max_short_edge)
    if scale >= 1.0:
        return image, 1.0
    width, height = image.size
    # round() rather than int(): a 0.5px bias here would drift the aspect ratio
    # on tall frames. max(1, ...) guards degenerate 1px-tall captures.
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.LANCZOS), scale
