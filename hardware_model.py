"""Hardware-based local model selection — the model follows the machine.

Elijah's rule (2026-09-24): NEVER hardcode a local model name. The right
local model is a function of the HARDWARE it runs on (RAM), and any user
can pull their own model anyway. So:

  1. On boot, probe total system RAM.
  2. Probe what the user ACTUALLY HAS PULLED in local Ollama.
  3. Pick the best pulled model that fits the hardware tier.
  4. If nothing pulled fits, keep the env override or a safe placeholder
     — and say so once in the boot log.

Resolution order:
  MASTER_AI_LOCAL_MODEL env  (explicit operator override, always wins)
  > best pulled model fitting the RAM tier (VLM preferred at equal fit)
  > any pulled model (degraded, logged)
  > last-resort placeholder (never crashes the engine)

Hardware tiers (RAM = total system GB -> max params):
  >= 32 GB : 14B
  >= 16 GB : 4B    <- Elijah's box (15 GB lands here at 4B ceiling)
  >=  8 GB : 3B
  <   8 GB : 2B
"""

import json
import os
import re
import urllib.request


def system_ram_gb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return max(1, int(int(line.split()[1]) // 1048576))
    except Exception:
        pass
    return 16  # sane assumption tier


def tier_max_b(ram_gb: int) -> int:
    if ram_gb >= 32:
        return 14
    if ram_gb >= 16:
        return 4
    if ram_gb >= 8:
        return 3
    return 2


def pulled_models(timeout: float = 2.0) -> list:
    """Names from local Ollama's /api/tags — what the user actually has."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def params_b(name: str):
    """Parameter size in billions from an Ollama tag.
    'qwen2.5vl:3b' -> 3.0, 'llama3.1:8b-instruct-q4' -> 8.0, 'llava:latest' -> None."""
    m = re.search(r":\s*(\d+(?:\.\d+)?)\s*b\b", name.lower())
    return float(m.group(1)) if m else None


def is_vlm(name: str) -> bool:
    n = name.lower()
    return "vl" in n or "vision" in n or "llava" in n


def pick_local_model(log=None) -> str:
    """Choose the local default from hardware tier + what's actually pulled."""
    env = os.environ.get("MASTER_AI_LOCAL_MODEL", "").strip()
    if env:
        return env  # explicit operator override always wins

    ram = system_ram_gb()
    max_b = tier_max_b(ram)
    pulled = pulled_models()
    if not pulled:
        return "qwen2.5vl:3b"  # nothing pulled; harmless placeholder

    def _score(name: str):
        b = params_b(name)
        if b is None:
            return (0, 0, 0)  # unparsable size: lowest priority
        fits = 1 if b <= max_b else 0
        vlm = 1 if is_vlm(name) else 0
        # fitting beats non-fitting; VLM preferred at equal fit; bigger
        # is better among fitting, smaller is better (cheaper) among
        # non-fitting so a degraded pick is the least-bad one
        return (fits, vlm, b if fits else -b)

    ranked = sorted(pulled, key=_score, reverse=True)
    best = ranked[0]
    if log:
        best_b = params_b(best)
        if best_b is not None and best_b > max_b:
            log(
                f"HARDWARE_PICK: nothing pulled fits RAM {ram}GB "
                f"(max {max_b}B) — degraded to '{best}'"
            )
        else:
            log(f"HARDWARE_PICK: RAM {ram}GB (max {max_b}B) -> {best}")
    return best
