"""Strict pre-flight validator for directives emitted by the model.

Wraps the existing permissive regex parser in master_ai.process_reply with a
"strict regex with reject-and-report" layer per the parity-rebuild plan
(~/.claude/plans/wise-petting-moth.md). The validator runs BEFORE dispatch
and BEFORE the existing structure-specific repair paths (master_ai.py:9468
for CREATE, 9489 for EDIT). When a directive is rejected, the caller
appends a `[VALIDATOR REJECT: {kind} — {reason}]` line to the conversation
history (user role, so the model sees it in the next round's
[PREVIOUS ROUND RESULTS]) and writes an audit row to
~/.master_ai_audit_typed.jsonl for offline tuning.

The validator is a NEW layer added BEFORE the existing inline guards in
confirm_run / confirm_runterm / confirm_create / confirm_edit / confirm_send_email.
Those guards stay as defense-in-depth.

Out of scope per the plan: BROWSER_* (lives in stt_server.py chrome_extension
branch, not process_reply), RUN_SKILL (parsed upstream by
_run_skill_reply_from_reply), REMEMBER (not execution-safety critical),
DONE / ASK (no target requirements).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple


_VALIDATOR_AUDIT_PATH = os.path.expanduser("~/.master_ai_audit_typed.jsonl")
_VALIDATOR_REJECT_PREFIX = "[VALIDATOR REJECT:"

# Path-traversal heuristic for READ: collapse user expansion + normalize, then
# look for a `..` segment that escapes the original anchor. Matches the same
# class of pattern the existing read_fence guards block at master_ai.py:9556,
# but surfaces at parse-time instead of dispatch-time so the model gets
# uniform reject-and-report.
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")

# SEND_EMAIL spec parser mirrors the one inline at master_ai.py:9311 — same
# key=value | key="value" | key='value' grammar. Duplicated here (not
# imported) to keep the validator decoupled from master_ai's internals.
_SEND_EMAIL_KV_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")


def validate_directive(
    kind: str,
    target: str,
    body: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (valid, reason). reason is "" on valid, a one-sentence
    human-readable explanation on reject (surfaced verbatim to the model
    via the [VALIDATOR REJECT] history line so it can self-correct).

    kind is the directive token (RUN, RUNTERM, READ, SEND_EMAIL, CREATE,
    EDIT). target is the post-`<kind>:` payload. body is the multi-line
    block content for CREATE/EDIT (None for single-line directives, or for
    CREATE/EDIT when the body wasn't extracted — that's the malformed
    case the validator catches).
    """
    k = (kind or "").upper().strip()
    t = (target or "")

    if k in ("RUN", "RUNTERM"):
        # Empty / whitespace-only command. The existing comprehension at
        # master_ai.py:9298/9300 filters these with a trailing `if c`, but
        # the validator now runs BEFORE that filter (per the rewrite to an
        # explicit loop) so empty RUN reaches us and gets a reject the
        # model can see, instead of a silent drop.
        if not t.strip():
            return False, "empty command (RUN/RUNTERM target was blank)"
        # Naked operators with no command — `; ls` or `| cat` etc.
        # _is_noop_cmd at master_ai.py:7328 already treats some as no-ops
        # but the validator surfaces them as a uniform reject for visibility.
        stripped = t.strip()
        if stripped in (";", "|", "&", "&&", "||", ":"):
            return False, "naked shell operator with no command"
        return True, ""

    if k == "READ":
        if not t.strip():
            return False, "empty path (READ target was blank)"
        # Normalize then check for traversal segments.
        path = os.path.normpath(os.path.expanduser(t.strip()))
        if _PATH_TRAVERSAL_RE.search(path):
            return False, "path traversal segment rejected (.. in normalized READ path)"
        return True, ""

    if k == "SEND_EMAIL":
        if not t.strip():
            return False, "empty SEND_EMAIL payload"
        spec = {}
        for m in _SEND_EMAIL_KV_RE.finditer(t):
            key = m.group(1).lower()
            val = (
                m.group(2)
                if m.group(2) is not None
                else (m.group(3) if m.group(3) is not None else m.group(4))
            )
            spec[key] = val
        if not spec.get("to"):
            return False, "SEND_EMAIL missing to= field"
        if not spec.get("subject"):
            return False, "SEND_EMAIL missing subject= field"
        return True, ""

    if k == "CREATE":
        if not t.strip():
            return False, "empty filepath (CREATE target was blank)"
        if body is None or not body.strip():
            return False, "CREATE missing <<<CONTENT / >>>CONTENT block body"
        return True, ""

    if k == "EDIT":
        if not t.strip():
            return False, "empty filepath (EDIT target was blank)"
        # body for EDIT is a (find, replace) tuple packed as a dict for
        # this validator's API: {"find": "...", "replace": "..."}.
        if not isinstance(body, dict):
            return False, "EDIT missing FIND / REPLACE block markers"
        if body.get("find") is None:
            return False, "EDIT missing FIND section"
        if body.get("replace") is None:
            # Empty REPLACE is allowed (acts as a delete); None means the
            # REPLACE marker wasn't present at all.
            return False, "EDIT missing REPLACE section"
        return True, ""

    # Unknown / out-of-scope kinds pass through — the validator does not
    # gate BROWSER_*, RUN_SKILL, REMEMBER, DONE, ASK per the plan's
    # out-of-scope list. They're handled (or not handled) elsewhere.
    return True, ""


def reject_message(kind: str, reason: str) -> str:
    """Build the conversation-history line the model sees in the next
    round's [PREVIOUS ROUND RESULTS]. Em-dash separator per operator spec.
    """
    return f"{_VALIDATOR_REJECT_PREFIX} {kind.upper()} — {reason}]"


def write_validator_reject_audit(
    kind: str,
    reason: str,
    *,
    request_id: Optional[str] = None,
    source: Optional[str] = None,
    target_preview: Optional[str] = None,
) -> None:
    """Append a single JSON-line row to ~/.master_ai_audit_typed.jsonl.
    Schema parallels the existing page_context_sanitize rows in stt_server.py:
      ts, kind="validator_reject", directive_kind, reason, request_id, source.
    Target preview is capped at 80 chars and the raw target is NEVER stored —
    same audit-no-leak discipline as the page-context sanitizer.
    """
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "validator_reject",
            "directive_kind": (kind or "").upper(),
            "reason": reason,
            "request_id": request_id or "",
            "source": source or "",
        }
        if target_preview:
            row["target_preview"] = str(target_preview)[:80]
        with open(_VALIDATOR_AUDIT_PATH, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # Audit write must never crash the request path. Drop silently;
        # the [VALIDATOR REJECT] history line is the load-bearing signal.
        pass


# Helper for the integration: combines reject_message + audit row + history
# append in one call so the six integration sites stay one-liners.
def apply_reject(history: list, kind: str, reason: str, **audit_kwargs) -> None:
    """Append the reject line to history (user role) AND write the audit
    row. Six integration sites call this on every invalid directive.
    """
    history.append({"role": "user", "content": reject_message(kind, reason)})
    write_validator_reject_audit(kind, reason, **audit_kwargs)
