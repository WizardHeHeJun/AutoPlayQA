from __future__ import annotations

import io


def _as_grayscale(image):
    """Accept PNG bytes or a PIL Image; return a grayscale ("L") Image."""
    from PIL import Image

    if isinstance(image, (bytes, bytearray)):
        image = Image.open(io.BytesIO(image))
    return image.convert("L")


def image_grayscale_stddev(image) -> float:
    """Standard deviation of grayscale pixel values for one screenshot.

    Accepts PNG bytes or a PIL Image. Near zero means a uniform frame
    (blank/black/white screen); normal game or app frames typically score well
    above 20. Used by the blank_screen recognition channel.
    """
    from PIL import ImageStat

    return float(ImageStat.Stat(_as_grayscale(image)).stddev[0])


def image_change_ratio(image_a, image_b, pixel_threshold: int = 16) -> float:
    """Fraction of pixels that visibly changed between two screenshots (0.0-1.0).

    Accepts PNG bytes or PIL Images. Cheap LLM-free check used by step
    verification: a tap that did nothing leaves the screen essentially
    identical. pixel_threshold filters sensor noise / subtle anti-aliasing
    differences.
    """
    from PIL import ImageChops

    img_a = _as_grayscale(image_a)
    img_b = _as_grayscale(image_b)
    if img_a.size != img_b.size:
        return 1.0

    diff = ImageChops.difference(img_a, img_b)
    histogram = diff.histogram()
    changed = sum(histogram[pixel_threshold:])
    total = img_a.size[0] * img_a.size[1]
    return changed / total if total else 0.0
