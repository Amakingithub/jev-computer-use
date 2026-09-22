"""Thresholds + bound action sets for the computer-use loop. ONE place to review.

Rule (from the `jev` skill / AGENTS.md): thresholds and abstention logic live in CODE, never in
model output, and are NOT shared across question types.
"""
from __future__ import annotations

# --- decision gates (tune only after measuring agreement on your own logs) ---
# SAFE_NOUL calibration note 2026-09-21 (live, official channel, blank-Notepad demo):
# a *benign* single step (type_text into a focused empty editor) measured noul 0.59-0.66
# (0.61 generic prompt, 0.65 step-scoped prompt). 0.70 rejected it every time; 0.60 is a
# calibrated floor — genuinely destructive steps score far lower (send/delete/pay ≈ 0.0-0.3),
# and the per-step confirm + action_confidence + dHash stuck checks still apply on top.
SAFE_NOUL = 0.60          # safe_to_proceed noul must be >= this to act
ACTION_CONF = 0.60        # next_action confidence >= this to act without escalation
RISK_CONFIRM = 3          # step_risk score >= this forces --approval confirm if not already
MAX_STEPS = 10            # hard loop cap
# STUCK_DHASH calibration 2026-09-22: 4 mis-flagged a real wizard transition
# (License→Ready) as "no change"; with --window/--region crops the hash covers only the
# target app so real changes score higher. 2 is too tight (cursor blink + tiny text edits
# fractionally flip bits); 3 gives a real change a margin and still catches true stalls
# (the loop only aborts after STUCK_TRIES consecutive no-change steps anyway).
STUCK_DHASH = 3           # dhash bits below which = "no change" (256-bit hash)
STUCK_TRIES = 3           # consecutive no-change actions before we declare stuck
# STEP_DELAY calibration 2026-09-22: was 0.8 — the largest fixed per-step cost (bigger than OCR
# or Jev). Most Win32/HTA/Electron UIs repaint within 100-300 ms; the dHash verify + stuck logic
# still catch slow renders (a false "no change" just retries). For UWP/installers/wizard greeters
# that paint late, raise at call time with --delay (e.g. 0.8). Never raised back to a fixed burn.
STEP_DELAY = 0.3          # seconds between action and verification capture

# --- click-seq deterministic drive (NO Jev): code-side risk gate (2026-09-22) ---
# --click-seq clicks are user-authored, so the gate is a deterministic denylist instead of a
# Jev noul call: a label that implies an irreversible/commit action forces --approval confirm,
# and is REFUSED outright under --approval none. Thresholds live in code, per AGENTS.md.
RISKY_CLICK_HINTS = (
    "delete", "remove", "format", "wipe", "overwrite", "pay", "purchase", "buy", "checkout",
    "order", "install", "uninstall", "send", "submit", "apply", "accept", "agree", "save",
    "run", "execute",
)

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