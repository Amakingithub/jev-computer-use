"""jev-computer-use — minimal CPU-first GUI agent.

Flow: capture → RapidOCR parse → Jev decide (which element, which action, safe?, risk?) →
act (pyautogui) → verify (dHash) → escalate / done / blocked.

Safety: default approval=confirm (prompt before every step). DESTRUCTIVE/SENSITIVE flows and
anything with step_risk >= RISK_CONFIRM force confirmation regardless. Run --dry-run first.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from ..decision_layer import DecisionLayer, NoProviderAvailable
from . import act, decide, parse_ui, screen, win32
from . import questions_computer_use as qc

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("computer-use")

LOGS_DIR = Path("logs")


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
    p.add_argument("--delay", type=float, default=qc.STEP_DELAY, help="seconds between action and verify")
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
    p.add_argument("--input-text", help="text to type when next_action=type_text ('|' separates per-step values)")
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


def match_element(elements: list[parse_ui.Element], target: str) -> parse_ui.Element | None:
    """Deterministic label → element for --click-seq (no Jev, no VLM).

    Exact normalized-equality first, then normalized substring (so 'Next' hits 'Next >').
    Ids are position-sorted so the first exact match is the top-left-most one.
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
    return None


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


def _step_capture(args, static_region: screen.Region | None) -> tuple[
        screen.Region | None, tuple[int, int], screen.Shot,
]:
    """Resolve --window (fresh every step: catches apps launched mid-run, window moves) or
    --region, then capture. Raises _WindowNotFound when --window matches nothing."""
    if args.window:
        w = win32.find_window(args.window)
        if w is None:
            raise _WindowNotFound(args.window)
        win32.focus_window(w.hwnd)
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
    return region, origin, screen.capture(region)


def _run_click_seq(args, static_region: screen.Region | None, step_log: list[dict], seq: _ArgPipe) -> None:
    """Deterministic OCR click path — NO Jev per step (~0.2-0.4 s/click).

    The woman-and-mouse "Calculator experience" generalized: each step OCRs the target
    window, resolves the next label (exact then substring), clicks its center, verifies by
    dHash. Risk gate is code-side (`RISKY_CLICK_HINTS`): a destructive/sensitive label
    forces --approval confirm and is refused under --approval none.
    """
    step = 0
    stuck = 0
    while step < args.max_steps:
        target = seq.take()
        if target is None:
            print("finished: done — click sequence complete")
            return
        step += 1
        log.info("click-seq step %d: click %r", step, target)
        try:
            region, origin, shot = _step_capture(args, static_region)
        except _WindowNotFound as exc:
            _dump_escalate(str(exc), "")
            step_log.append({"step": step, "target": target, "action": "click_element",
                             "confidence": 1.0, "reason": "window not found"})
            return
        elements = parse_ui.parse(shot.img, provider=args.ocr, origin=origin)
        inventory = parse_ui.state_text(elements)
        entry: dict = {"step": step, "target": target, "elements": len(elements),
                       "ocr_provider": args.ocr, "action": "click_element", "confidence": 1.0}
        print(f"[{step}] click_seq target={target!r} ({len(elements)} elements detected)")
        if not elements:
            entry["verify"] = {"error": "no elements detected"}
            _dump_escalate(f"click target {target!r}: no elements detected on screen", "")
            step_log.append(entry)
            return
        elem = match_element(elements, target)
        if elem is None:
            entry["verify"] = {"error": f"no element matching {target!r}"}
            _dump_escalate(f"click target {target!r} not found on screen (label mismatch — dump the "
                           "inventory first)", inventory)
            step_log.append(entry)
            return
        entry["element_id"] = elem.id
        risky = is_risky_click(target)
        entry["risk_class"] = "risky" if risky else "benign"
        click_pt = _abs(elem.center, origin)
        if risky and args.approval == "confirm":
            if not _confirm(f"  click {target!r} at {click_pt}? (destructive/sensitive class)"):
                print("aborted by user")
                entry["verify"] = {"aborted": True}
                step_log.append(entry)
                return
        elif risky and args.approval == "none":
            entry["verify"] = {"error": "destructive/sensitive label refused under --approval none"}
            _dump_escalate(f"target {target!r} is in the destructive/sensitive class — refused under "
                           "--approval none; run with --approval confirm", inventory)
            step_log.append(entry)
            return

        before = shot.dhash
        act.click(click_pt)
        time.sleep(args.delay)
        after = screen.capture(region)
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
                return
        else:
            stuck = 0
        step_log.append(entry)
    print(f"finished: max steps ({args.max_steps}) reached in click sequence")


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
    args = build_parser().parse_args(argv)
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

    try:
        if args.click_seq:
            _run_click_seq(args, static_region, step_log, _ArgPipe(args.click_seq))
            return 0

        for step in range(1, args.max_steps + 1):
            log.info("step %d: capturing + parsing", step)
            try:
                region, origin, shot = _step_capture(args, static_region)
            except _WindowNotFound as exc:
                _dump_escalate(str(exc), "")
                step_log.append({"step": step, "window": args.window, "action": "escalate",
                                 "reason": "window not found"})
                break
            elements = parse_ui.parse(shot.img, provider=args.ocr, origin=origin)
            inventory = parse_ui.state_text(elements)
            log.info("step %d: %d elements detected; deciding", step, len(elements))
            try:
                plan = decide.decide_plan(layer, args.goal, elements, history=history)
            except NoProviderAvailable as exc:
                msg = f"step {step}: no decision provider available. Put keys in .env, then retry.\n{exc}"
                log.error(msg)
                print(msg, file=sys.stderr)
                return 2

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
                log.info("dry-run: stopping before acting")
                break

            if plan.action in ("done", "blocked"):
                print(f"finished: {plan.action} — {plan.reason}")
                break
            if plan.escalate:
                _dump_escalate(plan.reason, inventory)
                break

            target = None
            if plan.element_id is not None:
                elem = next((e for e in elements if e.id == plan.element_id), None)
                if elem is None:
                    entry["verify"] = {"error": f"element #{plan.element_id} missing from inventory"}
                    _dump_escalate(f"element #{plan.element_id} disappeared from inventory", inventory)
                    break
                target = _abs(elem.center, origin)

            text = text_pipe.take() if plan.action == "type_text" else None
            key = key_pipe.take() if plan.action == "press_key" else None
            if plan.action == "type_text" and text is None:
                entry["verify"] = {"error": "no input-text value left"}
                _dump_escalate("type_text needs --input-text (no value supplied for this step)", inventory)
                break
            if plan.action == "press_key" and key is None:
                entry["verify"] = {"error": "no press-key value left"}
                _dump_escalate("press_key needs --press-key for this step", inventory)
                break

            if args.approval == "confirm" or plan.needs_confirm or (plan.risk or 0) >= qc.RISK_CONFIRM:
                if not _confirm(f"  act: {plan.action} on {target or 'viewport'}? (conf={plan.action_confidence:.2f}, "
                                f"risk={plan.risk})"):
                    print("aborted by user")
                    entry["verify"] = {"aborted": True}
                    break

            before = shot.dhash
            act.execute(plan.action, target, text, key)
            time.sleep(args.delay)
            after = screen.capture(region)
            diff = screen.hamming(before, after.dhash)
            entry["verify"] = {"dhash_diff": diff}
            print(f"  verify: dhash diff={diff}")

            if diff <= qc.STUCK_DHASH:
                entry["verify"]["stuck"] = True
                print("  warning: screen did not change after action")
            if sum(1 for e in step_log if e.get("verify", {}).get("stuck")) >= qc.STUCK_TRIES:
                print("finished: blocked — stuck (no screen change for several steps)")
                break
        else:
            print(f"finished: max steps ({args.max_steps}) reached without completion")
    except KeyboardInterrupt:  # pragma: no cover - interactive abort
        print("\naborted (Ctrl+C)")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("agent crashed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        summary = {"goal": args.goal, "click_seq": args.click_seq, "dry_run": args.dry_run, "window": args.window,
               "steps": step_log}
        report = LOGS_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"step log: {report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())