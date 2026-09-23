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
    available_actions: set[str] | None = None,
) -> list:
    options = {str(e.id): e.to_state_line() for e in elements}
    if not options:
        options = {"none": "no elements detected on screen"}
    options["__none__"] = "None of these elements would advance the goal (the needed control is absent or misread)"
    options["nonew"] = "act on the currently focused window (no element click needed)"
    action_options = qc.actions_for(goal)
    if available_actions is not None:
        action_options = {a: d for a, d in action_options.items() if a in available_actions}
    if not action_options:
        # never present an empty choice space: retain the terminal/escape actions so the model
        # can still signal completion, blockage or the need for a human.
        action_options = {
            a: d for a, d in qc.DEFAULT_ACTIONS.items() if a in ("done", "blocked", "escalate")
        }
    actions_instruction = (
        "Which action should run next to make progress on the goal?"
        if available_actions is None
        else (
            "Which action should run next to make progress on the goal? Only the actions listed "
            "below are executable right now — actions needing text/keystrokes whose budget is "
            "already spent are excluded, do NOT pick a type/paste/press action that is missing."
        )
    )
    qs = [
        choice(
            key="which_element",
            instructions=(
                "Which screen element should this task act on? Choose exactly the element that makes "
                "progress toward the goal. '__none__' = none of the listed elements is the control "
                "you need (the goal expects something not currently on screen). 'nonew' = act on the "
                "currently focused window (right for typing or keyboard shortcuts, which do not need "
                "a coordinate click). Pick a listed element only when the action must physically "
                "target that UI control."
            ),
            options=options,
        ),
        choice(
            key="next_action",
            instructions=actions_instruction,
            options=action_options,
        ),
        noul(
            key="task_done",
            instructions=(
                "Judging ONLY from the screen text above and your own previous steps, is the GOAL "
                "already fully completed — its result already visible on screen, nothing more needed?"
            ),
            criteria={
                "true": "Every requirement of the goal is visibly done on screen",
                "false": "At least one requirement is still missing, or its completion is unverified",
            },
        ),
        noul(
            key="control_absent",
            instructions=(
                "Is the UI control the goal needs NEXT genuinely absent from the screen — not just "
                "misread, but impossible to reach with any listed element or a keyboard shortcut?"
            ),
            criteria={
                "true": "The needed control is not among the listed elements and cannot be opened or used",
                "false": "The needed control is listed, or reachable by keyboard, or only "
                         "briefly hidden (menu/dialog not yet opened)",
            },
        ),
        noul(
            key="safe_to_proceed",
            instructions=(
                "Executing ONLY the action chosen in 'next_action' (applied to the element chosen in 'which_element'), "
                "starting right now, has no real downside and does not need a human watching. Rate just this one step, "
                "not the whole goal. Opening a menu or dialog (e.g. Ctrl+S opening a save dialog, Ctrl+V pasting text "
                "into a field) is NOT a side effect — nothing is written to disk, sent, or deleted by the step itself."
            ),
            criteria={
                "true": "This one step is reversible/benign (typing, navigation keys, opening a dialog or menu)",
                "false": (
                    "This step itself deletes, overwrites an existing file, sends, pays, installs, launches a shell, "
                    "or otherwise irreversibly changes state"
                ),
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
    history: list[dict] | None = None,
    available_actions: set[str] | None = None,
) -> ActionPlan:
    inventory = state_text(elements) or "(no text detected on screen)"
    state = {
        "goal": goal,
        "screen_elements": inventory,
        "instructions": extra_instructions or "",
        "previous_steps": history or [],
    }
    questions = build_questions(goal, inventory, elements, extra_instructions,
                                available_actions=available_actions)
    resp = layer.ask(state, questions)
    answers = resp.by_key()

    action = answers["next_action"].choice or "escalate"
    action_conf = answers["next_action"].confidence

    def _noul(key: str) -> float | None:
        ans = answers.get(key)
        return ans.noul if ans is not None else None  # a gateway may reply noul:null

    task_done_noul = _noul("task_done")
    absent_noul = _noul("control_absent")
    task_done = task_done_noul is not None and task_done_noul >= qc.TASK_DONE_NOUL
    control_absent = absent_noul is not None and absent_noul >= qc.CONTROL_ABSENT_NOUL

    elem_choice = answers.get("which_element")
    none_chosen = bool(elem_choice and elem_choice.choice == "__none__")
    element_id = None
    if elem_choice and elem_choice.choice and elem_choice.choice.isdigit():
        element_id = int(elem_choice.choice)  # nonew / none / __none__: element_id stays None
    safe = bool(answers.get("safe_to_proceed") and answers["safe_to_proceed"].noul >= qc.SAFE_NOUL)
    risk = answers.get("step_risk") and answers["step_risk"].score

    needs_confirm = bool(
        (action_conf is not None and action_conf < qc.ACTION_CONF)
        or (risk is not None and risk >= qc.RISK_CONFIRM)
    )

    # tiptour JevGrounding gates: never act on "none of these", stop early when already done.
    reason = "ok"
    if none_chosen and not task_done and action not in ("blocked", "escalate"):
        action = "blocked"
        reason = "model chose '__none__' — no element advances the goal" + (
            f" (control_absent={absent_noul:.2f})" if control_absent else ""
        )
    elif task_done and action not in ("blocked", "escalate", "done"):
        action = "done"
        reason = f"task_done gate ({task_done_noul:.2f} >= {qc.TASK_DONE_NOUL}) — goal already complete"
    elif action in ("blocked", "escalate"):
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