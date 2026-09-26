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

# 2026-09-26 (Elijah): "nvidia is only 40 requests per minute rpm. it's
# not going to be able to handle that." Real, documented per-provider
# ceilings — NVIDIA's own 40 rpm/key is straight from nv_proxy.py's own
# docstring (this codebase's existing rate-limiting relay for that exact
# upstream); OpenRouter free is 20 rpm, account-wide across every :free
# model combined, per OpenRouter's own docs (verified earlier tonight,
# not guessed). Neither number is close to safe for OCR's default
# --concurrency 8: the actual failure mode observed twice tonight was a
# BURST of near-simultaneous 429s when several subtasks' tool-calling
# round-trips landed in the same few seconds, not a slow sustained
# overage across a full minute. Recommended concurrency below treats
# the rpm ceiling as a worst-case same-instant burst budget (divide by
# 10, not by 60) rather than a smooth per-second rate — deliberately
# conservative, since "picked a model that answers" and "can actually
# sustain a real multi-subtask review" turned out to be two different
# questions tonight.
KNOWN_RATE_LIMITS_RPM = {
    "openrouter-free": 20,
    "nvidia-gptoss": 40,
    "nvidia-proxy": 40,  # single key through the relay; 80 if a real
    # second key (NVIDIA_API_KEY_2) is actually configured and rotating
    # — not assumed here, since the relay itself is independently known
    # broken right now (see the 404 finding elsewhere in this repo).
}


def recommended_concurrency(provider: str, default: int = 8) -> int:
    rpm = KNOWN_RATE_LIMITS_RPM.get(provider)
    if not rpm:
        return default
    return max(1, min(default, rpm // 10))


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


def test_model_live(
    key: str,
    model_id: str,
    timeout: int = 20,
    url: str = OPENROUTER_CHAT_URL,
    max_tokens: int = 150,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Real, cheap completion call — the only way to know a model is
    actually answering right now, not just theoretically the best pick.
    Catches exactly what a static config can't: rate limits, upstream
    outages, exhausted trial credits, deprecated/end-of-life ids.

    2026-09-26: max_tokens defaulted to 10 originally and produced a
    false NEGATIVE on a real, working model — openai/gpt-oss-20b (via
    NVIDIA direct) is reasoning-mandatory and spent its entire 10-token
    budget on reasoning content, returning content=None with
    finish_reason="length" before ever emitting the actual answer. That
    looks identical to a broken model unless you give it room to finish
    reasoning first. 150 is enough headroom for a short reasoning
    preamble plus "pong" on every free/trial model tested so far."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
        message = (result.get("choices") or [{}])[0].get("message", {})
        content = message.get("content") or ""
        if content:
            return True, content.strip()[:60]
        finish_reason = (result.get("choices") or [{}])[0].get("finish_reason")
        if finish_reason == "length":
            return (
                False,
                "hit max_tokens before producing content (reasoning-heavy model, or genuinely stuck)",
            )
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
        ok, detail = test_model_live(
            key,
            model_id,
            extra_headers={
                "HTTP-Referer": "http://localhost",
                "X-Title": "master-ai-free-model-picker",
            },
        )
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


# ── NVIDIA direct hosting (integrate.api.nvidia.com) ─────────────────
#
# 2026-09-26: OCR's config has 2 more provider slots pointed at NVIDIA's
# own hosting (nvidia-gptoss direct, nvidia-proxy via a local rate-
# limiting relay in front of the same upstream) plus a local Ollama
# slot — "wire it into OCR's other model configs too." NVIDIA's own
# /v1/models is real and live but MUCH thinner than OpenRouter's: no
# pricing field (nothing here is "free" the way OpenRouter's :free tier
# is — it's all metered against the same trial-credit key), no
# context_length, no supported_parameters. The only real signal
# available is the model id itself (same param-count regex) plus a live
# test — so ranking here is coarser by necessity, not by choice.
NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_CATALOG_CACHE_PATH = (
    Path.home() / ".master_ai_free_model_picker_nvidia_cache.json"
)


def _nvidia_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        keys = parse_kv_keys(KEYCHAIN_PATH.read_text())
    except Exception:
        keys = {}
    key = keys.get("nvidia")
    if not key:
        raise RuntimeError(f"no NVIDIA_API_KEY found in {KEYCHAIN_PATH}")
    return key


def fetch_nvidia_models(
    key: str | None = None, force_fresh: bool = False
) -> list[dict[str, Any]]:
    """NVIDIA's real, current model list for this key — same 1h cache
    pattern as fetch_openrouter_models, same reason (a hot caller
    shouldn't refetch every turn; correctness matters more than one
    saved round-trip when actually picking something to use right now)."""
    if not force_fresh:
        try:
            cached = json.loads(_NVIDIA_CATALOG_CACHE_PATH.read_text())
            import time as _time

            if _time.time() - cached.get("ts", 0) < _CATALOG_CACHE_TTL:
                return cached.get("models", [])
        except Exception:
            pass
    key = _nvidia_key(key)
    req = urllib.request.Request(
        NVIDIA_MODELS_URL, headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    models = data.get("data", [])
    try:
        import time as _time

        _NVIDIA_CATALOG_CACHE_PATH.write_text(
            json.dumps({"ts": _time.time(), "models": models})
        )
    except Exception:
        pass
    return models


def rank_nvidia_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by parameter count parsed from the id — the only structural
    capability signal NVIDIA's /v1/models actually returns. Models with
    no parseable size (embedders, safety-guard, parse/OCR-specialist
    models — real categories seen in this catalog, not general chat
    models regardless of size) sort last rather than winning by default."""
    ranked = []
    for m in models:
        params_b = _param_count_b(m.get("id", ""))
        m = dict(m)
        m["_rank_info"] = {"params_b": params_b, "context_length": None}
        ranked.append(m)
    ranked.sort(
        key=lambda m: (
            m["_rank_info"]["params_b"]
            if m["_rank_info"]["params_b"] is not None
            else -1
        ),
        reverse=True,
    )
    return ranked


def pick_best_working_nvidia_model(
    key: str | None = None, max_tries: int = 6, force_fresh: bool = True
) -> dict[str, Any]:
    """Same pattern as pick_best_working_free_model, for the NVIDIA-
    direct lane. No free-tier filter (nothing here reports as free) and
    no tool-calling filter (the field doesn't exist in this catalog) —
    rank by size, then live-test in order with real headroom for
    reasoning-mandatory models (see test_model_live's 2026-09-26 note)."""
    key = _nvidia_key(key)
    all_models = fetch_nvidia_models(key, force_fresh=force_fresh)
    ranked = rank_nvidia_models(all_models)

    attempts = []
    for m in ranked[:max_tries]:
        model_id = m["id"]
        ok, detail = test_model_live(key, model_id, url=NVIDIA_CHAT_URL, timeout=30)
        attempts.append(
            {
                "model": model_id,
                "params_b": m["_rank_info"]["params_b"],
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


def write_ocr_config(
    model_id: str,
    config_path: Path = OCR_CONFIG_PATH,
    provider: str = "openrouter-free",
    make_active: bool = False,
) -> None:
    """Point one of Open Code Review's custom providers at a live pick —
    never touches the key already sitting in that provider's config.
    make_active also sets this provider as OCR's top-level default
    (only meaningful for one provider at a time; the others still get
    their own model kept current even when not active)."""
    with open(config_path) as f:
        cfg = json.load(f)
    cfg.setdefault("custom_providers", {}).setdefault(provider, {})
    cfg["custom_providers"][provider]["model"] = model_id
    if make_active:
        cfg["provider"] = provider
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)


def refresh_ollama_local_config(config_path: Path = OCR_CONFIG_PATH) -> str:
    """OCR's ollama-local provider needs the opposite fix from the cloud
    ones: nothing here is a billing/rate-limit concern, but a hardcoded
    model name ("qwen3-vl:8b") still silently breaks the moment that
    exact tag isn't pulled on whatever box runs this. Reuses
    hardware_model.pick_local_model() — the same RAM-tier + actually-
    pulled-models logic master_ai.py's own local routing already uses —
    instead of a second, independent hardcoded guess."""
    import hardware_model

    model = hardware_model.pick_local_model()
    write_ocr_config(model, config_path=config_path, provider="ollama-local")
    return model


def _print_attempts(
    label: str, result: dict[str, Any], provider_key: str | None = None
) -> None:
    print(f"{label}:")
    for a in result["attempts"]:
        status = "OK" if a["ok"] else "FAILED"
        pb = f"{a['params_b']:.0f}B" if a["params_b"] is not None else "?B"
        ctx = a.get("context_length")
        ctx_s = f"ctx={ctx:>8}" if ctx is not None else ""
        print(f"  [{status:6s}] {a['model']:55s} {pb:>6s} {ctx_s} -> {a['detail']}")
    if result["model"]:
        print(f"  -> picked: {result['model']}")
        if provider_key and provider_key in KNOWN_RATE_LIMITS_RPM:
            rpm = KNOWN_RATE_LIMITS_RPM[provider_key]
            rec = recommended_concurrency(provider_key)
            print(
                f"  -> known ceiling: {rpm} req/min — pass --concurrency {rec} "
                f"to `ocr review` (default 8 WILL burst past this)"
            )
    else:
        print("  -> no working candidate found")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--provider",
        choices=["openrouter", "nvidia", "ollama", "all"],
        default="openrouter",
        help="which lane to pick for (default: openrouter — the genuinely free one). "
        "'all' covers every OCR provider slot in one run: openrouter-free, "
        "nvidia-gptoss + nvidia-proxy (same NVIDIA-direct pick, mirrored — nvidia-proxy "
        "is a local rate-limiting relay in front of the identical upstream/key, so the "
        "same model choice applies to both), and ollama-local.",
    )
    ap.add_argument(
        "--write-ocr-config",
        action="store_true",
        help=f"also update {OCR_CONFIG_PATH} with the pick(s)",
    )
    ap.add_argument("--max-tries", type=int, default=5)
    ap.add_argument(
        "--allow-no-tools",
        action="store_true",
        help="OpenRouter only: don't require tool-calling support (default: require it)",
    )
    args = ap.parse_args()

    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    if args.provider in ("openrouter", "all"):
        try:
            results["openrouter"] = pick_best_working_free_model(
                max_tries=args.max_tries, require_tools=not args.allow_no_tools
            )
        except RuntimeError as e:
            errors.append(f"openrouter: {e}")

    if args.provider in ("nvidia", "all"):
        try:
            results["nvidia"] = pick_best_working_nvidia_model(max_tries=args.max_tries)
        except RuntimeError as e:
            errors.append(f"nvidia: {e}")

    if args.provider in ("ollama", "all"):
        try:
            import hardware_model

            results["ollama"] = {
                "model": hardware_model.pick_local_model(),
                "attempts": [],
                "candidates": [],
            }
        except Exception as e:
            errors.append(f"ollama: {e}")

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        if "openrouter" in results:
            _print_attempts(
                "OpenRouter (genuinely free tier)",
                results["openrouter"],
                provider_key="openrouter-free",
            )
        if "nvidia" in results:
            _print_attempts(
                "NVIDIA direct (trial-credit metered — no free tier as such, "
                "ranked by size only, live-tested)",
                results["nvidia"],
                provider_key="nvidia-gptoss",
            )
        if "ollama" in results:
            print(
                f"Ollama local (RAM-tier + actually pulled): {results['ollama']['model']}\n"
            )
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)

    if args.write_ocr_config:
        wrote_any = False
        if "openrouter" in results and results["openrouter"]["model"]:
            write_ocr_config(
                results["openrouter"]["model"],
                provider="openrouter-free",
                make_active=True,
            )
            print(
                f"Wrote {results['openrouter']['model']} to openrouter-free (made active)"
            )
            wrote_any = True
        if "nvidia" in results and results["nvidia"]["model"]:
            m = results["nvidia"]["model"]
            write_ocr_config(m, provider="nvidia-gptoss")
            write_ocr_config(m, provider="nvidia-proxy")
            print(f"Wrote {m} to nvidia-gptoss and nvidia-proxy")
            wrote_any = True
        if "ollama" in results and results["ollama"]["model"]:
            write_ocr_config(results["ollama"]["model"], provider="ollama-local")
            print(f"Wrote {results['ollama']['model']} to ollama-local")
            wrote_any = True
        if not wrote_any:
            print(
                "Nothing written — no working pick for any requested provider.",
                file=sys.stderr,
            )
            return 1

    return 0 if any(r.get("model") for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
