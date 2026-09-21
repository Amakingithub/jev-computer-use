"""Typed question builders — one place for question constant shapes.

Jev rules this module encodes:
- one atomic judgment per question (a human answers it in < 1 s with the right context);
- `instructions` carries the literal question; the question *id* is code-only (not sent to the model);
- batch everything that shares a state into ONE call (speculative fan-out is ~free).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

QuestionType = Literal["choice", "score", "noul"]

Criteria = dict[str, Any] | list[Any]


@dataclass
class Question:
    key: str
    type: QuestionType
    instructions: str
    criteria: Any = field(default=None)

    def to_api(self) -> dict[str, Any]:
        q: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            q["criteria"] = self.criteria
        return q

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Question({self.key!r}, {self.type})"


def choice(key: str, instructions: str, options: dict[str, str]) -> Question:
    """Pick one of a known set (2..255 options). Add an 'other'/none option when unsure."""
    if not options:
        raise ValueError("choice needs at least one option")
    if len(options) > 255:
        raise ValueError("choice supports at most 255 options")
    return Question(key=key, type="choice", instructions=instructions, criteria=options)


def score(key: str, instructions: str, levels: list[str]) -> Question:
    """Rate on your own ordered levels (2..10). levels[0] is the lowest."""
    if not 2 <= len(levels) <= 10:
        raise ValueError("score needs 2..10 levels")
    return Question(key=key, type="score", instructions=instructions, criteria=levels)


def noul(key: str, instructions: str, criteria: dict[str, str] | None = None) -> Question:
    """Yes/no probability question. criteria={'true':..., 'false':...} when clarity helps."""
    return Question(key=key, type="noul", instructions=instructions, criteria=criteria)