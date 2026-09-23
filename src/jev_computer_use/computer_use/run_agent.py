"""jev-computer-use — minimal CPU-first GUI agent.

Flow: capture → RapidOCR parse → Jev decide (which element, which action, safe?, risk?) →
act (pyautogui) → verify (adaptive settle + dHash) → escalate / done / blocked.

Safety: default approval=confirm (prompt before every step). DESTRUCTIVE/SENSITIVE flows and
anything with step_risk >= RISK_CONFIRM force confirmation regardless. Run --dry-run first.

2026-09-22 perf/visibility rework (jev-ultrafast + arc-cua + tiptour-macos lessons):
- adaptive settle replaces the fixed post-action sleep (poll-to-stable, capped waits);
- inventory reuse: identical consecutive frames skip the re-parse entirely;
- FocusLost mid-type resumes the REMAINING substring after refocus (no duplicate);
- occlusion hit-test (WindowFromPoint) refuses blind clicks into overlays;
- --show-guide = live click-through overlay (target + inventory + status);
- --text-helper = optional tiny-LLM value writer (validated {"text": ...});
- Esc cancels the run immediately;
- `subtask` subcommand = arc-cua-style bounded JSON contract drive.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from ..decision_layer import DecisionLayer, NoProviderAvailable
from . import act, decide, overlay, parse_ui, record, screen, text_helper, win32
from . import questions_computer_use as qc

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("computer-use")

LOGS_DIR = Path("logs")

_VK_ESCAPE = 0x1B


class _ArgPipe:
    """Ordered per-step values for type/press content (delivered out-of-band, never from Jev).

    CLI '--input-text Hello|World' → step 1 types 'Hello', step 2 types 'World'.
    A step that runs out of values escalates instead of resending the last one.
    """

    def __init__(self, raw: str | None) -> None:
        self._items = [v for v in (raw.split("|") if raw else []) if v]

    def __bool__(self) -> bool:
        return bool(self._items)

    def take(self) -> str | None:
        return self._items.pop(0) if self._items else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jev-computer-use",
        description="CPU-first GUI agent: RapidOCR + Jev decisions + pyautogui + dHash verify. "
                    "Give --goal (loop mode) or --click-seq (deterministic text-to-click, no Jev).",
    )
    p.add_argument("--goal", help="natural-language task description (mutually exclusive with --click-seq)")
    p.add_argument("--dry-run", action="store_true", help="parse + decide + print plan, act nothing")
    p.add_argument("--approval", choices=("none", "confirm"), default="confirm",
                   help="confirm = ask before every step (default). none = demo/dev only.")
    p.add_argument("--max-steps", type=int, default=qc.MAX_STEPS)
    p.add_argument("--delay", type=float, default=qc.STEP_DELAY,
                   help="seconds to allow the UI to settle after an action (adaptive: we return "
                        "as soon as the frame is stable, so this is an upper bound for slow UIs)")
    p.add_argument("--region", help="x1,y1,x2,y2 (absolute screen coords) to watch")
    p.add_argument(
        "--window",
        help="capture ONLY this window (title match, case/diacritic-insensitive, FR/EN "
             "aliases: 'Bloc-notes'/'notepad', 'Calculatrice'/'Calculator', ...). Brings "
             "the window to the foreground and uses its live rect as the region each step, "
             "so OCR never sees the terminal or overlays. Mutually exclusive with --region.",
    )
    p.add_argument(
        "--click-seq",
        help="deterministic OCR click path — text labels separated by '|', clicked in order, "
             "NO Jev call per step (~0.2-0.4 s/click, the Calculator experience). "
             "e.g. --click-seq \"Two|Zero|Zero|Multiply by|Three|Five|Equals\". Labels with "
             "destructive/sensitive meaning (install, delete, accept, send, save...) force "
             "--approval confirm and are refused under --approval none.",
    )
    p.add_argument(
        "--input-text",
        help="text for next_action=type_text/paste_text ('|' separates per-step values). "
             "Prefer paste_text for punctuation-heavy/multi-line content (type_text mangles "
             "special chars on rich editors, drive-screen lesson).",
    )
    p.add_argument(
        "--press-key", help="key to press when next_action=press_key, e.g. Enter; '|' separates per-step values"
    )
    p.add_argument("--channel", action="append", default=[], choices=("typesafe", "cloudflare", "openrouter"),
                   help="restrict decision provider (repeatable)")
    p.add_argument(
        "--ocr",
        default="rapid",
        choices=("rapid", "windows", "glm", "a11y", "merge", "auto"),
        help="OCR engine: rapid (default, ONNX) | windows (WinRT, free, reads glyphs) | "
             "glm (local vision OCR, optional) | a11y (UIAutomation tree, 0 vision tokens) | "
             "merge (rapid+windows+a11y gap-fill) | auto (cascade)",
    )
    p.add_argument("--show-guide", action="store_true",
                   help="draw a live click-through overlay over the driven window: element "
                        "frames, planned target, cursor arrow + status line (tiptour lesson).")
    p.add_argument(
        "--record",
        metavar="DIR",
        help="save an ANNOTATED PNG per step + steps.jsonl (same drawing as the overlay) into DIR; "
             "watch it later with: jev-computer-use replay DIR  (jit demos of what the agent saw)",
    )
    p.add_argument("--text-helper", action="store_true",
                   help="when a step wants type_text/paste_text without an --input-text value, "
                        "ask a small optional chat model (TEXT_MODEL_API_KEY) to write the value "
                        "(validated {'text': ...} only; any failure escalates instead of guessing).")
    p.add_argument("--json", action="store_true", help="JSON step log on stdout")
    return p


def _parse_region(raw: str | None) -> screen.Region | None:
    if not raw:
        return None
    parts = [int(x) for x in raw.split(",")]
    if len(parts) != 4 or parts[2] <= parts[0] or parts[3] <= parts[1]:
        raise ValueError("--region needs x1,y1,x2,y2 with x2>x1 and y2>y1")
    return (parts[0], parts[1], parts[2], parts[3])


def _confirm(prompt: str) -> bool:
    answer = input(f"{prompt}  [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _abs(center: tuple[int, int], origin: tuple[int, int]) -> tuple[int, int]:
    """Convert an image-local element center to ABSOLUTE screen coords.

    OCR providers (rapid/windows/glm) and a11y (after the origin shift) all return boxes
    in IMAGE-LOCAL pixels. For a sub-region capture the click must land at
    `origin + center` — clicking bare `center` silently hits the body of the target
    window instead of the button (2026-09-22 wizard demo: 'Next' click diff=4, page
    never advanced)."""
    ox, oy = origin
    cx, cy = center
    return (cx + ox, cy + oy)


def _dump_escalate(reason: str, inventory: str) -> None:
    """Print an escalation with the live screen inventory so the caller (agent / VLM)
    can re-plan WITHOUT a separate capture+parse round; upstream used to dead-end on a
    bare 'escalate: ok' (2026-09-22 test 8)."""
    print(f"escalate: {reason}")
    if inventory.strip():
        print("  screen inventory:")
        for line in inventory.splitlines():
            print(f"    {line}")
    print("  > feed the screen inventory + goal to `local-vision` or a free VLM and re-plan.")


_NON_TEXT_INPUT_ROLES = frozenset({
    "ButtonControl", "ListItemControl", "MenuItemControl", "CheckBoxControl",
    "RadioButtonControl", "TabItemControl", "HyperlinkControl", "SliderControl",
    "TreeItemControl",
})


def _affordance_conflict(action: str, elem: parse_ui.Element) -> str | None:
    """arc-cua element→action schema: the legal-actions reason a control CANNOT do the plan.

    Returns a human reason (None = allowed). Code-side, so a schema violation is refused
    without burning a Jev retry and never blind-sent. OCR elements carry role "" — the gate
    only ever fires on a11y/merge reads that know the ControlType.
    """
    if not elem.role or action not in ("type_text", "paste_text"):
        return None
    if elem.role in _NON_TEXT_INPUT_ROLES:
        kind = elem.role[:-len("Control")] if elem.role.endswith("Control") else elem.role
        return (f"affordance: {kind.lower()} control #{elem.id} \"{elem.text[:24]}\" "
                f"can't take text — typing would hit a non-input control")
    return None


def match_element(elements: list[parse_ui.Element], target: str) -> parse_ui.Element | None:
    """Deterministic label → element for --click-seq (no Jev, no VLM).

    Exact normalized-equality first, then normalized substring (so 'Next' hits 'Next >'),
    then edit-distance-1 (tiptour strictLabelMatchesQuery: OCR silently swaps/drops one char,
    e.g. 'Setup' read as 'Setuр'). Ids are position-sorted so the first match is the
    top-left-most one. Short targets (< 3 chars) EXACT-match only — a 2-char query must not
    fuzzy-match some unrelated word.
    """
    t = win32.normalize_text(target)
    if not t:
        return None
    for e in elements:
        if win32.normalize_text(e.text) == t:
            return e
    for e in elements:
        if t in win32.normalize_text(e.text):
            return e
    if len(t) >= 3:
        for e in elements:
            lab = win32.normalize_text(e.text)
            if lab and abs(len(lab) - len(t)) <= 1 and _edit_distance(lab, t) <= 1:
                return e
    return None


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, bounded early — only ever called on labels we suspect are ~equal."""
    if a == b:
        return 0
    rows = [list(range(len(b) + 1))]
    for i, ca in enumerate(a, start=1):
        row = [i]
        prev_best = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            row.append(min(row[-1] + 1, rows[-1][j] + 1, rows[-1][j - 1] + cost))
            prev_best = min(prev_best, row[-1])
        if prev_best > 1:  # already >1 edits: bail, can't come back under with inserts
            return 2
        rows.append(row)
    return rows[-1][-1]


def _label_ratio(text: str, target: str) -> tuple[float, float] | None:
    """Fractional x-window [start, end) of the matched label inside a merged OCR label.

    2026-09-23 (tiptour narrowedTarget): a sentence merged into one OCR element ('Next > Save
    Setup') has ONE centroid — clicking it can hit the wrong word. Returns the span of the
    matched target so the caller clicks the WORD, not the sentence centroid. None when the
    target covers (nearly) the whole label, i.e. there is nothing to narrow. Character-offset
    based on the whitespace-stripped, diacritic-folded stream (same folding as normalize_text).
    """
    t = win32.normalize_text(target)
    if not t:
        return None
    folded: list[str] = []
    map_: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        f = win32.normalize_text(ch)
        if f:
            folded.append(f)
            map_.append(i)
    s = "".join(folded)
    L = len(s)
    if L == 0:
        return None
    pos = s.find(t)
    if pos < 0:
        if len(t) < 3:
            return None
        pos = next((i for i in range(L - len(t) + 1)
                    if _edit_distance(s[i:i + len(t)], t) <= 1), None)
        if pos is None:
            return None
    a = max(0.0, map_[pos] / len(text))
    b = min(1.0, (map_[min(pos + len(t), L) - 1] + 1) / len(text))
    if b - a >= 0.92 and a <= 0.06:  # whole label matched — no meaningful sub-target
        return None
    return a, max(a, b)


def _narrow_box(box: tuple[int, int, int, int], text: str, target: str) -> tuple[int, int, int, int]:
    """The matched label's sub-box; unchanged when the target spans the whole label."""
    r = _label_ratio(text, target)
    if r is None:
        return box
    x1, y1, x2, y2 = box
    w = max(x2 - x1, 1)
    a, b = r
    nx1 = min(x2 - 1, x1 + int(w * a))
    nx2 = max(nx1 + 1, x1 + int(w * b))
    return nx1, y1, nx2, y2


def is_risky_click(target: str) -> bool:
    """Deterministic code-side gate for click-seq: does the label imply an irreversible or
    commit action? (thresholds in code, not model output — mirrors the Jev noul/score gate)."""
    t = win32.normalize_text(target)
    return t and any(hint in t for hint in qc.RISKY_CLICK_HINTS)


class _WindowNotFound(RuntimeError):
    """--window resolved to nothing this step."""

    def __init__(self, title: str) -> None:
        super().__init__(f"window {title!r} not found (opened yet?)")
        self.title = title


def _refocus(hwnd: int | None, retries: int = 1) -> bool:
    """True only when the intended window OWNS the foreground right before an act.

    drive-screen rule #3 (2026-09-22): "never send input without confirming focus". Focus can
    be stolen between the capture/parse and the act — the input then lands in a window the
    agent cannot see and the dHash verify silently measures that window instead. Fast path is
    one GetForegroundWindow syscall (~µs); only a stolen focus pays the refocus + settle.
    """
    if hwnd is None:
        return True
    if win32.foreground_is(hwnd):
        return True
    win32.focus_window(hwnd, retries=retries)
    time.sleep(qc.STEP_DELAY)  # settle before re-check, same as _step_capture
    return win32.foreground_is(hwnd)


def _step_capture(args, static_region: screen.Region | None) -> tuple[
        screen.Region | None, tuple[int, int], screen.Shot, int | None,
]:
    """Resolve --window (fresh every step: catches apps launched mid-run, window moves) or
    --region, then capture. Returns (region, origin, shot, hwnd). Raises _WindowNotFound when
    --window matches nothing and AmbiguousWindowError when it matches several windows."""
    hwnd: int | None = None
    if args.window:
        w = win32.find_window(args.window)
        if w is None:
            raise _WindowNotFound(args.window)
        hwnd = w.hwnd
        win32.focus_window(hwnd)
        # Let the window settle as the foreground target before we capture/click: a click
        # issued in the same instant as SetForegroundWindow is often swallowed (foreground
        # lock / DWM activation animation) — live 2026-09-22: click-seq step 1 diff=0,
        # same coordinate clicked manually after a 0.5 s settle advanced the page.
        time.sleep(qc.STEP_DELAY)
        region = w.rect
        log.info("window %r @%s", w.title, region)
    else:
        region = static_region
    origin = (region[0], region[1]) if region else (0, 0)
    return region, origin, screen.capture(region), hwnd


def _escape_pressed() -> bool:
    """Esc cancels the whole run between step boundaries (won't kill a mid-action type)."""
    try:
        return bool(ctypes.WinDLL("user32").GetAsyncKeyState(_VK_ESCAPE) & 0x8000)
    except Exception:  # pragma: no cover - defensive
        return False


def _settle(args, region: screen.Region | None) -> screen.Shot:
    """Adaptive post-action settle (#2): poll captures until the frame STOPS changing.

    arc-cua lesson: a fixed sleep is wrong twice — it burns wall-clock after fast repaints
    and still acts blind against slow renders. We re-capture at SETTLE_POLL intervals; after
    SETTLE_FRAMES consecutive stable frames (dhash within STUCK_DHASH, same threshold as the
    verify) the UI is declared quiescent and the LAST shot is returned as `after` for the
    dHash verify. `--delay` becomes "give slow UIs up to N seconds" instead of a fixed burn.

    Raises KeyboardInterrupt on Ctrl+C (settle never swallows the abort). No, it doesn't.
    It just returns after the timeout — the loop checks Esc/requests itself.
    """
    min_s = min(qc.SETTLE_MIN, args.delay)
    timeout = max(qc.SETTLE_TIMEOUT, args.delay)
    started = time.perf_counter()
    last_dhash: int | None = None
    stable = 0
    final: screen.Shot | None = None
    while True:
        final = screen.capture(region)
        if last_dhash is not None and screen.hamming(last_dhash, final.dhash) <= qc.STUCK_DHASH:
            stable += 1
            if stable >= qc.SETTLE_FRAMES and (time.perf_counter() - started) >= min_s:
                return final
        else:
            stable = 0
        last_dhash = final.dhash
        if (time.perf_counter() - started) >= timeout:
            return final
        time.sleep(qc.SETTLE_POLL)


class _InventoryCache:
    """#3 inventory reuse + change-box incremental reparse.

    Parsing (RapidOCR ~200 ms / WinRT ~0.4-0.9 s) is the dominant per-step cost after a
    decision. Levels of reuse, cheapest first:
      1. identical frame (dhash distance <= 1) — the UI genuinely did not change, reuse the
         previous inventory verbatim (the shot is STILL captured for the verify base, so a
         reuse can never smuggle a stale before-image into an action's dHash check);
      2. change-box (2026-09-23, tiptour frame-skip refined): only a SMALL region changed →
         re-OCR just that crop and union it with the unchanged cached elements
         (~200 ms → ~40 ms on the common one-click step).
    Anything else (layout flip, new dialog) falls back to a full re-parse.
    """

    def __init__(self) -> None:
        self.region: screen.Region | None = None
        self.origin: tuple[int, int] = (0, 0)
        self.dhash: int | None = None
        self.elements: list[parse_ui.Element] | None = None
        self.last_l8: np.ndarray | None = None
        self.last_shape: tuple[int, int] | None = None

    def get(
        self,
        args,
        shot: screen.Shot,
        origin: tuple[int, int],
        region: screen.Region | None,
    ) -> list[parse_ui.Element] | None:
        if self.dhash is None or self.region != region or self.origin != origin:
            self._seed(shot, origin, region)
            self._remember_pixels(shot)
            return None  # nothing cached yet — caller does a full parse
        if screen.hamming(self.dhash, shot.dhash) <= 1:
            log.info("inventory reused — identical frame, %d elements without re-parse",
                     len(self.elements or []))
            self.dhash = shot.dhash
            self._remember_pixels(shot)
            return self.elements
        merged = self._incremental(args, shot, origin)
        if merged is not None:
            log.info("change-box reparse: %d elements merged from a crop of the changed region",
                     len(merged))
            self.elements = merged
            self.dhash = shot.dhash
            self._remember_pixels(shot)
            return merged
        # screen changed too broadly for an incremental step — full re-parse
        self._seed(shot, origin, region)
        self._remember_pixels(shot)
        return None

    def store(self, elements: list[parse_ui.Element]) -> None:
        self.elements = elements

    def _remember_pixels(self, shot: screen.Shot) -> None:
        try:
            self.last_l8 = np.asarray(shot.img.convert("L"))
            self.last_shape = shot.img.size
        except Exception:  # pragma: no cover - can't feed a diff without pixels
            self.last_l8 = None
            self.last_shape = None

    def _incremental(self, args, shot: screen.Shot, origin: tuple[int, int]) -> list[parse_ui.Element] | None:
        """Re-OCR only the changed region and merge it with the cached elements. None → full parse."""
        if args.ocr == "a11y" or self.elements is None or self.last_l8 is None:
            return None  # a11y walks the live OS tree — a crop gains nothing; always full
        if shot.img.size != self.last_shape:
            return None
        cur = np.asarray(shot.img.convert("L"))
        box = parse_ui.changed_region(self.last_l8, cur)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        try:
            crop = shot.img.crop((x1, y1, x2, y2))
            fresh = parse_ui.parse(
                crop,
                provider=args.ocr,
                origin=(origin[0] + x1, origin[1] + y1),
            )
        except Exception as exc:
            log.warning("incremental reparse failed (%s) — falling back to full parse", exc)
            return None
        shifted = parse_ui.shift_elements(fresh, x1, y1)
        return parse_ui.incremental_merge(self.elements, shifted, box)

    def _seed(self, shot: screen.Shot, origin: tuple[int, int], region: screen.Region | None) -> None:
        self.dhash = shot.dhash
        self.origin = origin
        self.region = region
        self.elements = None


def _focus_check(hwnd: int | None):
    return (lambda _hwnd=hwnd: win32.foreground_is(_hwnd)) if hwnd else None


def _record_step(
    record_dir: Path | None,
    shot: screen.Shot,
    elements: list[parse_ui.Element],
    target: tuple[int, int, int, int] | None,
    status: str,
    cursor: tuple[int, int] | None,
    step: int,
    entry: dict,
) -> None:
    """Per-step annotated PNG + steps.jsonl (--record). Every failure degrades to no-op."""
    if record_dir is None:
        return
    try:
        annotated = record.annotate_frame(shot.img, elements, target=target, status=status, cursor=cursor)
        (record_dir / f"step_{step:03d}.png").save(annotated)
        with (record_dir / "steps.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("record step %d failed (%s)", step, exc)


def _busy_status(args, step: int | None, elements: list[parse_ui.Element], extra: str = "") -> str:
    parts = ["jev-computer-use"]
    if getattr(args, "goal", None):
        parts.append(f"goal: {args.goal}")
    if step:
        parts.append(f"step {step}")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def _run_click_seq(args, static_region: screen.Region | None, step_log: list[dict], seq: _ArgPipe, guide,
                   record_dir: Path | None = None) -> str:
    """Deterministic OCR click path — NO Jev per step (~0.2-0.4 s/click).

    The woman-and-mouse "Calculator experience" generalized: each step OCRs the target
    window, resolves the next label (exact then substring), clicks its center, verifies by
    dHash. Risk gate is code-side (`RISKY_CLICK_HINTS`): a destructive/sensitive label
    forces --approval confirm and is refused under --approval none. Returns final status.
    """
    step = 0
    stuck = 0
    while step < args.max_steps:
        if _escape_pressed():
            print("finished: aborted (Esc pressed)")
            return "aborted"
        target = seq.take()
        if target is None:
            print("finished: done — click sequence complete")
            return "done"
        step += 1
        log.info("click-seq step %d: click %r", step, target)
        try:
            region, origin, shot, hwnd = _step_capture(args, static_region)
        except _WindowNotFound as exc:
            _dump_escalate(str(exc), "")
            step_log.append({"step": step, "target": target, "action": "click_element",
                             "confidence": 1.0, "reason": "window not found"})
            return "blocked"
        except win32.AmbiguousWindowError as exc:
            _dump_escalate(str(exc), "")
            step_log.append({"step": step, "target": target, "action": "click_element",
                             "confidence": 1.0, "reason": "window ambiguous"})
            return "blocked"
        elements = parse_ui.parse(shot.img, provider=args.ocr, origin=origin)
        inventory = parse_ui.state_text(elements)
        entry: dict = {"step": step, "target": target, "elements": len(elements),
                       "ocr_provider": args.ocr, "action": "click_element", "confidence": 1.0}
        print(f"[{step}] click_seq target={target!r} ({len(elements)} elements detected)")
        if not elements:
            entry["verify"] = {"error": "no elements detected"}
            _dump_escalate(f"click target {target!r}: no elements detected on screen", "")
            step_log.append(entry)
            return "blocked"
        elem = match_element(elements, target)
        if elem is None:
            entry["verify"] = {"error": f"no element matching {target!r}"}
            _dump_escalate(f"click target {target!r} not found on screen (label mismatch — dump the "
                           "inventory first)", inventory)
            step_log.append(entry)
            return "blocked"
        entry["element_id"] = elem.id
        risky = is_risky_click(target)
        entry["risk_class"] = "risky" if risky else "benign"
        click_box = _narrow_box(elem.box, elem.text, target)
        entry["narrowed"] = click_box != elem.box
        cbx1, cby1, cbx2, cby2 = click_box
        click_pt = _abs(((cbx1 + cbx2) // 2, (cby1 + cby2) // 2), origin)
        if guide.active:
            guide.draw(frames=[e.box for e in elements], target=click_box,
                       status=f"{_busy_status(args, step, elements, f'click-seq -> {target}')}",
                       cursor=click_pt,
                       labels=[(e.id, e.text) for e in elements])
        if record_dir:
            _record_step(record_dir, shot, elements, click_box,
                         f"step {step} click-seq -> {target}", click_pt, step, entry)
        if risky and args.approval == "confirm":
            if not _confirm(f"  click {target!r} at {click_pt}? (destructive/sensitive class)"):
                print("aborted by user")
                entry["verify"] = {"aborted": True}
                step_log.append(entry)
                return "aborted"
        elif risky and args.approval == "none":
            entry["verify"] = {"error": "destructive/sensitive label refused under --approval none"}
            _dump_escalate(f"target {target!r} is in the destructive/sensitive class — refused under "
                           "--approval none; run with --approval confirm", inventory)
            step_log.append(entry)
            return "blocked"

        if hwnd and not win32.point_owned_by(*click_pt, hwnd):
            entry["verify"] = {"error": "target occluded by another window"}
            _dump_escalate(f"click point {click_pt} is behind a window other than {args.window!r} "
                           "— refusing to click blind (hit-test)", inventory)
            step_log.append(entry)
            return "blocked"

        before = shot.dhash
        if not _refocus(hwnd):
            entry["verify"] = {"error": "focus lost before click"}
            _dump_escalate(
                f"window {args.window!r} no longer owns the foreground — refusing to click blind",
                inventory,
            )
            step_log.append(entry)
            return "blocked"
        act.click(click_pt)
        after = _settle(args, region)
        diff = screen.hamming(before, after.dhash)
        entry["verify"] = {"dhash_diff": diff}
        print(f"  verify: dhash diff={diff}")
        if diff <= qc.STUCK_DHASH:
            entry["verify"]["stuck"] = True
            print("  warning: screen did not change after click")
            stuck += 1
            if stuck >= qc.STUCK_TRIES:
                print("finished: blocked — stuck (no screen change for several clicks)")
                step_log.append(entry)
                return "blocked"
        else:
            stuck = 0
        step_log.append(entry)
    print(f"finished: max steps ({args.max_steps}) reached in click sequence")
    return "max_steps"


def _probe_providers() -> dict[str, bool]:
    """Query each OCR provider's availability in an ISOLATED interpreter.

    Ordering trap (found 2026-09-22, access-violation 0xC0000005): importing comtypes/UIAutomation
    (the a11y provider) BEFORE onnxruntime segfaults onnxruntime's native init in this process.
    Probing in a subprocess keeps the driver process pure: it only ever imports what it runs, so
    no native-import order can bite the live loop. ~0.5-1.5 s, doctor-only.
    """
    code = (
        "import json\n"
        "from jev_computer_use.computer_use import parse_ui\n"
        "out = {}\n"
        "for n in ('rapid', 'windows', 'glm', 'a11y'):\n"
        "    try:\n"
        "        out[n] = bool(parse_ui.get_provider(n).installed())\n"
        "    except Exception:\n"
        "        out[n] = False\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=60, text=True
        )
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        return {k: bool(v) for k, v in report.items()}
    except Exception:
        return {}


def _run_doctor(argv: list[str]) -> int:
    """Environment diagnostics before driving anything (drive-screen 'doctor' lesson).

    Cheaper than debugging a silent no-op mid-run: DPI scale (the 2026-09-22 125% crop bug),
    OCR engine availability, a real capture+parse, the clipboard, and the foreground query.
    Run once per machine/session change, then drive.
    """
    p = argparse.ArgumentParser(prog="jev-computer-use doctor")
    p.add_argument("--out", help="save a probe screenshot (PNG) to this path")
    opts = p.parse_args(argv)

    errors: list[str] = []

    def report(label: str, ok: bool, detail: str = "") -> None:
        if not ok and label not in errors:
            errors.append(label)
        suffix = f" — {detail}" if detail else ""
        print(f"  {'ok' if ok else 'FAIL':4} {label}{suffix}")

    print("== jev-computer-use doctor ==")

    dpi = win32.get_dpi()
    scale = f"{dpi / 96 * 100:.0f}%"
    report("DPI (process is DPI-aware; win32/UIA/mss all physical)", dpi >= 96, f"GetDpiForSystem={dpi} ({scale})")
    user32 = ctypes.WinDLL("user32")
    print(f"  screen: {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)} (physical)")

    for name, ok in _probe_providers().items():
        report(f"OCR provider {name}", ok, "(isolated subprocess — import-order-proof)")

    try:
        shot = screen.capture()
        els = parse_ui.parse(shot.img, provider="rapid")
        report("capture + rapid parse", True,
               f"{shot.img.width}x{shot.img.height}; {len(els)} elements detected")
        if opts.out:
            shot.img.save(opts.out)
            print(f"  probe saved: {opts.out}")
    except Exception as exc:
        report("capture + rapid parse", False, str(exc))

    try:
        saved = act.clipboard_snapshot()
        report("clipboard read", True, f"content {'non-empty' if saved else 'empty'}")
    except Exception as exc:
        report("clipboard read", False, str(exc))

    fg = win32.foreground()
    report("foreground query", fg is not None, f"fg hwnd={fg}")

    verdict = "all good" if not errors else f"PROBLEMS: {', '.join(errors)}"
    print(f"== done: {verdict} ==")
    return 1 if errors else 0


def _drive_goal(args, layer: DecisionLayer, static_region: screen.Region | None,
                step_log: list[dict], history: list[dict], text_pipe: _ArgPipe,
                key_pipe: _ArgPipe, guide, record_dir: Path | None = None) -> str:
    """Goal loop; returns a final status token: done|blocked|escalate|max_steps|aborted|dry_run|error."""
    final_status = "unknown"
    last_reason = ""
    cache = _InventoryCache()
    for step in range(1, args.max_steps + 1):
        if _escape_pressed():
            final_status = "aborted"
            last_reason = "Esc pressed — user cancelled"
            print("finished: aborted (Esc pressed)")
            break
        log.info("step %d: capturing + parsing", step)
        try:
            region, origin, shot, hwnd = _step_capture(args, static_region)
        except _WindowNotFound as exc:
            final_status = "blocked"
            last_reason = str(exc)
            _dump_escalate(str(exc), "")
            step_log.append({"step": step, "window": args.window, "action": "escalate",
                             "reason": "window not found"})
            break
        except win32.AmbiguousWindowError as exc:
            final_status = "blocked"
            last_reason = str(exc)
            _dump_escalate(str(exc), "")
            step_log.append({"step": step, "window": args.window, "action": "escalate",
                             "reason": "window ambiguous"})
            break

        elements = cache.get(args, shot, origin, region)
        if elements is None:
            elements = parse_ui.parse(shot.img, provider=args.ocr, origin=origin)
            cache.store(elements)
        inventory = parse_ui.state_text(elements)
        log.info("step %d: %d elements detected; deciding", step, len(elements))
        if guide.active:
            guide.draw(frames=[e.box for e in elements], target=None,
                       status=_busy_status(args, step, elements, "deciding…"),
                       labels=[(e.id, e.text) for e in elements])
        try:
            plan = decide.decide_plan(layer, args.goal, elements, history=history)
        except NoProviderAvailable as exc:
            final_status = "error"
            last_reason = f"no decision provider available:\n{exc}"
            log.error(last_reason)
            print(last_reason, file=sys.stderr)
            break

        entry = {
            "step": step,
            "elements": len(elements),
            "inventory": inventory or "(empty)",
            "ocr_provider": args.ocr,
            "action": plan.action,
            "element_id": plan.element_id,
            "confidence": plan.action_confidence,
            "safe": plan.safe,
            "risk": plan.risk,
            "reason": plan.reason,
            "provider": plan.provider,
            "model": plan.model,
        }
        step_log.append(entry)
        history.append(
            {
                "step": step,
                "action": plan.action,
                "element": plan.element_id,
                "safe": plan.safe,
                "risk": plan.risk,
            }
        )
        history = history[-8:]  # keep the last 8 steps visible to the model
        print(f"[{step}] action={plan.action} element#{plan.element_id} conf={plan.action_confidence:.2f} "
              f"safe={plan.safe} risk={plan.risk} ({plan.reason})")

        if args.dry_run:
            final_status = "dry_run"
            last_reason = plan.reason
            log.info("dry-run: stopping before acting")
            break

        if plan.action == "done":
            final_status = "done"
            last_reason = plan.reason
            print(f"finished: done — {plan.reason}")
            break
        if plan.action == "blocked":
            final_status = "blocked"
            last_reason = plan.reason
            print(f"finished: blocked — {plan.reason}")
            break
        if plan.escalate:
            final_status = "escalate"
            last_reason = plan.reason
            _dump_escalate(plan.reason, inventory)
            break

        target = None
        plan_box: tuple[int, int, int, int] | None = None
        if plan.element_id is not None:
            elem = next((e for e in elements if e.id == plan.element_id), None)
            if elem is None:
                final_status = "escalate"
                last_reason = f"element #{plan.element_id} disappeared from inventory"
                entry["verify"] = {"error": last_reason}
                _dump_escalate(last_reason, inventory)
                break
            # arc-cua affordance schema: a goal-loop action that CANNOT legally apply to the
            # chosen control type is refused code-side — no Jev retry burned, no blind send.
            conflict = _affordance_conflict(plan.action, elem)
            if conflict:
                final_status = "escalate"
                last_reason = conflict
                entry["verify"] = {"error": conflict}
                _dump_escalate(conflict, inventory)
                break
            target = _abs(elem.center, origin)
            plan_box = elem.box

        text = text_pipe.take() if plan.action in ("type_text", "paste_text") else None
        key = key_pipe.take() if plan.action == "press_key" else None
        if plan.action in ("type_text", "paste_text") and text is None:
            if args.text_helper and text_helper.configured():
                # #6: tiny LLM writes ONLY the value; any failure escalates, never guesses.
                local_els = [e.to_state_line() for e in elements]
                try:
                    text = text_helper.generate_text(
                        goal=args.goal,
                        field_label=plan.reason or plan.element_id or "the field",
                        inventory=local_els,
                        history=history,
                    )
                except text_helper.TextHelperError as exc:
                    final_status = "escalate"
                    last_reason = f"text-helper failed: {exc}"
                    entry["verify"] = {"error": last_reason}
                    _dump_escalate(f"{plan.action} needs a value — text-helper failed too ({exc})", inventory)
                    break
            else:
                final_status = "escalate"
                last_reason = f"{plan.action} needs --input-text (or --text-helper)"
                entry["verify"] = {"error": last_reason}
                _dump_escalate(last_reason, inventory)
                break
        if plan.action == "press_key" and key is None:
            final_status = "escalate"
            last_reason = "press_key needs --press-key for this step"
            entry["verify"] = {"error": last_reason}
            _dump_escalate(last_reason, inventory)
            break

        if guide.active:
            guide.draw(
                frames=[e.box for e in elements], target=plan_box,
                status=_busy_status(
                    args, step, elements,
                    f"plan: {plan.action} -> {target or 'viewport'} (conf={plan.action_confidence:.2f})"
                ),
                cursor=target,
                labels=[(e.id, e.text) for e in elements],
            )
        _record_step(record_dir, shot, elements, plan_box,
                     _busy_status(args, step, elements, f"plan: {plan.action}"), target, step, entry)

        if args.approval == "confirm" or plan.needs_confirm or (plan.risk or 0) >= qc.RISK_CONFIRM:
            if not _confirm(f"  act: {plan.action} on {target or 'viewport'}? (conf={plan.action_confidence:.2f}, "
                            f"risk={plan.risk})"):
                final_status = "aborted"
                last_reason = "user declined the action prompt"
                print("aborted by user")
                entry["verify"] = {"aborted": True}
                break

        # drive-screen rule #3: prove the intended window still owns the foreground right
        # before acting (fast path = one GetForegroundWindow; only a stolen focus pays).
        if not _refocus(hwnd):
            final_status = "escalate"
            last_reason = f"window {args.window!r} no longer owns the foreground"
            entry["verify"] = {"error": "focus lost before act"}
            _dump_escalate(last_reason, inventory)
            break

        # #5: never blind-click into an overlay sitting above the target app.
        if plan.action == "click_element" and target is not None and hwnd:
            if not win32.point_owned_by(*target, hwnd):
                final_status = "escalate"
                last_reason = f"click target {target} is behind another window (hit-test)"
                entry["verify"] = {"error": "target occluded by another window"}
                _dump_escalate(last_reason, inventory)
                break

        # #8 (tiptour pointer-mark lesson): glide the cursor onto the target before any
        # keyboard/scroll action so the operator sees the agent AIM, not only clicks.
        if target is not None and plan.action not in ("click_element", "double_click"):
            try:
                act.aim(target)
            except Exception:  # pragma: no cover - cosmetic; never abort the run for this
                log.warning("aim (cursor glide) failed", exc_info=True)

        before = shot.dhash
        try:
            act.execute(
                plan.action, target, text, key,
                focus_check=_focus_check(hwnd),
            )
        except act.FocusLostError as exc:
            # #4: type_text lost focus mid-send — refocus and send ONLY the remainder
            # (re-typing from the start would duplicate what already landed).
            if (
                plan.action == "type_text"
                and text is not None
                and exc.typed < exc.total
                and _refocus(hwnd, retries=2)
            ):
                remaining = text[exc.typed:]
                log.warning("focus lost mid-type (%d/%d) — refocusing, resuming %d chars",
                            exc.typed, exc.total, len(remaining))
                try:
                    act.type_text(remaining, focus_check=_focus_check(hwnd))
                except act.FocusLostError as exc2:
                    final_status = "escalate"
                    last_reason = f"focus lost again mid-type after resume ({exc2.typed}/{exc2.total})"
                    entry["verify"] = {"error": "focus_lost_midsend", "typed": exc2.typed, "total": exc2.total}
                    _dump_escalate(f"{last_reason} — screenshot before retrying", inventory)
                    break
                text = None  # fully delivered; don't re-evaluate below
            else:
                final_status = "escalate"
                last_reason = f"focus lost mid-{plan.action}: {exc.typed}/{exc.total} chars landed"
                entry["verify"] = {"error": "focus_lost_midsend", "typed": exc.typed, "total": exc.total}
                _dump_escalate(f"{last_reason} — screenshot before retrying "
                               "(re-sending the whole string duplicates what arrived)", inventory)
                break

        after = _settle(args, region)
        diff = screen.hamming(before, after.dhash)
        entry["verify"] = {"dhash_diff": diff}
        print(f"  verify: dhash diff={diff}")

        if diff <= qc.STUCK_DHASH:
            entry["verify"]["stuck"] = True
            print("  warning: screen did not change after action")
        if sum(1 for e in step_log if e.get("verify", {}).get("stuck")) >= qc.STUCK_TRIES:
            final_status = "blocked"
            last_reason = f"stuck (no screen change after {qc.STUCK_TRIES} actions)"
            print(f"finished: blocked — {last_reason}")
            break
    else:
        final_status = "max_steps"
        last_reason = f"max steps ({args.max_steps}) reached without completion"
        print(f"finished: {last_reason}")

    # keep the terminal reason visible to callers (subtask contract + logs)
    if final_status == "done" and not last_reason:
        last_reason = "goal completed"
    for e in step_log:
        e.setdefault("final_status", final_status)
    return final_status


_CONTRACT_STATUS = {
    "done": "SUBTASK_COMPLETE",
    "dry_run": "SUBTASK_COMPLETE",
    "blocked": "BLOCKED",
    "max_steps": "BLOCKED",
    "aborted": "BLOCKED",
    "escalate": "NEEDS_AGENT",
    "error": "NEEDS_AGENT",
    "unknown": "NEEDS_AGENT",
}


def _run_subtask(argv: list[str]) -> int:
    """arc-cua-style bounded contract drive: `subtask <payload.json>` → JSON result.

    Payload shape:
      {
        "goal": "…",                      # required (the only thing a model must produce)
        "inputs": ["Alice", "Bob"],       # optional ordered text values (like --input-text)
        "max_actions": 10,                # optional bounded step budget
        "options": {"window": "…", "ocr": "…", "approval": "none", "show_guide": true, …}
      }
    The goal loop is the SAME code path as the CLI (`_drive_goal`); we just templated the
    argparse defaults and require zero interactivity by default (approval=none). stdout is the
    machine-facing contract:
      {"result": "SUBTASK_COMPLETE|BLOCKED|NEEDS_AGENT", "status": <raw token>,
       "actions_taken": N, "reason": "…", "observations": [...], "history": [...]}
    """
    p = argparse.ArgumentParser(prog="jev-computer-use subtask")
    p.add_argument("payload", help="path to a .json task payload (see docstring)")
    opts = p.parse_args(argv)

    payload_path = Path(opts.payload)
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"result": "NEEDS_AGENT", "status": "error",
                          "actions_taken": 0, "reason": f"payload unreadable: {exc}",
                          "observations": [], "history": []}))
        return 2
    if not isinstance(payload, dict) or not str(payload.get("goal", "")).strip():
        print(json.dumps({"result": "NEEDS_AGENT", "status": "error", "actions_taken": 0,
                          "reason": "payload.goal required", "observations": [], "history": []}))
        return 2

    base = build_parser().parse_args([])  # argparse defaults (same code path as CLI)
    opt = payload.get("options") or {}
    ns = argparse.Namespace(**vars(base))
    for k, v in opt.items():
        if hasattr(ns, k):
            setattr(ns, k, v)
    ns.goal = payload["goal"]
    ns.click_seq = None
    ns.input_text = payload.get("inputs")
    if isinstance(ns.input_text, (list, tuple)):
        ns.input_text = "|".join(str(v) for v in ns.input_text)
    if payload.get("max_actions"):
        ns.max_steps = int(payload["max_actions"])
    ns.approval = opt.get("approval", "none")  # non-interactive by default (machine contract)
    ns.json = True  # step log always emitted on stdout

    layer = DecisionLayer(
        providers=[p_ for p_ in DecisionLayer._default_providers() if not ns.channel or p_.name in ns.channel]
    )
    guide = overlay.GuideOverlay()
    if ns.show_guide:
        guide.open()
    step_log: list[dict] = []
    history: list[dict] = []
    try:
        status = _drive_goal(ns, layer, _parse_region(ns.region), step_log, history,
                             _ArgPipe(ns.input_text), _ArgPipe(ns.press_key), guide)
    finally:
        guide.close()

    terminal = step_log[-1] if step_log else {}
    status = terminal.get("final_status") or status
    result = _CONTRACT_STATUS.get(status, "NEEDS_AGENT")
    actions = [e for e in step_log
               if e.get("action") not in ("done", "blocked", "escalate")
               and not e.get("verify", {}).get("aborted")]
    out = {
        "result": result,
        "status": status,
        "actions_taken": len(actions),
        "reason": terminal.get("verify", {}).get("error")
                  or str(terminal.get("reason", "")) or f"ended {status}",
        "observations": [
            {"step": e.get("step"), "action": e.get("action"), "element": e.get("element_id"),
             "dhash_diff": e.get("verify", {}).get("dhash_diff")}
            for e in step_log if e.get("action")
        ],
        "history": history,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _run_replay(argv: list[str]) -> int:
    """`je v-computer-use replay DIR` → stitch a --record dir into an animated GIF + captions."""
    p = argparse.ArgumentParser(prog="jev-computer-use replay")
    p.add_argument("dir", help="directory written by --record (step_*.png + steps.jsonl)")
    p.add_argument("--gif", help="output GIF path (default: <dir>/out.gif)")
    opts = p.parse_args(argv)
    d = Path(opts.dir)
    if not d.is_dir():
        print(f"replay: no such record dir: {d}", file=sys.stderr)
        return 2
    gif = Path(opts.gif) if opts.gif else d / "out.gif"
    n = record.assemble_gif(d, gif)
    print(f"replay: {n} frames -> {gif}")
    for c in record.captions(d):
        verify = c.get("verify") or {}
        print(f"  [{c.get('step'):>2}] {c.get('action')} el#{c.get('element_id')} "
              f"conf={c.get('confidence')} {c.get('reason') or ''} diff={verify.get('dhash_diff')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Console on this machine is cp1252; WinRT OCR reads CJK glyphs off-screen (menu icons,
    # ime candidates). Guarantee no UnicodeEncodeError can kill the loop mid-run.
    for _stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(_stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    if raw and raw[0] == "doctor":
        return _run_doctor(raw[1:])
    if raw and raw[0] == "subtask":
        return _run_subtask(raw[1:])
    if raw and raw[0] == "replay":
        return _run_replay(raw[1:])
    args = build_parser().parse_args(raw)
    static_region = _parse_region(args.region)
    if args.window and args.region:
        print("error: --window and --region are mutually exclusive", file=sys.stderr)
        return 2
    if bool(args.goal) == bool(args.click_seq):
        print("error: provide exactly one of --goal or --click-seq", file=sys.stderr)
        return 2

    providers = [p for p in DecisionLayer._default_providers() if not args.channel or p.name in args.channel]
    layer = DecisionLayer(providers=providers)
    text_pipe = _ArgPipe(args.input_text)
    key_pipe = _ArgPipe(args.press_key)
    LOGS_DIR.mkdir(exist_ok=True)
    step_log: list[dict] = []
    history: list[dict] = []

    guide = overlay.GuideOverlay()
    if args.show_guide:
        guide.open()
    record_dir: Path | None = Path(args.record) if args.record else None
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "meta.json").write_text(
            json.dumps({"goal": args.goal, "click_seq": args.click_seq, "window": args.window,
                        "region": args.region, "ocr": args.ocr, "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"recording annotated frames -> {record_dir}")
    try:
        if args.click_seq:
            _run_click_seq(args, static_region, step_log, _ArgPipe(args.click_seq), guide, record_dir)
            return 0
        _drive_goal(args, layer, static_region, step_log, history, text_pipe, key_pipe, guide, record_dir)
    except KeyboardInterrupt:  # pragma: no cover - interactive abort
        print("\naborted (Ctrl+C)")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("agent crashed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        guide.close()
        summary = {"goal": args.goal, "click_seq": args.click_seq, "dry_run": args.dry_run,
                   "window": args.window, "steps": step_log}
        report = LOGS_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"step log: {report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())