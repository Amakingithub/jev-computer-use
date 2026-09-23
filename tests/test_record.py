"""Tests for the --record / replay pipeline (annotated PNGs + GIF assembly)."""
from __future__ import annotations

from PIL import Image

from jev_computer_use.computer_use.parse_ui import Element
from jev_computer_use.computer_use.record import (
    annotate_frame,
    assemble_gif,
    captions,
)
from jev_computer_use.computer_use.run_agent import _abs_box, _local


def _img(w: int = 300, h: int = 200) -> Image.Image:
    return Image.new("RGB", (w, h), (64, 64, 64))


def _element(i: int, box: tuple[int, int, int, int], text: str = "label") -> Element:
    return Element(id=i, text=text, box=box, score=0.9, source="test")


def test_annotate_returns_same_size_rgb():
    img = annotate_frame(_img(), [_element(1, (10, 20, 90, 60))], target=(10, 20, 90, 60))
    assert img.size == (300, 200)
    assert img.mode == "RGB"


def test_annotate_draws_chips_and_target():
    base = _img()
    out = annotate_frame(
        base.copy(),
        [_element(1, (10, 20, 90, 60), "Save Setup")],
        target=(10, 20, 90, 60),
        status="step 2 plan: click_element",
        cursor=(80, 120),
    )
    assert out.tobytes() != base.tobytes()


def test_annotate_empty_elements_no_crash():
    out = annotate_frame(_img(), [], status="idle")
    assert out.size == (300, 200)


def test_annotate_handles_empty_box_gracefully():
    out = annotate_frame(_img(), [_element(9, (0, 0, 0, 0))])
    assert out.size == (300, 200)


def test_annotate_label_extends_dimension():
    out = annotate_frame(_img(), [_element(3, (10, 200, 100, 300), "x" * 60)])
    assert out.size == (300, 200)


def test_assemble_gif_creates_animation(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    for i in range(3):
        annotate_frame(_img(), [_element(1, (5 + i, 5, 40 + i, 40))]).save(d / f"step_{i:03d}.png")
    gif = d / "out.gif"
    assert assemble_gif(d, gif) == 3
    assert gif.exists()
    with Image.open(gif) as im:
        assert im.n_frames == 3
        assert im.size[0] <= 300


def test_assemble_gif_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert assemble_gif(d, d / "x.gif") == 0


def test_assemble_gif_downscales_wide_frames(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    annotate_frame(_img(2000, 100), []).save(d / "step_001.png")
    annotate_frame(_img(2000, 100), []).save(d / "step_002.png")
    gif = d / "out.gif"
    assemble_gif(d, gif, width=960)
    with Image.open(gif) as im:
        assert im.size[0] == 960


def test_captions_parses_jsonl_only_valid_lines(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "steps.jsonl").write_text(
        '{"step": 1, "action": "click_element"}\nnot-json\n{"step": 2}\n', encoding="utf-8"
    )
    caps = captions(d)
    assert [c.get("step") for c in caps] == [1, 2]


def test_captions_missing_file(tmp_path):
    assert captions(tmp_path / "nope") == []


def test_abs_box_shifts_to_screen():
    assert _abs_box((10, 20, 90, 60), (1920, 50)) == (1930, 70, 2010, 110)


def test_abs_box_zero_origin_identity():
    assert _abs_box((10, 20, 90, 60), (0, 0)) == (10, 20, 90, 60)


def test_local_roundtrip_with_abs():
    origin = (400, 150)
    pt = _abs_box((10, 20, 90, 60), origin)
    center = ((pt[0] + pt[2]) // 2, (pt[1] + pt[3]) // 2)
    assert _local(center, origin) == (50, 40)