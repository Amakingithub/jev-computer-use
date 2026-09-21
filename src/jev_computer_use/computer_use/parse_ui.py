"""Multi-provider OCR parsing → element inventory (the coordinate ground truth).

Cheap-first by design, fitted to this CPU (i7-7500U — no local 7B+ model):

- `rapid`   — RapidOCR (ONNX CPU, ~0.2 s). Default. Best text-box precision for UI labels.
- `windows` — Windows.Media.Ocr via WinRT (FREE, ships with Windows 11, zero model download,
              no API key). Reads glyphs/symbols ("+", "−", "=") that ONNX text OCR misses.
              ~0.4–0.9 s on this CPU.
- `glm`     — GLM-OCR style local vision OCR (optional, heavier). Escalation/deep-read only,
              NOT the per-step path. Import-guarded; skipped unless the engine is installed.
- `a11y`    — Windows UIAutomation accessibility tree (free, local, comtypes). 0 vision tokens:
              reads control Name + bounding rect straight from the OS. Catches icon-only
              buttons and widgets OCR cannot see at all. ~0.1–0.5 s walk (depth/node caps).
- `merge`   — rapid primary + windows + a11y gap-fill (dedupe by text/overlap).
- `auto`    — first installed provider that returns ≥1 element, in cascade order
              rapid → windows → glm → a11y.

a11y boxes are ABSOLUTE screen coords (WinRT/mss use the same 0,0 origin, so full-screen
capture maps 1:1). For sub-region captures pass origin=(x1,y1) so a11y boxes are shifted
into image-local coords exactly like OCR outputs.

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
_GRACE_A11Y = (
    "Windows UIAutomation not installed. Run: uv pip install uiautomation  "
    "(free, ships with Windows; also added as the 'uia11y' project extra)"
)


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


# UIAutomation control types that make useful agent targets ("actable" widgets).
_A11Y_ACTIONABLE = {
    "ButtonControl",
    "EditControl",
    "ListItemControl",
    "MenuItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "TabItemControl",
    "HyperlinkControl",
    "SliderControl",
    "TreeItemControl",
    "CustomControl",
}
_A11Y_MAX_DEPTH = 6
_A11Y_MAX_NODES = 400
_A11Y_MAX_SECONDS = 2.0  # wall-clock budget: a stuck/hung window must not freeze the loop


def _uia11y_reads(origin: RawBox = (0, 0)) -> list[RawRead]:
    """Walk the UIAutomation tree for actionable controls → (Name, abs_box, 1.0).

    Boxes come back in ABSOLUTE screen coords; `origin` is the capture-region origin
    (x1, y1) to shift into image-local coordinates. Pure COM reads — zero vision cost,
    which is exactly the injury a screen-reader ground truth fixes for icon-only buttons.
    A node cap + wall-clock budget keep the walk bounded on a CPU-first box.
    """
    try:
        import uiautomation as uia
    except ImportError:  # pragma: no cover - optional dep
        raise OcrError(_GRACE_A11Y) from None

    import time as _time

    started = _time.perf_counter()
    ctx = {"root": uia.GetRootControl(), "reads": [], "seen": set(), "nodes": 0}

    def emit(text: str, box: RawBox) -> None:
        # UIA walks both the caption-window and its content tree → identical controls
        # (same text + same box) can appear twice; collapse to one read.
        key = (text, box)
        if key in ctx["seen"]:
            return
        ctx["seen"].add(key)
        ctx["reads"].append((text, box, 1.0))

    def visit(ctrl, depth: int) -> None:
        if (ctx["nodes"] >= _A11Y_MAX_NODES
                or depth > _A11Y_MAX_DEPTH
                or _time.perf_counter() - started > _A11Y_MAX_SECONDS):
            return
        ctx["nodes"] += 1
        try:
            offscreen = bool(ctrl.IsOffscreen)
            rect = ctrl.BoundingRectangle
        except Exception:  # pragma: no cover - flaky COM element
            return
        if offscreen or rect is None:
            return
        x1, y1, x2, y2 = rect.left, rect.top, rect.right, rect.bottom
        w, h = x2 - x1, y2 - y1
        if w < 2 or h < 2:  # degenerate invisible box
            return
        ctype = getattr(ctrl, "ControlTypeName", "") or ""
        if ctype in _A11Y_ACTIONABLE:
            text = _safe_uia_name(getattr(ctrl, "Name", "") or "")
            if text:
                ox, oy = origin
                emit(text, (int(x1 - ox), int(y1 - oy), int(x2 - ox), int(y2 - oy)))
        try:
            for child in ctrl.GetChildren():
                visit(child, depth + 1)
        except Exception:  # pragma: no cover - flaky COM walk
            return

    try:
        for top in ctx["root"].GetChildren():
            visit(top, 1)
    except Exception:  # pragma: no cover - desktop root glitch
        pass
    return ctx["reads"]


def _safe_uia_name(raw: str) -> str:
    """UIA names can hold multibyte accessor/whitespace junk; keep it printable."""
    if raw is None:
        return ""
    return " ".join(raw.split()).strip()


class A11yUIAProvider(BaseProvider):
    """Windows UIAutomation (uiautomation wrapper on comtypes) — 0 vision tokens.

    Unlike OCR providers it does not read pixels; it reads the OS accessibility tree.
    Returns actionable controls (buttons, edits, list items…) with their accessible Name
    and absolute bounding rect. Catches icon-only controls OCR cannot see at all.
    """

    name = "a11y"

    def installed(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("uiautomation") is not None
        except Exception:  # pragma: no cover - defensive
            return False

    def parse_boxes(self, img: Image.Image) -> list[RawRead]:  # img unused: UIA walks live tree
        if not self.installed():
            raise OcrError(_GRACE_A11Y)
        return _uia11y_reads()


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
                "a11y": A11yUIAProvider(),
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


def parse(
    img: Image.Image,
    max_elements: int = 40,
    provider: str = "rapid",
    origin: tuple[int, int] = (0, 0),
) -> list[Element]:
    """OCR an image into an element inventory.

    provider: 'rapid' (default) | 'windows' | 'glm' | 'a11y' | 'merge' | 'auto'.
    origin:   (x1, y1) of the capture region in ABSOLUTE screen coords. Used only to
              shift a11y boxes back into image-local coordinates; OCR boxes are already
              image-local. Default (0,0) = full-screen capture (the common case).
    """
    known = ("rapid", "windows", "glm", "a11y", "merge", "auto")
    if provider not in known:
        raise OcrError(f"unknown OCR provider {provider!r}; known: {', '.join(known)}")

    def _run(name: str, cap: int | None = None) -> list[Element]:
        p = get_provider(name)
        if not p.installed():
            raise OcrError(f"{name}: engine not installed")
        if isinstance(p, A11yUIAProvider):
            return _to_elements(_uia11y_reads(origin), source=name, max_elements=cap or max_elements)
        return p.parse(img, max_elements=cap or max_elements)

    if provider == "merge":
        primary = _run("rapid")
        secondary: list[Element] = []
        for name in ("windows", "a11y"):
            try:
                secondary += _run(name, cap=max_elements * 2)
            except OcrError:
                continue
        return _merge_gap_fill(primary, secondary, max_elements=max_elements)

    if provider == "auto":
        last_error: Exception | None = None
        for name in ("rapid", "windows", "glm", "a11y"):
            p = get_provider(name)
            if not p.installed():
                last_error = OcrError(f"{name}: engine not installed")
                continue
            try:
                elements = _run(name)
            except Exception as exc:  # pragma: no cover - provider runtime error
                last_error = exc
                continue
            if elements:
                return elements
        raise OcrError(f"all OCR providers returned nothing ({last_error})") from last_error

    return _run(provider)


def state_text(elements: list[Element]) -> str:
    return "\n".join(e.to_state_line() for e in elements)