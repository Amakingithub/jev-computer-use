"""Tests for the computer-use step pipes (offline)."""
from __future__ import annotations

from jev_computer_use.computer_use.run_agent import _ArgPipe


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