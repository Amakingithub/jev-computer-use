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


class AmbiguousWindowError(RuntimeError):
    """More than one visible window matched `--window` — refusing instead of guessing.

    drive-screen rule #4 (2026-09-22): "an ambiguous window match is a stop, not a guess".
    On this desktop two Notepad documents / two Settings windows share near-identical
    titles, and clicking the Z-order-first one is how text lands in the wrong document.
    """

    def __init__(self, pattern: str, candidates: list[WindowInfo]) -> None:
        self.pattern = pattern
        self.candidates = candidates
        shown = "; ".join(f"{w.title!r} (hwnd={w.hwnd})" for w in candidates[:5])
        more = f" (+{len(candidates) - 5} more)" if len(candidates) > 5 else ""
        super().__init__(
            f"window {pattern!r} is ambiguous — {len(candidates)} matches: {shown}{more}. "
            "Pass a longer/more specific title (or use --region)."
        )


def find_window(pattern: str, enum: Callable | None = None) -> WindowInfo | None:
    """Visible window uniquely matching `pattern`. None when not found.

    Raises AmbiguousWindowError when several windows match (refuse, don't guess). A
    single unambiguous match is returned regardless of Z-order."""

    def _matches(w: WindowInfo) -> bool:
        return title_matches(pattern, w.title)

    candidates: list[WindowInfo] = []
    iterator = enum or _enum_top_windows
    iterator(candidates.append)
    matches = [w for w in candidates if _matches(w)]
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousWindowError(pattern, matches)
    return matches[0]


def foreground() -> int | None:
    """HWND of the current foreground window, or None."""
    hwnd = user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def foreground_is(hwnd: int) -> bool:
    """Does `hwnd` OWE the foreground right now? Proved immediately before acting, never
    assumed from an older focus_window call (drive-screen rule #3, 2026-09-22)."""
    return foreground() == int(hwnd)


def get_dpi() -> int:
    """System DPI (96 = no scaling). This process is DPI-aware, so all Win32 sticks on
    physical pixels matching mss — doctor reports this so scaling never bites silently."""
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:  # pragma: no cover - pre-1607 Windows
        return 96


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


def window_at(x: int, y: int) -> int:
    """Top-level window at absolute screen point (0 if none)."""
    pt = wt.POINT(int(x), int(y))
    top = user32.WindowFromPoint(pt)
    return int(top) if top else 0


_GA_ROOT = 2  # GetAncestor(_GA_ROOT) -> the true top-level owner


def root_owner(hwnd: int) -> int:
    """Root owner window of a handle (walks child/popups up to the top-level)."""
    root = user32.GetAncestor(hwnd, _GA_ROOT)
    return int(root) if root else 0


def point_owned_by(x: int, y: int, top_hwnd: int) -> bool:
    """Is the pixel under (x, y) actually inside `top_hwnd`'s tree? (#5, occlusion guard)

    2026-09-22 (arc-cua hit-test lesson): an always-on-top overlay, notification toast, or a
    second app can sit above the target app physically while our rect/enum says the target is
    focused. Clicking blind then lands IN the overlay. Before every click we resolve the
    window under the pixel with WindowFromPoint and walk it to its GA_ROOT owner — if that
    owner is not our target top-level, the click is DEFERRED (escalate flag) instead of fired.
    WindowFromPoint already skips invisible/disabled windows, so the check is cheap (~10 us).
    """
    hit = window_at(int(x), int(y))
    if not hit:
        return False
    return root_owner(hit) == int(top_hwnd)