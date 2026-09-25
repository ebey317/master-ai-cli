#!/usr/bin/env python3
"""Tests for the model-aware context watermark (2026-09-25).

Elijah's ask: "i want it to be 95% of the model's real window." The old
code measured every model against one hardcoded 120,000-char constant
(bumped 60k→120k on 2026-04-19 as a local-ollama freeze band-aid), so a
1M-context model threw away ~7/8 of a window it had paid for.

Run: /usr/bin/python3 test_context_watermark.py
"""

import sys

sys.path.insert(0, "/home/elijah/master-ai-cli")
import master_ai as m

FAILS = []


def check(name, cond, detail=""):  # noqa: ANN001
    detail = str(detail)
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


def with_model(name):
    m.PINNED_MODEL = name


# ── 1. The floor and ratio constants are what Elijah asked for ──────
check("fill ratio is 95%", m.CONTEXT_FILL_RATIO == 0.95, m.CONTEXT_FILL_RATIO)
check(
    "floor keeps the April-2026 freeze guard",
    m.CONTEXT_WATERMARK_FLOOR >= 60000,
    m.CONTEXT_WATERMARK_FLOOR,
)

# ── 2. Real OpenRouter windows resolve, free vs paid kept distinct ──
cases = [
    # (model, expected_tokens) — :free must NOT borrow the paid window
    ("nvidia/nemotron-3-ultra-550b-a55b:free", 1000000),
    ("grok-4.20", 2000000),
    ("openrouter/auto", 2000000),
]
for model, want in cases:
    with_model(model)
    toks, src = m._active_model_context_tokens()
    check(
        f"{model} -> {want:,} tokens",
        toks == want,
        f"got {toks} via {src}",
    )

# ── 3. The budget is 95% of window, converted to chars ──────────────
with_model("nvidia/nemotron-3-ultra-550b-a55b:free")
chars, toks, _ = m._context_watermark()
want = int(1000000 * 0.95 * m.CHARS_PER_TOKEN)
check("95% of 1M window in chars", chars == want, f"got {chars:,} want {want:,}")

# ── 4. No model is measured against the retired constant ────────────
#     (the old bug: 400k chars = 333% of 120k on every model)
probe_chars = 400000
pcts = {}
for model in (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "grok-4.20",
    "qwen2.5vl:3b",
):
    with_model(model)
    wm, _, _ = m._context_watermark()
    pcts[model] = round(100 * probe_chars / wm)
check(
    "a 1M model is not over 100% at 400k chars",
    pcts["nvidia/nemotron-3-ultra-550b-a55b:free"] < 100,
    pcts,
)
check(
    "windows produce different budgets (not one constant)",
    len(set(pcts.values())) >= 2,
    pcts,
)

# ── 5. Small-window models still get floored, never zero ────────────
with_model("perceptron/perceptron-mk1.5")
chars, toks, _ = m._context_watermark()
check(
    "36k-token model stays above the floor",
    chars >= m.CONTEXT_WATERMARK_FLOOR,
    f"{chars:,}",
)

# ── 6. Unknown model degrades to the legacy constant, never crashes ─
with_model("definitely-not-a-real-model-xyz")
chars, toks, src = m._context_watermark()
check("unknown model falls back safely", chars == m.CONTEXT_WATERMARK, src)
check("unknown model reports no tokens", toks is None, toks)

# ── 7. Empty/unset model doesn't explode ───────────────────────────
with_model(None)
try:
    chars, toks, src = m._context_watermark()
    check("unset model resolves", isinstance(chars, int) and chars > 0, (chars, src))
except Exception as e:  # noqa: BLE001
    check("unset model resolves", False, f"{type(e).__name__}: {e}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} -> {FAILS}")
    sys.exit(1)
print("OK — all watermark tests passed")
