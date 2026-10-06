#!/usr/bin/env python3
"""Watches ~/.hermes/logs/agent.log for a fresh NVIDIA rate limit and swaps
NVIDIA_API_KEY <-> NVIDIA_API_KEY_2 in ~/.hermes/.env when one shows up.

This is deliberately standalone -- NOT wired into Hermes' own retry logic.
Elijah wants his own external script controlling NVIDIA key rotation, not
Hermes deciding it internally. Hermes already re-reads .env on the next turn
(agent._try_refresh_env_client_credentials(), called per-turn in
conversation_loop.py), so a plain file edit here is enough -- no Hermes
restart needed for the swap to take effect.

Usage:
    python3 ~/scripts/nvidia_key_rotate_watcher.py            # run in foreground
    nohup python3 ~/scripts/nvidia_key_rotate_watcher.py &    # run in background
"""

import re
import time
from pathlib import Path

ENV_PATH = Path.home() / ".hermes" / ".env"
LOG_PATH = Path.home() / ".hermes" / "logs" / "agent.log"
WATCHER_LOG = Path.home() / ".hermes" / "logs" / "nvidia_key_rotate.log"

# Debounce: a single rate-limited turn produces up to 3 retry log lines
# (attempt 1/3, 2/3, 3/3) for the SAME underlying event. Without a cooldown
# this would swap keys 2-3 times per incident instead of once.
COOLDOWN_SECONDS = 30

_RATE_LIMIT_RE = re.compile(
    r"provider=nvidia\b.*(?:RateLimitError|HTTP 429|status.:429)", re.IGNORECASE
)


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(WATCHER_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_env_lines() -> list[str]:
    return ENV_PATH.read_text().splitlines(keepends=True)


def _swap_nvidia_keys() -> bool:
    """Swap the values of NVIDIA_API_KEY and NVIDIA_API_KEY_2 in .env. Returns
    True on success. Never logs either key's value."""
    lines = _read_env_lines()
    primary_idx = secondary_idx = None
    primary_val = secondary_val = None
    for i, line in enumerate(lines):
        m = re.match(r"^export NVIDIA_API_KEY=(.*)\n?$", line)
        if m:
            primary_idx, primary_val = i, m.group(1)
            continue
        m2 = re.match(r"^export NVIDIA_API_KEY_2=(.*)\n?$", line)
        if m2:
            secondary_idx, secondary_val = i, m2.group(1)
    if primary_idx is None or secondary_idx is None:
        _log(
            "ERROR: could not find both NVIDIA_API_KEY and NVIDIA_API_KEY_2 in .env -- not swapping"
        )
        return False
    lines[primary_idx] = f"export NVIDIA_API_KEY={secondary_val}\n"
    lines[secondary_idx] = f"export NVIDIA_API_KEY_2={primary_val}\n"
    ENV_PATH.write_text("".join(lines))
    return True


def _tail_f(path: Path):
    """Follow a growing/rotating log file, yielding new lines as they appear."""
    while not path.exists():
        time.sleep(1)
    f = open(path)
    f.seek(0, 2)  # start at end -- only react to NEW lines from here on
    inode = path.stat().st_ino
    while True:
        line = f.readline()
        if line:
            yield line
            continue
        time.sleep(0.5)
        try:
            if path.stat().st_ino != inode:  # log rotated
                f.close()
                f = open(path)
                inode = path.stat().st_ino
        except FileNotFoundError:
            pass


def main() -> None:
    _log(f"watcher started -- following {LOG_PATH}")
    last_swap = 0.0
    for line in _tail_f(LOG_PATH):
        if not _RATE_LIMIT_RE.search(line):
            continue
        now = time.time()
        if now - last_swap < COOLDOWN_SECONDS:
            continue  # same incident's retry attempts -- already swapped for this one
        _log("NVIDIA rate limit detected -- rotating keys")
        if _swap_nvidia_keys():
            last_swap = now
            _log(
                "swap complete (NVIDIA_API_KEY <-> NVIDIA_API_KEY_2). Hermes picks it up on its next turn."
            )


if __name__ == "__main__":
    main()
