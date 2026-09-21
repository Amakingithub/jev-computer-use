# JAGGEDNESS — jev-1.13 known edges + how we compensate

> Source of record: `docs.typesafe.ai/model-jaggedness/jev-1.13.md`. This is *how the model falls short*,
> and — importantly — *how this repo keeps it from mattering*. Read before tuning thresholds.

## 1. The jagged edges (official, condensed)

1. **Literal reading** — Jev answers the question as written, not as intended. Negations, scoping, and
   idioms get read at face value. → Write literal, unambiguous instructions.
2. **No math / no counting / no date arithmetic** — not a calculator; can't count reliably; score levels
   are weak for exact interpolation. → All arithmetic, counting, date logic stays **in code**.
3. **No date/time comparison** — dates read as text. → Use a Choice to extract parts, compare in code.
4. **Indirection hurts** — double negatives, multi-hop reasoning, ambiguous phrasing cost accuracy.
   → Atomic, single-hop questions; decompose judgments instead of asking one big one.
5. **Large state = context rot** — unrelated detail distracts. → Send only the state each question
   needs; the SDK/DAG keeps this explicit (see `docs/COMPUTER_USE_ARCH.md` inventory sizing).
6. **Adversarial state** — Jev does not treat state as hostile by default; prompt-injection *inside
   state* can shift answers. → Guardrail questions treat untrusted state (web, chat, files) as hostile;
   layout the real gate outside (noul + severity + code allowance).
7. **Contradictory instructions/criteria confuse it** — align phrasing across questions.
8. **No structural invariants** — `noul(a)` vs `1−noul(a)` are unrelated; don't reuse thresholds across
   question types. → One threshold per question in the constants module; compare same-type answers only.
9. **No generation** — not trained to write text; chaining choices as "generation" works poorly.
   → Any prose/planning goes to the LLM/VLM path (see COMPUTER_USE_ARCH escalation).

## 2. Confidence is not guaranteed correctness

Calibration claims (P(predicted|conf=c)→c) are **model-learnt, not independently verified yet** (see
JEV_DOSSIER §6 skeptic watch). Compensations:

- `confidence` gates *whether to act*, never *what the answer is* (keep thresholds in code constants).
- `SAFE_NOUL` / `ACTION_CONF` defaults are conservative; tune downward only after measuring agreement
  on your own logs.
- Confidence is derived from the answer's probability distribution — it is a routing signal, not a
  ground-truth probability.

## 3. Version drift

`jev-latest` moves weekly and is the SDK default. In production:
- Pin `jev-1.13.0` in `decision_layer`; **always log the echoed `model`** (response field) — it tells
  you which version actually answered through gateways (OpenRouter returns dated ids like
  `typesafe/jev-1.13-20260917`).
- Re-run a small golden test set on each bump before promoting a new default.

## 4. Where Jev must NEVER be used here (from AGENTS.md decision policy)

Deterministic logic (download-mgr routing, date parsing, dedup fingerprints, annotation mapping, file
ops), exact math, free-choice generation, and anything GIGO-sensitive where a silent 5% error is fatal
without an outer gate. When in doubt, run it through the confidence gate with a human confirm.