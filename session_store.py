"""Session persistence for Sensei (save / restore / atomic writes).

Extracted 2026-10-05 from master_ai.py along the session seam, move-only:
same docstrings, same control flow, same byte-for-byte file formats. Same
discipline as sensei_tables.py / runtime_state.py — this module never
imports master_ai at module scope, because master_ai imports IT.

The live runtime namespace (CHARS_SINCE_SAVE, CHATS_DIR, SESSION_TS,
_SAVE_LOCK, the lifecycle globals, colours, summarize_session/play_anim)
stays owned by master_ai: every function here takes `master_ai` as its
FIRST parameter — the same explicit-carrier pattern runtime_state.py already
uses (write_state(state, path, atomic_write_text)). Delegating wrappers in
master_ai bind it so external callers keep the exact old signatures:

    session_store.save_session(master_ai, history, ...)   # moved body
    master_ai.save_session(history, ...)                  # old signature

Keeping the lock, the counter, and the paths in master_ai (the process-live
module the REPL, TUI, and atexit handlers import) is deliberate: a copy in
session_store would silently fork the save-lock ownership and the char
counter, and resume-restore must write back into the very namespace those
consumers read.
"""

from __future__ import annotations

import functools
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "_atomic_write_text",
    "save_session",
    "_apply_session_globals",
    "_restore_structured_state",
    "_auto_save_background",
    "_bounded_save_session",
    "_request_auto_save",
    "_restore_reload_carry",
]


def _atomic_write_text(path: Any, text: str) -> None:
    """Write *text* to *path* atomically via a sibling temp file.

    Writes to a temp file in the same directory, fsyncs the fd, then
    os.replace()s it over the target so concurrent readers never see a
    partial write.  Cleans up the temp file on any error and re-raises.
    Parent directory must already exist.
    """
    path = Path(path)
    tmp_path: Path | None = None
    fd = -1
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w") as fh:
            fd = -1  # os.fdopen owns the fd now
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def save_session(
    master_ai: Any,
    history: Any,
    silent: bool = False,
    summarize_timeout: float | None = None,
) -> Any:
    with master_ai._SAVE_LOCK:
        msgs = [m for m in history if m.get("role") in ("user", "assistant")]
        if len(msgs) < 2:
            return
        master_ai.CHATS_DIR.mkdir(exist_ok=True)
        ts = master_ai.SESSION_TS  # always same file — overwrites, never duplicates
        date_str = master_ai._fmt_ampm()
        master_ai.CHARS_SINCE_SAVE = 0

    def _is_structured_block(content: str) -> Any:
        """True for tool-result / file / directive blocks that should keep
        their own labeled block shape in the saved transcript, not be
        flattened under a generic 'You:' line."""
        if not content:
            return False
        prefixes = (
            "[RUN RESULT]",
            "[RUNTERM RESULT]",
            "[SEARCH RESULT]",
            "[READ RESULT]",
            "[File contents]",
            "[EDIT RESULT]",
            "[CREATE RESULT]",
            "[TASK LIST RESULT]",
            "[SUBAGENT RESULT]",
            "[SUBAGENT ERROR]",
            "[SEND_EMAIL RESULT]",
            "[SEND_TELEGRAM RESULT]",
            "[BROWSER RESULT]",
            "[TOOL BLOCKED]",
            "[TOOL FAILED]",
            "[READ FAILED]",
            "[HOOK BLOCKED]",
            "[Directive repair]",
            "[User declined",
            "[System:",
            "[Full last session transcript]",
            "[Resumed session from",
        )
        return any(content.lstrip().startswith(p) for p in prefixes)

    def _block_label_for(content: str) -> str:
        """Pick a human label for a structured block based on its prefix."""
        c = content.lstrip()
        if c.startswith("[RUN RESULT]") or c.startswith("[RUNTERM RESULT]"):
            return "RUN"
        if c.startswith("[SEARCH RESULT]"):
            return "SEARCH"
        if c.startswith("[READ RESULT]") or c.startswith("[File contents]"):
            return "READ"
        if c.startswith("[EDIT RESULT]"):
            return "EDIT"
        if c.startswith("[CREATE RESULT]"):
            return "CREATE"
        if c.startswith("[TASK LIST RESULT]"):
            return "TASKS"
        if c.startswith("[SUBAGENT RESULT]") or c.startswith("[SUBAGENT ERROR]"):
            return "SUBAGENT"
        if c.startswith("[SEND_EMAIL RESULT]"):
            return "EMAIL"
        if c.startswith("[SEND_TELEGRAM RESULT]"):
            return "TELEGRAM"
        if c.startswith("[BROWSER RESULT]"):
            return "BROWSER"
        if c.startswith("[TOOL BLOCKED]") or c.startswith("[HOOK BLOCKED]"):
            return "BLOCKED"
        if c.startswith("[TOOL FAILED]") or c.startswith("[READ FAILED]"):
            return "FAILED"
        if c.startswith("[Directive repair]"):
            return "REPAIR"
        if c.startswith("[User declined"):
            return "DECLINED"
        if c.startswith("[System:"):
            return "SYSTEM"
        if c.startswith("[Full last session transcript]") or c.startswith(
            "[Resumed session from"
        ):
            return "RESUME"
        return "BLOCK"

    # Full chat log — build in memory first, then write atomically under lock
    chat_path = master_ai.CHATS_DIR / f"{ts}.chat"
    _chat_parts: list[str] = []
    for m in msgs:
        role = m.get("role", "")
        content = (m.get("content") or "")[:200000]
        if role == "user" and _is_structured_block(content):
            label = _block_label_for(content)
            _chat_parts.append(f"\n[{date_str}] ── {label} ──\n")
            _chat_parts.append(f"{content}\n")
        else:
            label = "You" if role == "user" else "AI"
            _chat_parts.append(f"[{date_str}] {label}: {content}\n")
    with master_ai._SAVE_LOCK:
        _atomic_write_text(chat_path, "".join(_chat_parts))
        # 2026-10-05: structured runtime state rides alongside the transcript.
        # The text files preserve the conversation; this preserves the RUNTIME
        # — pending no-TTY approvals, recent subagent dispatch feedback, the
        # last blocked/denied/hook-blocked directive feedback, a truncated
        # cloud reply waiting on 'proceed', and an unapproved plan — none of
        # which the transcript faithfully represents. Collection + write both
        # happen UNDER the lock so a concurrent save can never interleave two
        # .state.json halves. Failures here log and move on: the .chat
        # transcript is the durable artifact, state is an add-on, and a
        # broken snapshot must never block the save that carries the actual
        # conversation.
        try:
            import runtime_state

            st = runtime_state.collect_state(history)
            # Re-pull the three live lifecycle dicts here (collect_state's
            # defaults are empty) so the snapshot reflects the real globals.
            st["tool_lifecycle"] = runtime_state.collect_tool_lifecycle(
                blocked=master_ai.__dict__.get("_LAST_BLOCKED_ACTION") or {},
                denied=master_ai.__dict__.get("_LAST_DENIED_ACTION") or {},
                hook_block=master_ai.__dict__.get("_LAST_HOOK_BLOCK") or {},
                live_typed=master_ai._LAST_LIVE_TYPED_ACTIONS,
                pending_continuation=master_ai.__dict__.get("PENDING_CONTINUATION"),
                pending_plan_text=master_ai.__dict__.get("PENDING_PLAN_TEXT"),
                pending_plan_request=master_ai.__dict__.get("PENDING_PLAN_REQUEST"),
                mode=master_ai.__dict__.get("MODE", ""),
            )
            runtime_state.write_state(
                st, runtime_state.state_path_for(chat_path), _atomic_write_text
            )
        except Exception as e:
            master_ai.log(f"SAVE_STATE_ERROR: {e}")

    if not silent:
        master_ai.play_anim(master_ai._A_BOW, delay=0.14, color=master_ai.C)
        print(f"\n{master_ai.C}  📝 Summarizing session...{master_ai.X}", flush=True)

    if summarize_timeout is not None and summarize_timeout > 0:
        _sum_result: list[Any] = [None, None]
        _sum_done = threading.Event()

        def _do_summarize() -> None:
            try:
                _sum_result[0], _sum_result[1] = master_ai.summarize_session(history)
            finally:
                _sum_done.set()

        threading.Thread(target=_do_summarize, daemon=True).start()
        if not _sum_done.wait(timeout=summarize_timeout):
            master_ai.log(
                "SAVE_SESSION_SUMMARY_TIMEOUT: skipping session summary (time limit exceeded)"
            )
            if not silent:
                print(
                    f"{master_ai.G}  ✅ Session saved → {chat_path.name}{master_ai.X}"
                )
            return
        title, summary = _sum_result[0], _sum_result[1]
    else:
        title, summary = master_ai.summarize_session(history)
    if summary:
        summary_path = master_ai.CHATS_DIR / f"{ts}.summary"
        summary_body = f"[Session {date_str}]\n"
        if title:
            summary_body += f"Title: {title}\n"
        summary_body += f"{summary}\n"
        with master_ai._SAVE_LOCK:
            _atomic_write_text(summary_path, summary_body)
        if not silent:
            print(
                f"{master_ai.G}  ✅ Saved + summarized → {summary_path.name}{master_ai.X}"
            )
            print(f"{master_ai.D}  {summary[:200]}{master_ai.X}")
    elif not silent:
        print(f"{master_ai.G}  ✅ Session saved → {chat_path.name}{master_ai.X}")


def _apply_session_globals(master_ai: Any, name: Any, value: Any) -> None:
    """Write one restored runtime-state key into the live namespace.

    The lifecycle globals live in sensei_tables (the shared-state module
    master_ai's own code re-assigns through globals()[...] — see
    _record_blocked_action et al.), so restore goes through the same
    two-step: update sensei_tables' value, then master_ai's binding, exactly
    the way PENDING_USER_NOTE / PENDING_PLAN_TEXT are already managed.
    """
    try:
        import sensei_tables

        if hasattr(sensei_tables, name):
            setattr(sensei_tables, name, value)
    except Exception:
        pass
    setattr(master_ai, name, value)


def _restore_structured_state(
    master_ai: Any, chat_path: Any, history: Any, announce: bool = False
) -> Any:
    """Restore the structured runtime state saved beside a .chat transcript.

    Reads <ts>.state.json (sibling of chat_path), then re-hydrates the live
    runtime: pending approvals re-enter approval_queue as PENDING (never
    auto-approved — new ids, fresh timestamps), subagent result blocks
    re-enter history as the same [SUBAGENT RESULT] feedback the live
    dispatch path produces, and tool-lifecycle globals
    (_LAST_BLOCKED_ACTION / _LAST_DENIED_ACTION / _LAST_HOOK_BLOCK /
    _LAST_LIVE_TYPED_ACTIONS / PENDING_CONTINUATION / PENDING_PLAN_TEXT)
    come back so the next process_reply() turn surfaces them exactly as an
    uninterrupted session would.

    Fail-open by design: any drift (missing file, unparsable JSON, schema
    mismatch, unexpected shape) logs loudly and resume falls back to the
    existing transcript-injection behavior — a broken snapshot must never
    crash startup. Returns a short summary dict, or None when nothing was
    restored.
    """
    try:
        import runtime_state
    except Exception as e:
        master_ai.log(f"RESUME_STATE_IMPORT_ERROR: {e}")
        return None
    try:
        state = runtime_state.load_state(runtime_state.state_path_for(chat_path))
    except runtime_state.StateSchemaError as e:
        # Loud, single-line drift signal — then fall back silently to the
        # text-only resume path that already worked.
        master_ai.log(f"RESUME_STATE_DRIFT: {e} ({Path(chat_path).name})")
        return None
    except Exception as e:
        master_ai.log(f"RESUME_STATE_ERROR: {e}")
        return None

    summary: dict = {"approvals_restored": 0, "subagents_restored": 0}
    try:
        summary["approvals_restored"] = runtime_state.restore_pending_approvals(
            state.get("pending_approvals")
        )
        summary["subagents_restored"] = runtime_state.restore_subagents(
            (state.get("subagents") or {}).get("recent"), history
        )
        tls = runtime_state.restore_tool_lifecycle(
            state.get("tool_lifecycle"),
            functools.partial(_apply_session_globals, master_ai),
        )
        summary["lifecycle"] = tls
        if summary["approvals_restored"]:
            print(
                f"  {master_ai.Y}⏳ {summary['approvals_restored']} pending approval(s) "
                f"restored — review with 'pending'{master_ai.X}"
            )
        if summary["subagents_restored"]:
            print(
                f"  {master_ai.C}↺ {summary['subagents_restored']} subagent result(s) "
                f"reloaded into context{master_ai.X}"
            )
        if announce:
            line = runtime_state.restore_summary_line(summary)
            if line:
                print(f"  {master_ai.D}{line}{master_ai.X}")
        master_ai.log(
            "RESUME_STATE_RESTORED: "
            + runtime_state.restore_summary_line(summary).replace("; ", ", ")
        )
        return summary
    except Exception as e:
        # Anything unexpected during application still must not break resume.
        master_ai.log(f"RESUME_STATE_APPLY_ERROR: {e}")
        return None


def _auto_save_background(master_ai: Any, history: Any) -> None:
    """Run save_session silently in a background thread."""
    try:
        save_session(master_ai, list(history), silent=True)
    except Exception:
        pass


def _bounded_save_session(master_ai: Any, history: Any, timeout: float = 8.0) -> None:
    """save_session() on a daemon thread with a hard wall-clock bound.

    2026-09-07 already proved this live for the TUI's SIGTERM handler:
    save_session() -> summarize_session() -> a cloud model call chained
    through multiple providers with no bound on that path left the process
    alive 8+ minutes after SIGTERM, defeating the whole point of a shutdown
    handler (a supervisor can't restart what won't die). Every OTHER exit
    path that calls save_session() at shutdown (plain atexit, non-TUI
    SIGTERM/SIGHUP, Ctrl-C in the REPL loop, the module-level
    KeyboardInterrupt catch) had the exact same unbounded exposure and never
    got the fix — this is that fix, shared, so it can't drift out of sync
    across the sites again. Always returns within `timeout` seconds; a slow
    save loses at most the session summary, never blocks shutdown."""
    done = threading.Event()

    def _bg() -> None:
        try:
            save_session(master_ai, list(history), silent=True)
        finally:
            done.set()

    threading.Thread(target=_bg, daemon=True).start()
    done.wait(timeout=timeout)


def _request_auto_save(master_ai: Any, history: Any) -> None:
    """Save the current session shortly after a turn completes."""
    if not history:
        return
    if master_ai.AUTO_SAVE_EVERY_TURN:
        threading.Thread(
            target=_auto_save_background, args=(master_ai, list(history)), daemon=True
        ).start()
        return
    if master_ai.CHARS_SINCE_SAVE >= master_ai.AUTO_SAVE_THRESHOLD:
        threading.Thread(
            target=_auto_save_background, args=(master_ai, list(history)), daemon=True
        ).start()


def _restore_reload_carry(master_ai: Any, path: Any | None = None) -> Any:
    """Consume the pending reload-carry note and return it, or None.

    When master_ai.py live-reloads it writes the operator's in-flight
    instruction to ~/.master_ai_reload_carry and restarts; on the way back
    up this hands that note to the new process.

    Extracted 2026-09-28 out of main()'s startup block so the behaviour is
    callable and testable -- as an inline block it had no seam, which is
    why the unlink-on-failed-read fix that shipped alongside it could only
    be verified by inspection.

    A carry older than RESUME_FLAG_MAX_AGE is discarded: a stale flag must
    not revive a session from hours ago. The file is unlinked even when the
    read raises, otherwise an unreadable carry file survives and re-logs
    AUTO_RELOAD_CARRY_RESTORE_ERROR on every startup until someone deletes
    it by hand. Never raises -- a bad carry file must not stop startup.
    """
    carry = Path(path) if path is not None else master_ai._RELOAD_CARRY_FILE
    try:
        if not carry.exists():
            return None
        age = time.time() - carry.stat().st_mtime
        if age > master_ai.RESUME_FLAG_MAX_AGE:
            master_ai.log(
                f"CARRY_EXPIRED: age={age:.0f}s > {master_ai.RESUME_FLAG_MAX_AGE}s, discarding"
            )
            carry.unlink(missing_ok=True)
            return None
        try:
            carried = carry.read_text()
        finally:
            carry.unlink(missing_ok=True)
        return carried or None
    except Exception as e:
        master_ai.log(f"AUTO_RELOAD_CARRY_RESTORE_ERROR: {e}")
        return None
