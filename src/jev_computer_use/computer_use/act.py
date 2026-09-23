"""pyautogui execution layer. OCR element ids → coordinates → mouse/keyboard.
FAILSAFE is on: yank the mouse to a top screen corner to abort.

drive-screen lessons folded in (2026-09-22):
- focus is proved by the caller between capture and act (never blind-send any input);
- long `type_text` is chunked and re-proves foreground mid-send (FOCUS_LOST_MIDSEND);
- `paste_text` (atomic clipboard) is the default for anything punctuation-heavy or multi-line;
- a bare `win`/`super` key is REFUSED (the Start menu is a CoreWindow nothing can close).
"""
from __future__ import annotations

import ctypes
import time
from collections.abc import Callable

import pyautogui

from . import questions_computer_use as qc

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1

_TYPE_CHUNK = 25  # chars between focus re-checks (one GetForegroundWindow per chunk ~ µs)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_GMEM_ZEROINIT = 0x0040

_BARE_WIN_KEYS = {"win", "super", "cmd", "windows", "lwin", "rwin", "leftwin", "rightwin"}


class FocusLostError(RuntimeError):
    """Raised when focus was stolen mid-`type_text`; carries how many chars landed."""

    def __init__(self, typed: int, total: int) -> None:
        self.typed = typed
        self.total = total
        super().__init__(f"focus lost mid-type: {typed}/{total} chars landed")


def click(center: tuple[int, int], *, button: str = "left", times: int = 1) -> None:
    pyautogui.click(*center, button=button, clicks=max(1, times))


def double_click(center: tuple[int, int]) -> None:
    pyautogui.doubleClick(*center)


def aim(center: tuple[int, int], duration: float = 0.06) -> None:
    """Glide the OS cursor onto the target WITHOUT clicking.

    2026-09-23 (tiptour pointer-mark lesson): the operator SEES the agent aim at the
    element before every keyboard/scroll action, not just clicks (pyautogui moves the
    cursor for clicks on its own). Hover alone never changes focus, so this is purely
    visual; keep the glide short so it costs ~60 ms, not a pause.
    """
    pyautogui.moveTo(center[0], center[1], duration=duration)


def validate_key(key: str) -> tuple[str, ...]:
    """Normalize a key/chord and REFUSE a bare Win/super press (Start-menu trap)."""
    parts = tuple(k.strip() for k in key.split("+") if k.strip())
    if not parts:
        raise ValueError("empty key")
    if len(parts) == 1 and parts[0].lower() in _BARE_WIN_KEYS:
        raise ValueError(
            f"refusing bare key {parts[0]!r}: it opens the Start menu (a CoreWindow nothing can "
            "close or target). Use a chord like 'win+shift+left' to move a window, or launch via "
            "start / Start-Process / a URL scheme instead."
        )
    return parts


def press(key: str) -> None:
    parts = validate_key(key)
    if len(parts) > 1:  # e.g. "ctrl+s" -> hotkey(ctrl, s)
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])


def hotkey(*keys: str) -> None:
    pyautogui.hotkey(*keys)


def type_text(text: str, interval: float = 0.05, focus_check: Callable[[], bool] | None = None) -> None:
    """Type `text` char-by-char, re-proving foreground every `_TYPE_CHUNK` chars.

    The per-char interval stays (interval=0 drops characters on app startup, 2026-09-21).
    If focus is stolen mid-send (drive-screen FOCUS_LOST_MIDSEND measurement: 200 chars lost
    108 to a focus thief), stop and raise so the caller screenshots before retrying instead
    of duplicating the part that already arrived.
    """
    total = len(text)
    sent = 0
    while sent < total:
        chunk = text[sent: sent + _TYPE_CHUNK]
        pyautogui.typewrite(chunk, interval=interval)
        sent += len(chunk)
        if sent < total and focus_check is not None and not focus_check():
            raise FocusLostError(sent, total)


def clipboard_snapshot() -> str | None:
    """Read clipboard text via Win32 (None when empty / not text)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not user32.OpenClipboard(0):
        raise OSError("clipboard busy")
    try:
        h = user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            size = int(kernel32.GlobalSize(h))
            if size <= 2:
                return ""
            return ctypes.wstring_at(p, size // 2)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_set(text: str) -> None:
    """Replace the clipboard with `text` (CF_UNICODETEXT). Ownership transferred to Win32."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not user32.OpenClipboard(0):
        raise OSError("clipboard busy")
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = None
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(_GMEM_MOVEABLE | _GMEM_ZEROINIT, len(data))
        if not h:
            raise OSError("GlobalAlloc failed")
        p = kernel32.GlobalLock(h)
        if not p:
            raise OSError("GlobalLock failed")
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(_CF_UNICODETEXT, h)
        h = None  # clipboard owns it now
    finally:
        if h:
            try:
                kernel32.GlobalFree(h)
            except OSError:
                pass
        user32.CloseClipboard()


def paste_text(text: str) -> None:
    """Atomic clipboard paste (immune to keyboard layout / IME mangling), then restore."""
    saved = clipboard_snapshot()
    try:
        clipboard_set(text)
        pyautogui.hotkey("ctrl", "v")
    finally:
        try:
            clipboard_set(saved or "")
        except OSError:  # pragma: no cover - another app grabbed the clipboard
            pass  # leaving ours in place is the lesser evil


def scroll(clicks: int = 1, center: tuple[int, int] | None = None) -> None:
    """Scroll the view under the POINTER, not the focused window.

    A wheel event goes to whatever is under the mouse (drive-screen lesson), so move onto
    the target element first when one was chosen.
    """
    if center is not None:
        pyautogui.moveTo(*center)
        time.sleep(0.05)
    pyautogui.scroll(clicks)


def execute(
    action: str,
    center: tuple[int, int] | None,
    text: str | None,
    key: str | None,
    focus_check: Callable[[], bool] | None = None,
) -> None:
    """Run one bounded action by name. Coordinates must come from OCR (never model output)."""
    if action == "click_element":
        if center is None:  # 'nonew' / viewport target -> click the screen centre (e.g. focus the editor)
            w, h = pyautogui.size()
            center = (w // 2, h // 2)
        click(center)
    elif action in ("type_text", "paste_text"):
        if text is None:
            raise ValueError(f"{action} requires --input-text")
        if center is not None:
            click(center)
        if action == "paste_text":
            paste_text(text)
        else:
            type_text(text, focus_check=focus_check)
    elif action == "press_key":
        if key is None:
            raise ValueError("press_key requires --press-key")
        press(key)
    elif action == "scroll":
        scroll(1, center)
    elif action == "wait":
        # explicit pause for splash/console/slow disjoint UIs — bounded, never a live model loop
        time.sleep(qc.WAIT_SECONDS)
    elif action in ("done", "blocked", "escalate"):
        return  # no-op; terminal actions handled by the loop
    else:
        raise ValueError(f"unknown action {action!r}")