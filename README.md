# jev-computer-use

TypeSafe **Jev ("System One")** decision layer + a **hardware-fitted computer-use cascade** for this
machine (i7-7500U · 16 GB · GTX 950M = CPU-first).

**Jev is not an LLM.** It reads text/JSON `state`, answers typed `choice` / `score` / `noul` questions
with calibrated probabilities + confidence, and returns in 70–500 ms at **$0.042/M input tokens
(output free)**. This repo wraps it behind a **provider cascade** (official → Cloudflare free →
OpenRouter → local heuristic) and uses it as the decision/guardrail layer *inside* a minimal GUI agent.

```
capture → parse (RapidOCR/ONNX) → decide (Jev) → act (pyautogui) → verify (dHash) → escalate (free VLM / local-vision)
```

```
E:\Projects\jev-computer-use\
├─ README.md
├─ docs\                 JEV_DOSSIER · COMPUTER_USE_ARCH · FREE_PROVIDERS · JAGGEDNESS
├─ src\jev_computer_use\
│  ├─ questions.py           typed question builders (choice/score/noul)
│  ├─ decision_layer.py      provider cascade + answer normalization
│  ├─ decision_cli.py        `jev-decision` CLI
│  └─ computer_use\
│     ├─ screen.py           mss capture + dHash + regions
│     ├─ parse_ui.py         RapidOCR → element inventory {id,text,box,center}
│     ├─ decide.py           Jev questions + thresholds → action plan
│     ├─ act.py              pyautogui/Win32 execution
│     └─ run_agent.py        `jev-computer-use` loop + step log + approval
└─ tests\
```

## Setup

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[computer-use,dev]"
Copy-Item .env.example .env   # fill TYPESAFE_API_KEY (+ Cloudflare for the free tier; see api-providers.md)
```

(Python 3.12 — several ML wheels lack 3.14; uv resolves it. All installs on E: per system policy.)

## CLI

```powershell
# Decision layer — batch all questions in one call
jev-decision "I was charged twice. Please refund me." `
  --noul   "wants_refund|Does the customer ask for money back?|true=A refund is requested|false=No refund requested" `
  --choice "department|Which team should handle this?|billing=Payments, invoices, refunds;technical=Bugs, integrations" `
  --score  "frustration|How frustrated is the customer?|Calm|Frustrated|Very angry"

jev-decision state.json --questions questions.json --json   # or a full questions map
jev-decision --status                                       # which providers are configured

# Computer use (dry-run first!)
jev-computer-use --goal "open Notepad, type Hello, save to E:\tmp\n.txt" --dry-run
jev-computer-use --goal "open Notepad, type Hello, save to E:\tmp\n.txt" --approval confirm --max-steps 12
```

## Provider cascade (per call, first success wins)

| # | Provider | Env | Notes |
|---|---|---|---|
| 1 | TypeSafe official | `TYPESAFE_API_KEY` | best fidelity; waitlist-gated key already in `api-providers.md` |
| 2 | Cloudflare Workers AI `typesafe/jev` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | free 10k neurons/day, no TypeSafe key |
| 3 | OpenRouter `typesafe/jev-1.13` | `OPENROUTER_API_KEY` | same price; **needs prepaid credits** |
| 4 | Local heuristic | — | noul-only, low confidence, last ditch |

Every answer records its `provider` + echoed `model`. Keep ≥2 channels configured; providers churn
fast in this space.

## Related skills

- `.agents/skills/jev/SKILL.md` — the decision-layer expertise (primitives, thresholds, jaggedness).
- `.agents/skills/computer-use/SKILL.md` — this loop, safety model, escalation.
- Knowledge: `C:\Users\USER\.agents\knowledge\jev-ai-model-research.md` (full 2026-09-18 assessment).

Keys: never commit; live in `api-providers.md` + project-local `.env` (gitignored).