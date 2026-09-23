"""Tests for the click-seq label matcher incl. the edit-distance-1 fuzzy pass."""
from __future__ import annotations

from jev_computer_use.computer_use.parse_ui import Element
from jev_computer_use.computer_use.run_agent import (
    _edit_distance,
    _label_ratio,
    _narrow_box,
    match_element,
)


def _e(i: int, text: str, y: int = 10) -> Element:
    return Element(id=i, text=text, box=(0, y, 80, y + 20), score=0.9, source="test")


def test_exact_match_wins():
    els = [_e(1, "Next >"), _e(2, "Next")]
    assert match_element(els, "Next") is els[1]


def test_substring_matches_after_exact():
    els = [_e(1, "Next"), _e(2, "Next >")]
    assert match_element(els, "Next") is els[0]


def test_substring_tail():
    els = [_e(1, "Кнопка Next >")]
    assert match_element(els, "Next") is els[0]


def test_fuzzy_one_swapped_char():
    els = [_e(1, "setop")]  # OCR read 'Setup' as 'setop'
    assert match_element(els, "setup") is els[0]


def test_fuzzy_one_inserted_char():
    els = [_e(1, "savee")]
    assert match_element(els, "save") is els[0]


def test_fuzzy_one_deleted_char():
    els = [_e(1, "save")]
    assert match_element(els, "savee") is els[0]


def test_exact_beats_fuzzy():
    els = [_e(1, "setop"), _e(2, "setup")]
    assert match_element(els, "setup") is els[1]


def test_position_order_picks_first_fuzzy():
    els = [_e(1, "setop", y=100), _e(2, "savee", y=10)]
    assert match_element(els, "setup") is els[0]


def test_short_target_exact_only():
    els = [_e(1, "oak")]  # 'ok' has dist 1 but is too short to fuzzy-match
    assert match_element(els, "ok") is None


def test_distance_two_rejected():
    els = [_e(1, "abcd")]
    assert match_element(els, "abzz") is None


def test_empty_target_rejected():
    assert match_element([_e(1, "anything")], "   ") is None


def test_edit_distance_unit_values():
    assert _edit_distance("setup", "setop") == 1
    assert _edit_distance("save", "savee") == 1
    assert _edit_distance("abcd", "abzz") == 2
    assert _edit_distance("", "") == 0
    assert _edit_distance("ab", "cdef") >= 1  # early-bail path still rejects far strings


def test_label_ratio_narrows_right_word():
    a, b = _label_ratio("Next > Save Setup", "Setup")
    assert a > 0.5
    assert b == 1.0


def test_label_ratio_narrows_middle_word():
    a, b = _label_ratio("Next > Save Setup", "Save")
    assert 0.0 < a < b < 1.0


def test_label_ratio_full_match_is_none():
    assert _label_ratio("Save Setup", "save setup") is None


def test_label_ratio_empty_text_none():
    assert _label_ratio("", "Next") is None


def test_narrow_box_returns_sub_box():
    box = (100, 40, 400, 70)
    out = _narrow_box(box, "Next > Save Setup", "Setup")
    assert out[0] > box[0]
    assert out[2] <= box[2]
    assert out[1] == box[1] and out[3] == box[3]


def test_narrow_box_full_match_unchanged():
    box = (100, 40, 400, 70)
    assert _narrow_box(box, "Next > Save Setup", "Next > save Setup") == box