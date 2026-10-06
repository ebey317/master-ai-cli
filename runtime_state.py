#!/usr/bin/env python3
"""Master AI — structured runtime-state snapshot for session resume.

The save path (master_ai.save_session) writes a .chat transcript and a
.summary recap; both survive restarts. What did NOT survive was every piece
of structured runtime state: pending no-TTY approval-queue entries,
in-flight tool lifecycle outcomes (the last blocked/denied/hook-blocked
directive the model was told about), truncated cloud replies waiting on a
'proceed', unapproved plans, and the last subagent dispatch records.

This module is the bridge. It snapshots that state into one versioned JSON
file next to the transcript (CHATS_DIR/<ts>.state.json), and restores it on
resume. Every restore is fail-open: a missing, corrupt, version-mismatched,
or schema-drifted file is logged loudly and resume falls back to the
existing transcript-injection behavior. State never blocks startup.

Layout (session_state_v1):

    {
      "schema": "session_state_v1",
      "saved_at": <unix float>,
      "pending_approvals": [approval_queue entry dicts (PENDING only)],
      "subagents": {"recent": [ [<ts>, "<[SUBAGENT RESULT] block>"], ... ]},
      "tool_lifecycle": {
          "last_blocked_action": {...} | null,
          "last_denied_action":  {...} | null,
          "last_hook_block":     {...} | null,
          "recent_typed_actions": [ ... up to 30 TypedAction dicts ... ],
          "pending_continuation": {...} | null,
          "pending_plan": {"request": str|None, "text": str|None} | null,
          "mode": str
      }
    }

Restores are conservative by design:
  * pending approvals go back PENDING through approval_queue.queue() —
    new ids, fresh ts, never auto-approved and never RAN;
  * subagent records re-enter the conversation as [SUBAGENT RESULT] context
    exactly the way live dispatches feed back (inert data, not directives);
  * tool-lifecycle globals are restored as-is so the next process_reply()
    turn surfaces [TOOL BLOCKED] / [User declined ...] / [HOOK BLOCKED]
    the same way an uninterrupted session would.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

STATE_SCHEMA = "session_state_v1"

# Bounds so a pathologically large runtime never produces a giant state file.
MAX_PENDING_APPROVALS = 50
MAX_RECENT_SUBAGENTS = 50
MAX_RECENT_TYPED_ACTIONS = 30
MAX_PENDING_PLAN_CHARS = 100_000
MAX_ENTRY_JSON_CHARS = 60_000


class StateSchemaError(Exception):
    """Saved state blob is unreadable, wrong-shape, or version-mismatched."""


def state_path_for(chat_path: Path) -> Any:
    """Sibling .state.json path for a saved .chat transcript path."""
    chat_path = Path(chat_path)
    return chat_path.with_suffix(".state.json")


# ── collect ─────────────────────────────────────────────────────────
# Import indirection defaults; master_ai passes its own accessors so this
# module stays importable without pulling the orchestrator in.


def _clamp_json_obj(obj: Any, budget: int = MAX_ENTRY_JSON_CHARS) -> Any:
    """Shrink one entry until json.dumps(size) <= budget, else return None.

    Approval/typed-action payloads can hold big file bodies (CREATE/EDIT
    diffs). The queue file itself already stores full payloads, so the
    snapshot bound exists only to keep one pathological entry from bloating
    the state file — the restore path re-reads the authoritative queue.
    """
    try:
        if len(json.dumps(obj, default=str)) <= budget:
            return obj
    except Exception:
        return None
    if isinstance(obj, dict):
        out = dict(obj)
        for k in sorted(out, key=len, reverse=True):
            out[k] = str(out[k])[:500]
            if len(json.dumps(out, default=str)) <= budget:
                return out
        return None
    return None


def collect_pending_approvals() -> list:
    """Current PENDING approval_queue entries, bounded, JSON-legal."""
    try:
        import approval_queue

        entries = [
            e
            for e in approval_queue.list_pending()
            if isinstance(e, dict) and e.get("id")
        ]
    except Exception:
        return []
    out = []
    for e in entries[:MAX_PENDING_APPROVALS]:
        e2 = _clamp_json_obj(e)
        if e2 is not None:
            out.append(e2)
    return out


def collect_subagents(history: Any) -> list:
    """Recent [SUBAGENT RESULT]/[SUBAGENT ERROR] feedback blocks.

    Extracted from the saved-from live dispatch path (process_reply appends
    these as user-role tool feedback). We keep (ts, content) pairs so the
    restore can rebuild them as real history turns.
    """
    out = []
    try:
        for m in history or []:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content") or ""
            if content.lstrip().startswith(("[SUBAGENT RESULT]", "[SUBAGENT ERROR]")):
                out.append([float(time.time()), content[:4000]])
                if len(out) >= MAX_RECENT_SUBAGENTS:
                    break
    except Exception:
        return []
    return out


def collect_tool_lifecycle(
    blocked: Any = None,
    denied: Any = None,
    hook_block: Any = None,
    live_typed: Any = None,
    pending_continuation: Any = None,
    pending_plan_text: Any = None,
    pending_plan_request: Any = None,
    mode: Any = None,
) -> dict:
    """Build the tool_lifecycle section from live runtime globals.

    master_ai passes the real globals in (read via globals().get in the
    caller, None elsewhere) so this stays side-effect free.
    """
    cont = None
    if isinstance(pending_continuation, dict) and pending_continuation.get("so_far"):
        msgs = pending_continuation.get("messages")
        cont = {
            "provider": pending_continuation.get("provider"),
            "messages": msgs if isinstance(msgs, list) else None,
            "so_far": str(pending_continuation.get("so_far")),
        }
        cont = _clamp_json_obj(cont, budget=20_000)

    plan_text = str(pending_plan_text or "")
    if len(plan_text) > MAX_PENDING_PLAN_CHARS:
        plan_text = ""
    plan = None
    if plan_text.strip():
        plan = {
            "request": str(pending_plan_request or "")[:2000],
            "text": plan_text,
        }

    typed_recent = []
    if isinstance(live_typed, list):
        for d in live_typed[-MAX_RECENT_TYPED_ACTIONS:]:
            try:
                d2 = dict(d)
                d2.pop("create_content", None)
                d2.pop("edit_new", None)
                typed_recent.append(_clamp_json_obj(d2) or {})
            except Exception:
                continue

    def _small(d: Any) -> Any:
        return _clamp_json_obj(d) if isinstance(d, dict) else None

    return {
        "last_blocked_action": _small(blocked) or None,
        "last_denied_action": _small(denied) or None,
        "last_hook_block": _small(hook_block) or None,
        "recent_typed_actions": typed_recent,
        "pending_continuation": cont,
        "pending_plan": plan,
        "mode": str(mode or ""),
    }


def collect_state(history: Any) -> dict:
    """Assemble the full versioned snapshot dict."""
    return {
        "schema": STATE_SCHEMA,
        "saved_at": time.time(),
        "pending_approvals": collect_pending_approvals(),
        "subagents": {"recent": collect_subagents(history)},
        "tool_lifecycle": collect_tool_lifecycle(),
    }


# ── write / load ────────────────────────────────────────────────────


def serialize_state(state: dict) -> str:
    return json.dumps(state, sort_keys=True, default=str)


def write_state(state: dict, path: Any, atomic_write_text) -> None:
    """Write the snapshot via master_ai's _atomic_write_text (same temp +
    fsync + os.replace discipline as .chat/.summary). Raises on failure."""
    serialize_state(state)  # fail here, before touching disk, if unserializable
    atomic_write_text(path, serialize_state(state))


def load_state(path: Any, max_age_s: float = 7 * 24 * 3600) -> dict:
    """Read + validate a snapshot. Raises StateSchemaError (never anything
    else) on absent file, unparsable JSON, wrong/missing schema version, or
    wrong top-level shape — callers treat that as drift and fall back."""

    class _Drift(StateSchemaError):
        pass

    def _bad(msg: str) -> _Drift:
        return _Drift(msg)

    def _read() -> str:
        p = Path(path)
        if not p.exists():
            raise _bad("no state file")
        age = time.time() - p.stat().st_mtime
        if age > max_age_s:
            raise _bad(f"state older than {int(max_age_s)}s (age={age:.0f}s)")
        try:
            return p.read_text(errors="replace")
        except Exception as e:
            raise _bad(f"unreadable: {e}")

    def _parse(text: str) -> dict:
        try:
            obj = json.loads(text)
        except Exception as e:
            raise _bad(f"unparsable JSON: {e}")
        if not isinstance(obj, dict):
            raise _bad("top-level shape is not an object")
        if obj.get("schema") != STATE_SCHEMA:
            raise _bad(
                f"schema mismatch: expected {STATE_SCHEMA!r}, got {obj.get('schema')!r}"
            )
        return obj

    return _parse(_read())


# ── restore ─────────────────────────────────────────────────────────


def restore_pending_approvals(entries: Any) -> int:
    """Re-queue PENDING approvals. Returns count restored.

    Each is added back through approval_queue.queue() with the original
    payload so the registered handlers can replay it after Elijah approves.
    A restore can never mark anything approved: entries land PENDING with a
    new id and fresh timestamp (the original id is preserved in `why`).
    """
    if not isinstance(entries, list):
        return 0
    import approval_queue

    restored = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            payload = e.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            approval_queue.queue(
                entry_type=str(e.get("type") or "run_command"),
                who=str(e.get("who") or "runtime_state.resume"),
                what=str(e.get("what") or "")[:500],
                where=str(e.get("where") or ""),
                why=f"[resumed approval {e.get('id', '?')}] {str(e.get('why') or '')}"[
                    :500
                ],
                how=str(e.get("how") or ""),
                diff=str(e.get("diff") or "")[:4000],
                trigger=str(e.get("trigger") or "")[:2000],
                payload=payload,
            )
            restored += 1
        except Exception:
            continue
    return restored


def restore_subagents(recent: Any, history: list) -> int:
    """Re-inject recent subagent result blocks as user-role context turns.

    Mirrors the live path's own feedback shape ([SUBAGENT RESULT] blocks
    appended by process_reply). The restored assistant ack mirrors the
    ack the live path relies on. Returns count restored.
    """
    if not isinstance(recent, list) or not isinstance(history, list):
        return 0
    restored = 0
    for item in recent:
        if not (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[1], str)
            and item[1].strip()
        ):
            continue
        content = item[1]
        history.append(
            {
                "role": "user",
                "content": (
                    f"{content}\n\nThe subagent results above are now part "
                    "of the context. If the task is complete, answer "
                    "concisely. If more work is needed, emit the next "
                    "directive."
                ),
            }
        )
        restored += 1
    if restored:
        history.append(
            {
                "role": "assistant",
                "content": (
                    "Restored the most recent subagent results from the "
                    "saved session — treating them as inert context, not "
                    "new instructions."
                ),
            }
        )
    return restored


def restore_tool_lifecycle(section: Any, apply_globals) -> dict:
    """Push tool-lifecycle state back into the live runtime.

    ``apply_globals(name, value)`` writes each key into master_ai's
    namespace (and sensei_tables' shared globals where they live). Returns
    a summary dict of what was actually restored.
    """
    summary = {
        "blocked": False,
        "denied": False,
        "hook_block": False,
        "typed_actions": 0,
        "continuation": False,
        "plan": False,
        "mode": "",
    }
    if not isinstance(section, dict):
        return summary

    if (
        isinstance(section.get("last_blocked_action"), dict)
        and section["last_blocked_action"]
    ):
        apply_globals("_LAST_BLOCKED_ACTION", section["last_blocked_action"])
        summary["blocked"] = True
    if (
        isinstance(section.get("last_denied_action"), dict)
        and section["last_denied_action"]
    ):
        apply_globals("_LAST_DENIED_ACTION", section["last_denied_action"])
        summary["denied"] = True
    if isinstance(section.get("last_hook_block"), dict) and section["last_hook_block"]:
        apply_globals("_LAST_HOOK_BLOCK", section["last_hook_block"])
        summary["hook_block"] = True

    typed = section.get("recent_typed_actions")
    if isinstance(typed, list) and typed:
        cap = 200
        try:
            import sensei_tables

            cap = getattr(sensei_tables, "_LIVE_TYPED_ACTIONS_CAP", 200)
        except Exception:
            pass
        apply_globals("_LAST_LIVE_TYPED_ACTIONS", list(typed)[-cap:])
        summary["typed_actions"] = len(typed)

    cont = section.get("pending_continuation")
    if isinstance(cont, dict) and cont.get("so_far"):
        apply_globals(
            "PENDING_CONTINUATION",
            {
                "provider": cont.get("provider"),
                "messages": cont.get("messages") or [],
                "so_far": str(cont.get("so_far")),
            },
        )
        summary["continuation"] = True

    plan = section.get("pending_plan")
    if isinstance(plan, dict) and str(plan.get("text") or "").strip():
        apply_globals("PENDING_PLAN_TEXT", str(plan.get("text") or ""))
        apply_globals("PENDING_PLAN_REQUEST", str(plan.get("request") or ""))
        summary["plan"] = True

    mode = str(section.get("mode") or "")
    if mode in ("plan", "review", "auto"):
        summary["mode"] = mode
    return summary


def restore_summary_line(summary: dict) -> str:
    """One human line for the resume recap, from a restore_summary() dict."""
    if not isinstance(summary, dict):
        return ""
    bits = []
    if summary.get("approvals_restored"):
        bits.append(f"{summary['approvals_restored']} pending approval(s) requeued")
    if summary.get("subagents_restored"):
        bits.append(f"{summary['subagents_restored']} subagent result(s) reloaded")
    tls = summary.get("lifecycle") or {}
    if tls.get("blocked"):
        bits.append("last blocked action feedback restored")
    if tls.get("continuation"):
        bits.append("truncated reply can be finished with 'proceed'")
    if tls.get("plan"):
        bits.append("unapproved plan re-presented (reply 'go' to execute)")
    return "; ".join(bits)


if __name__ == "__main__":
    print("runtime_state: library module (collection needs master_ai globals)")
