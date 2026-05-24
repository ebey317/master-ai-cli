#!/usr/bin/env python3
"""validate_keys.py — blindfold-compliant API key validator.

Reads ~/.master_ai_keys (JSON), hits each provider's auth-gated endpoint,
reports ✓/✗ per key. Key VALUES are never printed, logged, or written
anywhere outside the in-memory Authorization header.

Usage:
    python3 ~/scripts/validate_keys.py
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

KEY_FILE = Path.home() / ".master_ai_keys"

# Maps the keys-file slug -> (display name, validation URL, header builder)
# Header builder takes the key value, returns headers dict.
# Keys are looked up case-insensitively below.
PROVIDERS = {
    "groq":         ("Groq",          "https://api.groq.com/openai/v1/models",          lambda k: {"Authorization": f"Bearer {k}"}),
    "openrouter":   ("OpenRouter",    "https://openrouter.ai/api/v1/auth/key",          lambda k: {"Authorization": f"Bearer {k}"}),
    "cerebras":     ("Cerebras",      "https://api.cerebras.ai/v1/models",              lambda k: {"Authorization": f"Bearer {k}"}),
    "fireworks":    ("Fireworks.ai",  "https://api.fireworks.ai/inference/v1/models",   lambda k: {"Authorization": f"Bearer {k}"}),
    "gemini":       ("Gemini",        None,                                              lambda k: {}),  # special: key in URL query
    "firecrawl":    ("Firecrawl",     "https://api.firecrawl.dev/v1/team/credit-usage", lambda k: {"Authorization": f"Bearer {k}"}),
    "serper":       ("Serper.dev",    "https://google.serper.dev/search",                lambda k: {"X-API-KEY": k, "Content-Type": "application/json"}),
    "ollama":       ("Ollama Cloud",  "https://ollama.com/v1/models",                    lambda k: {"Authorization": f"Bearer {k}"}),
    "openai":       ("OpenAI",        "https://api.openai.com/v1/models",                lambda k: {"Authorization": f"Bearer {k}"}),
    "anthropic":    ("Anthropic",     "https://api.anthropic.com/v1/models",             lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "huggingface":  ("HuggingFace",   "https://huggingface.co/api/whoami-v2",            lambda k: {"Authorization": f"Bearer {k}"}),
    "hf":           ("HuggingFace",   "https://huggingface.co/api/whoami-v2",            lambda k: {"Authorization": f"Bearer {k}"}),
    "deepseek":     ("DeepSeek",      "https://api.deepseek.com/v1/models",              lambda k: {"Authorization": f"Bearer {k}"}),
    "mistral":      ("Mistral",       "https://api.mistral.ai/v1/models",                lambda k: {"Authorization": f"Bearer {k}"}),
}

# Slugs that are tracking metadata, not auth keys — skip silently
NON_KEY_SLUGS = {"openrouter_tokens_date", "openrouter_tokens_today"}

# Realistic browser UA — some providers (Groq, Cerebras, Fireworks) front
# their API with a Cloudflare-style WAF that returns 403 to Python-urllib's
# default UA even with a valid key. Sending a normal UA gets through.
_UA = "Mozilla/5.0 (X11; Linux x86_64) validate_keys.py/2 (off-grid validator)"

# Fallback probe: for providers that 403 on /models, try a minimal chat
# completion. If THAT works, the key is valid — the /models endpoint just
# isn't user-scope accessible. Each entry: (url, body_dict).
_CHAT_FALLBACK = {
    "groq":      ("https://api.groq.com/openai/v1/chat/completions",
                  {"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1}),
    "cerebras":  ("https://api.cerebras.ai/v1/chat/completions",
                  {"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1}),
    "fireworks": ("https://api.fireworks.ai/inference/v1/chat/completions",
                  {"model": "accounts/fireworks/models/llama-v3p1-8b-instruct", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1}),
}


def _request(url: str, headers: dict, body: bytes | None = None, method: str | None = None) -> int:
    """Send a request; return HTTP status code. Raises HTTPError on >=400."""
    hdrs = {"User-Agent": _UA, **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method or ("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def validate(key_name: str, key_value: str) -> tuple[str, str]:
    """Return (status_glyph, message). Never returns the key value."""
    slug = key_name.lower()
    if slug in NON_KEY_SLUGS:
        return ("·", "tracking metadata (not an auth key) — skipped")
    if slug not in PROVIDERS:
        return ("?", "no validator wired for this provider")
    label, url, header_fn = PROVIDERS[slug]
    headers = header_fn(key_value)

    # Build the primary request
    body: bytes | None = None
    method: str | None = None
    if slug == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key_value}"
    elif slug == "serper":
        body = b'{"q":"ping"}'
        method = "POST"

    try:
        _request(url, headers, body=body, method=method)
        return ("✓", f"{label}: valid (HTTP 200)")
    except urllib.error.HTTPError as primary_err:
        # On 403 with WAF-style block, try chat-completion fallback before declaring dead
        if primary_err.code in (401, 403) and slug in _CHAT_FALLBACK:
            fb_url, fb_body = _CHAT_FALLBACK[slug]
            fb_headers = {**header_fn(key_value), "Content-Type": "application/json"}
            try:
                _request(fb_url, fb_headers, body=json.dumps(fb_body).encode(), method="POST")
                return ("✓", f"{label}: valid (chat fallback OK, primary {primary_err.code})")
            except urllib.error.HTTPError as fb_err:
                if fb_err.code in (401, 403):
                    return ("✗", f"{label}: invalid/revoked (chat fallback also {fb_err.code})")
                if fb_err.code == 429:
                    return ("⚠", f"{label}: rate-limited but auth OK (chat HTTP 429)")
                if fb_err.code == 402:
                    return ("⚠", f"{label}: auth OK but out of credits (chat HTTP 402)")
                if fb_err.code == 400:
                    # 400 from a chat probe means auth passed; model name or body wrong
                    return ("✓", f"{label}: auth OK (chat fallback HTTP 400 — model name; key valid)")
                return ("?", f"{label}: primary {primary_err.code} / chat fallback HTTP {fb_err.code}")
            except Exception as e:
                return ("?", f"{label}: primary {primary_err.code} / chat fallback {type(e).__name__}")
        if primary_err.code in (401, 403):
            return ("✗", f"{label}: invalid/revoked (HTTP {primary_err.code})")
        if primary_err.code == 429:
            return ("⚠", f"{label}: rate-limited but auth OK (HTTP 429)")
        if primary_err.code == 402:
            return ("⚠", f"{label}: auth OK but out of credits (HTTP 402)")
        return ("?", f"{label}: HTTP {primary_err.code}")
    except urllib.error.URLError as e:
        return ("?", f"{label}: network error ({e.reason})")
    except Exception as e:
        return ("?", f"{label}: error ({type(e).__name__})")


def main() -> int:
    if not KEY_FILE.exists():
        print(f"ERR: {KEY_FILE} not found")
        return 2
    try:
        keys = json.loads(KEY_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"ERR: {KEY_FILE} is not valid JSON: {e}")
        return 2

    if not isinstance(keys, dict) or not keys:
        print(f"(no keys found in {KEY_FILE})")
        return 0

    print(f"Validating {len(keys)} key(s) from {KEY_FILE}\n")
    print(f"{'STATUS':<8} {'KEY NAME':<25} MESSAGE")
    print("-" * 80)
    for name in sorted(keys.keys()):
        value = keys[name]
        if not isinstance(value, str) or not value.strip():
            print(f"{'-':<8} {name:<25} (empty value)")
            continue
        glyph, msg = validate(name, value)
        print(f"{glyph:<8} {name:<25} {msg}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
