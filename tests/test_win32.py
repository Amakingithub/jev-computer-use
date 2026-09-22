"""Tests for the Win32 window resolution layer (offline — fake EnumWindows)."""
from __future__ import annotations

from jev_computer_use.computer_use.win32 import WindowInfo, find_window, title_matches


def test_title_matches_case_and_diacritics() -> None:
    assert title_matches("Bloc-notes", "Bloc-notes - document1")
    assert title_matches("bloc-notes", "BLOC-NOTES - sans titre")
    assert title_matches("é", "Éditeur de texte")  # diacritics folded


def test_title_matches_bilingual_aliases() -> None:
    assert title_matches("notepad", "Sans titre - Bloc-notes")       # EN pattern → FR title
    assert title_matches("Bloc-notes", "Untitled - Notepad")         # FR pattern → EN title
    assert title_matches("untitled", "Bloc-notes")                   # alias through both
    assert title_matches("calculatrice", "Calculator")
    assert title_matches("Calculator", "Calculatrice")
    assert title_matches("task manager", "Gestionnaire des tâches")


def test_title_matches_rejects_unrelated() -> None:
    assert not title_matches("notepad", "Calculator")
    assert not title_matches("", "anything")
    assert not title_matches("paint", "Calculator")
    assert not title_matches("Calc", "OneNote")


def test_find_window_topmost_first() -> None:
    lower = [
        WindowInfo(hwnd=101, title="Untitled - Notepad", pid=10, rect=(0, 0, 800, 600)),
        WindowInfo(hwnd=202, title="Calculator", pid=11, rect=(0, 0, 400, 500)),
    ]

    def fake_enum(visit):
        for w in lower:
            visit(w)
        # a second Notepad deeper in Z-order under the Calculator must NOT win
        visit(WindowInfo(hwnd=303, title="Bloc-notes - Untitled", pid=12, rect=(0, 0, 700, 500)))

    got = find_window("notepad", enum=fake_enum)
    assert got is not None
    assert got.hwnd == 101  # topmost visible match, not the later FR-titled one


def test_find_window_missing() -> None:
    def fake_enum(visit):
        visit(WindowInfo(hwnd=1, title="Calculator", pid=1, rect=(0, 0, 10, 10)))

    assert find_window("paint", enum=fake_enum) is None