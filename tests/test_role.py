"""Tests for Element.role plumbing (arc-cua affordance) + the action-schema gate."""
from __future__ import annotations

from jev_computer_use.computer_use.parse_ui import (
    Element,
    _to_elements,
    shift_elements,
)
from jev_computer_use.computer_use.run_agent import _affordance_conflict


def test_to_elements_carries_role():
    reads = [("Save Setup", (0, 0, 10, 10), 0.9, "ButtonControl")]
    els = _to_elements(reads, source="a11y", max_elements=5)
    assert len(els) == 1
    assert els[0].role == "ButtonControl"


def test_to_elements_empty_role_default():
    reads = [("Label", (0, 0, 10, 10), 0.9, "")]
    els = _to_elements(reads, source="rapid", max_elements=5)
    assert els[0].role == ""


def test_element_defaults_role_empty():
    e = Element(id=1, text="x", box=(0, 0, 5, 5), score=1.0)
    assert e.role == ""


def test_shift_elements_propagates_role():
    e = Element(id=1, text="Ok", box=(10, 10, 40, 30), score=1.0,
                source="a11y", role="ButtonControl")
    shifted = shift_elements([e], 5, 7)[0]
    assert shifted.role == "ButtonControl"
    assert shifted.box == (15, 17, 45, 37)


def test_affordance_conflicts_on_button_typing():
    e = Element(id=1, text="Install", box=(0, 0, 10, 10), score=1.0,
                source="a11y", role="ButtonControl")
    reason = _affordance_conflict("type_text", e)
    assert reason is not None
    assert "can't take text" in reason


def test_affordance_allows_click_on_button():
    e = Element(id=1, text="Install", box=(0, 0, 10, 10), score=1.0,
                source="a11y", role="ButtonControl")
    assert _affordance_conflict("click_element", e) is None


def test_affordance_allows_typing_into_edit():
    e = Element(id=2, text="Name", box=(0, 0, 10, 10), score=1.0,
                source="a11y", role="EditControl")
    assert _affordance_conflict("type_text", e) is None
    assert _affordance_conflict("paste_text", e) is None


def test_affordance_never_fires_for_unknown_role():
    e = Element(id=3, text="whatever", box=(0, 0, 10, 10), score=1.0, source="rapid")
    assert _affordance_conflict("type_text", e) is None