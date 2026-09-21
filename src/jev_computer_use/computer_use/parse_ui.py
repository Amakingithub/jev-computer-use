"""Multi-provider OCR parsing → element inventory (the coordinate ground truth).

Cheap-first by design, fitted to this CPU (i7-7500U — no local 7B+ model):

- `rapid`   — RapidOCR (ONNX CPU, ~0.2 s). Default. Best text-box precision for UI labels.
- `windows` — Windows.Media.Ocr via WinRT (FREE, ships with Windows 11, zero model download,
              no API key). Reads glyphs/symbols ("+", "−", "=") that ONNX text OCR misses.
              ~0.4–0.9 s on this CPU.
- `glm`     — GLM-OCR style local vision OCR (optional, heavier). Escalation/deep-read only,
              NOT the per-step path. Import-guarded; skipped unless the engine is installed.
- `merge`   — rapid primary + windows gap-fill (dedupe by text). Recovers glyph-only tokens.
- `auto`    — first installed provider that returns ≥1 element, in cascade order
              rapid → windows → glm.

Element text + boxes feed the Jev state; Jev/VLMs only reference element *ids*, never pixels.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from rapidocr_onnxruntime import RapidOCR

__all__ = [
    "Element",
    "parse",
    "state_text",
    "get_provider",
    "available_providers",
    "OcrError",
]

_GRACE_RAPID = "RapidOCR not installed. Run: uv pip install rapidocr-onnxruntime"
_GRACE_WINDOWS = (
    "Windows OCR (WinRT) packages not installed. Run:\n"
    "  uv pip install winrt-Windows.Media.Ocr winrt-Windows.Globalization winrt-Windows.Graphics.Imaging "
    "winrt-Windows.Storage winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-runtime"
)
_GRACE_GLM = "GLM-style OCR engine not installed (optional escalation provider)."


class OcrError(RuntimeError):
    """A specific OCR provider cannot run (missing engine/deps)."""


@dataclass
class Element:
    id: int
    text: str
    box: tuple[int, int, int, int]  # x1, y1, x2, y2
    score: float
    source: str = "ocr"  # which provider produced this element

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


RawBox = tuple[int, int, int, int]  # x1, y1, x2, y2 (absolute screen coords)
RawRead = tuple[str, RawBox, float]  # (text, box, score)


class BaseProvider:
    name = "base"

    def installed(self) -> bool:
        return False

    def parse_boxes(self, img: Image.Image) -> list[RawRead]:
        raise NotImplementedError

    # shared normalisation: number + cap + annotate source
    def parse(self, img: Image.Image, max_elements: int = 40) -> list[Element]:
        reads = self.parse_boxes(img)
        return _to_elements(reads, source=self.name, max_elements=max_elements)


class RapidOCRProvider(BaseProvider):
    name = "rapid"
    _engine: RapidOCR | None = None

    def installed(self) -> bool:
        return _rapid_available()

    def _get_engine(self) -> RapidOCR:
        if RapidOCRProvider._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                try:
                    from rapidocr import RapidOCR  # v2 package name
                except ImportError as exc:  # pragma: no cover
                    raise OcrError(_GRACE_RAPID) from exc
            RapidOCRProvider._engine = RapidOCR()
        return RapidOCRProvider._engine

    def parse_boxes(self, img: Image.Image) -> list[RawRead]:
        arr = np.asarray(img)
        out = self._get_engine()(arr)
        result = out[0] if isinstance(out, tuple) else out
        reads: list[RawRead] = []
        for detect in result or []:
            if isinstance(detect, dict):  # v2 result dicts
                box = detect.get("box")
                text = detect.get("text", "")
                score = detect.get("score", 0.0)
            else:  # v1 positional: [box(4x2), text, score]
                box, text, score = detect
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            int_box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
            reads.append((str(text), int_box, float(score)))
        return reads


class WindowsOCRProvider(BaseProvider):
    """Windows.Media.Ocr (WinRT) — free, local, ships with Windows 11.

    Catches glyph/symbol tokens ("+", "−", "=", "×") that ONNX text OCR cannot read,
    which is exactly the Calculator operator-key gap. Coordinate origin is the WinRT
    bitmap, i.e. the same 0,0 as mss screenshots, so boxes map 1:1.
    """

    name = "windows"

    def installed(self) -> bool:
        return _windows_import() is not None

    def parse_boxes(self, img: Image.Image) -> list[RawRead]:
        if not self.installed():
            raise OcrError(_GRACE_WINDOWS)
        words = asyncio.run(_winrt_ocr_words(img))
        reads: list[RawRead] = []
        for text, box in words:
            x1, y1, x2, y2 = box
            if x2 <= x1 or y2 <= y1:  # empty/degenerate box
                continue
            reads.append((text, (x1, y1, x2, y2), 1.0))
        return reads


class GLMOcrProvider(BaseProvider):
    """Local vision-OCR escalation provider (optional, heavier).

    Not the per-step path on this CPU. Kept import-guarded so the registry stays complete;
    only used explicitly via provider="glm" or as 'auto' last resort.
    """

    name = "glm"

    def installed(self) -> bool:
        try:
            import glmocr  # noqa: F401
            return True
        except ImportError:  # pragma: no cover - optional dep
            return False

    def parse_boxes(self, img: Image.Image) -> list[RawRead]:  # pragma: no cover - optional
        if not self.installed():
            raise OcrError(_GRACE_GLM)
        import glmocr

        result = glmocr.recognize(img)  # provider-specific return shape
        reads: list[RawRead] = []
        for item in result if isinstance(result, list) else []:
            text = item.get("text", "")
            xs = [p[0] for p in (item.get("box") or [])]
            ys = [p[1] for p in (item.get("box") or [])]
            if xs and ys:
                box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
                reads.append((text, box, float(item.get("score", 1.0))))
        return reads


def _rapid_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("rapidocr_onnxruntime") is not None or (
            importlib.util.find_spec("rapidocr") is not None
        )
    except Exception:  # pragma: no cover - defensive
        return False


def _windows_import():
    try:
        import winrt.windows.globalization  # noqa: F401
        import winrt.windows.graphics.imaging  # noqa: F401
        import winrt.windows.media.ocr  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401

        return True
    except Exception:  # pragma: no cover - optional dep
        return None


async def _winrt_ocr_words(img: Image.Image) -> list[tuple[str, RawBox]]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(data)
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:  # pragma: no cover - no recognizer language pack installed
        return []
    res = await engine.recognize_async(bmp)

    out: list[tuple[str, RawBox]] = []
    for line in res.lines:
        for word in line.words:
            r = word.bounding_rect
            text = word.text.strip()
            if text:
                out.append((text, (round(r.x), round(r.y), round(r.x + r.width), round(r.y + r.height))))
    return out


_PROVIDERS: dict[str, BaseProvider] = {}


def _registry() -> dict[str, BaseProvider]:
    if not _PROVIDERS:
        _PROVIDERS.update(
            {
                "rapid": RapidOCRProvider(),
                "windows": WindowsOCRProvider(),
                "glm": GLMOcrProvider(),
            }
        )
    return _PROVIDERS


def get_provider(name: str) -> BaseProvider:
    try:
        return _registry()[name]
    except KeyError:
        raise OcrError(f"unknown OCR provider {name!r}; known: {', '.join(_registry())}") from None


def available_providers() -> list[str]:
    """Providers whose engine is actually installed/usable right now."""
    return [name for name, p in _registry().items() if p.installed()]


def _to_elements(reads: list[RawRead], *, source: str, max_elements: int) -> list[Element]:
    elements: list[Element] = []
    for i, (text, box, score) in enumerate(reads):
        text = str(text).strip()
        if not text or box is None:
            continue
        elements.append(Element(id=i + 1, text=text, box=box, score=float(score), source=source))
        if len(elements) >= max_elements:
            break
    return elements


def _overlap_ratio(a: RawBox, b: RawBox) -> float:
    a_x1, a_y1, a_x2, a_y2 = a
    b_x1, b_y1, b_x2, b_y2 = b
    ix = max(0, min(a_x2, b_x2) - max(a_x1, b_x1))
    iy = max(0, min(a_y2, b_y2) - max(a_y1, b_y1))
    inter = ix * iy
    small = min((a_x2 - a_x1) * (a_y2 - a_y1), (b_x2 - b_x1) * (b_y2 - b_y1))
    return inter / small if small > 0 else 0.0


def _merge_gap_fill(primary: list[Element], secondary: list[Element], max_elements: int) -> list[Element]:
    """primary kept as-is; secondary only adds elements whose text is new and whose box
    does not substantially overlap an existing element (avoids double-counting the same
    control read by both engines)."""
    out = list(primary)
    seen_text: set[str] = {e.text.lower() for e in primary}
    for e in secondary:
        if len(out) >= max_elements:
            break
        key = e.text.lower()
        if key in seen_text:
            continue
        if any(_overlap_ratio(e.box, o.box) > 0.6 for o in out):
            continue
        # renumber as a fresh id past the existing set
        fresh_id = out[-1].id + 1 if out else 1
        e.id = fresh_id
        out.append(e)
        seen_text.add(key)
    return out


def parse(img: Image.Image, max_elements: int = 40, provider: str = "rapid") -> list[Element]:
    """OCR an image into an element inventory.

    provider: 'rapid' (default) | 'windows' | 'glm' | 'merge' | 'auto'.
    """
    if provider not in ("rapid", "windows", "glm", "merge", "auto"):
        raise OcrError(f"unknown OCR provider {provider!r}; known: rapid, windows, glm, merge, auto")

    if provider == "merge":
        primary = get_provider("rapid").parse(img, max_elements=max_elements)
        if get_provider("windows").installed():
            secondary = get_provider("windows").parse(img, max_elements=max_elements * 2)
            return _merge_gap_fill(primary, secondary, max_elements=max_elements)
        return primary

    if provider == "auto":
        last_error: Exception | None = None
        for name in ("rapid", "windows", "glm"):
            p = get_provider(name)
            if not p.installed():
                last_error = OcrError(f"{name}: engine not installed")
                continue
            try:
                elements = p.parse(img, max_elements=max_elements)
            except Exception as exc:  # pragma: no cover - provider runtime error
                last_error = exc
                continue
            if elements:
                return elements
        raise OcrError(f"all OCR providers returned nothing ({last_error})") from last_error

    return get_provider(provider).parse(img, max_elements=max_elements)


def state_text(elements: list[Element]) -> str:
    return "\n".join(e.to_state_line() for e in elements)