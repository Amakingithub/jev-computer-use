# COMPUTER_USE_ARCH — a CPU-first GUI agent for this machine

> Goal: an honest, cheap, safe computer-use loop runnable on an i7-7500U / 16 GB / GTX 950M laptop.
> Everything below is scoped to "minimal working prototype" per the approved plan (2026-09-21).

## Why this shape (and why Jev can't do it alone)

Jev has **no image input and no text generation**. Feeding it a screenshot is impossible; asking it to
write a pyautogui script is pointless. What it *is* unbeatable at inside a GUI loop is the bounded
step decision:

```
capture → parse → decide (Jev) → act → verify → escalate
```

- If a single Jev decision ≈ 100 ms / ~$0.0004 and the alternatives (7B VLM / frontier LLM) cost
  10–100× more per step, the cheap decision layer pays for the whole agent. This is exactly the
  `browser-use/jev-ultrafast` architecture (DOM snapshot → one Jev call picks op+element → only
  `TYPE_TEXT` touches an LLM), which booked a real flight in 7.1 s / $0.0039.

## The loop

### 1. Capture — `screen.py`
- `mss` full-screen or region grab → PIL RGB.
- `dhash(image, size=8)` → 64-bit perceptual hash; `hamming(prev, cur)` → screen-change score.
- Used for both state input and **change verification**.

### 2. Parse — `parse_ui.py`
- **RapidOCR** (ONNX CPU, ~0.2 s) → elements `{id, text, box(x1,y1,x2,y2), center, conf}`.
- Render an **element inventory** consumed by Jev:
  ```
  1. "Open" @ (230,180)
  2. "File" @ (120,90)
  ...
  ```
- Cap ~40 elements (Jev Choice ≤255). Because OCR gives the *boxes*, Jev and any downstream VLM only
  ever reference element **ids** — no coordinate hallucination, no pixel access needed.

### 3. Decide — `decide.py` + `questions_computer_use.py`
One batched Jev call per step (speculative fan-out):
| question | type | purpose |
|---|---|---|
| `which_element` | choice over inventory ids | pick the UI target |
| `next_action` | choice over goal action set | click / type / press / scroll / done / blocked / escalate |
| `safe_to_proceed` | noul | risk gate (abstain if < `SAFE_NOUL`) |
| `step_risk` | score (1–4) | severity signal for confirm/block |

Thresholds are **code constants in ONE file**: `SAFE_NOUL = 0.7`, `ACTION_CONF = 0.6`, `MAX_STEPS`,
`STUCK_DHASH = 2` (bits of change), `STUCK_TRIES = 3`. Tune on your own logged runs.

### 4. Act — `act.py`
- pyautogui (`pyautogui.FAILSAFE = True` — nudge mouse to a corner to abort), target = OCR box center.
- `click`, `type_text`, `press`, `hotkey`, `scroll`. Win32/UIA accessibility handled later as an
  optional richer source; OCR is the coordinate source of truth.

### 5. Verify — `run_agent.py`
- Recapture after each act; `hamming(dhash_before, dhash_after)`.
- No change → same action retried; after `STUCK_TRIES` → `blocked`/recover; step log emitted as JSON.
- Approval profiles: `none` (demo/dev only), `confirm` (prompt per step — default for everything that
  isn't explicitly sandboxed), curated-allowlist (future).

### 6. Escalate
When any gate fails — low Jev confidence, no safe action, or the step is open-ended ("write the email
body") — hand the item + element inventory to: a **free cloud VLM** (Gemini free tier / Groq /
Cloudflare `qwen3.8-27b-VL`) or the offline **`local-vision`** skill (Qwen3.5-0.8B, llama.cpp
localhost:8081). Jev stays the router; the VLM only does what needs vision or prose.

## Safety model (mirrors `E:\SYSTEM_POLICY.md` + `.agents` staged approval)

- Dry-run before any real run; scripts target a scratch window (Notepad/sandbox) first.
- `confirm` required for destructive/sensitive flows (delete, git push, pay, mail, installs).
- Jev noul risk gate before high-risk actions; confidence-gated confirm.
- dHash proof per step; max-steps cap; full step log (`logs/`, gitignored).
- Calibrated ≠ correct — keep the human in the loop until agreement is measured on real workloads.

## Hardware-fit matrix

| Component | Choice here | Why |
|---|---|---|
| OCR | RapidOCR (ONNX CPU) | ~0.2 s/frame; Apache-2.0; no GPU needed |
| Object/icon detect (later) | OmniParser (YOLOv9-E) or GUI-Owl-1.5 | needs ONNX/DirectML; OmniParser weights AGPL → personal use only |
| Decision | Jev (cascade) | ~0.1 s, ~$0.0004/step, calibrated |
| Open-ended semantics | free cloud VLM / `local-vision` | no local 7B possible (2 GB VRAM, CPU-only) |

## Reference implementations

- jev-ultrafast — https://github.com/browser-use/jev-ultrafast (the op/decision pattern we reuse)
- Enikk — https://github.com/gtt116/enikk (Windows YOLO+OCR+VLM, cost-minimizing parsing; best base)
- Auto-Use — https://github.com/auto-use/auto-use (hybrid UIA+vision, Groq/OpenRouter wiring)
- OmniParser — https://github.com/microsoft/OmniParser
- Navigator- (dHash verify + safety gate) — github.com/Satoshi88818/Navigator-