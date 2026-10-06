#!/usr/bin/env python3
# sensei_router.py — cloud dispatch layer for Sensei
# Firehose: hermes-405b → deepseek-r1 → groq → nemotron → gpt-oss → gemini → openrouter
# Fish tank: anthropic — manual pin only, 4-turn window, 500-char system cap

import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

KEYS_FILE = Path.home() / ".master_ai_keys"
LOG_FILE = Path.home() / "scripts/master.log"


def _log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _keys():
    try:
        return json.loads(KEYS_FILE.read_text())
    except Exception:
        return {}


def ask_cloud_groq(messages):
    key = _keys().get("groq")
    if not key:
        return None
    _log("CLOUD [groq/llama-3.3-70b]")
    messages = messages[-3:]  # last 3 turns — cap context window
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 1024,
        "stream": False,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "python-requests/2.31.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        _log(f"GROQ_ERROR: HTTP {e.code}")
        return None
    except Exception as e:
        _log(f"GROQ_ERROR: {e}")
        return None


def ask_cloud_gemini(messages):
    key = _keys().get("gemini")
    if not key:
        return None
    _log("CLOUD [gemini/2.0-flash]")
    text = "\n".join(m["content"] for m in messages if m["role"] != "system")
    payload = {"contents": [{"parts": [{"text": text}]}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        _log(f"GEMINI_ERROR: {e}")
        return None


def ask_cloud_anthropic(messages):
    key = _keys().get("anthropic")
    if not key:
        return None
    _log("CLOUD [anthropic/claude-sonnet-4-6] [fish-tank+cache]")
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_msgs = [m for m in messages if m["role"] != "system"]
    user_msgs = user_msgs[-4:]  # fish tank: 4-turn window only
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system": [
            {
                "type": "text",
                "text": system[:500],
                "cache_control": {
                    "type": "ephemeral"
                },  # freeze system prompt in cloud memory
            }
        ],
        "messages": user_msgs,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["content"][0]["text"]
    except Exception as e:
        _log(f"ANTHROPIC_ERROR: {e}")
        return None


def _ask_openrouter(messages, model, label, timeout=60):
    key = _keys().get("openrouter")
    if not key:
        return None
    _log(f"CLOUD [openrouter/{label}]")
    messages = messages[-3:]  # last 3 turns — cap context window
    payload = {"model": model, "messages": messages}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "http://localhost",
            "X-Title": "sensei",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        diag = {
            401: "AUTH FAIL",
            403: "AUTH FAIL",
            429: "RATE LIMIT",
            402: "OUT OF CREDITS",
        }.get(e.code, f"HTTP {e.code}")
        _log(f"OPENROUTER_ERROR [{label}]: {diag}")
        return None
    except Exception as e:
        _log(f"OPENROUTER_ERROR [{label}]: {e}")
        return None


def ask_cloud_hermes(messages):
    return _ask_openrouter(
        messages, "nousresearch/hermes-3-llama-3.1-405b:free", "hermes-405B", timeout=90
    )


def ask_cloud_r1(messages):
    return _ask_openrouter(
        messages, "deepseek/deepseek-r1:free", "deepseek-r1", timeout=90
    )


def ask_cloud_nemotron(messages):
    return _ask_openrouter(
        messages, "nvidia/nemotron-3-super-120b-a12b:free", "nemotron-120B", timeout=60
    )


def ask_cloud_gptoss(messages):
    return _ask_openrouter(
        messages, "openai/gpt-oss-120b:free", "gpt-oss-120B", timeout=60
    )


def ask_cloud_llama(messages):
    return _ask_openrouter(
        messages, "meta-llama/llama-3.3-70b-instruct:free", "llama-3.3-70b", timeout=30
    )


def ask_cloud(messages, provider="groq"):
    fn_map = {
        "groq": ask_cloud_groq,
        "gemini": ask_cloud_gemini,
        "hermes-405b": ask_cloud_hermes,
        "deepseek-r1": ask_cloud_r1,
        "nemotron": ask_cloud_nemotron,
        "gpt-oss-120b": ask_cloud_gptoss,
        "openrouter": ask_cloud_llama,
        "anthropic": ask_cloud_anthropic,  # manual pin only
    }
    r = fn_map.get(provider, ask_cloud_groq)(messages)
    if r:
        return r
    # Firehose fallback — Anthropic is NOT in this chain
    for label, fn in [
        ("hermes-405b", ask_cloud_hermes),
        ("deepseek-r1", ask_cloud_r1),
        ("groq", ask_cloud_groq),
        ("nemotron", ask_cloud_nemotron),
        ("gpt-oss-120b", ask_cloud_gptoss),
        ("gemini", ask_cloud_gemini),
        ("openrouter", ask_cloud_llama),
    ]:
        r = fn(messages)
        if r:
            _log(f"FALLBACK_HIT: {label}")
            return r
    return None
