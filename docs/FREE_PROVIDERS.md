# FREE_PROVIDERS — the zero-to-near-zero decision/VLM stack (churn-aware)

> Rule of this era: providers and free models die fast (GitHub Models retired 2026-07-30; Groq dropped
> Llama-3.3 the same month). Keep ≥2 channels configured and run a monthly churn check (the
> `last30days` skill does this). Everything below is verified as live at 2026-09-21.

## 1. Jev channels (decision layer)

| Channel | Endpoint / config | Cost | Notes |
|---|---|---|---|
| **TypeSafe official** | `POST api.typesafe.ai/v1/systemone` | $0.042/M in, $0 out | ✅ *verified live 2026-09-21* (key in `api-providers.md`); model `jev-1.13.0`, ~1.4–4.2 s/decision, `usage.input_tokens` returned |
| **Cloudflare Workers AI** | `POST /client/v4/accounts/{ACT}/ai/run` `{model:"typesafe/jev", input:{state,questions}}`; Bearer token | free tier ~10k neurons/day | ⚠️ *on THIS account returns `HTTP 402 Insufficient balance; add money to your gateway or use BYOK`* — channel is tried then skipped by the cascade; needs prepaid/BYOK to activate |
| **OpenRouter** | `POST openrouter.ai/api/alpha/decisions` model `typesafe/jev-1.13` (or `…/api/v1/systemone` for SDK compat) | $0.042/M; **needs prepaid credits** | ✅ *verified live 2026-09-21* (~$1.28e-5/decision, ~1.2–1.8 s); 3 keys in `api-providers.md`, first usable wins |
| Vercel AI Gateway | model `typesafe-ai/jev` (AI SDK `experimental_evaluate`) | billed | if Vercel is in the stack |
| Netlify AI Gateway | model `typesafe-ai/jev` | billed | — |
| Vercel AI Gateway | model `typesafe-ai/jev` (AI SDK `experimental_evaluate`) | billed | if Vercel is in the stack |
| Netlify AI Gateway | model `typesafe-ai/jev` | billed | — |

## 2. Open/offline Jev-likes (fall back in `decision_layer`)

| Name | Model | Runs on this machine? | Notes |
|---|---|---|---|
| **Laya** | ModernBERT-large (~421M) | ✅ CPU 200–460 ms | Apache-2.0, ECE 0.081; best local System-One-ish story |
| **poorjev** | NLI + conformal abstention | ✅ CPU | principles only; small, monkeypatchable |
| litjev / openjev-sglang / Decider / OpenJevPro / OpenDecision | Gemma-3-4B variants etc. | ❌ need GPU/VRAM | not for a 2 GB GTX 950M |

Integration note: Laya is opt-in in `decision_layer.py` (`LayaProvider`), not default.

## 3. Free LLM / VLM tiers that back the computer-use loop

| Provider | Free tier | Use for | Key present? |
|---|---|---|---|
| Cloudflare Workers AI | 10k neurons/day; 50+ models, `qwen3.8-27b-VL` (vision) | VLM grounding, text fast path | ✅ but Jev model 402s on this account until prepaid/BYOK |
| Groq | 1000 req/day; GPT-OSS 120B, Qwen3 32B | fast text classifier/planner | ✅ (2 keys) |
| Google AI Studio (Gemini) | free tier (3.x Flash, vision) | VLM screens, grounding | ✅ |
| OpenRouter | ~20 `:free` models, 50/day | VLM/LLM escape hatch | ✅ (3 keys) |
| Mistral | $10/mo | LLM | ✅ |
| Cerebras | free tier | fast text | ✅ |
| Cohere | 1000 calls/mo | `classify`/`rerank` complements | ✅ |

`local-vision` (offline): Qwen3.5-0.8B via llama.cpp at `localhost:8081` — last-resort vision, no internet.

## 4. Cost model sanity

- Jev: $0.042/M input = **1M × 500-token decisions ≈ $21**. A GUI agent at 10 decisions/s all day is
  pocket change; the LLM/VLM paths (free tiers) dominate only when escalating.
- Cloudflare neurons vs TypeSafe token pricing differ; for low volume the free tier easily covers the
  prototype (but see the 402 caveat above — this account needs prepaid/BYOK first).
  Track usage via responses' `usage.input_tokens`/`usage.cost`.

## 5. Churn watchlist (re-check monthly)

- TypeSafe model version drift (`jev-latest` moves weekly) — pin `jev-1.13.0` in prod.
- OpenRouter KPI/credit requirements; `:free` roster rotates.
- Cloudflare free-tier neuron allocation; Workers AI pricing (unified billing for 3rd-party).
- Groq/Google/Cerebras model lineups (they've already dropped/rotated models in 2026).