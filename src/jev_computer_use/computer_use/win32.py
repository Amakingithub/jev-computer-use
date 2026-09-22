"""Win32 top-level window introspection + focus (ctypes user32, zero extra deps).

Used by the `--window <titre>` mode: resolve a target window by title (with FR/EN
aliases + diacritic-insensitive matching), bring it to the foreground, and feed its
rect to mss as the capture region — so OCR never sees the opencode terminal or other
overlays (the 2026-09-22 inventory-pollution fix).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .screen import Region


def _set_dpi_aware() -> None:
    """Make this process DPI-aware *before* any window query.

    The process defaults to system-DPI-unaware: GetWindowRect / UIA report LOGICAL
    pixels while mss.capture works in PHYSICAL pixels. On a scaled display (e.g.
    1920x1080 @125% -> 1536x864 logical) that 1.25x mismatch silently crops the wrong
    screen area and skews every a11y box/OCR coordinate (2026-09-22 live-demo find:
    wizard content misread as desktop icons). Forcing PER_MONITOR awareness aligns
    win32 + UIA + mss on physical pixels.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # legacy fallback
        except Exception:  # pragma: no cover - anything else gained awareness already
            pass


_set_dpi_aware()


user32 = ctypes.WinDLL("user32", use_last_error=True)

_SW_RESTORE = 9
_SW_MINIMIZE = 6
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002
_GWLP_HINSTANCE = -6
_MAX_TITLE = 512


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    rect: Region  # x1, y1, x2, y2 absolute screen coords


# normalized (diacritic-stripped) pattern → aliases. Keep both directions explicit
# so "notepad" <-> "bloc-notes", "calculator" <-> "calculatrice", etc. Just work.
_TITLE_ALIASES: dict[str, list[str]] = {
    "bloc-notes": ["notepad", "untitled", "sans titre"],
    "notepad": ["bloc-notes"],
    "untitled": ["sans titre", "bloc-notes"],
    "calculatrice": ["calculator"],
    "calculator": ["calculatrice"],
    "explorateur": ["explorer", "ce pc", "fichiers"],
    "explorer": ["explorateur"],
    "parametres": ["settings"],
    "settings": ["parametres"],
    "gestionnaire des taches": ["task manager"],
    "task manager": ["gestionnaire des taches"],
}


def normalize_text(text: str) -> str:
    """Lowercase + strip diacritics so 'Bloc-notes' == 'bloc-notes' (also used by the
    --click-seq label matcher)."""
    folded = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


def title_matches(pattern: str, actual: str) -> bool:
    """Case/diacritic-insensitive substring + bilingual alias match.

    'Bloc-notes' matches any running Notepad title; 'notepad' and 'untitled' also match.
    'Calculatrice' matches 'Calculator' and vice-versa.
    """
    pat = normalize_text(pattern)
    act = normalize_text(actual or "")
    if not pat or not act:
        return False
    if pat in act:
        return True
    return any(alias in act for alias in _TITLE_ALIASES.get(pat, []))


def _enum_top_windows(visit: Callable[[WindowInfo], None]) -> None:
    """Enumerate visible top-level windows in Z-order (topmost first)."""

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        visit(WindowInfo(
            hwnd=int(hwnd),
            title=title,
            pid=int(pid.value),
            rect=(
                int(rect.left), int(rect.top),
                int(rect.right), int(rect.bottom),
            ),
        ))
        return True

    if not user32.EnumWindows(_cb, 0):
        raise ctypes.WinError(ctypes.get_last_error())


def find_window(pattern: str, enum: Callable | None = None) -> WindowInfo | None:
    """Topmost visible window whose title matches `pattern`. None when not found."""
    candidates: list[WindowInfo] = []
    iterator = enum or _enum_top_windows
    iterator(candidates.append)
    for w in candidates:  # EnumWindows is Z-order: first match = topmost
        if title_matches(pattern, w.title):
            return w
    return None


def focus_window(hwnd: int, retries: int = 1) -> bool:
    """Bring the window to the foreground despite Windows foreground restrictions.

    Uses the documented Alt-key trick (releases the foreground lock), then
    minimize/restore as a stronger fallback. Returns True when it owns the foreground.
    """
    for _ in range(max(1, retries) + 1):
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.keybd_event(_VK_MENU, 0, 0, 0)
        user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        if user32.GetForegroundWindow() == int(hwnd):
            return True
        # stronger fallback: minimize + restore resets the Z-order/foreground grant
        user32.ShowWindow(hwnd, _SW_MINIMIZE)
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        if user32.GetForegroundWindow() == int(hwnd):
            return True
    return False