"""Thresholds + bound action sets for the computer-use loop. ONE place to review.

Rule (from the `jev` skill / AGENTS.md): thresholds and abstention logic live in CODE, never in
model output, and are NOT shared across question types.
"""
from __future__ import annotations

# --- decision gates (tune only after measuring agreement on your own logs) ---
SAFE_NOUL = 0.70          # safe_to_proceed noul must be >= this to act
ACTION_CONF = 0.60        # next_action confidence >= this to act without escalation
RISK_CONFIRM = 3          # step_risk score >= this forces --approval confirm if not already
MAX_STEPS = 10            # hard loop cap
STUCK_DHASH = 2           # dhash bits below which = "no change" after an action
STUCK_TRIES = 3           # consecutive no-change actions before we declare stuck
STEP_DELAY = 0.8          # seconds between action and verification capture

# --- generic bounded action set (per goal, narrow it down) ---
DEFAULT_ACTIONS: dict[str, str] = {
    "click_element": "Click the selected element",
    "type_text": "Type text into the selected element",
    "press_key": "Press a special key (Enter, Tab, Esc, Ctrl+S, ...)",
    "scroll": "Scroll the current view",
    "done": "The goal is complete",
    "blocked": "Stuck — no way to make progress with the available elements",
    "escalate": "Requires vision/reasoning a human or a VLM must provide",
}

# Goal -> allowed action subset. Add rules here; the decide step only offers these options.
GOAL_ACTION_MAP: dict[str, list[str]] = {
    "form": ["click_element", "type_text", "press_key", "scroll", "done", "blocked", "escalate"],
    "text": ["click_element", "type_text", "press_key", "scroll", "done", "blocked", "escalate"],
    "browse": ["click_element", "type_text", "press_key", "scroll", "done", "blocked", "escalate"],
    "default": ["click_element", "type_text", "press_key", "scroll", "done", "blocked", "escalate"],
}

RISK_LEVELS = [
    "No side effects (navigation, reading, focusing)",
    "Low risk (single click / keystroke, easily undone)",
    "Moderate risk (multi-step, data entry, menu actions)",
    "High risk — destructive / sensitive (delete, save/persist, send, pay, install, shell)",
]


def actions_for(goal: str) -> dict[str, str]:
    key = next((k for k in GOAL_ACTION_MAP if k.lower() in goal.lower()), "default")
    allowed = GOAL_ACTION_MAP[key]
    return {a: d for a, d in DEFAULT_ACTIONS.items() if a in allowed}