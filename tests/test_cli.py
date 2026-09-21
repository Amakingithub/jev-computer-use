"""Tests for the jev-decision CLI argument/question parsing (offline)."""
from __future__ import annotations

import pytest

from jev_computer_use.decision_cli import (
    build_parser,
    collect_questions,
    parse_choice_arg,
    parse_noul_arg,
    parse_score_arg,
)


def test_parse_noul_simple() -> None:
    q = parse_noul_arg("urgent|Does this convey urgency?")
    assert q.key == "urgent"
    assert q.type == "noul"
    assert q.criteria is None


def test_parse_noul_with_criteria() -> None:
    q = parse_noul_arg("p|refund?|true=A refund is requested|false=No refund requested")
    assert q.criteria == {"true": "A refund is requested", "false": "No refund requested"}


def test_parse_choice() -> None:
    q = parse_choice_arg("dept|Which team?|billing=Payments, invoices;technical=Bugs, outages")
    assert q.type == "choice"
    assert q.criteria == {"billing": "Payments, invoices", "technical": "Bugs, outages"}


def test_parse_choice_without_desc() -> None:
    q = parse_choice_arg("color|Which one?|red;green")
    assert q.criteria == {"red": "", "green": ""}


def test_parse_score() -> None:
    q = parse_score_arg("frustration|How frustrated?|Calm|Frustrated|Very angry")
    assert q.type == "score"
    assert q.criteria == ["Calm", "Frustrated", "Very angry"]


def test_parse_score_too_few_levels() -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        parse_score_arg("s|rate?|only")


def test_collect_questions_composition() -> None:
    parser = build_parser()
    ns = parser.parse_args([
        "--noul", "a|is x",
        "--choice", "b|which?|a=aa;b=bb",
        "--score", "c|how?|L1|L2",
    ])
    qs = collect_questions(ns)
    assert [q.key for q in qs] == ["a", "b", "c"]
    assert qs[0].type == "noul"
    assert qs[1].type == "choice"
    assert qs[2].type == "score"