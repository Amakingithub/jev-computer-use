"""pyautogui execution layer. OCR element ids → coordinates → mouse/keyboard.
FAILSAFE is on: yank the mouse to a top screen corner to abort."""
from __future__ import annotations

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


def click(center: tuple[int, int], *, button: str = "left", times: int = 1) -> None:
    pyautogui.click(*center, button=button, clicks=max(1, times))


def double_click(center: tuple[int, int]) -> None:
    pyautogui.doubleClick(*center)


def type_text(text: str, interval: float = 0.0) -> None:
    pyautogui.typewrite(text, interval=interval)


def press(key: str) -> None:
    pyautogui.press(key)


def hotkey(*keys: str) -> None:
    pyautogui.hotkey(*keys)


def scroll(clicks: int = 1) -> None:
    pyautogui.scroll(clicks)


def execute(action: str, center: tuple[int, int] | None, text: str | None, key: str | None) -> None:
    """Run one bounded action by name. Coordinates must come from OCR (never model output)."""
    if action == "click_element":
        if center is None:
            raise ValueError("click_element requires an element center")
        click(center)
    elif action == "type_text":
        if text is None:
            raise ValueError("type_text requires --input-text")
        if center is not None:
            click(center)
        type_text(text)
    elif action == "press_key":
        if key is None:
            raise ValueError("press_key requires --press-key")
        press(key)
    elif action == "scroll":
        scroll(1)
    elif action in ("done", "blocked", "escalate"):
        return  # no-op; terminal actions handled by the loop
    else:
        raise ValueError(f"unknown action {action!r}")