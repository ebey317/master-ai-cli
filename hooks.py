"""Master AI hook system (P1.4).

Hooks fire at lifecycle moments around typed actions and can BLOCK an
action by returning a FireResult with blocked=True. Built-in safe hooks
ship enabled (syntax check on post_edit / post_create, secret scan on
pre_create). User-defined shell hooks load from ~/.master_ai_hooks.json
and DEFAULT TO DISABLED — they ride the same fire pipeline once enabled.

Public API:
    fire(kind, target, *, action=None) -> FireResult
    list_hooks() -> list[Hook]
    enable(hook_id) -> bool
    disable(hook_id) -> bool
    KINDS                — frozenset of recognized hook kinds

Hook kinds (lifecycle phase + directive kind):
    pre_run | post_run | pre_runterm | post_runterm
    pre_read | post_read
    pre_create | post_create
    pre_edit | post_edit

A blocked FireResult carries `reason` (one-line description) and
`hook_id`. The master_ai integration writes this to history through the
existing [HOOK BLOCKED] message path. Hook execution errors are NOT
blocks — they're hook bugs, logged and skipped.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

HOOKS_CONFIG = Path.home() / ".master_ai_hooks.json"


KINDS = frozenset({
    "pre_run", "post_run",
    "pre_runterm", "post_runterm",
    "pre_read", "post_read",
    "pre_create", "post_create",
    "pre_edit", "post_edit",
    "on_blocked",  # 2026-05-11: fires when ANY action lands BLOCKED.
                   # Sibling to pre_/post_ — observes a state outcome,
                   # not a lifecycle phase. Used by auto-extract-lesson.
    "turn_answer_start",  # Phase 5.6 (2026-05-15): fires once per /chat
                          # response right before the assistant reply text
                          # is returned to the extension. target = the
                          # reply string. Subscribers can record metrics,
                          # tee to a transcript log, or trigger UI hints.
                          # NEVER blocks — observers only.
})


@dataclass
class FireResult:
    blocked: bool = False
    reason: str = ""
    hook_id: str = ""


@dataclass
class Hook:
    id: str
    kind: str
    fn: Optional[Callable] = None       # built-in: Python callable
    shell: Optional[str] = None         # user-defined: shell template
    enabled: bool = True
    timeout_s: int = 30
    source: str = "builtin"


class HookRegistry:
    def __init__(self):
        self._hooks: list[Hook] = []

    def register(self, hook: Hook) -> None:
        if hook.kind not in KINDS:
            raise ValueError(
                f"unknown hook kind {hook.kind!r}; expected one of {sorted(KINDS)}"
            )
        self._hooks.append(hook)

    def list_hooks(self) -> list[Hook]:
        return list(self._hooks)

    def enabled_for(self, kind: str) -> list[Hook]:
        return [h for h in self._hooks if h.kind == kind and h.enabled]

    def find(self, hook_id: str) -> Optional[Hook]:
        for h in self._hooks:
            if h.id == hook_id:
                return h
        return None

    def enable(self, hook_id: str) -> bool:
        h = self.find(hook_id)
        if h is None:
            return False
        h.enabled = True
        return True

    def disable(self, hook_id: str) -> bool:
        h = self.find(hook_id)
        if h is None:
            return False
        h.enabled = False
        return True

    def fire(self, kind: str, target: Any, *, action: Any = None) -> FireResult:
        if kind not in KINDS:
            return FireResult(hook_id="<unknown-kind>")
        for h in self.enabled_for(kind):
            try:
                if h.fn is not None:
                    res = h.fn(target, action=action)
                    if isinstance(res, FireResult) and res.blocked:
                        return FireResult(blocked=True,
                                          reason=res.reason or "blocked",
                                          hook_id=h.id)
                elif h.shell:
                    cmd = h.shell.replace("{target}", str(target))
                    try:
                        r = subprocess.run(cmd, shell=True, capture_output=True,
                                           text=True, timeout=h.timeout_s)
                    except subprocess.TimeoutExpired:
                        continue  # hook timeout is NOT a block
                    if r.returncode != 0:
                        reason_src = (r.stderr or r.stdout
                                       or f"exit {r.returncode}").strip()
                        first_line = (reason_src.splitlines() or [""])[0][:200]
                        return FireResult(blocked=True,
                                          reason=f"{h.id}: {first_line}",
                                          hook_id=h.id)
            except Exception:
                # Hook implementation errors are NOT blocks — they're bugs
                # in the hook itself. Log via the caller's mechanism (this
                # module has no logger of its own); fall through.
                continue
        return FireResult()


# ── Built-in hook implementations ────────────────────────────────────

def _syntax_check_py(target, action=None) -> FireResult:
    """post_edit / post_create hook. Runs ``python3 -m py_compile`` on
    .py targets; blocks on syntax errors. Non-.py paths pass through.
    """
    target = str(target or "")
    if not target.endswith(".py"):
        return FireResult()
    if not Path(target).is_file():
        return FireResult()
    try:
        r = subprocess.run(["python3", "-m", "py_compile", target],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return FireResult()  # don't block on tool failure
    if r.returncode == 0:
        return FireResult()
    err_src = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
    last_line = (err_src.splitlines() or [""])[-1][:300]
    return FireResult(blocked=True,
                      reason=f"python syntax error: {last_line}",
                      hook_id="syntax-check-py")


def _syntax_check_sh(target, action=None) -> FireResult:
    """P1.6 post_edit / post_create hook. Runs ``bash -n`` on shell
    script targets (.sh / .bash / .zsh). Non-shell paths pass through.
    Coding-task verification gate — caught before the next directive
    chain step would try to execute the broken script.
    """
    target = str(target or "")
    if not target.endswith((".sh", ".bash", ".zsh")):
        return FireResult()
    if not Path(target).is_file():
        return FireResult()
    try:
        r = subprocess.run(["bash", "-n", target],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return FireResult()
    if r.returncode == 0:
        return FireResult()
    err_src = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
    first_line = (err_src.splitlines() or [""])[0][:300]
    return FireResult(blocked=True,
                      reason=f"shell syntax error: {first_line}",
                      hook_id="syntax-check-sh")


_SECRET_PATTERNS = [
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'),                            "AWS access key id"),
    (re.compile(r'\bASIA[0-9A-Z]{16}\b'),                            "AWS temporary key"),
    (re.compile(r'-----BEGIN (?:RSA |OPENSSH |DSA |EC )?PRIVATE KEY-----'),
                                                                     "private key block"),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b'),                  "GitHub token"),
    (re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}\b'),                "Slack token"),
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'),                          "OpenAI-style API key"),
]


def _auto_extract_lesson(target, action=None) -> FireResult:
    """on_blocked hook — close the self-teaching loop.

    When an action lands in BLOCKED status (RUN exit-127 hallucination,
    fence refusal, hook block, etc.), this fires a quick background
    Ollama call asking the small local model for a one-line lesson and
    stores it via master_ai.confirm_remember() on success. Async — never
    blocks the user. Rate-limited to avoid burning the CPU box on long
    chains of blocked actions.

    Skips extraction for:
      - policy / fence blocks (security guardrails — no useful lesson)
      - empty-payload blocks (parser cleanup, not user-facing)
      - calls beyond the per-session cap

    The lesson goes through confirm_remember() so it gets the same
    validation (200-char cap, dedup, MEMORY_FILE write) the user's
    `remember:` command and the model's REMEMBER: directive use.
    """
    if not isinstance(action, dict):
        return FireResult()
    kind = (action.get("kind") or "").strip()
    blocked_target = (action.get("target") or "").strip() or str(target or "")
    reason = (action.get("reason") or "").strip()
    audit_kind = (action.get("audit_kind") or "").upper()
    if not blocked_target or not reason:
        return FireResult()
    # Skip uninteresting / no-lesson cases
    if "POLICY" in audit_kind or "FENCE" in audit_kind:
        return FireResult()
    if "EMPTY" in audit_kind or "MISSING" in audit_kind:
        return FireResult()
    # Rate limit
    global _EXTRACT_COUNT_SESSION
    with _EXTRACT_LOCK:
        if _EXTRACT_COUNT_SESSION >= _EXTRACT_MAX_PER_SESSION:
            return FireResult()
        _EXTRACT_COUNT_SESSION += 1
    # Fire async so the user isn't blocked
    import threading
    threading.Thread(
        target=_extract_lesson_worker,
        args=(kind, blocked_target, reason),
        daemon=True,
        name="auto-extract-lesson",
    ).start()
    return FireResult()


# Rate-limit globals for the auto-extract hook
import threading as _threading
_EXTRACT_LOCK = _threading.Lock()
_EXTRACT_COUNT_SESSION = 0
_EXTRACT_MAX_PER_SESSION = 10
_EXTRACT_LESSON_MODEL = "qwen2.5:3b"   # fast small model; falls back below
_EXTRACT_LESSON_TIMEOUT_S = 12
_EXTRACT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


def _extract_lesson_worker(kind: str, target: str, reason: str) -> None:
    """Background worker: asks the small local model for a one-line
    lesson from a blocked action, stores it via confirm_remember() if
    the model returns something usable. All exceptions swallowed —
    failure is invisible by design."""
    prompt = (
        f"A {kind or 'tool'} action was BLOCKED by safeguards.\n"
        f"Action: {target[:200]}\n"
        f"Reason: {reason[:200]}\n\n"
        "If there is a one-line factual lesson worth remembering so "
        "this doesn't repeat (e.g. \"X isn't installed on this box, "
        "use Y\"), reply with JUST the lesson, no preamble, max 150 "
        "chars.\n"
        "If there is no useful generic lesson, reply with exactly: SKIP"
    )
    try:
        import urllib.request as _ureq
        import json as _json
        body = _json.dumps({
            "model": _EXTRACT_LESSON_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 80, "temperature": 0.2},
        }).encode()
        req = _ureq.Request(
            _EXTRACT_OLLAMA_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ureq.urlopen(req, timeout=_EXTRACT_LESSON_TIMEOUT_S) as r:
            data = _json.loads(r.read())
    except Exception:
        return
    lesson = (data.get("response") or "").strip()
    if not lesson:
        return
    lesson_line = (lesson.splitlines() or [""])[0].strip()
    if lesson_line.upper().strip().rstrip(".!:") == "SKIP":
        return
    # Strip directive prefix if the model wrapped it
    lesson_line = re.sub(r'^\s*REMEMBER:\s*', '', lesson_line, flags=re.IGNORECASE).strip()
    # Sanity bounds
    if len(lesson_line) < 10 or len(lesson_line) > 200:
        return
    # Store via master_ai's path so it goes through confirm_remember's
    # validation + dedup + MEMORY_FILE write.
    try:
        import sys
        import os as _os
        _scripts = _os.path.expanduser("~/scripts")
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        import master_ai as _ma
        _ma.confirm_remember(lesson_line)
    except Exception:
        pass


def _secret_scan(target, action=None) -> FireResult:
    """pre_create hook. Scans the CREATE content for secret patterns.
    The action argument (if provided) carries .create_content; otherwise
    if ``target`` itself is a string and not a real existing file path,
    it's treated as content. Blocks on any match.
    """
    text = ""
    if action is not None:
        text = getattr(action, "create_content", None) or ""
    if not text and isinstance(target, str):
        # Heuristic: if it has newlines or is long, treat as content.
        if "\n" in target or len(target) > 200:
            text = target
    if not text:
        return FireResult()
    for pat, label in _SECRET_PATTERNS:
        if pat.search(text):
            return FireResult(blocked=True,
                              reason=f"detected {label} in CREATE content",
                              hook_id="secret-scan")
    return FireResult()


def _syntax_check_run_cmd(cmd, action=None) -> FireResult:
    """pre_run / pre_runterm hook. Validates a model-authored shell string
    before it reaches subprocess. Checks shlex tokenization and runs
    ``bash -n`` on the command. Blocks if either fails so malformed
    directives (e.g. unbalanced backticks, stray XML tags) never execute.
    """
    cmd = str(cmd or "").strip()
    if not cmd:
        return FireResult()
    try:
        shlex.split(cmd)
    except ValueError as e:
        return FireResult(blocked=True,
                          reason=f"shell tokenization failed: {e}",
                          hook_id="syntax-check-run")
    try:
        r = subprocess.run(["bash", "-n", "-c", cmd],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return FireResult()
    if r.returncode == 0:
        return FireResult()
    err_src = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
    first_line = (err_src.splitlines() or [""])[0][:300]
    return FireResult(blocked=True,
                      reason=f"shell syntax error: {first_line}",
                      hook_id="syntax-check-run")


# ── Module-level registry ─────────────────────────────────────────────

_REGISTRY = HookRegistry()
_REGISTRY.register(Hook(id="syntax-check-py-post-edit", kind="post_edit",
                        fn=_syntax_check_py, source="builtin"))
_REGISTRY.register(Hook(id="syntax-check-py-post-create", kind="post_create",
                        fn=_syntax_check_py, source="builtin"))
_REGISTRY.register(Hook(id="syntax-check-sh-post-edit", kind="post_edit",
                        fn=_syntax_check_sh, source="builtin"))
_REGISTRY.register(Hook(id="syntax-check-sh-post-create", kind="post_create",
                        fn=_syntax_check_sh, source="builtin"))
_REGISTRY.register(Hook(id="secret-scan-pre-create", kind="pre_create",
                        fn=_secret_scan, source="builtin"))
_REGISTRY.register(Hook(id="syntax-check-run-pre-run", kind="pre_run",
                        fn=_syntax_check_run_cmd, source="builtin"))
_REGISTRY.register(Hook(id="syntax-check-run-pre-runterm", kind="pre_runterm",
                        fn=_syntax_check_run_cmd, source="builtin"))
# 2026-05-11: auto-extract-lesson hook — closes the REMEMBER:
# self-teaching loop. When an action lands BLOCKED, an async worker
# asks the small local model for a one-line lesson and stores it via
# confirm_remember(). Default-enabled; user can disable via
# `hooks disable auto-extract-lesson` (P1.4 hook command).
_REGISTRY.register(Hook(id="auto-extract-lesson", kind="on_blocked",
                        fn=_auto_extract_lesson, source="builtin"))


def _load_user_hooks(path: Path = HOOKS_CONFIG) -> int:
    """Load user-defined hooks from JSON config. Returns count loaded.
    User hooks default to enabled=False unless the file explicitly sets
    enabled=true. Malformed entries are skipped without raising.
    """
    if not path or not Path(path).is_file():
        return 0
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return 0
    raw = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return 0
    loaded = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        hid = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip().lower()
        shell = entry.get("shell")
        if not hid or kind not in KINDS or not shell:
            continue
        enabled = bool(entry.get("enabled", False))
        timeout = int(entry.get("timeout_s") or 30)
        try:
            _REGISTRY.register(Hook(id=hid, kind=kind, shell=shell,
                                    enabled=enabled, timeout_s=timeout,
                                    source="user"))
            loaded += 1
        except ValueError:
            continue
    return loaded


# Load at import time. Failures are swallowed inside _load_user_hooks.
_load_user_hooks()


def fire(kind: str, target: Any, *, action: Any = None) -> FireResult:
    return _REGISTRY.fire(kind, target, action=action)


def list_hooks() -> list[Hook]:
    return _REGISTRY.list_hooks()


def enable(hook_id: str) -> bool:
    return _REGISTRY.enable(hook_id)


def disable(hook_id: str) -> bool:
    return _REGISTRY.disable(hook_id)


def reload_user_hooks(path: Path = HOOKS_CONFIG) -> int:
    """Drop all user-sourced hooks and reload from JSON. Built-in hooks
    are untouched. Returns count of user hooks loaded."""
    _REGISTRY._hooks = [h for h in _REGISTRY._hooks if h.source == "builtin"]
    return _load_user_hooks(path)
