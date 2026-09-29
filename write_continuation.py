#!/usr/bin/env python3
"""Write a continuation hint when session context is about to compress.

Usage: call write_continuation(goal, last_user_prompt, pending_list)
Output: creates/updates ~/.hermes/cache/scratch/__CONTINUATION__
"""

import os

SCRATCH = "/home/elijah/.hermes/cache/scratch"
CONT_FILE = os.path.join(SCRATCH, "__CONTINUATION__")


def write_continuation(goal: str, last_user_prompt: str, pending: list[str]) -> str:
    """Write the continuation hint file and return its path."""
    os.makedirs(SCRATCH, exist_ok=True)
    lines = [
        f"== CONTINUE: {goal} ==",
        f"Last user: {last_user_prompt}",
        "Pending:",
    ]
    for item in pending[:5]:
        lines.append(f"  - {item}")
    if len(pending) > 5:
        lines.append(f"  - ... and {len(pending) - 5} more")
    content = "\n".join(lines)
    with open(CONT_FILE, "w") as f:
        f.write(content)
    return CONT_FILE


def read_continuation() -> str | None:
    """Read and return the continuation hint, or None if not present."""
    try:
        return open(CONT_FILE).read()
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    # Simple test
    result = write_continuation("Test goal", "Test prompt?", ["item 1", "item 2"])
    print(read_continuation())
