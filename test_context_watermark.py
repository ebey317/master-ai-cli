#!/usr/bin/env python3
"""Tests for the model-aware context watermark (2026-09-25).

Elijah's ask: "i want it to be 95% of the model's real window." The old
code measured every model against one hardcoded 120,000-char constant
(bumped 60k->120k on 2026-04-19 as a local-ollama freeze band-aid), so a
1M-context model threw away ~7/8 of a window it had paid for.

Run standalone:  /usr/bin/python3 test_context_watermark.py

Converted to a real pytest module 2026-09-28. It used to be a bare script
whose checks ran at import and whose `sys.exit(1)` aborted pytest's whole
collection with INTERNALERROR -- one unavailable fixture cost the entire
841-test suite its signal. It also contributed zero tests to the suite,
because pytest collected no test functions from it.

The catalog-backed checks depend on ~/.master_ai_openrouter_models_cache.json,
which master_ai.py deliberately deletes at every startup and refetches in the
background. That makes them environment-dependent by nature, so they now SKIP
with a stated reason when the catalog cannot answer, instead of failing and
taking the run with them. The catalog-independent checks always run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import master_ai as m  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_pinned_model():
    """PINNED_MODEL is module global; never leak it between tests."""
    saved = m.PINNED_MODEL
    yield
    m.PINNED_MODEL = saved


def with_model(name):
    m.PINNED_MODEL = name


def _tokens_for(model):
    """(tokens, source) as the watermark resolver sees it, or (None, src)."""
    with_model(model)
    return m._active_model_context_tokens()


# ── 1. The floor and ratio constants are what Elijah asked for ──────


def test_fill_ratio_is_95_percent():
    assert m.CONTEXT_FILL_RATIO == 0.95


def test_floor_keeps_the_april_2026_freeze_guard():
    assert m.CONTEXT_WATERMARK_FLOOR >= 60000


# ── 2. Real OpenRouter windows resolve, free vs paid kept distinct ──
# Catalog-backed: skips rather than fails when the cache is unavailable.


def _catalog_loaded():
    """Skip helper for the catalog-backed expectations.

    The premise of these checks is "the OpenRouter catalog is loaded and
    reports this window". Gate on that premise directly rather than on the
    model resolving: a model absent from a stale catalog still resolves,
    just to a static fallback value, which would read as a real failure.
    """
    cache = m._OPENROUTER_MODELS_CACHE
    if not cache.exists():
        pytest.skip(
            f"{cache} is absent; master_ai.py clears it at startup and "
            "refetches in the background, so these expectations cannot hold"
        )
    try:
        payload = json.loads(cache.read_text())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{cache} is unreadable: {exc}")
    items = payload if isinstance(payload, list) else payload.get("models", [])
    if not items:
        pytest.skip(f"{cache} is empty")
    return True


@pytest.mark.parametrize(
    "model,expected",
    [
        # :free must NOT borrow the paid window
        ("nvidia/nemotron-3-ultra-550b-a55b:free", 1000000),
        ("grok-4.20", 2000000),
        ("openrouter/auto", 2000000),
    ],
)
def test_real_windows_resolve_to_expected_tokens(model, expected):
    _catalog_loaded()
    toks, src = _tokens_for(model)
    assert toks == expected, f"got {toks} via {src}"


# ── 3. The budget is 95% of window, converted to chars ──────────────


def test_budget_is_95_percent_of_window_in_chars():
    model = "nvidia/nemotron-3-ultra-550b-a55b:free"
    _catalog_loaded()
    with_model(model)
    chars, _toks, _ = m._context_watermark()
    want = int(1000000 * 0.95 * m.CHARS_PER_TOKEN)
    assert chars == want, f"got {chars:,} want {want:,}"


# ── 4. No model is measured against the retired constant ────────────
#     (the old bug: 400k chars = 333% of 120k on every model)
PROBE_CHARS = 400000


def _pct_at_probe(model):
    with_model(model)
    wm, _, _ = m._context_watermark()
    return round(100 * PROBE_CHARS / wm)


def test_a_1m_model_is_not_over_100_percent_at_400k_chars():
    model = "nvidia/nemotron-3-ultra-550b-a55b:free"
    _catalog_loaded()
    assert _pct_at_probe(model) < 100


def test_windows_produce_different_budgets_not_one_constant():
    pcts = {
        "qwen2.5vl:3b": _pct_at_probe("qwen2.5vl:3b"),
        "perceptron/perceptron-mk1.5": _pct_at_probe("perceptron/perceptron-mk1.5"),
    }
    assert len(set(pcts.values())) >= 2, pcts


# ── 5. Small-window models still get floored, never zero ────────────


def test_small_window_model_stays_above_the_floor():
    with_model("perceptron/perceptron-mk1.5")
    chars, _toks, _ = m._context_watermark()
    assert chars >= m.CONTEXT_WATERMARK_FLOOR, f"{chars:,}"


# ── 6. Unknown model degrades to the floor, never crashes ───────────


def test_unknown_model_falls_back_to_floor():
    with_model("definitely-not-a-real-model-xyz")
    chars, _toks, _src = m._context_watermark()
    assert chars == m.CONTEXT_WATERMARK_FLOOR


def test_unknown_model_reports_no_tokens():
    with_model("definitely-not-a-real-model-xyz")
    _chars, toks, _src = m._context_watermark()
    assert toks is None


# ── 7. Empty/unset model doesn't explode ───────────────────────────


def test_unset_model_resolves():
    with_model(None)
    chars, _toks, _src = m._context_watermark()
    assert isinstance(chars, int) and chars > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
