# JEV_DOSSIER — TypeSafe System One, from first principles

> Compiled 2026-09-21 · Authoritative think: `docs.typesafe.ai` (esp. `api.md`, `primitives.md`,
> `confidence.md`, `patterns.md`, `model-jaggedness/jev-1.13.md`) + the 2026-09-18 last30days sweep in
> `C:\Users\USER\.agents\knowledge\jev-ai-model-research.md` + `jevaiguide.com`/OpenRouter channels.

---

## 1. Identity

TypeSafe AI left stealth **2026-09-15** with a $40M DCVC seed. CEO Diogo Almeida (co-inventor RLHF,
InstructGPT/ChatGPT/GPT-4 at OpenAI). Jev is their flagship **System One model** — not a chat model.

- **Paradigm:** Kahneman's System 1 — fast, cheap, automatic judgments — engineered as a model class.
- **Name:** William Stanley Jevons' paradox: 10× cheaper intelligence ⇒ more demand. Pricing is the pitch.
- **Output contract:** *unstructured state in, typed probabilistic decisions out.* No prose, no parsing.
- **Training:** RLCD (Reinforcement Learning for Calibrated Decisions) — optimizes for probabilities
  matching outcomes, i.e. P(predicted=outcome | confidence=c) → c over many predictions.

## 2. The primitives (exact, per docs)

| Primitive | Question fields | Answer fields | Bounds |
|---|---|---|---|
| **choice** | `type:"choice"`, `instructions`, `criteria:{option: rubric}` | `choice` (argmax), `probabilities` (sum=1), `confidence` | 2–255 options |
| **score** | `type:"score"`, `instructions`, `criteria:[L1..Lk]` ordered | `score` (may be between levels), `legend`, `probabilities`, `confidence` | 2–10 levels |
| **noul** | `type:"noul"`, `instructions`, `criteria:{true,false}` optional | `noul` (0…1 prob of yes) | — |

- **Batching:** every question in one request evaluates the same state **in parallel**; latency scales
  with the longest question, not the count. Fan-out is the default pattern, not an optimization.
- **Independence:** answers never leak into each other; no context-rot from added questions.
- **`confidence`** is derived from the distribution peakedness; **noul has no separate confidence**.
- `instructions` may be an object/array to attach data, referenced in backticks (e.g.
  `` `ticket.messages[0].text` ``). Question ids are code-only, not sent to the model.

## 3. HTTP contract (pin `jev-1.13.0`; log echoed `model`)

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <key>
{ "model": "jev-latest", "state": <str|obj|arr>, "questions": { "<id>": {…primitive…} } }
```
```
{ "model": "jev-1.13.0",
  "answers": { "<id>": { … }, … },
  "usage": { "input_tokens": N, "output_tokens": N } }
```
Errors: `401` key · `422` body validation · `429` rate · `529` overload (exponential backoff; SDKs do it).

Models: `jev-latest` (alias, stable), `jev-preview` (cutting), `jev-1.13.0` (pin). Pricing $0.042/M in,
$0/M out. Limits ~1200 req/min, 250k tok/s, 32k max for state+longest question in 64k window (dynamic
during early access). No image/audio/video input; English strongest.

**Client:** `pip install typesafe-sdk` (`TYPESAFE_API_KEY`), `from typesafe_sdk import Choice, Noul,
Score, TypeSafeClient`. Point the same SDK at OpenRouter by changing `base_url` (see FREE_PROVIDERS).

## 4. Channels (no official key required)

1. **Cloudflare Workers AI** — `POST /client/v4/accounts/{ACT}/ai/run` with `{"model":"typesafe/jev",
   "input":{state,questions}}`; token `Authorization: Bearer`. Free tier (10k neurons/day), 32k ctx.
2. **OpenRouter** — `POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`; or
   `https://openrouter.ai/api/v1/systemone` for TypeSafe-SDK compatibility. Prepaid credits; identical
   `answers`, extra `id`/`provider`/`usage.cost`.
3. **Vercel AI Gateway** `typesafe-ai/jev` (AI SDK 7 `experimental_evaluate`); **Netlify AI Gateway**.
4. Agent skill: `claude plugin marketplace add typesafe-ai/skills` / `npx skills add typesafe-ai/skills
   --skill typesafe-ai` — adapted into `.agents/skills/jev`.

## 5. Patterns we rely on (docs cookbooks)

- **Speculative fan-out / parallel questions** — 13 questions ≈ same latency/state, ~12× cheaper than
  13 calls. *Batch everything.*
- **Confidence-gated routing** — answer tells *what*, confidence tells *whether to act*; thresholds
  scale with risk. *Thresholds are yours, tuned per workflow, in code.*
- **Composite scoring** — split complex judgments into atomic scores, weight in code.
- **Intent routing** — Jev in front of a workflow routes to deterministic / specialist LLM / human.
- **llm_guardrails** — in/out screens: "is this a jailbreak?" noul + severity score ⇒ pass/review/block.
- **skill_suggestion** — rank N skills in one call (≤255 options), re-judge top-3 with real text.
- **node tool-call verification/gallery classification / citation_check / RAG passage scoring**
  (`classifying_rag_passages`) — passive checks you're using today in `.agents`.

## 6. Benchmarks & calibration (verification status)

| Model | Acc (TypeSafe eval) | $/case | Latency |
|---|---|---|---|
| **Jev** | 67.8% | 0.0004 | 0.4 s |
| GPT-5.6 Terra | 67.9% | 0.0304 | 10.1 s |
| Claude Sonnet 5 | 67.8% | 0.117 | 78 s |
| Claude Opus 5 | 73.1% | 0.1761 | 37.8 s |

- Independent evidence is thin but positive: Malte Ubl ~6× faster classifier saturation vs Gemini Flash
  Lite; @VorpalHex GPT-5nano→Jev LLM-injection defense "scores better + hundreds × faster".
- **Skeptic watch:** a "90% confident" figure may be a model-written token, not a proven calibration
  curve — independent calibration studies are still pending. Hence: *tune thresholds on your own labels,
  keep confirm gates.*
- Comm scored worst at high state size/over-32-question/gateway-platform edges; response `model` field
  is the source of truth for what actually answered.

## 7. Jaggedness summary (see JAGGEDNESS.md)

Literal reading, no math/counting/date arithmetic, indirection hurts, large-state context rot,
adversarial-state sensitivity, noul/negation not interchangeable, no generation. *Keep arithmetic, dates,
thresholds, and abstention logic in code.*

## 8. Alternative decision models (open, for offline)

- **Laya** (Apache-2.0, ModernBERT-large ~421M) — CPU 193–464 ms, ECE 0.081; best local *System-One-ish*
  option for this machine. Optional integration path in decision_layer (not default).
- **poorjev** (NLI + conformal abstention) — small, robust abstention.
- litjev / openjev-sglang / Decider / OpenDecision / OpenJevPro — need GPU or heavier; not for this box.
- Don't chase parity: Jev's win is cost/latency at similar accuracy, not exceeding frontier LLMs.

## 9. Verification links

docs.typesafe.ai (llms.txt, api.md, primitives.md, confidence.md, model-jaggedness/jev-1.13.md, sdk) ·
typesafe.ai/blog/introducing-system-one-models-and-jev · github.com/typesafe-ai/skills ·
github.com/browser-use/jev-ultrafast · jevaiguide.com (Cloudflare/OpenRouter channel details) ·
`C:\Users\USER\.agents\knowledge\jev-ai-model-research.md`.