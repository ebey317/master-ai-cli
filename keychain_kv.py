"""Single source of truth for reading ~/.master_ai_keys (KEY=VALUE format,
not JSON — see KEYCHAIN.md). Extracted 2026-09-26.

Before this, _KV_KEY_MAP and _parse_kv_keys were hand-copied into
master_ai.py, gate.py, and setup_wizard.py independently. They had
already drifted: master_ai.py's copy had 8 more provider mappings
(Poolside, QwenCloud Token Plan, Tinyfish, Telegram, OpenCode Go,
Firecrawl, NVIDIA's second key) than the other two, and only
master_ai.py's parser filtered out placeholder/redacted values via
_looks_like_real_key — gate.py and setup_wizard.py would have silently
accepted a redaction placeholder as a real key. A provider added to one
copy silently doesn't exist in the other two; this is exactly the
"hardcoded and drifts" pattern flagged across the rest of tonight's
audit, just at the key-loading layer. One shared module closes that gap
for good — add a provider here once, every consumer sees it.
"""

_KV_KEY_MAP = {
    "OPENROUTER_API_KEY": "openrouter",
    "GROQ_API_KEY": "groq",
    "GEMINI_API_KEY": "gemini",
    "ANTHROPIC_CONSOLE_KEY": "anthropic",
    "CEREBRAS_API_KEY": "cerebras",
    "FIREWORKS_API_KEY": "fireworks",
    "OPENAI_API_KEY": "openai",
    "DEEPSEEK_API_KEY": "deepseek",
    "HUGGINGFACE_TOKEN": "huggingface",
    "HF_TOKEN": "huggingface",
    "NVIDIA_API_KEY": "nvidia",
    "NVIDIA_API_KEY_2": "nvidia2",
    # 2026-09-07: Elijah's QwenCloud Token Plan (paid, $6/mo) — wired into
    # Hermes already (see project_qwen_token_plan_setup memory). WS key
    # confirmed dead (401); mapped anyway so it's visible/trackable rather
    # than silently missing, but never used for dispatch.
    "QWEN_TOKENPLAN_API_KEY": "qwen",
    "QWEN_TOKENPLAN_WS_API_KEY": "qwen_ws",
    "TINYFISH_API_KEY": "tinyfish",
    # 2026-09-25: Poolside direct inference (OpenAI-compatible), Laguna
    # agentic-coding models. Verified against docs.poolside.ai before
    # wiring in — see the equivalent Hermes plugin at
    # ~/.hermes/hermes-agent/plugins/model-providers/poolside/.
    "POOLSIDE_API_KEY": "poolside",
    "TELEGRAM_BOT_TOKEN": "telegram",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    # 2026-09-12: OpenCode Go ($10/mo subscription, https://opencode.ai/go)
    # — same Zen API shape as the keyless free relay but requires Bearer
    # auth and hits /zen/go/v1.
    "OPENCODE_API_KEY": "opencode_go",
    # 2026-09-25: Firecrawl key lives in ~/.hermes/.env for Hermes but the
    # keychain row had the redaction placeholder, so firecrawl_fetch() was
    # permanently on the "key not set" path. Mapped here so the kv parser
    # picks up the real key once restored.
    "FIRECRAWL_API_KEY": "firecrawl",
}

# Reverse map (short name -> canonical uppercase env-style name), used by
# consumers that need to write a key back (setup_wizard.py). HF_TOKEN is
# excluded because it's an alias of HUGGINGFACE_TOKEN, not the canonical
# name for "huggingface".
_CANONICAL_NAME = {v: k for k, v in _KV_KEY_MAP.items() if k != "HF_TOKEN"}


def _looks_like_real_key(val):
    """Reject corrupted/placeholder key values before they ever reach a
    request. 2026-08-24: every key in the keychain had been overwritten
    with a redaction placeholder ('«redacted:gsk_…»' style text) instead of
    the real secret. Real API keys are plain ASCII tokens; a placeholder
    or any other non-ASCII value crashes urllib/http.client deep inside
    urlopen() with a raw UnicodeEncodeError instead of failing cleanly,
    which looks like a hang as the router retries every provider in a
    loop. Treat non-ASCII or bracketed values as absent so callers take
    the normal 'key not configured' path instead."""
    if not val:
        return False
    if any(ord(c) > 127 for c in val):
        return False
    if val.startswith(("<", "[", "«", "REDACTED", "redacted")):
        return False
    return True


def parse_kv_keys(text):
    """KEY=VALUE lines -> {short_name: value}, skipping blanks, comments,
    unmapped names, and anything that fails _looks_like_real_key."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name, val = name.strip(), val.strip()
        short = _KV_KEY_MAP.get(name)
        if short and val and short not in out and _looks_like_real_key(val):
            out[short] = val
    return out
