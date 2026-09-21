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

### 2. Parse — `parse_ui.py` (multi-provider OCR layer, 2026-09-22)
- **RapidOCR** (ONNX CPU, ~0.2 s) → elements `{id, text, box(x1,y1,x2,y2), center, conf, source}`.
- Render an **element inventory** consumed by Jev:
  ```
  1. "Open" @ (230,180)
  2. "File" @ (120,90)
  ...
  ```
- Cap ~40 elements (Jev Choice ≤255). Because OCR gives the *boxes*, Jev and any downstream VLM only
  ever reference element **ids** — no coordinate hallucination, no pixel access needed.

The a11y provider is the **text-free ground truth**: it reads the OS accessibility tree, not pixels,
so it sees actionable controls (buttons, edits, list items) with their accessible Name and absolute
rect exactly as the screen reader would. It closes the biggest blind spot of a pixel-only-first loop —
icon-only controls whose glyphs no OCR engine can decode. Verified on this machine: Calculator's
blue `+`/`−`/`=` keys come through as `Plus`/`Minus`/`Equals` at their real screen coordinates
(~1.0 s with node/depth/time caps).

**Providers** (`move parse_ui.parse(..., provider=...)`):
| provider | engine | cost / notes |
|---|---|---|
| `rapid` (default) | RapidOCR ONNX | ~0.2 s, free, local |
| `windows` | WinRT `Windows.Media.Ocr` | **free, local, zero-download, zero API key**; reads glyph tokens ONNX misses (`+`, `-`, `=`, CJK). Needs `pip install winrt-Windows.Media.Ocr winrt-Windows.Globalization winrt-Windows.Graphics.Imaging winrt-Windows.Storage winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-runtime` (all 3.2.1) — packaged as the `ocr-windows` extra (`uv pip install -e ".[ocr-windows]"`). Ships en-US + fr-FR recognizers on this machine. |
| `a11y` | Windows UIAutomation tree (comtypes) | **0 vision tokens**: actionable control Name + absolute rect straight from the OS. Catches icon-only buttons OCR cannot see at all (Calculator's `+`/`=` read as `Plus`/`Equals`). Bounded by node/depth/time caps (~1 s). `uv pip install -e ".[uia11y]"`. Boxes are absolute screen coords — must pass `origin=(x1,y1)` for sub-region captures. |
| `glm` | `glmocr` (PaddleOCR, optional) | heavier; import-guarded so absence never breaks the role back |
| `merge` | rapid + windows + **a11y gap-fill** | gap-fill by text/IoU overlap through all three free tiers; operator keys end up on the inventory |
| `auto` | cascade rapid → windows → glm → a11y | first provider that yields ≥1 element wins |

Every provider degrades to a graceful `OcrError` message that names the missing engine and the exact
install command; the loop never hard-crashes on a missing OCR tier.

### 3. Decide — `decide.py` + `questions_computer_use.py`
One batched Jev call per step (speculative fan-out):
| question | type | purpose |
|---|---|---|
| `which_element` | choice over inventory ids | pick the UI target |
| `next_action` | choice over goal action set | click / type / press / scroll / done / blocked / escalate |
| `safe_to_proceed` | noul | risk gate (abstain if < `SAFE_NOUL`) |
| `step_risk` | score (1–4) | severity signal for confirm/block |

Thresholds are **code constants in ONE file**: `SAFE_NOUL = 0.60` (lowered from 0.70 after calibration:
benign type_text into a focused empty Notepad measured noul 0.59-0.66), `ACTION_CONF = 0.6`,
`STUCK_DHASH = 4` (bits of change, 16x16 = 256-bit dHash; raised from 2/8x8 which missed subtle
typed edits scoring 0-2), `STUCK_TRIES = 3`. Tune on your own logged runs.

### 4. Act - `act.py`
- pyautogui (`pyautogui.FAILSAFE = True` - nudge mouse to a corner to abort), target = OCR box center.
- `click`, `type_text` (interval 0.05 s - 0 drops chars), `press`, `hotkey` (ctrl+s-style combos),
  `scroll`. Win32/UIA accessibility handled later as an optional richer source; OCR is the coordinate
  source of truth.
- `element_id=None` / `none`/`nonew` target defaults to a viewport-center click (no more
  `ValueError: click_element requires an element center`).

### 4b. Win11 Save As dialog - validated recipe (2026-09-21)
- **The "File name" box takes a NAME only.** Typing a full path (`E:\tmp\n.txt`) triggers
  "file name is not valid because of the special characters" - backslashes are illegal in a filename.
  Content (name text, keystrokes) is delivered out-of-band via `--input-text` / `--press-key` pipes,
  never generated by Jev.
- To steer the dialog to a folder deterministically (OCR-driven clicks):
  1. after Ctrl+S, click the drive node in the left tree (e.g. "Nouveau nom (E:)") - breadcrumb confirms
  2. click inside the file list, type the folder name (`tmp`) to filter
  3. double-click the row (breadcrumb now shows `>E:> tmp >`)
  4. click the File name field, Ctrl+A, type the name (`n.txt`) - NO path
  5. click the real **Save** button. Careful: OCR also reports "Save as" (the dialog title, y~20);
     filter by exact text `Save` with y > 500.
- Win11 Notepad session-restores every previously-open unsaved window on *any* new launch - a fresh
  `Start-Process notepad` picks up the last session (title may be `*Hello - Notepad`, not
  `*Untitled*`). Target by newest unsaved window (exclude the user's real documents by pid), or open a
  clean blank window with Ctrl+Shift+N and pick the newest `Untitled`.
- Verification: the dialog remembers its last folder per app (ComDlg32), so the file may land in an
  arbitrary recent folder. Ground truth = the shell Recent link
  `$env:APPDATA\Microsoft\Windows\Recent\n.txt.lnk` (TargetPath resolve).

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
| OCR (cheap tier) | RapidOCR (ONNX CPU) | ~0.2 s/frame; Apache-2.0; no GPU needed |
| OCR (cheap glyph tier) | WinRT `Windows.Media.Ocr` (`--ocr windows`/`merge`) | free + local + no API key; catches `+ − =` operator glyphs rapid misses |
| Text-free ground truth | UIAutomation tree (`--ocr a11y`/`merge`) | 0 vision tokens; actionable Name + rect from the OS; reads icon-only buttons OCR can't see (Calculator `Plus`/`Equals`) |
| OCR (escalation) | `glmocr` optional | heavier local PaddleOCR; escalate-only |
| Object/icon detect (later) | OmniParser (YOLOv9-E) or GUI-Owl-1.5 | needs ONNX/DirectML; OmniParser weights AGPL → personal use only |
| Object/icon detect (later) | OmniParser (YOLOv9-E) or GUI-Owl-1.5 | needs ONNX/DirectML; OmniParser weights AGPL → personal use only |
| Decision | Jev (cascade) | ~0.1 s, ~$0.0004/step, calibrated |
| Open-ended semantics | free cloud VLM / `local-vision` | no local 7B possible (2 GB VRAM, CPU-only) |

## Reference implementations

- jev-ultrafast — https://github.com/browser-use/jev-ultrafast (the op/decision pattern we reuse)
- Enikk — https://github.com/gtt116/enikk (Windows YOLO+OCR+VLM, cost-minimizing parsing; best base)
- Auto-Use — https://github.com/auto-use/auto-use (hybrid UIA+vision, Groq/OpenRouter wiring)
- OmniParser — https://github.com/microsoft/OmniParser
- Navigator- (dHash verify + safety gate) — github.com/Satoshi88818/Navigator-