"""Capture + perceptual-change verification (no GPU, no heavy deps)."""
from __future__ import annotations

from dataclasses import dataclass

import mss
from PIL import Image

try:  # Pillow >= 9.1
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - old Pillow
    RESAMPLE = Image.LANCZOS

Region = tuple[int, int, int, int]  # x1, y1, x2, y2 (absolute screen coords)


@dataclass
class Shot:
    img: Image.Image
    dhash: int
    region: Region | None = None


def _dhash(img: Image.Image, size: int = 16) -> int:
    """Perceptual hash: compare neighbouring pixels per row. 256-bit by default.

    16x16 (256 bits) so subtle changes in a mostly-blank editor register:
    an 8x8 hash was so coarse that typing a single word on a 1920x1080 screen
    could flip < 2 bits and be mis-classified as "no change" (2026-09-21).
    """
    gray = img.convert("L").resize((size + 1, size), RESAMPLE)
    px = gray.load()
    h = 0
    for y in range(size):
        for x in range(size):
            h <<= 1
            if px[x, y] > px[x + 1, y]:
                h |= 1
    return h


def hamming(a: int, b: int) -> int:
    """Bits that differ between two dhashes (0 = identical, up to 64)."""
    return (a ^ b).bit_count()


def capture(region: Region | None = None, monitor: int = 1) -> Shot:
    """Full-screen (monitor index) or regional capture. Region is absolute screen coords."""
    with mss.mss() as sct:
        if region is None:
            mon = sct.monitors[monitor]
            box = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            x1, y1, x2, y2 = region
            box = {"left": x1, "top": y1, "width": max(1, x2 - x1), "height": max(1, y2 - y1)}
        raw = sct.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        return Shot(img=img, dhash=_dhash(img), region=region)


def changed(a: Shot, b: Shot, threshold: int = 2) -> bool:
    """True when the screen moved more than `threshold` dhash bits."""
    return hamming(a.dhash, b.dhash) > threshold