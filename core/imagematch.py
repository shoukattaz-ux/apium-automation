"""Finding and checking elements by their picture.

When an element is picked, a small image of it is cut from the screenshot and kept
next to the scripts (``configs/images``). At run time the image is used two ways:

* **check**: does the screen, at the place the selectors found, still look like the
  picked element? (one vote for that element)
* **search**: if no selector finds anything, where on the screenshot does the image
  appear? Only a strong match that appears once counts.

Matching is normalised cross-correlation on grayscale images (so brightness changes
don't matter much), computed with FFTs on a downscaled screenshot to stay fast.
It works on the same phone model and theme the image was taken on; icons, tabs and
buttons match well, changing content (photos, posts) does not, and that's fine: the
image is one vote among the selectors, not the only judge.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

CHECK_THRESHOLD = 0.80     # the found element looks like the picked one
SEARCH_THRESHOLD = 0.88    # a match found by searching the whole screen
SEARCH_WIDTH = 360         # the screenshot is scaled down to about this width for searching
CHECK_PADDING = 6          # pixels of slack around the found element when checking
ROUGH_THRESHOLD = 0.3      # spots the rough search passes on to the exact check...
ROUGH_CANDIDATES = 8       # ...at most this many
MIN_PATCH_VARIANCE = 1e-4  # per-pixel variance below which a patch counts as flat
MIN_SIZE = 12              # elements smaller than this (px) don't get an image
MAX_SCREEN_FRACTION = 0.5  # nor do ones covering more than half the screen (too generic)


@dataclass(frozen=True)
class Match:
    score: float
    bounds: tuple[int, int, int, int]  # in screenshot pixels
    unique: bool


def load_gray(data: bytes | Path | str) -> np.ndarray:
    """PNG bytes or a file → grayscale float array in 0..1."""
    source = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else Path(data)
    with Image.open(source) as image:
        return np.asarray(image.convert("L"), dtype=np.float32) / 255.0


def _resize(gray: np.ndarray, factor: float) -> np.ndarray:
    if factor == 1:
        return gray
    height, width = gray.shape
    size = (max(1, round(width / factor)), max(1, round(height / factor)))
    image = Image.fromarray((gray * 255).astype(np.uint8)).resize(size, Image.BOX)
    return np.asarray(image, dtype=np.float32) / 255.0


def worth_keeping(bounds: tuple[int, int, int, int], screen: tuple[int, int]) -> bool:
    """Is this element a sensible size to identify by picture?"""
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width < MIN_SIZE or height < MIN_SIZE:
        return False
    return not (screen[0] and screen[1] and width * height > screen[0] * screen[1] * MAX_SCREEN_FRACTION)


def crop_png(screenshot_png: bytes, bounds: tuple[int, int, int, int], layout: tuple[int, int]) -> bytes:
    """Cut an element out of a screenshot. ``bounds`` are layout coordinates, scaled to the screenshot."""
    with Image.open(io.BytesIO(screenshot_png)) as image:
        scale = image.width / layout[0] if layout[0] else 1.0
        box = tuple(int(round(v * scale)) for v in bounds)
        box = (max(0, box[0]), max(0, box[1]), min(image.width, box[2]), min(image.height, box[3]))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            raise ValueError("the element is outside the screenshot")
        out = io.BytesIO()
        image.crop(box).convert("RGB").save(out, "PNG")
        return out.getvalue()


def save_template(png: bytes, folder: Path) -> str:
    """Store a picked element's image; returns its path relative to ``folder``'s parent (configs)."""
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(png).hexdigest()[:16] + ".png"
    target = folder / name
    if not target.exists():
        target.write_bytes(png)
    return f"{folder.name}/{name}"


def ncc_map(screen: np.ndarray, template: np.ndarray) -> np.ndarray | None:
    """Normalised cross-correlation of ``template`` at every position inside ``screen``.

    Returns an (H-h+1, W-w+1) array of scores in -1..1, or None when the template is
    flat (a plain colour matches everything, so it can't identify anything) or larger
    than the screen.
    """
    height, width = screen.shape
    h, w = template.shape
    if h > height or w > width:
        return None
    t = template - template.mean()
    t_norm = float(np.sqrt((t * t).sum()))
    if t_norm < 1e-3:
        return None
    shape = (height + h - 1, width + w - 1)
    numerator = np.fft.irfft2(np.fft.rfft2(screen, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)
    numerator = numerator[h - 1:height, w - 1:width]

    def window_sum(values: np.ndarray) -> np.ndarray:
        integral = np.pad(values, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]

    n = h * w
    sums = window_sum(screen.astype(np.float64))
    squares = window_sum(screen.astype(np.float64) ** 2)
    variance = np.maximum(squares - sums * sums / n, 0)
    denominator = np.sqrt(variance) * t_norm
    # A flat patch of screen (one plain colour) has no pattern to match; without this
    # floor, rounding noise there would divide out to a perfect score.
    textured = variance > n * MIN_PATCH_VARIANCE
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(textured, numerator / denominator, 0.0)
    return np.clip(scores, -1.0, 1.0)


def search(screen: np.ndarray, template: np.ndarray, threshold: float = SEARCH_THRESHOLD) -> Match | None:
    """Where does ``template`` appear on ``screen``? None unless the best match reaches ``threshold``.

    Two stages: a rough search on a scaled-down screenshot finds the most promising
    spots, then each is scored exactly at full resolution. ``unique`` is False when a
    second, separate spot also reaches the threshold (e.g. the same icon twice), in
    which case the caller shouldn't pick either.
    """
    factor = max(1.0, screen.shape[1] / SEARCH_WIDTH)
    factor = min(factor, max(1.0, min(template.shape) / 8))  # keep the small template at least ~8 px
    rough = ncc_map(_resize(screen, factor), _resize(template, factor))
    if rough is None:
        return None
    h, w = template.shape
    found: list[tuple[float, int, int]] = []
    for _ in range(ROUGH_CANDIDATES):
        y, x = np.unravel_index(int(np.argmax(rough)), rough.shape)
        if rough[y, x] < ROUGH_THRESHOLD:
            break
        # Suppress this spot so the next pass finds a different one.
        sh, sw = max(1, int(h / factor / 2)), max(1, int(w / factor / 2))
        rough[max(0, y - sh):y + sh + 1, max(0, x - sw):x + sw + 1] = -1
        left, top = int(round(x * factor)), int(round(y * factor))
        score = similarity(screen, template, (left, top, left + w, top + h), pad=int(factor) + 2, resize=False)
        if score >= threshold:
            found.append((score, left, top))
    if not found:
        return None
    found.sort(reverse=True)
    best, left, top = found[0]
    # Refine the position at full resolution around the rough spot.
    pad = int(factor) + 2
    region_left, region_top = max(0, left - pad), max(0, top - pad)
    region = screen[region_top:top + h + pad, region_left:left + w + pad]
    scores = ncc_map(region, template)
    if scores is not None:
        y, x = np.unravel_index(int(np.argmax(scores)), scores.shape)
        left, top = region_left + int(x), region_top + int(y)
    separate = [f for f in found[1:] if abs(f[1] - left) > w / 2 or abs(f[2] - top) > h / 2]
    return Match(best, (left, top, left + w, top + h), unique=not separate)


def similarity(screen: np.ndarray, template: np.ndarray, bounds: tuple[int, int, int, int],
               pad: int = CHECK_PADDING, resize: bool = True) -> float:
    """How much the screen at ``bounds`` (screenshot pixels) looks like ``template`` (allowing ``pad`` px of shift)."""
    height, width = screen.shape
    left, top = max(0, bounds[0] - pad), max(0, bounds[1] - pad)
    right, bottom = min(width, bounds[2] + pad), min(height, bounds[3] + pad)
    region = screen[top:bottom, left:right]
    if region.size == 0:
        return 0.0
    found_h, found_w = bounds[3] - bounds[1], bounds[2] - bounds[0]
    if resize and found_w > 0 and found_h > 0 and (found_w, found_h) != (template.shape[1], template.shape[0]):
        # Same element drawn at a slightly different size: compare at the found size.
        template = np.asarray(Image.fromarray((template * 255).astype(np.uint8)).resize(
            (found_w, found_h), Image.BILINEAR), dtype=np.float32) / 255.0
    scores = ncc_map(region, template)
    return float(scores.max()) if scores is not None else 0.0


# ------------------------------------------------------------------ look-alikes
# Elements that look the same except for a small part (cloned apps with a numbered badge,
# list rows that differ in one word) can't be told apart by the whole picture: two clones are
# ~95% alike. At pick time we find the part where the picked element differs from its
# look-alikes (the "mark") and keep only that; at run time each candidate's mark area is
# compared with it.

# Measured on 49 clone icons differing only in a badge number: the right one scores 1.00 (the
# phone draws identical icons pixel for pixel), the closest wrong one up to 0.91.
MARK_THRESHOLD = 0.95       # the candidate's mark looks like the picked one...
MARK_MARGIN = 0.04          # ...and clearly better than the next best candidate
MIN_MARK_FRACTION = 0.003   # differences smaller than this share of the element are noise


def _crop_gray(shot: np.ndarray, bounds: tuple[int, int, int, int], size: tuple[int, int] | None = None) -> np.ndarray:
    left, top, right, bottom = (max(0, bounds[0]), max(0, bounds[1]),
                                min(shot.shape[1], bounds[2]), min(shot.shape[0], bounds[3]))
    region = shot[top:bottom, left:right]
    if size and region.size and (region.shape[1], region.shape[0]) != size:
        region = np.asarray(Image.fromarray((region * 255).astype(np.uint8)).resize(size, Image.BILINEAR),
                            dtype=np.float32) / 255.0
    return region


def distinguishing_mark(screenshot_png: bytes, target: tuple[int, int, int, int],
                        alikes: list[tuple[int, int, int, int]], layout: tuple[int, int]
                        ) -> tuple[bytes, tuple[float, float, float, float]] | None:
    """The part of ``target`` that differs from its look-alikes: (PNG of that part, its box as
    fractions of the element). None if they look identical (nothing to tell them apart by).
    Bounds are layout coordinates."""
    shot = load_gray(screenshot_png)
    scale = shot.shape[1] / layout[0] if layout[0] else 1.0

    def scaled(bounds):
        return tuple(int(round(v * scale)) for v in bounds)

    picked = _crop_gray(shot, scaled(target))
    if picked.size == 0 or not alikes:
        return None
    size = (picked.shape[1], picked.shape[0])
    diffs = [np.abs(picked - _crop_gray(shot, scaled(other), size)) for other in alikes]
    diff = np.mean([d for d in diffs if d.shape == picked.shape], axis=0)
    # Smooth so a digit's strokes form one region, then keep what clearly stands out.
    kernel = max(3, min(size) // 12)
    smooth = np.asarray(Image.fromarray((np.clip(diff, 0, 1) * 255).astype(np.uint8)).resize(
        (max(1, size[0] // kernel), max(1, size[1] // kernel)), Image.BOX).resize(size, Image.BILINEAR),
        dtype=np.float32) / 255.0
    threshold = max(0.06, float(smooth.max()) * 0.5)
    mask = smooth > threshold
    if mask.mean() < MIN_MARK_FRACTION:
        return None
    rows, cols = np.where(mask)
    pad = max(2, kernel // 2)
    top, bottom = max(0, rows.min() - pad), min(size[1], rows.max() + pad + 1)
    left, right = max(0, cols.min() - pad), min(size[0], cols.max() + pad + 1)
    patch = picked[top:bottom, left:right]
    out = io.BytesIO()
    Image.fromarray((patch * 255).astype(np.uint8)).save(out, "PNG")
    box = (left / size[0], top / size[1], right / size[0], bottom / size[1])
    return out.getvalue(), box


def mark_score(shot: np.ndarray, mark: np.ndarray, bounds: tuple[int, int, int, int],
               box: tuple[float, float, float, float]) -> float:
    """How much the mark area of an element at ``bounds`` (screenshot pixels) looks like ``mark``."""
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    area = (int(bounds[0] + box[0] * width), int(bounds[1] + box[1] * height),
            int(bounds[0] + box[2] * width), int(bounds[1] + box[3] * height))
    if area[2] - area[0] < 2 or area[3] - area[1] < 2:
        return 0.0
    return similarity(shot, mark, area, pad=3)


def pick_by_mark(scores: list[float]) -> int | None:
    """Index of the clear winner among candidates' mark scores, or None if none is clearly it."""
    if not scores:
        return None
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    best = scores[ranked[0]]
    runner_up = scores[ranked[1]] if len(ranked) > 1 else -1.0
    if best >= MARK_THRESHOLD and best - runner_up >= MARK_MARGIN:
        return ranked[0]
    return None
