"""Resolve plan-debate slots against LIVE provider catalogs at call time.

Problem (2026-09-24): PLAN_DEBATE_MERGER defaulted to a hardcoded
'minimax/minimax-m3:free' that OpenRouter delisted — the debate's
convergence gate silently starved and plan mode produced nothing.

Design (per Elijah's framework-agnostic-fix rule, 2026-09-12): no new
hardcoded pins. Defaults are *preferences*; at debate time each slot is
validated against the live catalog and, if delisted, replaced by the
best currently-free instruction-follower. Env overrides set in
master_ai.py are honored as the first preference, not as a pin — a
delisted override gets substituted too, with a loud note.
"""

import json
import os
import time
import urllib.request
from pathlib import Path

# Preference ladders: free, mid-size, instruction-following (not
# reasoning monologuers), on OpenRouter. Any entry live in the catalog
# wins; the list is a preference, not a pin.
_MERGER_PREFS = (
    "minimax/minimax-m3:free",  # historical default, usually delisted
    "qwen/qwen3.8-27b:free",
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free",
)
_PLANNER_B_PREFS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nex-agi/nex-n2.5-pro:free",
    "qwen/qwen3.8-27b:free",
    "inclusionai/ling-3.0-flash-fin:free",
)

_CACHE = {"ts": 0.0, "ids": set(), "free": []}
_TTL = 3600


def _catalog():
    """(ids:set, free:list) from OpenRouter live, else Sensei's disk cache,
    else (None, None) meaning 'cannot validate — degrade, don't invent'."""
    if time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["ids"], _CACHE["free"]
    ids, free = set(), []
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "master-ai/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            ids.add(mid)
            if str(m.get("pricing", {}).get("prompt", "")) == "0":
                free.append(mid)
    except Exception:
        try:
            p = Path.home() / ".master_ai_openrouter_models_cache.json"
            d = json.loads(p.read_text())
            for m in d.get("models", []):
                if m.get("id"):
                    ids.add(m["id"])
                    if m.get("free"):
                        free.append(m["id"])
        except Exception:
            return None, None
    _CACHE.update(ids=ids, free=free, ts=time.time())
    return ids, free


def _resolve_slot(slug, prefs, role):
    ids, free = _catalog()
    if ids is None:
        return slug  # catalog unreachable: degrade gracefully
    if slug and slug in ids:
        return slug
    # candidates: operator env override first, then preference ladders
    env_key = f"PLAN_DEBATE_{role.upper()}_PREFS"
    extra = [s.strip() for s in os.environ.get(env_key, "").split(",") if s.strip()]
    pick = next((c for c in (slug, *extra) if c and c in ids), None)
    if not pick:
        pick = next((c for c in prefs if c in ids), None)
    if not pick:
        print(
            f"  [plan-debate] {role} '{slug}' delisted and no preferred "
            f"candidate is live — keeping it (call will fail loudly)",
            flush=True,
        )
        return slug
    if pick != slug:
        tag = "free" if pick in free else "PAID"
        print(
            f"  [plan-debate] {role} '{slug or '(empty)'} not in catalog "
            f"-> {pick} ({tag})",
            flush=True,
        )
    return pick


def resolve_debate_slots(planner_a, planner_b, merger, fallback):
    """Validate every debate slot at debate time. Only OpenRouter slugs
    are catalog-checked here; ollama-cloud/opencode lanes already
    validate against their own live catalogs in their ask_* paths."""
    out = []
    for slug, prefs, role in (
        (planner_a, _PLANNER_B_PREFS, "planner_a"),
        (planner_b, _PLANNER_B_PREFS, "planner_b"),
        (merger, _MERGER_PREFS, "merger"),
        (fallback, _MERGER_PREFS, "fallback"),
    ):
        if isinstance(slug, str) and slug.startswith(("ollama-cloud::", "opencode::")):
            out.append(slug)
            continue
        out.append(_resolve_slot(slug, prefs, role))
    return tuple(out)
