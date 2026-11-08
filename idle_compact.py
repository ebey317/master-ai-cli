"""Safe snapshot handling for idle conversation compaction.

The controller never touches the active history from a worker thread.  The
caller supplies an isolated cancellable local summarizer and drives the timer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

IDLE_COMPACTION_LOCAL_ONLY = True
KEEP_RECENT = 12


def _cancellable_command(
    args: list[str], payload: str, cancel: threading.Event, timeout: float
) -> str:
    """Stop only the child request; never unload models or stop Ollama."""
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
    )
    deadline = time.monotonic() + timeout
    try:
        first = True
        while not cancel.is_set() and time.monotonic() < deadline:
            try:
                output, _ = process.communicate(
                    input=payload if first else None, timeout=0.2
                )
                return output if process.returncode == 0 else ""
            except subprocess.TimeoutExpired:
                first = False
        return ""
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def local_summarizer(model: str, messages: list[dict], cancel: threading.Event) -> str:
    """Use normal ask_local only for a verified on-disk Ollama model."""
    if (
        not IDLE_COMPACTION_LOCAL_ONLY
        or not model
        or ":cloud" in model.lower()
        or "::" in model
        or cancel.is_set()
    ):
        return ""
    modelfile = _cancellable_command(
        ["ollama", "show", model, "--modelfile"], "", cancel, 5
    )
    source = next(
        (
            line[5:].strip()
            for line in modelfile.splitlines()
            if line.startswith("FROM ")
        ),
        "",
    )
    if not source.startswith("/") or not os.path.isfile(source) or cancel.is_set():
        return ""
    text = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in messages)
    prompt = (
        "Summarize this conversation for an assistant continuing the same task. "
        "Preserve facts, decisions, paths and unresolved work.\n\n" + text
    )
    script = (
        "import json,sys; import master_ai; data=json.load(sys.stdin); "
        "result=master_ai.ask_local([{'role':'user','content':data['prompt']}], model=data['model']); "
        "print('IDLE_SUMMARY_RESULT='+json.dumps(result))"
    )
    output = _cancellable_command(
        [sys.executable, "-c", script],
        json.dumps({"prompt": prompt, "model": model}),
        cancel,
        120,
    )
    marker = next(
        (
            line[len("IDLE_SUMMARY_RESULT=") :]
            for line in reversed(output.splitlines())
            if line.startswith("IDLE_SUMMARY_RESULT=")
        ),
        "",
    )
    try:
        value = json.loads(marker)
        return value if isinstance(value, str) else ""
    except ValueError:
        return ""


def fingerprint(history: list[dict[str, Any]]) -> str:
    """Include every field, including content, tool IDs and message order."""
    data = json.dumps(
        history, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def split_history(
    history: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]] | None:
    # Reordering a mid-conversation system message would change its meaning.
    seen_conversation = False
    for message in history:
        if message.get("role") == "system":
            if seen_conversation:
                return None
        else:
            seen_conversation = True
    systems = [message for message in history if message.get("role") == "system"]
    conversation = [message for message in history if message.get("role") != "system"]
    if len(conversation) <= KEEP_RECENT:
        return None
    boundary = len(conversation) - KEEP_RECENT
    # A tool response must remain with the assistant tool call which caused it.
    while boundary and conversation[boundary].get("role") == "tool":
        boundary -= 1
    if not boundary:
        return None
    return systems, conversation[:boundary], conversation[boundary:]


class IdleCompactor:
    """Prepare once when idle, then conditionally apply on the owner thread."""

    def __init__(self, summarize: Callable, clock: Callable = time.monotonic):
        self.summarize = summarize
        self.clock = clock
        self._lock = threading.Lock()
        self._generation = 0
        self._timer: threading.Timer | None = None
        self._cancel: threading.Event | None = None
        self._result: tuple | None = None
        self._due = 0.0

    def arm(
        self,
        history: list[dict],
        session: str,
        delay: float,
        *,
        trusted: bool,
        busy: bool = False,
    ) -> None:
        self.cancel()
        if delay <= 0 or not trusted or busy:
            return
        try:
            snapshot = copy.deepcopy(history)
            parts = split_history(snapshot)
            if parts is None:
                return
            token = fingerprint(snapshot)
        except (TypeError, ValueError, RecursionError):
            return
        with self._lock:
            generation = self._generation
            event = threading.Event()
            self._cancel = event
            self._due = self.clock() + delay
            timer = threading.Timer(
                delay, self._prepare, args=(snapshot, session, token, generation, event)
            )
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _prepare(
        self,
        snapshot: list[dict],
        session: str,
        token: str,
        generation: int,
        cancel: threading.Event,
    ) -> None:
        if cancel.is_set() or self.clock() < self._due:
            return
        parts = split_history(snapshot)
        if parts is None:
            return
        try:
            summary = self.summarize(copy.deepcopy(parts[1]), cancel)
        except Exception:
            return
        if not isinstance(summary, str) or not summary.strip() or cancel.is_set():
            return
        with self._lock:
            if generation == self._generation and not cancel.is_set():
                self._result = (session, token, parts, summary.strip())

    def consume(self, history: list[dict], session: str) -> bool:
        with self._lock:
            result = self._result
        self.cancel()
        try:
            current = fingerprint(history)
        except (TypeError, ValueError, RecursionError):
            return False
        if not result or result[0] != session or result[1] != current:
            return False
        _, _, (systems, older, recent), summary = result
        note = {
            "role": "user",
            "content": f"[CONTEXT COMPACTED — {len(older)} earlier message(s) condensed]\n{summary}",
        }
        history[:] = systems + [note] + recent
        return True

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            timer = self._timer
            if timer:
                timer.cancel()
            if self._cancel:
                self._cancel.set()
            self._timer = None
            self._cancel = None
            self._result = None
        # A running request must close before the next turn is allowed to start.
        if timer and timer is not threading.current_thread():
            timer.join()
