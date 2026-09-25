#!/usr/bin/env python3
"""Minimal prewarm — keep Master AI's primary brain and eyes hot in Ollama.

Reverted 2026-04-21 evening to the simple pattern that worked before.
The "real-shape" prewarm (behavior + memory injection) timed out even
at 300s on Skylake CPU. Too much for the hardware. This script just
loads model weights and sets keep_alive — nothing clever.

Requires Ollama to allow at least two loaded models:
  OLLAMA_MAX_LOADED_MODELS=2
"""

import json
import urllib.request


def _generate(model, prompt, timeout=360):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_predict": 8, "temperature": 0},
    }
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=timeout).read()


def main():
    warmups = [
        ("master-ai", "You are Sensei. Reply with the single word: ready."),
    ]
    ok = []
    failed = []
    for model, prompt in warmups:
        try:
            _generate(model, prompt)
            ok.append(model)
        except Exception as e:
            failed.append(f"{model}: {e}")
    if ok:
        print("prewarmed: " + ", ".join(ok))
    if failed:
        print("prewarm failed: " + " | ".join(failed))
        # Keep this oneshot non-fatal so boot/session startup does not fail.
        raise SystemExit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"prewarm failed: {e}")
        raise SystemExit(0)
