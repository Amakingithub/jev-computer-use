"""Unit tests for the decision layer (no network — heuristic + raw normalization)."""
from __future__ import annotations

import pytest

from jev_computer_use.decision_layer import (
    Answer,
    DecisionLayer,
    DecisionResponse,
    LocalHeuristicProvider,
    NoProviderAvailable,
    ProviderUnavailable,
)
from jev_computer_use.questions import choice, noul, score


def test_question_to_api_noul() -> None:
    q = noul("urgent", "Does this convey urgency?", {"true": "explicit", "false": "none"})
    assert q.to_api() == {
        "type": "noul",
        "instructions": "Does this convey urgency?",
        "criteria": {"true": "explicit", "false": "none"},
    }


def test_question_to_api_choice() -> None:
    q = choice("dept", "Which team?", {"billing": "payments", "technical": "bugs"})
    api = q.to_api()
    assert api["type"] == "choice"
    assert api["criteria"] == {"billing": "payments", "technical": "bugs"}


def test_question_bounds() -> None:
    with pytest.raises(ValueError):
        score("s", "rate?", ["only-one"])
    with pytest.raises(ValueError):
        choice("c", "which?", {f"k{i}": str(i) for i in range(256)})


def test_answer_from_raw_types() -> None:
    choice_a = Answer.from_raw(
        "dept",
        "choice",
        {"choice": "billing", "probabilities": {"billing": 0.88, "technical": 0.12}, "confidence": 0.81},
        "typesafe",
        "jev-1.13.0",
    )
    assert choice_a.choice == "billing"
    assert choice_a.confidence == 0.81
    assert choice_a.to_dict()["choice"] == "billing"

    noul_a = Answer.from_raw("urgent", "noul", {"noul": 0.95}, "cloudflare", "jev-1.13.0")
    assert noul_a.noul == 0.95

    sco = Answer.from_raw(
        "risk",
        "score",
        {"score": 1.05, "legend": {"0": "low", "1": "mid"}, "probabilities": {"0": 0.0, "1": 1.0}, "confidence": 0.9},
        "openrouter",
        "typesafe/jev-1.13-20260917",
    )
    assert sco.score == 1.05
    assert sco.legend == {"0": "low", "1": "mid"}


def test_response_by_key() -> None:
    resp = DecisionResponse(
        answers=[
            Answer.from_raw("a", "noul", {"noul": 0.9}, "typesafe"),
            Answer.from_raw("b", "choice", {"choice": "x", "confidence": 0.5}, "typesafe"),
        ],
        provider="typesafe",
        model="jev-1.13.0",
    )
    assert resp.by_key()["b"].choice == "x"
    assert resp.to_dict()["provider"] == "typesafe"


def test_heuristic_noul_only() -> None:
    prov = LocalHeuristicProvider()
    q = noul(
        "p",
        "Does the text mention a refund?",
        {"true": "the text requests a refund", "false": "no refund mentioned"},
    )
    resp = prov.ask("Please refund my duplicate charge.", [q])
    ans = resp.by_key()["p"]
    assert 0.0 <= ans.noul <= 1.0
    assert resp.provider == "heuristic"


def test_heuristic_rejects_choice() -> None:
    prov = LocalHeuristicProvider()
    with pytest.raises(ProviderUnavailable):
        prov.ask("x", [choice("c", "which?", {"a": "a", "b": "b"})])


def test_cascade_to_heuristic_for_noul() -> None:
    layer = DecisionLayer(providers=[LocalHeuristicProvider()])
    resp = layer.ask(
        "Requesting a refund, please.",
        [noul("p", "refund requested?", {"true": "refund", "false": "other"})],
    )
    assert resp.provider == "heuristic"


def test_cascade_raises_when_unanswerable() -> None:
    layer = DecisionLayer(providers=[LocalHeuristicProvider()])
    with pytest.raises(NoProviderAvailable) as ei:
        layer.ask("x", [choice("c", "which?", {"a": "a", "b": "b"})])
    assert any("only answers noul" in e for e in ei.value.errors)


def test_status_shows_providers() -> None:
    layer = DecisionLayer(providers=[])
    layer.providers = [LocalHeuristicProvider()]
    rows = layer.status()
    assert rows[0]["provider"] == "heuristic"
    assert rows[0]["configured"] is True