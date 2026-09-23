"""Tests for the change-box incremental reparse (tiptour frame-skip, refined)."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from jev_computer_use.computer_use.parse_ui import (
    Element,
    changed_region,
    incremental_merge,
    shift_elements,
)
from jev_computer_use.computer_use.run_agent import _InventoryCache


class Shot:
    def __init__(self, img: Image.Image, dhash: int) -> None:
        self.img = img
        self.dhash = dhash


def _white(w: int = 400, h: int = 300) -> Image.Image:
    return Image.new("L", (w, h), 255)


def _stamp(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=0)
    return img


def _args(ocr: str = "rapid") -> SimpleNamespace:
    return SimpleNamespace(ocr=ocr)


def test_changed_region_none_when_identical() -> None:
    a = np.asarray(_white())
    assert changed_region(a, a.copy()) is None


def test_changed_region_size_mismatch() -> None:
    a = np.asarray(np.zeros((10, 10), np.uint8))
    assert changed_region(a, np.zeros((5, 5), np.uint8)) is None


def test_changed_region_localized_change() -> None:
    a = np.asarray(_white())
    b = np.asarray(_stamp(_white(), (250, 100, 320, 180)))
    box = changed_region(a, b)
    assert box is not None
    x1, y1, x2, y2 = box
    assert x1 <= 250 + 6 and y1 <= 100 + 6  # padded
    assert x2 >= 320 and y2 >= 180


def test_changed_region_too_big_returns_none() -> None:
    a = np.asarray(_white())
    b = np.asarray(_stamp(_white(), (0, 0, 399, 299)))  # whole frame flipped
    assert changed_region(a, b) is None


def test_shift_elements() -> None:
    els = [Element(1, "x", (5, 5, 50, 40), 1.0)]
    out = shift_elements(els, 100, 50)
    assert out[0].box == (105, 55, 150, 90)
    assert els[0].box == (5, 5, 50, 40)  # original untouched


def test_incremental_merge_keeps_outside_drops_inside_renumbers() -> None:
    prev = [
        Element(1, "Title", (10, 10, 100, 30), 0.9),
        Element(2, "OK", (200, 200, 250, 230), 0.9),
    ]
    fresh = [Element(7, "Switch", (255, 110, 300, 150), 0.9)]
    out = incremental_merge(prev, fresh, (250, 100, 320, 180))
    assert [e.text for e in out] == ["Title", "Switch", "OK"]
    assert [e.id for e in out] == [1, 2, 3]
    assert out[1].box == (255, 110, 300, 150)


def test_incremental_merge_dedups_crop_edge_straddle() -> None:
    prev = [Element(1, "Pad text", (10, 10, 120, 30), 0.9)]
    fresh = [Element(2, "Pad text", (6, 6, 118, 28), 0.9)]  # re-read via crop edge pad
    out = incremental_merge(prev, fresh, (0, 0, 400, 40))
    assert [e.text for e in out] == ["Pad text"]
    assert len(out) == 1


def test_cache_identical_frame_reuses_without_ocr(monkeypatch) -> None:
    img = _white()
    cache = _InventoryCache()
    assert cache.get(_args(), Shot(img, 0), (0, 0), None) is None  # seeds
    cache.store([Element(1, "Title", (10, 10, 100, 30), 0.9)])
    called = {"n": 0}

    import jev_computer_use.computer_use.parse_ui as pu

    real = pu.parse

    def _fake(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(pu, "parse", _fake)
    els = cache.get(_args(), Shot(img, 0), (0, 0), None)
    assert [e.text for e in els] == ["Title"]
    assert called["n"] == 0  # identical frame → no OCR at all


def test_cache_change_box_incremental(monkeypatch) -> None:
    img1 = _white()
    cache = _InventoryCache()
    assert cache.get(_args(), Shot(img1, 0), (0, 0), None) is None  # seeds
    cache.store([
        Element(1, "Title", (10, 10, 100, 30), 0.9),
        Element(2, "OK", (200, 200, 250, 230), 0.9),
    ])
    # same frame again → reuse, and the diff base is refreshed
    els = cache.get(_args(), Shot(img1, 0), (0, 0), None)
    assert [e.text for e in els] == ["Title", "OK"]

    import jev_computer_use.computer_use.parse_ui as pu

    def _fake_parse(img, provider="rapid", origin=(0, 0)):
        assert provider == "rapid"
        assert img.width < 400  # a crop, not the full frame
        return [Element(1, "Switch", (5, 5, 50, 40), 0.9)]

    monkeypatch.setattr(pu, "parse", _fake_parse)

    img2 = _stamp(img1, (250, 100, 320, 180))
    merged = cache.get(_args(), Shot(img2, 0xFF), (0, 0), None)
    assert merged is not None
    texts = [e.text for e in merged]
    assert texts == ["Title", "Switch", "OK"]  # re-sorted + renumbered
    sw = next(e for e in merged if e.text == "Switch")
    assert sw.id == 2
    assert sw.box[0] >= 240 and sw.box[1] >= 90  # shifted back into capture-local coords
    assert sw.box[0] <= 250 + 6 + 50 and sw.box[2] <= 320 + 6


def test_cache_a11y_never_incremental(monkeypatch) -> None:
    img1 = _white()
    cache = _InventoryCache()
    assert cache.get(_args("a11y"), Shot(img1, 0), (0, 0), None) is None
    cache.store([Element(1, "Title", (10, 10, 100, 30), 0.9)])

    import jev_computer_use.computer_use.parse_ui as pu

    def _fake_parse(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("a11y provider must fall back to a full parse, not a crop")

    monkeypatch.setattr(pu, "parse", _fake_parse)
    img2 = _stamp(img1, (250, 100, 320, 180))
    assert cache.get(_args("a11y"), Shot(img2, 0xFF), (0, 0), None) is None  # full parse path