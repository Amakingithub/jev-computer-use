"""jev-decision — CLI for the Jev System One decision layer.

Batch many questions into ONE Jev call (speculative fan-out). Supports both inline question
shorthand and a full questions JSON file.

Examples:
  jev-decision "I was charged twice. Please refund me." ^
      --noul   "wants_refund|Does the customer ask for money back?|true=A refund is requested|false=No refund" ^
      --choice "department|Which team should handle this?|billing=Payments, invoices;technical=Bugs" ^
      --score  "frustration|How frustrated is the customer?|Calm|Frustrated|Very angry" ^
      --json
  jev-decision state.json --questions questions.json --json
  jev-decision --status
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .decision_layer import DecisionLayer, NoProviderAvailable
from .questions import Question

SHORTHAND = {
    "noul": "Noul (yes/no probability):  ID|Does [claim]?|true=..|false=..",
    "choice": "Choice (pick from set):    ID|Which [..]?|opt1=desc;opt2=desc",
    "score": "Score (ordered levels):     ID|How [..]?|Low|Mid|High",
}


def _split_top(s: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return parts


def _parse_criteria_map(text: str) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for part in _split_top(text, ";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            opt, desc = part.split("=", 1)
            criteria[opt.strip()] = desc.strip()
        else:
            criteria[part] = ""
    return criteria


def parse_noul_arg(arg: str) -> Question:
    parts = _split_top(arg, "|")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("noul shorthand needs: ID|instructions[|true=..|false=..]")
    key, instructions = parts[0], parts[1]
    true_desc = false_desc = None
    for piece in parts[2:]:
        piece = piece.strip()
        if piece.startswith("true="):
            true_desc = piece[len("true="):].strip()
        elif piece.startswith("false="):
            false_desc = piece[len("false="):].strip()
    criteria = None
    if true_desc is not None or false_desc is not None:
        criteria = {}
        if true_desc is not None:
            criteria["true"] = true_desc
        if false_desc is not None:
            criteria["false"] = false_desc
    return Question(key=key, type="noul", instructions=instructions, criteria=criteria)


def parse_choice_arg(arg: str) -> Question:
    parts = _split_top(arg, "|")
    if len(parts) < 3 or not parts[2].strip():
        raise argparse.ArgumentTypeError("choice shorthand needs: ID|instructions|opt1=desc;opt2=desc")
    key, instructions = parts[0], parts[1]
    return Question(key=key, type="choice", instructions=instructions, criteria=_parse_criteria_map(parts[2]))


def parse_score_arg(arg: str) -> Question:
    parts = _split_top(arg, "|")
    if len(parts) < 4:
        raise argparse.ArgumentTypeError("score shorthand needs: ID|instructions|Low|Mid|High")
    key, instructions = parts[0], parts[1]
    levels = [p for p in parts[2:] if p]
    if not 2 <= len(levels) <= 10:
        raise argparse.ArgumentTypeError("score needs 2..10 levels")
    return Question(key=key, type="score", instructions=instructions, criteria=levels)


def load_questions_file(path: str | Path) -> list[Question]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    questions: list[Question] = []
    for key, spec in data.items():
        qtype = spec.get("type")
        if qtype not in ("choice", "score", "noul"):
            raise ValueError(f"question {key!r}: invalid type {qtype!r}")
        questions.append(
            Question(key=key, type=qtype, instructions=spec["instructions"], criteria=spec.get("criteria"))
        )
    return questions


def _read_state(value: str | None, state_file: str | None, positional: str | None) -> Any:
    if state_file:
        text = Path(state_file).read_text(encoding="utf-8").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if value:
        text = value.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if positional:
        text = positional.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return sys.stdin.read() if not sys.stdin.isatty() else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-decision",
        description="Batch typed questions to the Jev System One decision layer (provider cascade).",
    )
    parser.add_argument("positional", nargs="?", help="state string (or JSON). Alias for --state.")
    parser.add_argument("--state", help="state string (or JSON).")
    parser.add_argument("--state-file", help="read state from file (JSON auto-detected).")
    parser.add_argument("--questions", type=Path, help="questions JSON file: {id: {type,instructions,criteria}}.")
    parser.add_argument(
        "--noul", action="append", default=[], metavar="ID|INSTR[|true=..|false=..]", help=SHORTHAND["noul"]
    )
    parser.add_argument(
        "--choice", action="append", default=[], metavar="ID|INSTR|opt=desc;opt=desc", help=SHORTHAND["choice"]
    )
    parser.add_argument(
        "--score", action="append", default=[], metavar="ID|INSTR|L1|L2|L3", help=SHORTHAND["score"]
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        choices=("typesafe", "cloudflare", "openrouter", "heuristic"),
        help="restrict to these providers (repeatable; default = full cascade).",
    )
    parser.add_argument("--model", help="override model (e.g. jev-1.13.0).")
    parser.add_argument("--json", action="store_true", help="JSON output.")
    parser.add_argument("--status", action="store_true", help="show configured channels and exit.")
    return parser


def collect_questions(args: argparse.Namespace) -> list[Question]:
    questions: list[Question] = []
    if args.questions:
        questions.extend(load_questions_file(args.questions))
    questions.extend(parse_noul_arg(a) for a in args.noul)
    questions.extend(parse_choice_arg(a) for a in args.choice)
    questions.extend(parse_score_arg(a) for a in args.score)
    if not questions:
        raise SystemExit("no questions given (use --noul/--choice/--score or --questions)")
    return questions


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.status:
        _print_status(args)
        return 0

    state = _read_state(args.state, args.state_file, args.positional)
    if not state and not args.positional:
        raise SystemExit("no state given (positional arg, --state, --state-file, or piped stdin)")
    questions = collect_questions(args)

    providers = [
        p for p in DecisionLayer._default_providers()
        if not args.channel or p.name in args.channel
    ]
    layer = DecisionLayer(providers=providers)

    try:
        resp = layer.ask(state, questions, model=args.model)
    except NoProviderAvailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(resp.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"provider:  {resp.provider}  model: {resp.model}  latency: {resp.latency_ms}ms")
        for key, answer in resp.by_key().items():
            print(f"  {key} [{answer.type}]: " + json.dumps(answer.to_dict(), ensure_ascii=False))
    return 0


def _print_status(args: argparse.Namespace) -> None:
    print("Jev decision channels (provider  cascade | configured | default model)")
    for row in DecisionLayer().status():
        print(f"  {row['provider']:<12} {str(row['configured']):<11} {row['default_model']}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())