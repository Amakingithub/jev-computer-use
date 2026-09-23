"""Per-step annotated recording + GIF replay (`--record` / `je (see)v-computer-use replay`).

2026-09-23 (jev-ultrafast record->demo, tiptour /v1/action-history lesson): the live GDI
overlay is only visible DURING the run; `--record` renders the same annotations into a PNG
per step plus a steps.jsonl, and `replay` stitches them into a watchable GIF with captions —
you can see what the agent saw/planned even when you weren't looking.

Renders with Pillow (already a dependency) so it works headless/offline and is unit-testable,
unlike the GDI overlay. All helpers are pure and never touch the screen.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .parse_ui import Element

_FRAME = (0, 120, 255)
_TARGET_FILL = (255, 230, 0)
_TARGET_EDGE = (160, 0, 0)
_RIBBON = (40, 40, 40)
_TEXT = (255, 255, 255)
_CHIP_BG = (25, 25, 25)
_ARROW = (220, 0, 0)
_MAX_LABEL = 34


def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, 12)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate_frame(
    img: Image.Image,
    elements: list[Element],
    target: tuple[int, int, int, int] | None = None,
    status: str = "",
    cursor: tuple[int, int] | None = None,
) -> Image.Image:
    """Render the same info as the GDI guide onto a copy of the frame (image-local coords)."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font()
    w, h = out.size

    for e in elements:
        x1, y1, x2, y2 = e.box
        if x2 <= x1 or y2 <= y1:
            continue
        draw.rectangle([x1, y1, x2, y2], outline=_FRAME, width=1)
        label = f"{e.id}. {e.text}"[:_MAX_LABEL].replace("\n", " ")
        cx1 = min(max(0, x1), w - 40)
        cy1 = max(0, y1 - 16) if y1 >= 40 else y1 + 2
        cy1 = min(cy1, h - 16)
        draw.rectangle([cx1, cy1, min(w, cx1 + 120), cy1 + 15], fill=_CHIP_BG)
        draw.text((cx1 + 3, cy1), label, fill=_TEXT, font=font)

    if target:
        tx1, ty1, tx2, ty2 = target
        draw.rectangle([tx1, ty1, tx2, ty2], fill=_TARGET_FILL, outline=_TARGET_EDGE, width=2)
    if cursor:
        cx, cy = cursor
        draw.line([(cx, cy), (cx + 12, cy + 12)], fill=_ARROW, width=2)
    if status:
        draw.rectangle([0, h - 32, w, h], fill=_RIBBON)
        draw.text((8, h - 27), status[:_MAX_LABEL * 2], fill=_TEXT, font=font)
    return out


def assemble_gif(
    record_dir: Path,
    out: Path,
    width: int = 960,
    duration_ms: int = 700,
) -> int:
    """Stitch `step_*.png` from a --record dir into an animated GIF; returns frame count."""
    frames = sorted(record_dir.glob("step_*.png"))
    if not frames:
        return 0
    imgs: list[Image.Image] = []
    for p in frames:
        im = Image.open(p).convert("RGB")
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)))
        imgs.append(im)
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=duration_ms, loop=0)
    return len(imgs)


def captions(record_dir: Path) -> list[dict]:
    """Read steps.jsonl produced by --record (missing file -> empty list)."""
    path = record_dir / "steps.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out