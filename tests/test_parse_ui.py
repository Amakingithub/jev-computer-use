"""Offline tests for the multi-provider OCR layer (no engine, no screen needed)."""
from __future__ import annotations

from PIL import Image

from jev_computer_use.computer_use.parse_ui import (
    Element,
    OcrError,
    _merge_gap_fill,
    _overlap_ratio,
    _to_elements,
    available_providers,
    get_provider,
    parse,
    state_text,
)


def test_to_elements_number_and_cap() -> None:
    reads = [("A", (0, 0, 10, 10), 0.9), ("B", (20, 0, 30, 10), 0.8), ("C", (40, 0, 50, 10), 0.7)]
    els = _to_elements(reads, source="rapid", max_elements=2)
    assert [e.id for e in els] == [1, 2]
    assert all(e.source == "rapid" for e in els)
    assert els[0].center == (5, 5)
    assert els[0].to_state_line().startswith('1. "A" @ (5,5)')


def test_to_elements_skips_empty_text() -> None:
    reads = [("", (0, 0, 10, 10), 0.9), ("ok", (20, 0, 30, 10), 0.9)]
    assert [e.text for e in _to_elements(reads, source="x", max_elements=10)] == ["ok"]


def test_unknown_provider_raises() -> None:
    try:
        get_provider("nope")
        raise AssertionError("expected OcrError")
    except OcrError as exc:
        assert "unknown OCR provider" in str(exc)
    try:
        parse(Image.new("RGB", (10, 10)), provider="nope")
        raise AssertionError("expected OcrError")
    except OcrError as exc:
        assert "unknown OCR provider" in str(exc)


def test_registry_contains_all_names() -> None:
    for name in ("rapid", "windows", "glm", "a11y"):
        p = get_provider(name)
        assert p.name == name


def test_available_providers_only_installed() -> None:
    names = available_providers()
    assert isinstance(names, list)
    assert "rapid" in names  # rapidocr installed in this venv by test setup
    assert "a11y" in names  # uiautomation installed in this venv by test setup
    # glm is a stub with a non-existent module → must never report available
    assert "glm" not in names


def test_overlap_ratio() -> None:
    assert _overlap_ratio((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert _overlap_ratio((0, 0, 10, 10), (100, 100, 110, 110)) == 0.0
    assert 0.0 < _overlap_ratio((0, 0, 10, 10), (5, 5, 15, 15)) < 1.0


def test_merge_gap_fill_dedups_by_text_and_box() -> None:
    primary = [Element(id=1, text="File", box=(0, 0, 40, 20), score=0.9, source="rapid")]
    secondary = [
        Element(id=1, text="File", box=(0, 0, 40, 20), score=0.9, source="windows"),   # same text → skipped
        Element(id=2, text="+", box=(60, 0, 70, 20), score=1.0, source="windows"),     # new glyph → added
        Element(id=3, text="Layout", box=(5, 5, 40, 20), score=0.9, source="windows"), # overlaps File heavily → skipped
    ]
    merged = _merge_gap_fill(primary, secondary, max_elements=10)
    assert [e.text for e in merged] == ["File", "+"]
    assert merged[1].id == 2  # renumbered past the primary set
    assert merged[1].source == "windows"


def test_merge_gap_fill_respects_cap() -> None:
    primary = Element(id=1, text="File", box=(0, 0, 40, 20), score=0.9, source="rapid")
    secondary = [
        Element(id=i, text=f"x{i}", box=(i * 50, 0, i * 50 + 30, 20), score=1.0, source="windows")
        for i in range(2, 20)
    ]
    merged = _merge_gap_fill([primary], secondary, max_elements=3)
    assert len(merged) == 3


def test_uia11y_reads_shifts_by_origin() -> None:
    """a11y boxes are ABSOLUTE screen coords; parse(origin=...) must shift them to image-local."""
    import jev_computer_use.computer_use.parse_ui as pu

    fake = [(("Add", (100, 200, 120, 220), 1.0))]
    calls: list[tuple[int, int]] = []

    def fake_reads(origin):
        calls.append(origin)
        ox, oy = origin
        return [(t, (x1 - ox, y1 - oy, x2 - ox, y2 - oy), s) for (t, (x1, y1, x2, y2), s) in fake]

    orig = pu._uia11y_reads
    pu._uia11y_reads = fake_reads
    try:
        els = pu.parse(Image.new("RGB", (10, 10)), provider="a11y", origin=(50, 60))
        assert calls == [(50, 60)]
        assert len(els) == 1
        assert els[0].text == "Add"
        assert els[0].box == (50, 140, 70, 160)  # abs (100,200)-(120,220) minus origin (50,60)
        assert els[0].source == "a11y"
    finally:
        pu._uia11y_reads = orig


def test_state_text_multiline() -> None:
    els = [
        Element(id=1, text="Open", box=(0, 0, 10, 10), score=1.0),
        Element(id=2, text="Save", box=(0, 20, 10, 30), score=1.0),
    ]
    out = state_text(els)
    assert out == '1. "Open" @ (5,5)\n2. "Save" @ (5,25)'