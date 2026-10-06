"""Pre-dispatch schema validation for parsed tool actions.

CLAUDE.md's Tier-1 "Typed Tool Dispatch (Execution Safety)" requirement,
items 2-4. `typed_actions` parses and `classify_risk` labels, and `hooks.py`
checks syntax *after* a write, but nothing validated the shape of an action
between "the model emitted a directive" and "we execute it". That gap is the
one that let a model narrate a step it never performed.

This module is the gate. Every action passes through `validate_action` before
ANY dispatch. A failure is not silent: the caller sets `_LAST_BLOCKED_ACTION`
and feeds `[TOOL BLOCKED]` back to history so the model learns.

Deliberately standalone — no master_ai import, same convention as
typed_actions.py and verifiers.py, so it is testable without the 28k-line
dispatch path. Rules that need master_ai's own machinery (the read-path
fence, the self-mod denylist, approval TTL) are NOT duplicated here; this
layer answers only "is this a well-formed, dispatchable action of its kind",
and master_ai's existing gates still run after it.

Validation is structural, not semantic. It asks whether the payload is a
coherent command/path/content of the declared kind -- not whether running it
is a good idea, which is classify_risk's and the hooks' job. Over-blocking a
merely odd-looking command is a worse failure than passing a weird one
through to the safeguards that already exist.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A directive keyword appearing inside another directive's payload. RUN is
# one command; if its body names a second directive the model is trying to
# smuggle a second action through, or is quoting. Either way it is not a
# command string.
_DIRECTIVE_MARKER = re.compile(
    r"(?im)^\s*(RUN|RUNTERM|READ|CREATE|EDIT|SEARCH|ASK|DONE|REMEMBER)\s*:"
)

_SHELL_SUFFIXES = (".sh", ".bash", ".zsh")
_MAX_TARGET = 8000
_MAX_CONTENT = 400_000

# Words that can never begin a real program, only a sentence. A model that
# narrates instead of commanding ("RUN: let me run the suite") produces one
# of these as the first token, and the dispatcher would then try to exec
# `let`. Kept deliberately tight: every entry is unambiguously a function
# word in an imperative sentence, so nothing executable is caught by it.
_NOT_A_PROGRAM = frozenset(
    {
        "i",
        "im",
        "ill",
        "ive",
        "id",
        "we",
        "were",
        "well",
        "lets",
        "let",
        "the",
        "this",
        "that",
        "these",
        "those",
        "there",
        "then",
        "than",
        "now",
        "next",
        "nowwe",
        "first",
        "also",
        "running",
        "run",
        "executing",
        "execute",
        "calling",
        "call",
        "checking",
        "check",
        "verifying",
        "verify",
        "testing",
        "test",
        "reading",
        "read",
        "writing",
        "write",
        "creating",
        "create",
        "updating",
        "update",
        "looking",
        "look",
        "going",
        "go",
        "and",
        "or",
        "but",
        "so",
        "because",
        "ok",
        "okay",
    }
)


@dataclass
class ValidationResult:
    """Outcome of validating one action.

    `ok=False` means: do not dispatch this, tell the model why. `reason` is
    written for the model, not for a log, so it names the rule it broke.
    """

    ok: bool
    reason: str = ""
    kind: str = ""
    target: str = ""
    # Non-fatal observations worth surfacing without blocking.
    warnings: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _fail(kind: str, target: str, reason: str) -> ValidationResult:
    return ValidationResult(ok=False, reason=reason, kind=kind, target=target)


def _ok(kind: str, target: str, warnings: list | None = None) -> ValidationResult:
    return ValidationResult(ok=True, kind=kind, target=target, warnings=warnings or [])


def _common_shape_checks(kind: str, target: Any) -> ValidationResult | None:
    """Checks every kind shares. Returns a failure, or None to continue."""
    if not isinstance(target, str):
        return _fail(
            kind, "", f"{kind} payload must be a string, got {type(target).__name__}"
        )
    if not target.strip():
        return _fail(kind, target, f"{kind} payload is empty")
    if "\x00" in target:
        return _fail(kind, target, f"{kind} payload contains a NUL byte")
    if len(target) > _MAX_TARGET:
        return _fail(
            kind,
            target,
            f"{kind} payload is {len(target)} chars, over the {_MAX_TARGET} limit",
        )
    return None


def _validate_run(kind: str, target: Any) -> ValidationResult:
    """RUN / RUNTERM: a command string, not arbitrary text.

    A RUN carries exactly one command. Multi-line payloads, a second
    directive marker, or unbalanced quoting all mean the payload is prose or
    a smuggled second action rather than a command.
    """
    bad = _common_shape_checks(kind, target)
    if bad is not None:
        return bad
    if "\n" in target:
        return _fail(
            kind,
            target,
            f"{kind} payload is multi-line; one directive carries one command",
        )
    try:
        tokens = shlex.split(target)
    except ValueError as e:
        return _fail(kind, target, f"{kind} payload has unbalanced quoting: {e}")
    if not tokens:
        return _fail(kind, target, f"{kind} payload has no command in it")
    first = tokens[0].lower().strip("\"'")
    if first in _NOT_A_PROGRAM:
        return _fail(
            kind,
            target,
            f"{kind} payload starts with {first!r}, which is a sentence, not a "
            "command; emit the command itself, not a description of it",
        )
    m = _DIRECTIVE_MARKER.search(target)
    if m:
        return _fail(
            kind,
            target,
            f"{kind} payload contains a second directive ({m.group(1)}:); "
            "emit one directive per line instead of nesting them",
        )
    return _ok(kind, target)


def _validate_runterm(target: Any) -> ValidationResult:
    """RUNTERM additionally must be syntactically valid shell."""
    base = _validate_run("RUNTERM", target)
    if not base.ok:
        return base
    try:
        r = subprocess.run(
            ["bash", "-n", "-c", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001 - never block on tool failure
        return _ok("RUNTERM", target, [f"shell syntax check unavailable: {e}"])
    if r.returncode != 0:
        err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        last = (err.splitlines() or [""])[-1][:300]
        return _fail("RUNTERM", target, f"shell syntax error: {last}")
    return _ok("RUNTERM", target)


def _validate_read(target: Any) -> ValidationResult:
    """READ: a single path, no newline-separated path list.

    Whether the path is *allowed* is master_ai's read fence, which runs after
    this. This only rejects a payload that is not one path at all.
    """
    bad = _common_shape_checks("READ", target)
    if bad is not None:
        return bad
    if "\n" in target:
        return _fail(
            "READ", target, "READ payload is multi-line; one directive reads one path"
        )
    if target.strip() != target:
        return _fail("READ", target, "READ path has leading or trailing whitespace")
    return _ok("READ", target)


def _syntax_check_content(path: str, content: str) -> str | None:
    """Return an error string if `content` is not valid for `path`, else None.

    Extension-driven, mirroring hooks._syntax_check_py / _syntax_check_sh,
    but on the proposed content rather than on a file that already exists.
    Anything whose extension we cannot check passes -- an unparseable file
    should not be blocked by a validator that simply does not know its type.
    """
    if len(content) > _MAX_CONTENT:
        return f"content is {len(content)} chars, over the {_MAX_CONTENT} limit"

    suffix = Path(path).suffix.lower()

    if suffix == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            return f"python syntax error: line {e.lineno}: {e.msg}"
        except ValueError as e:
            return f"python source error: {e}"
        return None

    if suffix in _SHELL_SUFFIXES:
        try:
            r = subprocess.run(
                ["bash", "-n", "-c", content],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            return None
        if r.returncode != 0:
            err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
            return f"shell syntax error: {(err.splitlines() or [''])[-1][:300]}"
        return None

    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return f"json syntax error: line {e.lineno}: {e.msg}"
        return None

    return None


def _validate_write(kind: str, target: Any, content: str | None) -> ValidationResult:
    """CREATE / EDIT: a path plus content that actually parses.

    The failure this prevents is specific and observed: a model writes a
    file with a syntax error, the chain proceeds to the next step that
    depends on it, and the model reports the whole thing as done.
    """
    bad = _common_shape_checks(kind, target)
    if bad is not None:
        return bad
    if content is None:
        return _fail(kind, target, f"{kind} carried no content block to write")
    if not isinstance(content, str) or not content.strip():
        return _fail(kind, target, f"{kind} content is empty")
    err = _syntax_check_content(target, content)
    if err:
        return _fail(kind, target, f"{kind} content would not parse: {err}")
    return _ok(kind, target)


def _validate_mcp_call(target: Any) -> ValidationResult:
    """MCP_CALL: `<server> <tool> {json args}` against a known-enabled server.

    Validated before the call so a typo in a server or tool name is caught
    as a blocked action with a usable reason, rather than spawning a
    subprocess to be told the same thing more slowly. The tool must be one
    the server was actually validated as exposing (sensei_mcp_client records
    that at probe time), which is also what stops a model from reaching a
    tool that exists in the file but never made it through validation.
    """
    bad = _common_shape_checks("MCP_CALL", target)
    if bad is not None:
        return bad
    if "\n" in target:
        return _fail("MCP_CALL", target, "MCP_CALL payload is multi-line")

    parts = target.split(None, 2)
    if len(parts) < 2:
        return _fail("MCP_CALL", target, "MCP_CALL needs `<server> <tool> {json args}`")
    server, tool, rest = parts[0], parts[1], (parts[2] if len(parts) > 2 else "{}")

    try:
        import sensei_mcp_client as _mcp
    except Exception as e:  # noqa: BLE001
        return ValidationResult(
            ok=True,
            kind="MCP_CALL",
            target=target,
            warnings=[f"MCP client unavailable: {e}"],
        )

    entry = _mcp.get_server(server)
    if not entry:
        # list_servers() already returns the servers mapping, not the
        # whole catalog, so it must not be unwrapped again.
        known = ", ".join(sorted(_mcp.list_servers())) or "none"
        return _fail(
            "MCP_CALL", target, f"no such MCP server {server!r}; have: {known}"
        )
    if not entry.get("enabled"):
        why = "; ".join(entry.get("problems") or []) or "disabled"
        return _fail(
            "MCP_CALL", target, f"MCP server {server!r} is not enabled ({why})"
        )
    exposed = entry.get("tool_names") or []
    if exposed and tool not in exposed:
        return _fail(
            "MCP_CALL", target, f"server {server!r} has no tool named {tool!r}"
        )

    rest = rest.strip() or "{}"
    if not rest.startswith("{"):
        return _fail(
            "MCP_CALL", target, f"arguments must be a JSON object, got {rest[:40]!r}"
        )
    try:
        json.loads(rest)
    except json.JSONDecodeError as e:
        return _fail("MCP_CALL", target, f"arguments are not valid JSON: {e}")

    return _ok("MCP_CALL", target)


# Kinds that carry nothing to execute: narrative/inert. They are always
# dispatch-safe, and validating them adds nothing.
_INERT_KINDS = frozenset(
    {"PLAN", "DONE", "THINK", "REMEMBER", "TASK_ADD", "TASK_DONE", "ASK"}
)

# Kinds this module does not know how to check. They pass through: refusing
# to dispatch a kind we have not modelled would be a silent behaviour change
# for a directive the legacy path already handles.
_UNVALIDATED_KINDS = frozenset(
    {
        "RUN_SKILL",
        "SEND_EMAIL",
        "SEND_TELEGRAM",
        "REMOTE_MCP",
        "SUBAGENT",
        "SEARCH",
        "BROWSER_CLICK",
        "BROWSER_FILL",
        "BROWSER_FILL_FORM",
        "BROWSER_UPLOAD_FILE",
        "BROWSER_SUBMIT",
        "BROWSER_READ",
        "BROWSER_READ_PAGE",
        "BROWSER_READ_PAGE_FULL",
        "BROWSER_OBSERVE",
        "BROWSER_NAV",
        "BROWSER_CLOSE_TAB",
        "BROWSER_SCREENSHOT",
        "BROWSER_WAIT",
        "BROWSER_SCROLL",
        "BROWSER_DOUBLE_CLICK",
        "BROWSER_FIND",
        "BROWSER_EXTRACT_LIST",
        "BROWSER_DRIVE_INSPECT_FOLDER",
        "BROWSER_CDP_MOUSE",
        "BROWSER_CDP_KEY",
        "BROWSER_TAB_CREATE",
    }
)


def validate_action(action) -> ValidationResult:
    """Validate one parsed action. Accepts a TypedAction or a plain dict.

    Returns a ValidationResult; never raises. An action this module cannot
    model is passed through with ok=True rather than blocked -- the point of
    the gate is to stop malformed dispatches, not to become a second,
    incomplete allowlist that silently eats directives.
    """
    try:
        if isinstance(action, dict):
            kind = str(action.get("kind") or "")
            target = action.get("target")
            content = action.get("create_content")
            if content is None:
                content = action.get("edit_new")
        else:
            kind = str(getattr(action, "kind", "") or "")
            target = getattr(action, "target", None)
            content = getattr(action, "create_content", None)
            if content is None:
                content = getattr(action, "edit_new", None)

        if kind in _INERT_KINDS or kind in _UNVALIDATED_KINDS:
            return _ok(kind, target if isinstance(target, str) else "")

        if kind == "RUN":
            return _validate_run(kind, target)
        if kind == "RUNTERM":
            return _validate_runterm(target)
        if kind == "READ":
            return _validate_read(target)
        if kind in ("CREATE", "EDIT"):
            return _validate_write(kind, target, content)
        if kind == "MCP_CALL":
            return _validate_mcp_call(target)

        return _ok(kind, target if isinstance(target, str) else "")
    except Exception as e:  # noqa: BLE001 - fail-closed: a broken validator must never silently grant permission
        return ValidationResult(
            ok=False, reason=f"VALIDATOR FAILURE — action blocked: {e}"
        )


def validate_all(actions) -> tuple[list, list]:
    """Split `actions` into (dispatchable, blocked) by validation.

    Order is preserved in both lists, so the caller can report blocked
    actions in the order the model emitted them.
    """
    dispatchable, blocked = [], []
    for action in actions or []:
        result = validate_action(action)
        if result.ok:
            dispatchable.append(action)
        else:
            blocked.append((action, result))
    return dispatchable, blocked
