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
from . import act, decide, parse_ui, screen
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
        description="CPU-first GUI agent: RapidOCR + Jev decisions + pyautogui + dHash verify.",
    )
    p.add_argument("--goal", required=True, help="natural-language task description")
    p.add_argument("--dry-run", action="store_true", help="parse + decide + print plan, act nothing")
    p.add_argument("--approval", choices=("none", "confirm"), default="confirm",
                   help="confirm = ask before every step (default). none = demo/dev only.")
    p.add_argument("--max-steps", type=int, default=qc.MAX_STEPS)
    p.add_argument("--delay", type=float, default=qc.STEP_DELAY, help="seconds between action and verify")
    p.add_argument("--region", help="x1,y1,x2,y2 (absolute screen coords) to watch")
    p.add_argument("--input-text", help="text to type when next_action=type_text ('|' separates per-step values)")
    p.add_argument(
        "--press-key", help="key to press when next_action=press_key, e.g. Enter; '|' separates per-step values"
    )
    p.add_argument("--channel", action="append", default=[], choices=("typesafe", "cloudflare", "openrouter"),
                   help="restrict decision provider (repeatable)")
    p.add_argument(
        "--ocr",
        default="rapid",
        choices=("rapid", "windows", "glm", "merge", "auto"),
        help="OCR engine: rapid (default, ONNX) | windows (WinRT, free, reads glyphs) | "
             "glm (local vision OCR, optional) | merge (rapid + windows gap-fill) | auto (cascade)",
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
    region = _parse_region(args.region)

    providers = [p for p in DecisionLayer._default_providers() if not args.channel or p.name in args.channel]
    layer = DecisionLayer(providers=providers)
    text_pipe = _ArgPipe(args.input_text)
    key_pipe = _ArgPipe(args.press_key)
    LOGS_DIR.mkdir(exist_ok=True)
    step_log: list[dict] = []
    history: list[dict] = []

    try:
        for step in range(1, args.max_steps + 1):
            log.info("step %d: capturing + parsing", step)
            shot = screen.capture(region)
            elements = parse_ui.parse(shot.img, provider=args.ocr)
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
                print(f"escalate: {plan.reason}")
                print("  > feed the screen inventory + goal to `local-vision` or a free VLM and re-plan.")
                break

            target = None
            if plan.element_id is not None:
                elem = next((e for e in elements if e.id == plan.element_id), None)
                if elem is None:
                    entry["verify"] = {"error": f"element #{plan.element_id} missing from inventory"}
                    print("escalate: element disappeared from inventory")
                    break
                target = elem.center

            text = text_pipe.take() if plan.action == "type_text" else None
            key = key_pipe.take() if plan.action == "press_key" else None
            if plan.action == "type_text" and text is None:
                print("escalate: type_text needs --input-text (no value supplied for this step)")
                entry["verify"] = {"error": "no input-text value left"}
                break
            if plan.action == "press_key" and key is None:
                print("escalate: press_key needs --press-key for this step")
                entry["verify"] = {"error": "no press-key value left"}
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
        summary = {"goal": args.goal, "dry_run": args.dry_run, "steps": step_log}
        report = LOGS_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"step log: {report}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())