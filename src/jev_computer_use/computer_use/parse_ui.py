"""RapidOCR screen parsing → element inventory (the coordinate ground truth).

Element text + boxes feed the Jev state; Jev/VLMs only reference element *ids*, never pixels.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from rapidocr_onnxruntime import RapidOCR

_engine = None
_GRACE = "RapidOCR not installed. Run: uv pip install rapidocr-onnxruntime"


@dataclass
class Element:
    id: int
    text: str
    box: tuple[int, int, int, int]  # x1, y1, x2, y2
    score: float

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def to_state_line(self) -> str:
        cx, cy = self.center
        text = self.text.replace("\n", " ").strip()
        if len(text) > 40:
            text = text[:40] + "…"
        return f'{self.id}. "{text}" @ ({cx},{cy})'


def get_engine() -> RapidOCR:
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            try:
                from rapidocr import RapidOCR  # v2 package name
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(_GRACE) from exc
        _engine = RapidOCR()
    return _engine


def parse(img: Image.Image, max_elements: int = 40) -> list[Element]:
    """OCR an image into an element inventory (capped at `max_elements`)."""
    arr = np.asarray(img)
    out = get_engine()(arr)
    result = out[0] if isinstance(out, tuple) else out
    elements: list[Element] = []
    for i, detect in enumerate(result or []):
        if isinstance(detect, dict):  # v2 result dicts
            box = detect.get("box")
            text = detect.get("text", "")
            score = detect.get("score", 0.0)
        else:  # v1 positional: [box(4x2), text, score]
            box, text, score = detect
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        int_box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        elements.append(Element(id=i + 1, text=str(text), box=int_box, score=float(score)))
        if len(elements) >= max_elements:
            break
    return elements


def state_text(elements: list[Element]) -> str:
    return "\n".join(e.to_state_line() for e in elements)