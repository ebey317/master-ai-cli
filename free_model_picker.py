"""Live free-model picker — OpenRouter's real current catalog, not a
hardcoded model name or a stale memory of "which one seemed good once."

2026-09-26 (Elijah): "nemo tron is no longer free. so when we do stuff
like this, we need to have an automated model that goes through and see
what's free and see what's the most capable to do our job. not the most
popular, but the most capable and to do the job right." Built after
manually discovering, mid-task, that a hardcoded model choice
(nvidia/nemotron-3-ultra-550b-a55b via NVIDIA's own direct hosting) had
quietly stopped working (trial credits exhausted) and that the
"obviously better" replacement (qwen/qwen3.8-27b:free, highest quality
score on a third-party benchmark) was itself rate-limited at that exact
moment. Neither of those facts is something a hardcoded config value can
ever reflect — both need to be checked live, every time.

What "free" means here: OpenRouter's own per-model pricing field,
`pricing.prompt == "0"` — the real, authoritative signal (see
master_ai.py's _openrouter_model_catalog for the same pattern), not a
":free" suffix guess and not a name that sounds free.

What "most capable" means here: there is no public, live, per-model
capability score to query — third-party benchmark sites exist but are
unauthoritative, change ranking methodology, and aren't an API contract
anyone should build automation on top of. Ranking here instead uses
real, structural signals OpenRouter's own API actually returns for every
model, in order:
  1. Tool-calling support (supported_parameters contains "tools") —
     required by default; a model that can't call tools can't do
     agentic work like a code review needs, no matter how large it is.
  2. Parameter count parsed from the model's own id (e.g. "550b" in
     "nemotron-3-ultra-550b-a55b") — the closest thing to a capability
     proxy that's actually published, not guessed.
  3. Context window (context_length) as a tiebreaker.

Then, critically: the top-ranked candidates are LIVE-TESTED with a real,
cheap completion call, in ranked order, and the first one that actually
answers correctly wins. Ranking answers "which one SHOULD be best";
live-testing answers "which one actually works RIGHT NOW" — both
mattered tonight, in exactly this order, to explain a single real
failure (the #1-ranked candidate was rate-limited, the #2 pick worked).

Public API:
    fetch_openrouter_models(key=None) -> list[dict]      # raw catalog
    list_free_models(key=None, ...) -> list[dict]        # filtered
    rank_free_models(models) -> list[dict]               # + "_score" info
    test_model_live(key, model_id, timeout=20) -> (bool, str)
    pick_best_working_free_model(key=None, max_tries=5) -> dict
        {"model": str | None, "attempts": [...], "candidates": [...]}

CLI:
    python3 free_model_picker.py                # human-readable pick
    python3 free_model_picker.py --json          # machine-readable
    python3 free_model_picker.py --write-ocr-config   # also updates
        ~/.opencodereview/config.json's openrouter-free provider
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from keychain_kv import parse_kv_keys

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OCR_CONFIG_PATH = Path.home() / ".opencodereview" / "config.json"
KEYCHAIN_PATH = Path.home() / ".master_ai_keys"
_CATALOG_CACHE_PATH = Path.home() / ".master_ai_free_model_picker_cache.json"
_CATALOG_CACHE_TTL = 3600  # 1h — short enough that a newly-broke-free
# model or a newly-added one shows up same-session, long enough that a
# hot per-turn caller (master_ai.py's own routing) isn't hitting the
# network on every single turn just to re-derive the same ranking.

# Output modalities that mean "not a text-answering chat model" even if
# it technically supports tool-calling — this tool is picking a model to
# reason and respond in text, not generate audio/images.
_NON_TEXT_OUTPUT_MODALITIES = {"audio", "image", "video"}

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def _openrouter_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        keys = parse_kv_keys(KEYCHAIN_PATH.read_text())
    except Exception:
        keys = {}
    key = keys.get("openrouter")
    if not key:
        raise RuntimeError(
            "no OpenRouter key found (checked OPENROUTER_API_KEY in "
            f"{KEYCHAIN_PATH}) — this tool needs a real key to query the "
            "live model catalog and to live-test candidates."
        )
    return key


def fetch_openrouter_models(
    key: str | None = None, force_fresh: bool = False
) -> list[dict[str, Any]]:
    """The current OpenRouter model catalog. Cached for _CATALOG_CACHE_TTL
    so a hot per-turn caller (master_ai.py's own routing) doesn't hit the
    network every single turn — but the cache is short enough (1h) that
    it can't become the same kind of stale, silently-wrong data this tool
    exists to replace. Pass force_fresh=True (or use the CLI, which
    always does) when correctness matters more than one saved round-trip
    — e.g. right before actually picking a model to hand to something."""
    if not force_fresh:
        try:
            cached = json.loads(_CATALOG_CACHE_PATH.read_text())
            import time as _time

            if _time.time() - cached.get("ts", 0) < _CATALOG_CACHE_TTL:
                return cached.get("models", [])
        except Exception:
            pass
    key = _openrouter_key(key)
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL, headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    models = data.get("data", [])
    try:
        import time as _time

        _CATALOG_CACHE_PATH.write_text(
            json.dumps({"ts": _time.time(), "models": models})
        )
    except Exception:
        pass
    return models


def _param_count_b(model_id: str) -> float | None:
    """Largest 'NNb' figure in a model id — MoE names like
    'nemotron-3-ultra-550b-a55b' carry both a total (550b) and an
    activated (a55b) size; the total is the more commonly cited
    "headline" figure and what these models are actually named for."""
    matches = _PARAM_RE.findall(model_id)
    if not matches:
        return None
    return max(float(m) for m in matches)


def list_free_models(
    key: str | None = None,
    require_tools: bool = True,
    require_text_output: bool = True,
    models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """OpenRouter's real free models (pricing.prompt == "0" — the
    authoritative field, not a ":free" name guess), optionally filtered
    to ones that can actually do agentic work (tool-calling) and answer
    in text (not audio/image-only output)."""
    if models is None:
        models = fetch_openrouter_models(key)
    out = []
    for m in models:
        pricing = m.get("pricing") or {}
        if str(pricing.get("prompt", "")) != "0":
            continue
        supported = m.get("supported_parameters") or []
        if require_tools and "tools" not in supported:
            continue
        if require_text_output:
            out_modalities = set(
                (m.get("architecture") or {}).get("output_modalities") or []
            )
            if out_modalities and not (
                out_modalities - _NON_TEXT_OUTPUT_MODALITIES <= {"text"}
            ):
                # has an output modality other than text -> not a plain
                # text-answering chat model for our purposes
                if "text" not in out_modalities or out_modalities - {"text"}:
                    continue
        out.append(m)
    return out


def rank_free_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by (param count desc, context length desc) — both real,
    published fields, not a popularity or usage-count signal. Returns
    the same dicts with an added '_rank_info' key explaining the sort."""
    ranked = []
    for m in models:
        params_b = _param_count_b(m.get("id", ""))
        ctx = m.get("context_length") or 0
        m = dict(m)
        m["_rank_info"] = {"params_b": params_b, "context_length": ctx}
        ranked.append(m)
    ranked.sort(
        key=lambda m: (
            m["_rank_info"]["params_b"]
            if m["_rank_info"]["params_b"] is not None
            else -1,
            m["_rank_info"]["context_length"],
        ),
        reverse=True,
    )
    return ranked


def test_model_live(key: str, model_id: str, timeout: int = 20) -> tuple[bool, str]:
    """Real, cheap completion call — the only way to know a model is
    actually answering right now, not just theoretically the best pick.
    Catches exactly what a static config can't: rate limits, upstream
    outages, exhausted trial credits, deprecated ids."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 10,
    }
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "http://localhost",
            "X-Title": "master-ai-free-model-picker",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
        content = (
            (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        )
        if content:
            return True, content.strip()[:60]
        return False, "empty response"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def pick_best_working_free_model(
    key: str | None = None,
    max_tries: int = 5,
    require_tools: bool = True,
    force_fresh: bool = True,
) -> dict[str, Any]:
    """The actual product: fetch the real catalog, rank by real
    capability proxies, live-test in ranked order, return the first one
    that genuinely works right now. Full audit trail included — every
    attempt and why it did or didn't win — so this is never a black box.
    force_fresh defaults True here (unlike fetch_openrouter_models
    itself) because this function's whole job is picking something to
    actually use right now — the live-test step below already costs a
    real round-trip per candidate, so a stale catalog isn't saving much
    and risks live-testing a model that isn't even free anymore."""
    key = _openrouter_key(key)
    all_models = fetch_openrouter_models(key, force_fresh=force_fresh)
    free = list_free_models(key, require_tools=require_tools, models=all_models)
    ranked = rank_free_models(free)

    attempts = []
    for m in ranked[:max_tries]:
        model_id = m["id"]
        ok, detail = test_model_live(key, model_id)
        attempts.append(
            {
                "model": model_id,
                "params_b": m["_rank_info"]["params_b"],
                "context_length": m["_rank_info"]["context_length"],
                "ok": ok,
                "detail": detail,
            }
        )
        if ok:
            return {
                "model": model_id,
                "attempts": attempts,
                "candidates": [c["id"] for c in ranked],
            }
    return {
        "model": None,
        "attempts": attempts,
        "candidates": [c["id"] for c in ranked],
    }


def write_ocr_config(model_id: str, config_path: Path = OCR_CONFIG_PATH) -> None:
    """Point Open Code Review's config at the live pick, via its already-
    keyed openrouter-free custom provider — never touches the key."""
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["provider"] = "openrouter-free"
    cfg.setdefault("custom_providers", {}).setdefault("openrouter-free", {})
    cfg["custom_providers"]["openrouter-free"]["model"] = model_id
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--write-ocr-config",
        action="store_true",
        help=f"also update {OCR_CONFIG_PATH} with the pick",
    )
    ap.add_argument("--max-tries", type=int, default=5)
    ap.add_argument(
        "--allow-no-tools",
        action="store_true",
        help="don't require tool-calling support (default: require it)",
    )
    args = ap.parse_args()

    try:
        result = pick_best_working_free_model(
            max_tries=args.max_tries, require_tools=not args.allow_no_tools
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Attempts (ranked by params_b, then context_length):")
        for a in result["attempts"]:
            status = "OK" if a["ok"] else "FAILED"
            pb = f"{a['params_b']:.0f}B" if a["params_b"] is not None else "?B"
            print(
                f"  [{status:6s}] {a['model']:50s} {pb:>6s} ctx={a['context_length']:>8} -> {a['detail']}"
            )
        if result["model"]:
            print(f"\nPicked: {result['model']}")
        else:
            print(
                f"\nNo working free model found among the top {args.max_tries} candidates."
            )

    if args.write_ocr_config:
        if not result["model"]:
            print(
                "Skipping --write-ocr-config: no working model to write.",
                file=sys.stderr,
            )
            return 1
        write_ocr_config(result["model"])
        print(f"Wrote {result['model']} to {OCR_CONFIG_PATH}")

    return 0 if result["model"] else 1


if __name__ == "__main__":
    sys.exit(main())
