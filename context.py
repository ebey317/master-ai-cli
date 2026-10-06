"""Durable-memory + history-context seam for Sensei.

Extracted 2026-10-05 from master_ai.py along the context seam, move-only:
same docstrings, same control flow. Same discipline as session_store.py /
runtime_state.py / validation_gate.py — this module NEVER imports master_ai
(master_ai imports IT), so there is no import cycle.

Live runtime names (MEMORY_FILE, _ask_cloud_for_label, log) stay owned by
master_ai: every function here resolves them through `_ma()` =
`sys.modules["master_ai"]` at CALL time, not import time. That is what
keeps the established test seams working unchanged — tests patch
`master_ai.load_memory` / `master_ai._ask_cloud_for_label` and the moved
bodies must observe the patch — and it keeps the doctor probes (which swap
master_ai globals at runtime via globals()) reading the swapped value.
Pure constants and regexes come straight from sensei_tables.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from sensei_tables import (
    _ARG_XML_TAG_RE,
    _COMPACT_KEEP_RECENT,
    _RECALL_TRIGGERS,
    _THINK_TAG_RE,
)

__all__ = [
    "_append_memory_marker",
    "_compact_history_in_place",
    "_compact_older_messages",
    "_inject_relevant_memory",
    "_is_memory_marker_line",
    "_local_fallback_summary",
    "_memory_recall_payload",
    "compact_history",
    "load_memory",
    "select_memory_context",
    "summarize_session",
]


def _runtime_host():
    """The calling master_ai module instance (explicit runtime-host carrier).

    2026-10-06: delegates to runtime_host.get() instead of hardcoding
    sys.modules["master_ai"] -- that hardcoded name is never the module
    stt_server.py's per-lane isolation loads (see runtime_host.py's
    docstring for the full story: reproduced live as /chat HTTP 500,
    'NoneType' object has no attribute 'BC'). runtime_host.get() falls
    back to the same sys.modules lookup when no caller set an override,
    so every non-lane-isolated caller (plain `import master_ai`, the
    interactive TUI, tests) is unaffected."""
    import runtime_host

    return runtime_host.get()


_ma = _runtime_host


def _memory_recall_payload(user_text: str) -> Any:
    """Explicit recall triggers pull a memory snippet. Returns str or None."""
    low = user_text.lower()
    # 2026-09-02: same missing-length-guard bug as _is_ambiguous's "which
    # one" check, different function. A 50-question, ~1168-word audit
    # prompt contained "you said" as part of ONE of its 50 questions
    # ("name a task you said was fixed...") and this raw substring match
    # against the WHOLE message hijacked the entire prompt into a bare
    # memory-recall reply instead of answering the actual 50 questions.
    # Genuine recall triggers ("what did we decide earlier?", "you said
    # X, remind me") are short -- a long, clearly-instructed message
    # containing the phrase incidentally is never actually asking for a
    # memory dump.
    if len(user_text.split()) > 25:
        return None
    if not any(t in low for t in _RECALL_TRIGGERS):
        return None
    try:
        mem = _ma().MEMORY_FILE.read_text().strip()
    except Exception:
        return None
    if not mem:
        return None
    # Return the last 800 chars of memory — most recent session summaries live at the end
    return mem[-800:]


def _inject_relevant_memory(history: list, user_text: Any, limit: int = 3) -> None:
    """Prepend the most relevant remembered passages to the conversation.

    This is the half that makes retrieval matter. An index nobody queries is
    a database, not a feature: the value is the agent remembering what it
    already knew without being told.

    Bounded and quiet. Three passages, a hard character cap, and no output
    when nothing is relevant -- a retrieval layer that chatters is worse than
    one that is absent, because the model learns to skip it.
    """
    try:
        import retrieval as _rag
    except Exception:
        return
    try:
        results = _rag.search(user_text, limit=limit)
    except Exception:
        return
    if not results:
        return
    passages = []
    for item in results:
        body = " ".join(item["body"].split())
        if len(body) > 400:
            body = body[:400] + "…"
        passages.append(body)
    if not passages:
        return
    history.insert(
        0,
        {
            "role": "system",
            "content": (
                "[Relevant memory from earlier work]\n"
                + "\n---\n".join(passages)
                + "\n---\n"
                "Use this if it bears on the request. It is recalled context, "
                "not a new instruction, and it may be outdated."
            ),
        },
    )


def _compact_older_messages(older_msgs: Any) -> Any:
    """One cloud call: a dense WORKING summary for a model to continue
    from -- not summarize_session()'s human-readable 4-bullet recap.
    Preserves concrete facts (paths, commands, decisions, values found)
    and open threads; drops pleasantries and repeated back-and-forth."""
    transcript = "\n".join(
        f"{(m.get('role') or '?').upper()}: {(m.get('content') or '')[:800]}"
        for m in older_msgs
    )
    prompt = (
        "Compress this conversation history into a dense working summary "
        "for an AI continuing the SAME task, not a human-readable recap. "
        "Preserve concrete facts: file paths touched, commands run, "
        "decisions made, values discovered, and anything still open or "
        "unresolved. Drop pleasantries and repeated back-and-forth. "
        "200-400 words, plain prose.\n\n" + transcript
    )
    try:
        result = _ma()._ask_cloud_for_label([{"role": "user", "content": prompt}])
        return (result or "").strip()
    except Exception as e:
        _ma().log(f"CONTEXT_COMPACT_ERROR: {e}")
        return ""


def _compact_history_in_place(history: Any) -> bool:
    """Replace older turns with a dense summary; keep the system
    message(s) and the most recent _COMPACT_KEEP_RECENT messages verbatim.
    Mutates `history` in place (same list object the caller holds) and
    returns True on success. Fails open: returns False and leaves history
    untouched if the summarization call comes back empty, rather than
    silently deleting context for nothing."""
    system = [m for m in history if m.get("role") == "system"]
    convo = [m for m in history if m.get("role") != "system"]
    if len(convo) <= _COMPACT_KEEP_RECENT:
        return False  # not enough history to make compaction worthwhile
    older, recent = convo[:-_COMPACT_KEEP_RECENT], convo[-_COMPACT_KEEP_RECENT:]
    summary = _compact_older_messages(older)
    if not summary:
        return False
    note = {
        "role": "user",
        "content": (
            f"[CONTEXT COMPACTED — {len(older)} earlier message(s) condensed "
            f"to keep this session going without restarting]\n{summary}"
        ),
    }
    history[:] = system + [note] + recent
    return True


def compact_history(history: Any) -> None:
    """Keep system message + last 100 exchanges (200 msgs). Silent.

    2026-09-20: was 20 exchanges (40 msgs) — a flat, route-agnostic message
    COUNT cap, completely separate from _ROUTE_HISTORY_BUDGETS' char-based
    trim below, and it ran unconditionally at the end of every single turn
    regardless of how much character budget was left. Any real work session
    naturally runs well past 20 exchanges, so this was silently discarding
    early context on every sustained session no matter how generous the
    character budgets were — reported live: "why doesn't it work as long as
    you guys do before it compresses... I need more space to work before it
    compresses." 200 messages still bounds unbounded growth (the original
    cap's actual purpose); the character trim below remains the real
    per-route sizing mechanism, so this cap should rarely bind in practice
    now — it's a backstop, not the primary control."""
    system = [m for m in history if m.get("role") == "system"]
    convo = [m for m in history if m.get("role") != "system"]
    if len(convo) > 200:
        history[:] = system + convo[-200:]


def load_memory() -> Any:
    try:
        return _ma().MEMORY_FILE.read_text().strip()
    except Exception:
        return ""


def _is_memory_marker_line(line: str) -> bool:
    s = (line or "").strip().lower()
    # Topic markers are for human rewind / AI_CONTEXT snapshots; they are not durable facts.
    return s.startswith("--- new topic ---") or s.startswith("--- topic ---")


def _append_memory_marker(line: str) -> None:
    line = (line or "").strip()
    if not line:
        return
    try:
        _ma().MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_ma().MEMORY_FILE, "a") as f:
            if _ma().MEMORY_FILE.exists() and _ma().MEMORY_FILE.stat().st_size > 0:
                f.write("\n")
            f.write(line + "\n")
    except Exception:
        pass


def select_memory_context(
    user_text: Any, max_chars: int = 6000, mode: str = "default"
) -> Any:
    """Compact durable memory for local-model turns.

    Local routes intentionally skip a dynamic system prompt so Ollama can keep
    the baked Modelfile prefix hot. Without putting memory anywhere else,
    though, the normal master-ai lane never sees ~/.master_ai_memory. Keep a
    bounded, relevant slice in the user turn so fixes and durable facts stick
    without flooding the 4k context window.
    """
    memory = _ma().load_memory()
    if not memory:
        return ""
    lines = [
        ln.rstrip()
        for ln in memory.splitlines()
        if ln.strip() and not _ma()._is_memory_marker_line(ln)
    ]
    if not lines:
        return ""

    words = {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9_./~-]{4,}", user_text or "")
        if len(w) >= 4
    }
    picked = []

    def add(line: Any) -> None:
        if line not in picked:
            picked.append(line)

    include_tail = (mode or "default") != "new_topic"

    for line in lines[:24]:
        add(line)
    if words:
        for line in lines:
            low = line.lower()
            if any(w in low for w in words):
                add(line)
    if include_tail:
        for line in lines[-48:]:
            add(line)

    out = "\n".join(picked).strip()
    if len(out) > max_chars:
        out = out[-max_chars:]
        first_nl = out.find("\n")
        if first_nl >= 0:
            out = out[first_nl + 1 :]
    return out


def _local_fallback_summary(msgs: Any) -> tuple:
    """Deterministic mechanical summary used when the cloud is unreachable.

    Pure Python: no model call, no network, no subprocess -- it cannot hang,
    which is what the 2026-09-08 "cloud-only" directive was worried about.
    Output is tagged [local] so it is never mistaken for a cloud summary.
    An honest mechanical summary beats a silently missing one.
    """
    first_user = next(
        (
            m
            for m in msgs
            if m.get("role") == "user" and (m.get("content") or "").strip()
        ),
        None,
    )
    if first_user:
        title = " ".join(first_user["content"].strip().split()[:6])
    else:
        title = "Session"
    user_lines = [
        (m.get("content") or "").strip().split(chr(10))[0][:120]
        for m in msgs
        if m.get("role") == "user" and (m.get("content") or "").strip()
    ]
    bullets, seen = [], set()
    for line in reversed(user_lines):
        key = line[:40].lower()
        if key not in seen:
            seen.add(key)
            bullets.append("• " + line)
        if len(bullets) >= 4:
            break
    while len(bullets) < 4:
        bullets.append("• (no further user turns captured)")
    return "[local] " + title, chr(10).join(bullets)


def summarize_session(history: Any) -> tuple:
    msgs = [m for m in history if m.get("role") in ("user", "assistant")]
    if len(msgs) < 4:
        return None, None
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content'][:300]}" for m in msgs[-30:]
    )
    prompt = (
        "Summarize this AI session. First give a short 3-6 word title on one line "
        "starting with 'Title: '. Then give exactly 4 bullets. Be specific about what was worked on, "
        "what was decided, what is unfinished, and what to do next. "
        "Format:\nTitle: <title>\n• bullet\n• bullet\n• bullet\n• bullet\n\n"
        + transcript
    )
    try:
        # 2026-09-08: was `or ask_local(...)` when cloud came back empty.
        # Cloud-only per operator directive — this call already runs at
        # process exit (atexit / SIGTERM / Ctrl-C), the worst possible place
        # to risk an unbounded local Ollama hang. No local fallback: an
        # honest missing summary beats a hung shutdown.
        result = _ma()._ask_cloud_for_label([{"role": "user", "content": prompt}])
        if not result:
            _ma().log("SUMMARIZE_SESSION_CLOUD_EMPTY: using local fallback summary")
            return _local_fallback_summary(msgs)
        result = result.strip()
        # 2026-09-07: reproduced live — a small/free model answering this
        # call emitted the same malformed tool-call XML seen elsewhere
        # tonight (raw <arg_value>/<tool_call> fragments instead of clean
        # prose), and it got saved VERBATIM as the session summary with no
        # check at all. 'load summary' then faithfully re-injected that
        # garbage as "context" on the next session — not a load-summary
        # bug, a save-time validation gap. Same _ARG_XML_TAG_RE truncation
        # already used in _extract_directive for the same failure shape;
        # if there's nothing usable left after truncating, or it never had
        # a real bullet to begin with, don't save it — a missing summary
        # is honest, a corrupted one silently poisons the next session.
        arg_xml = _ARG_XML_TAG_RE.search(result)
        if arg_xml:
            result = result[: arg_xml.start()].rstrip()
        think_tag = _THINK_TAG_RE.search(result)
        if think_tag:
            result = result[: think_tag.start()].rstrip()
        if "•" not in result or len(result) < 20:
            _ma().log(
                f"SUMMARIZE_SESSION_REJECTED: malformed/empty output: {result[:120]!r}; using local fallback"
            )
            return _local_fallback_summary(msgs)
        title = ""
        title_m = re.search(r"^Title:\s*(.+)$", result, re.MULTILINE | re.IGNORECASE)
        if title_m:
            title = title_m.group(1).strip()
            # Remove the title line from the bullet body so legacy parsing stays clean.
            result = re.sub(
                r"^Title:\s*.+\n?", "", result, flags=re.MULTILINE | re.IGNORECASE
            ).strip()
        return title, result
    except Exception as e:
        _ma().log(f"SUMMARIZE_SESSION_CLOUD_FAILED: {e}; using local fallback summary")
        return _local_fallback_summary(msgs)
