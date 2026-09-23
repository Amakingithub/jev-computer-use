"""Optional tiny-LLM text writer for TYPE_TEXT (jev-ultrafast TEXT_VALUE lesson).

The decision layer picks WHAT to do but never writes prose into fields — the planner may say
"type_text into <full-name>" without providing the actual literals. Rather than ask a comment
field to be filled with fixed text, this OPTIONAL helper asks a small cheap chat model
(DeepSeek, default) to produce ONLY the user-facing value for that field, output validated as
the strict JSON object {"text": <string>} — no reasoning, no plan, no escape hatch.

Enabled only when all three are true, and only for goals whose step requests a text value:
  * `--text-helper` flag is set on the CLI,
  * `TEXT_MODEL_API_KEY` is in the environment,
  * the step's type_text/paste_text call leaves `text_value=None` for us to fill.
A strict {"text": ...} parse failure is treated as ESCALATE (never write a guess).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..decision_layer import session

log = logging.getLogger("jev")

# Output MUST be exactly this shape — anything else is discarded (jev-ultrafast contract).
TEXT_JSON_SCHEMA = {
    "text": "the exact user-facing value to write into the field (nothing else, no code fences)",
}
_MAX_TEXT_LEN = 2000


class TextHelperError(RuntimeError):
    """Missing config or a failed validate-generate round. Caller escalates."""


class NotConfigured(TextHelperError):
    pass


def _config() -> tuple[str, str, str]:
    key = os.getenv("TEXT_MODEL_API_KEY", "").strip()
    if not key:
        raise NotConfigured("TEXT_MODEL_API_KEY is not set")
    base = os.getenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("TEXT_MODEL", "deepseek-chat").strip()
    return key, base, model


def configured() -> bool:
    return bool(os.getenv("TEXT_MODEL_API_KEY", "").strip())


def _reasoning_switch(base: str) -> dict[str, Any]:
    if "deepseek.com" in base:
        return {"thinking": {"type": "disabled"}}
    return {"reasoning": {"enabled": True, "effort": "low"}}


def generate_text(
    *,
    goal: str,
    field_label: str,
    inventory: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
    constraints: str = "",
) -> str:
    """Answer the field value from {goal, field_label, inventory, history}. Strict JSON.

    Raises TextHelperError on any failure — run_agent turns that into an ESCALATE step.
    """
    key, base, model = _config()
    system = (
        "You write ONE user-facing value for a UI automation agent. Given the goal, the field "
        f"label, and the visible controls, return a single JSON object of this EXACT shape: "
        f"{json.dumps(TEXT_JSON_SCHEMA, ensure_ascii=False)}. "
        "Rules: only a literal value the user would type (no explanations, no markdown, no "
        "escapes); empty string if the label is not a text-entry field; do not invent names or "
        "numbers that are not implied by the goal; keep it compact (<= 2000 characters)."
    )
    user_payload = {
        "goal": goal,
        "field_label": field_label,
        "visible_controls": inventory,
        "history": history or [],
    }
    if constraints:
        user_payload["constraints"] = constraints
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        **_reasoning_switch(base),
    }
    try:
        r = session().post(f"{base}/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {key}"}, timeout=30.0)
    except Exception as exc:
        raise TextHelperError(f"text-helper transport error: {exc}") from exc
    if r.status_code >= 400:
        raise TextHelperError(f"text-helper HTTP {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TextHelperError(f"text-helper response unparseable: {exc}") from exc
    if not isinstance(parsed, dict) or set(parsed.keys()) != {"text"}:
        raise TextHelperError(f"text-helper returned unexpected shape: {parsed!r}")
    text = parsed["text"]
    if not isinstance(text, str) or not text.strip():
        raise TextHelperError("text-helper returned empty text")
    if len(text) > _MAX_TEXT_LEN:
        raise TextHelperError(f"text-helper text too long ({len(text)} > {_MAX_TEXT_LEN})")
    log.info("text-helper produced %d chars for %r", len(text), field_label)
    return text