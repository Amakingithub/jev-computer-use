"""Tests for the tiptour-driven decision gates (__none__ escape hatch, task_done/control_absent)."""
from __future__ import annotations

from jev_computer_use.computer_use.decide import decide_plan
from jev_computer_use.computer_use.parse_ui import Element
from jev_computer_use.decision_layer import Answer, DecisionResponse


class FakeLayer:
    """Stands in for DecisionLayer: returns a scripted raw answer per question key."""

    def __init__(self, raws: dict[str, dict]) -> None:
        self.raws = raws

    def ask(self, state, questions, model=None):
        answers = []
        for q in questions:
            raw = dict(self.raws.get(q.key, {"type": q.type}))
            if q.type == "choice":
                keys = list(q.criteria.keys())
                c = raw.get("choice")
                raw.setdefault(
                    "probabilities", {k: (1.0 if k == c else 0.0) for k in keys}
                )
                raw.setdefault("confidence", 1.0)
            answers.append(Answer.from_raw(q.key, q.type, raw, provider="fake", model="fake"))
        return DecisionResponse(answers=answers, provider="fake", model="fake")


ELEMENTS = [Element(id=1, text="Name field", box=(10, 10, 120, 30), score=0.9)]


def _base() -> dict[str, dict]:
    return {
        "which_element": {"type": "choice", "choice": "1"},
        "next_action": {"type": "choice", "choice": "click_element"},
        "task_done": {"type": "noul", "noul": 0.1},
        "control_absent": {"type": "noul", "noul": 0.1},
        "safe_to_proceed": {"type": "noul", "noul": 0.9},
        "step_risk": {"type": "score", "score": 1},
    }


def test_normal_click_plan_unchanged() -> None:
    plan = decide_plan(FakeLayer(_base()), "fill the form", ELEMENTS)
    assert plan.action == "click_element"
    assert plan.element_id == 1
    assert plan.safe is True
    assert plan.needs_confirm is False


def test_none_escape_hatch_becomes_blocked() -> None:
    raws = _base()
    raws["which_element"] = {"type": "choice", "choice": "__none__"}
    raws["control_absent"] = {"type": "noul", "noul": 0.95}
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "blocked"
    assert plan.element_id is None
    assert "control_absent" in plan.reason


def test_none_escape_hatch_respects_explicit_escalate() -> None:
    raws = _base()
    raws["which_element"] = {"type": "choice", "choice": "__none__"}
    raws["next_action"] = {"type": "choice", "choice": "escalate"}
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "escalate"


def test_none_plus_done_gate_reports_done() -> None:
    raws = _base()
    raws["which_element"] = {"type": "choice", "choice": "__none__"}
    raws["next_action"] = {"type": "choice", "choice": "press_key"}
    raws["task_done"] = {"type": "noul", "noul": 0.97}
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "done"
    assert "task_done gate" in plan.reason


def test_task_done_overrides_a_planned_click() -> None:
    raws = _base()
    raws["task_done"] = {"type": "noul", "noul": 0.92}
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "done"
    assert plan.element_id == 1


def test_task_done_below_threshold_keeps_action() -> None:
    raws = _base()
    raws["task_done"] = {"type": "noul", "noul": 0.3}
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "click_element"


def test_omitted_gates_fail_safe() -> None:
    """A gateway that omits the new heads must yield a normal plan, never crash."""
    raws = _base()
    raws.pop("task_done")
    raws.pop("control_absent")
    plan = decide_plan(FakeLayer(raws), "fill the form", ELEMENTS)
    assert plan.action == "click_element"
    assert plan.element_id == 1


def test_empty_screen_still_offers_escape_and_nonew() -> None:
    raws = _base()
    raws["which_element"] = {"type": "choice", "choice": "__none__"}
    raws["control_absent"] = {"type": "noul", "noul": 0.9}
    plan = decide_plan(FakeLayer(raws), "fill the form", [])
    assert plan.action == "blocked"
    assert plan.element_id is None