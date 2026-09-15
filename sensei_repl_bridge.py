"""Drives master_ai.py's classic (SENSEI_TUI=0) REPL as a persistent
subprocess so external callers (telegram_gateway.py) can reuse its full
~90-command surface (doctor, sessions list, model, memory, etc.) without
reimplementing any of it and without touching master_ai.py itself.

Those commands only exist as `if lo == "...":` handlers deeply closed over
main()'s local `history`/print() calls -- not importable as functions. But
SENSEI_TUI=0 already makes main() use bare input()/print() instead of
prompt_toolkit's raw-mode TUI, which tolerates a piped (non-TTY) stdin/
stdout just fine (verified live: `printf 'doctor\\nx\\n' | SENSEI_TUI=0
python3 master_ai.py` runs doctor and exits cleanly, no crash). So this
module just drives that classic REPL like a scripted terminal session:
send a line to stdin, read output on a background thread, and cut the
response off at the next prompt-box reappearance.

One process per gateway lifetime (not per-message) -- startup does a real
system check (Ollama, keys, memory) that takes real time; paying that once
at gateway boot is fine, paying it per Telegram message is not.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

MASTER_AI_DIR = Path(__file__).resolve().parent
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PROMPT_MARKER = "│ 🥷"  # the actual input() prompt line only -- bare "🥷"
# also appears as banner decoration ("🥷 POWERED BY: MASTER AI...") much
# earlier in startup output, which made the first version of this detector
# fire on the banner instead of the real prompt.
_BOX_START_RE = re.compile(r"^\s*MODE:")
_LEADING_BOX_CLOSE_RE = re.compile(r"^[│└─┘\s]+$")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _strip_trailing_prompt_box(text: str) -> str:
    """Remove the recurring MODE:.../┌── ✏.../⌘ type:.../│ 🥷 box that
    reappears after every command's real output, keeping only the response
    content above it."""
    lines = text.splitlines()
    # Find the last line containing the prompt marker; the box starts a
    # few lines above it at the "MODE:" line.
    prompt_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _PROMPT_MARKER in lines[i]:
            prompt_idx = i
            break
    if prompt_idx is None:
        return text.strip()
    box_start = prompt_idx
    for i in range(prompt_idx, -1, -1):
        if _BOX_START_RE.search(lines[i]):
            box_start = i
            break
    body = lines[:box_start]
    # Drop a leading box-bottom-border fragment: the child redraws the
    # *previous* prompt's closing border as the first thing it prints
    # after receiving a command, before this command's real output.
    while body and _LEADING_BOX_CLOSE_RE.match(body[0]):
        body.pop(0)
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join(body).strip()


class SenseiRepl:
    """One persistent `python3 master_ai.py` (SENSEI_TUI=0) subprocess."""

    def __init__(self, startup_timeout: float = 30.0):
        env = os.environ.copy()
        env["SENSEI_TUI"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            ["python3", "-u", str(MASTER_AI_DIR / "master_ai.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(MASTER_AI_DIR),
            env=env,
            text=True,
            bufsize=1,
        )
        self._raw = ""
        self._consumed = 0  # chars already handed back to a previous send()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._await_prompt(timeout=startup_timeout)
        with self._lock:
            self._consumed = len(self._raw)

    def _read_loop(self) -> None:
        # Raw byte-level read, NOT line iteration: input()'s own prompt
        # string (the "🥷" line) is written with no trailing newline, so
        # `for line in stdout` never yields it until a LATER command
        # happens to supply one -- a permanent one-command lag. Reading
        # raw chunks off the fd sidesteps newline boundaries entirely.
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                self._raw += chunk.decode("utf-8", errors="replace")

    def _unconsumed(self) -> str:
        with self._lock:
            return self._raw[self._consumed :]

    def _await_prompt(self, timeout: float) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pending = _strip_ansi(self._unconsumed())
            if _PROMPT_MARKER in pending:
                time.sleep(0.2)  # let any trailing flush land
                return _strip_ansi(self._unconsumed())
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        return _strip_ansi(self._unconsumed())

    def send(self, command: str, timeout: float = 30.0) -> str:
        if self.proc.poll() is not None:
            raise RuntimeError("sensei REPL subprocess has exited")
        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        raw = self._await_prompt(timeout=timeout)
        with self._lock:
            self._consumed = len(self._raw)
        return _strip_trailing_prompt_box(raw)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write("x\n")
                self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


if __name__ == "__main__":
    repl = SenseiRepl()
    print("=== doctor ===")
    print(repl.send("doctor"))
    print("=== sessions list ===")
    print(repl.send("sessions list"))
    repl.close()
