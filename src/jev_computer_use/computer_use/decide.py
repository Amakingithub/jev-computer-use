"""Jev decides the bounded next step from the OCR element inventory — never pixels.

One batched Jev call per step (speculative fan-out): which element, which action, is it safe,
how risky. Thresholds live in `questions_computer_use.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..decision_layer import DecisionLayer
from ..questions import choice, noul, score
from . import questions_computer_use as qc
from .parse_ui import Element, state_text


@dataclass
class ActionPlan:
    action: str
    element_id: int | None
    action_confidence: float | None
    safe: bool
    risk: float | None
    needs_confirm: bool
    reason: str
    provider: str
    model: str | None
    answers: dict = field(default_factory=dict)

    @property
    def escalate(self) -> bool:
        return self.action == "escalate" or not self.safe or self.needs_confirm


def build_questions(
    goal: str,
    inventory_text: str,
    elements: list[Element],
    extra_instructions: str | None = None,
) -> list:
    options = {str(e.id): e.to_state_line() for e in elements}
    if not options:
        options = {"none": "no elements detected on screen"}
    qs = [
        choice(
            key="which_element",
            instructions="Which screen element should this task act on?",
            options=options,
        ),
        choice(
            key="next_action",
            instructions="Which action should run next to make progress on the goal?",
            options=qc.actions_for(goal),
        ),
        noul(
            key="safe_to_proceed",
            instructions="It is safe to execute the chosen action automatically right now.",
            criteria={
                "true": "Executing this action has no destructive, persistent, or irreversible side effects",
                "false": "It could damage, delete, send, spend, or irreversibly change something",
            },
        ),
        score(
            key="step_risk",
            instructions="How risky is the chosen action?",
            levels=qc.RISK_LEVELS,
        ),
    ]
    return qs


def decide_plan(
    layer: DecisionLayer,
    goal: str,
    elements: list[Element],
    extra_instructions: str | None = None,
) -> ActionPlan:
    inventory = state_text(elements) or "(no text detected on screen)"
    state = {
        "goal": goal,
        "screen_elements": inventory,
        "instructions": extra_instructions or "",
    }
    questions = build_questions(goal, inventory, elements, extra_instructions)
    resp = layer.ask(state, questions)
    answers = resp.by_key()

    action = answers["next_action"].choice or "escalate"
    action_conf = answers["next_action"].confidence
    element_id = None
    elem_choice = answers.get("which_element")
    if elem_choice and elem_choice.choice and elem_choice.choice.isdigit():
        element_id = int(elem_choice.choice)
    safe = bool(answers.get("safe_to_proceed") and answers["safe_to_proceed"].noul >= qc.SAFE_NOUL)
    risk = answers.get("step_risk") and answers["step_risk"].score

    needs_confirm = bool(
        (action_conf is not None and action_conf < qc.ACTION_CONF)
        or (risk is not None and risk >= qc.RISK_CONFIRM)
    )

    reason = "ok"
    if action in ("blocked", "escalate"):
        reason = f"model chose {action!r}: " + (qc.DEFAULT_ACTIONS.get(action) or action)
    elif not safe:
        reason = f"risk gate failed (noul={answers['safe_to_proceed'].noul:.2f} < {qc.SAFE_NOUL})"

    return ActionPlan(
        action=action,
        element_id=element_id,
        action_confidence=action_conf,
        safe=safe,
        risk=risk,
        needs_confirm=needs_confirm,
        reason=reason,
        provider=resp.provider,
        model=resp.model,
        answers={k: a.to_dict() for k, a in answers.items()},
    )