"""Startup key gate for Master AI.

master_ai requires either (a) at least one provider API key in
``~/.master_ai_keys`` or (b) a running local Ollama server. If neither is
present, ``ensure_ready()`` prints a banner and launches the interactive
bash key prompt (``setup_keys.sh``) in the current terminal, then re-checks.
Still nothing -> exit 1 with instructions.

Routing itself stays in ``master_ai.detect_route()``: it reads the same
keys file and auto-picks the best lane (groq fast lane, openrouter deep
lane, gemini free tier, fireworks fallback, cerebras opt-in, local Ollama
default). This module only answers one question: *can we run at all?*

Public API:
    check_ready() -> dict   {"ready": bool, "lanes": {...}, "problems": [...]}
    ensure_ready() -> None  block until ready or exit(1); runs the bash prompt
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

KEYS_FILE = Path(os.environ.get("MASTER_AI_KEYS_FILE", Path.home() / ".master_ai_keys"))
OLLAMA_URL = "http://localhost:11434"

# Cloud providers the router actually consumes (see master_ai.detect_route).
CLOUD_PROVIDERS = ("groq", "openrouter", "gemini", "fireworks", "cerebras")
# Optional extras that enrich routing but are never required to run.
OPTIONAL_PROVIDERS = ("brave", "serper", "firecrawl")

def _find_setup_script() -> Path | None:
    """Locate setup_keys.sh: beside this module, in ~/scripts (install.sh
    copies everything there), or in the current directory."""
    candidates = [
        Path(__file__).with_name("setup_keys.sh"),
        Path.home() / "scripts" / "setup_keys.sh",
        Path.cwd() / "setup_keys.sh",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _load_keys() -> dict:
    try:
        return json.loads(KEYS_FILE.read_text())
    except Exception:
        return {}


def _ollama_alive(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def check_ready() -> dict:
    """Inspect the current configuration without touching the terminal."""
    keys = _load_keys()
    lanes = {
        "local_ollama": _ollama_alive(),
        **{p: bool(str(keys.get(p) or "").strip()) for p in CLOUD_PROVIDERS},
    }
    extras = {p: bool(str(keys.get(p) or "").strip()) for p in OPTIONAL_PROVIDERS}
    ready = lanes["local_ollama"] or any(lanes[p] for p in CLOUD_PROVIDERS)

    problems = []
    if not ready:
        problems.append(
            "No API key configured and no local Ollama server detected. "
            "Master AI needs at least one lane to run."
        )
    return {"ready": ready, "lanes": lanes, "extras": extras, "problems": problems, "keys_file": str(KEYS_FILE)}


def _print_banner(state: dict) -> None:
    lanes = state["lanes"]
    print("=" * 64)
    print("  MASTER AI — STARTUP CHECK")
    print("=" * 64)
    for name, ok in lanes.items():
        mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        print(f"  {mark} {name:<14} {'available' if ok else 'not configured'}")
    print("-" * 64)
    for p in state["problems"]:
        print(f"  \033[33m{p}\033[0m")
    print()
    print(f"  Keys file: {state['keys_file']}")
    print("=" * 64)


def _run_bash_prompt() -> bool:
    """Launch the interactive bash key prompt in the user's terminal."""
    script = _find_setup_script()
    if script is None:
        print("  setup_keys.sh not found (looked beside gate.py, ~/scripts/, and cwd)")
        return False
    print("  Launching interactive key setup...\n")
    try:
        subprocess.run(["bash", str(script)], check=False)
        return True
    except Exception as e:
        print(f"  setup_keys.sh failed: {e}")
        return False


def ensure_ready() -> None:
    """Block until a usable lane exists; exit(1) otherwise.

    Flow: check -> if not ready, show banner + run bash prompt -> re-check
    once. If the user walks away without entering anything, exit with
    instructions instead of starting a dead session.
    """
    state = check_ready()
    if state["ready"]:
        return

    _print_banner(state)
    _run_bash_prompt()

    state = check_ready()
    if state["ready"]:
        print("\n  \033[32m✓ Ready.\033[0m Lanes detected: "
              + ", ".join(k for k, v in state["lanes"].items() if v))
        return

    print("\n  \033[31m✗ Master AI cannot start without an API key or local Ollama.\033[0m")
    print("    Fix it with one of:")
    print("      bash setup_keys.sh          # paste a provider key (Groq/OpenRouter are free)")
    print("      curl -fsSL https://ollama.com/install.sh | sh   # local models, no key")
    print("      master-ai --setup           # full interactive setup wizard")
    sys.exit(1)
