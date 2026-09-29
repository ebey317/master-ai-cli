#!/usr/bin/env python3
"""
fire-kimi-cleanup.py — minimal Ollama Cloud dispatch shim for cleanup tasks.

Calls https://ollama.com/v1/chat/completions with kimi-k2.7-code, returns the
raw JSON response. Use for the cleanup-plan verification phase (Kimi reviews
each step's output and either approves or flags a rollback).

Usage:
    . ~/.hermes/.env  # exports OLLAMA_API_KEY
    python3 fire-kimi-cleanup.py <step_label> <plan_excerpt> <actual_output>

Prints the model's reply + finish_reason. Exit code: 0 on approve, 1 on
flag, 2 on transport error.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://ollama.com/v1/chat/completions"
MODEL = "kimi-k2.7-code"


def call_kimi(system: str, user: str, max_tokens: int = 1024) -> dict:
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    if not api_key:
        sys.stderr.write("ERROR: OLLAMA_API_KEY not exported. Run: . ~/.hermes/.env\n")
        sys.exit(2)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.stderr.write(f"HTTP {e.code}: {body[:500]}\n")
        sys.exit(2)
    except urllib.error.URLError as e:
        sys.stderr.write(f"URL error: {e}\n")
        sys.exit(2)


SYSTEM = (
    "You are Kimi K2.7-code, an execution verifier. Do NOT use internal "
    "reasoning or chain-of-thought blocks. Output ONLY the final verdict "
    "line, nothing else.\n\n"
    "For each step you receive: STEP label, PLAN excerpt, ACTUAL output.\n"
    "Reply with ONE LINE in this exact format:\n"
    "  APPROVE — <one-line reason>\n"
    "  or\n"
    "  FLAG — <what is wrong> + ROLLBACK: <one-line fix>\n"
    "No preamble. No reasoning. No restating inputs. Just the verdict line."
)


def main() -> int:
    if len(sys.argv) != 4:
        sys.stderr.write(
            "usage: fire-kimi-cleanup.py <step_label> <plan_excerpt> <actual_output>\n"
        )
        return 2
    label, plan, actual = sys.argv[1], sys.argv[2], sys.argv[3]
    user = (
        f"STEP: {label}\n"
        f"PLAN: {plan[:800]}\n"
        f"OUTPUT: {actual[:800]}\n"
        "Reply one line only. APPROVE or FLAG."
    )
    resp = call_kimi(SYSTEM, user, max_tokens=1500)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    finish = resp.get("choices", [{}])[0].get("finish_reason", "?")
    usage = resp.get("usage", {})
    print(f"KIMI_USAGE: {json.dumps(usage)}")
    print(f"KIMI_FINISH: {finish}")
    print(f"KIMI_VERDICT: {content}")
    if content.startswith("APPROVE"):
        return 0
    if content.startswith("FLAG"):
        return 1
    return 1  # any non-approve response is a flag


if __name__ == "__main__":
    sys.exit(main())
