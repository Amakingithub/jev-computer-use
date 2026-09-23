"""Live on-screen guide (`--show-guide`, tiptour-macos pointer/detection overlay lesson).

Draws the current inventory (element frames), the planned target, a cursor arrow and a status
line directly on the desktop in front of the driven app, so the operator SEES what the agent
is about to click (speed/visibility request — was previously invisible inside OCR text).

Implementation: one borderless layered WS_POPUP over the whole screen, painted with GDI
(board-level FillRect + DrawText) and made transparent with a MANUEL colorkey
(SetLayeredWindowAttributes LWA_COLORKEY). We deliberately do NOT use per-pixel alpha
(UpdateLayeredWindow + DIB) — colorkey is dramatically simpler and 100 % reliable here, and
WS_EX_TRANSPARENT makes every mouse click pass through to the app below (no focus steal,
no swallowed clicks) so the guide can never break the driven run.

NO message pump is required: all drawing happens on the caller thread between its own
steps; RedrawWindow(RDW_INVALIDATE|RDW_UPDATENOW) paints synchronously. Every call is
wrapped so any GDI failure degrades to "no overlay" instead of crashing the loop.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger("jev")

_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_GWLP_WNDPROC = -4
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = -1
_SWP_ASYNCWINDOWPOS = 0x0010
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
_WS_POPUP = 0x80000000
_LWA_COLORKEY = 0x00000001
_RDW_INVALIDATE = 0x0001
_RDW_UPDATENOW = 0x0100
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


def _rgb(r: int, g: int, b: int) -> int:
    return int(r) | (int(g) << 8) | (int(b) << 16)


_BG = _rgb(255, 0, 255)          # magenta transparent key (unlikely in driven UIs)
_FRAME = _rgb(0, 120, 255)        # inventory element frames (blue)
_TARGET_FILL = _rgb(255, 230, 0)  # planned target block (amber)
_TARGET_EDGE = _rgb(160, 0, 0)    # planned target outline (dark red)
_FLASH_FILL = _rgb(0, 200, 120)   # after-action confirm block (green)
_FLASH_EDGE = _rgb(0, 90, 50)     # after-action outline (dark green)
_ARROW = _rgb(220, 0, 0)          # cursor arrow (red)
_STATUS_BG = _rgb(40, 40, 40)     # status ribbon (dark)
_STATUS_FG = _rgb(255, 255, 255)  # status text (white)
_CHIP_BG = _rgb(25, 25, 25)       # element-label chips (dark lozenge)
_CHIP_FG = _rgb(255, 255, 255)    # chip text (white)
_CHIP_MAX = 34                    # chars kept in a chip label before ellipsis

_wndclass_registered = False
_class_name = "JevGuideOverlay_W1"


def _ensure_class() -> None:
    global _wndclass_registered
    if _wndclass_registered:
        return
    wc = wt.WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = _user32.DefWindowProcW
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    wc.hCursor = _user32.LoadCursorW(None, 32512)  # IDC_ARROW
    wc.lpszClassName = _class_name
    _user32.RegisterClassW(ctypes.byref(wc))
    _wndclass_registered = True


class GuideOverlay:
    """Full-screen click-through guide. All methods are safe no-ops when inactive."""

    def __init__(self) -> None:
        self._hwnd = 0
        self._dc = 0
        self._w = 0
        self._h = 0

    @property
    def active(self) -> bool:
        return bool(self._hwnd)

    def open(self) -> bool:
        try:
            _ensure_class()
            self._w = _user32.GetSystemMetrics(_SM_CXSCREEN)
            self._h = _user32.GetSystemMetrics(_SM_CYSCREEN)
            ex = _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST
            self._hwnd = _user32.CreateWindowExW(
                ex, _class_name, "jev-guide", _WS_POPUP,
                0, 0, self._w, self._h, 0, 0, _kernel32.GetModuleHandleW(None), None,
            )
            if not self._hwnd:
                return False
            _user32.SetLayeredWindowAttributes(self._hwnd, _BG, 255, _LWA_COLORKEY)
            _user32.SetWindowPos(self._hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                 _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS | _SWP_SHOWWINDOW)
            _user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
            self.clear()
            return True
        except Exception:
            log.warning("guide overlay could not open (continuing headless)", exc_info=True)
            self.close()
            return False

    def _dc_ok(self) -> bool:
        if self._hwnd and not self._dc:
            self._dc = _user32.GetDC(self._hwnd)
        return bool(self._dc)

    def clear(self) -> None:
        if not self._dc_ok():
            return
        try:
            self._fill(_BG, 0, 0, self._w, self._h)
            self._present()
        except Exception:
            log.warning("guide clear failed", exc_info=True)

    def draw(
        self,
        frames: list[tuple[int, int, int, int]],
        target: tuple[int, int, int, int] | None,
        status: str,
        cursor: tuple[int, int] | None = None,
        labels: list[tuple[int, str]] | None = None,
    ) -> None:
        self._render(frames, target, status, cursor, labels,
                     fill=_TARGET_FILL, edge=_TARGET_EDGE)

    def flash(
        self,
        target: tuple[int, int, int, int] | None,
        status: str,
        cursor: tuple[int, int] | None = None,
        frames: list[tuple[int, int, int, int]] | None = None,
        labels: list[tuple[int, str]] | None = None,
    ) -> None:
        """'Confirmation' state: the target stays GREEN until the next plan draw replaces it.

        2026-09-23 (#10): the plan draw answers "what WILL I click?", flash answers "that
        just happened" — drawn AFTER the settle+verify, so it costs zero latency (it overlaps
        the Jev think-time of the NEXT step, ~1 s) and the operator clearly sees the green
        confirm block while the agent re-decides.
        """
        self._render(frames or [], target, status, cursor, labels,
                     fill=_FLASH_FILL, edge=_FLASH_EDGE)

    def _render(
        self,
        frames: list[tuple[int, int, int, int]],
        target: tuple[int, int, int, int] | None,
        status: str,
        cursor: tuple[int, int] | None,
        labels: list[tuple[int, str]] | None,
        *,
        fill: int,
        edge: int,
    ) -> None:
        if not self._dc_ok():
            return
        try:
            self._fill(_BG, 0, 0, self._w, self._h)
            for i, (fx1, fy1, fx2, fy2) in enumerate(frames):
                if fx2 <= fx1 or fy2 <= fy1:
                    continue
                if fx1 < 0:
                    fx1 = 0
                if fy1 < 0:
                    fy1 = 0
                if fx2 > self._w:
                    fx2 = self._w
                if fy2 > self._h:
                    fy2 = self._h
                self._frame(_FRAME, fx1, fy1, fx2, fy2, 1)
                if labels and i < len(labels):
                    self._chip(f"{labels[i][0]}. {labels[i][1]}", fx1, fy1)
            if target:
                tx1, ty1, tx2, ty2 = target
                tx1 = max(0, tx1)
                ty1 = max(0, ty1)
                tx2 = min(self._w, tx2)
                ty2 = min(self._h, ty2)
                if tx2 > tx1 and ty2 > ty1:
                    self._fill(fill, tx1, ty1, tx2, ty2)
                    self._frame(edge, tx1, ty1, tx2, ty2, 2)
            if cursor:
                cx, cy = int(cursor[0]), int(cursor[1])
                if 0 <= cx < self._w and 0 <= cy < self._h:
                    self._arrow(cx, cy)
            if status:
                self._status(status)
            self._present()
        except Exception:
            log.warning("guide render failed", exc_info=True)

    def close(self) -> None:
        try:
            if self._dc:
                _user32.ReleaseDC(self._hwnd, self._dc)
                self._dc = 0
            if self._hwnd:
                _user32.DestroyWindow(self._hwnd)
                self._hwnd = 0
        except Exception:
            log.warning("guide close failed", exc_info=True)

    # --- raw GDI helpers (all on the caller thread) ---
    def _brush(self, rgb: int) -> int | None:
        b = _gdi32.CreateSolidBrush(rgb)
        return b if b else None

    def _fill(self, rgb: int, x1: int, y1: int, x2: int, y2: int) -> None:
        brush = self._brush(rgb)
        if not brush:
            return
        try:
            _gdi32.SelectObject(self._dc, brush)
            _gdi32.PatBlt(self._dc, x1, y1, x2 - x1, y2 - y1, 0x00F00021)  # PATCOPY
        finally:
            _gdi32.DeleteObject(brush)

    def _frame(self, rgb: int, x1: int, y1: int, x2: int, y2: int, thick: int) -> None:
        self._fill(rgb, x1, y1, x2, y1 + thick)
        self._fill(rgb, x1, y2 - thick, x2, y2)
        self._fill(rgb, x1, y1, x1 + thick, y2)
        self._fill(rgb, x2 - thick, y1, x2, y2)

    def _arrow(self, cx: int, cy: int) -> None:
        pts = (
            (cx, cy),
            (cx + 14, cy + 6),
            (cx + 6, cy + 8),
            (cx + 13, cy + 18),
            (cx + 6, cy + 19),
            (cx + 14, cy + 28),
            (cx + 8, cy + 32),
            (cx - 1, cy + 22),
            (cx - 9, cy + 30),
            (cx - 9, cy + 12),
        )
        polygon = (wt.POINT * len(pts))()
        for i, (px, py) in enumerate(pts):
            polygon[i] = wt.POINT(px, py)
        brush = self._brush(_ARROW)
        if not brush:
            return
        try:
            old = _gdi32.SelectObject(self._dc, brush)
            _gdi32.SetBkMode(self._dc, 1)  # TRANSPARENT
            pen = _gdi32.CreatePen(0, 0, 0)  # PS_SOLID / width 0 = cosmetic
            old_pen = _gdi32.SelectObject(self._dc, pen)
            _gdi32.Polygon(self._dc, polygon, len(pts))
            _gdi32.SelectObject(self._dc, old_pen)
            _gdi32.DeleteObject(pen)
            _gdi32.SelectObject(self._dc, old)
        finally:
            _gdi32.DeleteObject(brush)

    def _chip(self, text: str, fx: int, fy: int) -> None:
        """Dark lozenge with `id. label` at the frame's top-left, ellipsized.

        Placed ABOVE the frame when it fits on screen, below otherwise (never over the
        status ribbon). Text is measured with GetTextExtentPoint32W so the backing box
        hugs the glyphs instead of guessing.
        """
        if len(text) > _CHIP_MAX:
            text = text[:_CHIP_MAX] + "…"
        font = _gdi32.CreateFontW(-13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, "Segoe UI")
        if not font:
            return
        try:
            old_font = _gdi32.SelectObject(self._dc, font)
            size = wt.SIZE()
            _gdi32.GetTextExtentPoint32W(self._dc, text, len(text), ctypes.byref(size))
            cw, ch = int(size.cx), int(size.cy)
            bx1 = max(0, min(fx, self._w - cw - 6))
            by1 = fy - ch - 6
            if by1 < 36 or by1 + ch + 6 > self._h:  # ribbon or screen edge → below the frame
                by1 = min(fy + ch + 4, self._h - ch - 6)
            bx2 = min(self._w, bx1 + cw + 6)
            by2 = by1 + ch + 4
            if bx2 <= bx1 or by2 <= by1:
                return
            self._fill(_CHIP_BG, bx1, by1, bx2, by2)
            _gdi32.SetBkMode(self._dc, 1)  # TRANSPARENT
            _gdi32.SetTextColor(self._dc, _CHIP_FG)
            rc = wt.RECT(bx1 + 3, by1 + 2, bx2 - 3, by2 - 2)
            _gdi32.DrawTextW(self._dc, text, -1, ctypes.byref(rc), 0x0000 | 0x0004 | 0x0100)  # left|top|noprefix
            _gdi32.SelectObject(self._dc, old_font)
        finally:
            _gdi32.DeleteObject(font)

    def _status(self, text: str) -> None:
        self._fill(_STATUS_BG, 0, 0, self._w, 34)  # dark ribbon for legibility on any UI
        font = _gdi32.CreateFontW(-15, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0, 0, "Segoe UI")
        if not font:
            return
        try:
            old_font = _gdi32.SelectObject(self._dc, font)
            _gdi32.SetBkMode(self._dc, 1)
            _gdi32.SetBkColor(self._dc, _STATUS_BG)
            _gdi32.SetTextColor(self._dc, _STATUS_FG)
            rc = wt.RECT(8, 4, self._w - 8, 30)
            _gdi32.DrawTextW(self._dc, text, -1, ctypes.byref(rc), 0x00A0 | 0x0004 | 0x0100)  # left|end|noprefix
            _gdi32.SelectObject(self._dc, old_font)
        finally:
            _gdi32.DeleteObject(font)

    def _present(self) -> None:
        _user32.RedrawWindow(self._hwnd, None, None, _RDW_INVALIDATE | _RDW_UPDATENOW)