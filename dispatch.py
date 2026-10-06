"""Directive reply-parsing + dispatch seam for Sensei.

Extracted 2026-10-05 from master_ai.py along the dispatch seam, move-only:
same docstrings, same control flow, same branch order. process_reply()
runs here along with every reply-parsing and dispatch helper it uses
exclusively (_xml_tool_calls_to_directives and the malformed-directive
repair family, the RUN_SKILL family, _extract_browser_actions,
_split_run_policy, _run_mcp_call_spec).

Same discipline as session_store.py / validation_gate.py — this module
NEVER imports master_ai (master_ai imports IT), so there is no import
cycle. Per contract it does not import context.py either: the dispatch
seam shares nothing runtime-live with the context seam (_validation_gate
was already extracted to validation_gate.py in round 2 and dispatch stays
on it through the host delegate).

Live runtime names (confirm_* gates, MODE/ACTIVE_TASK lifecycle globals,
RunResult, log/render_reply, the run-policy helpers) stay owned by
master_ai: every function here resolves them through `_ma()` =
`sys.modules["master_ai"]` at CALL time, not import time. That is what
keeps the established test seams working unchanged — tests patch
`master_ai.confirm_run` / `master_ai.web_search` / `master_ai.MODE` etc.
and the moved dispatch body must observe every patch, including doctor
probes that reassign master_ai globals at runtime. The `globals()` reads
and writes the legacy body used are re-expressed as attribute get/set on
the same host namespace, so state written by confirm_run on the host is
the exact state process_reply reads here — no forked copies. Pure
constants and regexes come straight from sensei_tables.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from sensei_tables import (
    _ARG_XML_TAG_RE,
    _BARE_KEYWORD_ARG_RE,
    _BARE_KEYWORD_LINE_RE,
    _BROWSER_DIRECTIVE_RE,
    _BROWSER_SEP_RE,
    _DIRECTIVE_KEYWORDS_RE,
    _JSON_TOOL_CALL_RE,
    _MAX_LINE_REPEATS,
    _MISTRAL_TOOL_CALLS_RE,
    _SHELL_SYNTAX_MARKER_RE,
    _SKILL_SESSION_MARKER,
    _THINK_TAG_RE,
    _TOOL_CALL_TAG_RE,
    _VISUAL_RUN_WORDS,
    _XML_FUNCTION_NAME_ALIASES,
    _XML_FUNCTION_PARAM_RE,
    _XML_FUNCTION_RE,
    _XML_INVOKE_RE,
    _XML_PARAM_RE,
    BC,
    BG,
    BOLD,
    C,
    D,
    M,
    R,
    X,
    Y,
)

__all__ = [
    "_extract_browser_actions",
    "_join_bare_keyword_lines",
    "_json_tool_call_to_directive_line",
    "_normalize_directive_lines",
    "_parse_run_skill_payload",
    "_real_directive_line",
    "_reply_claims_unexecuted_action",
    "_resume_skill_reply_from_turn",
    "_run_mcp_call_spec",
    "_run_skill_reply_from_reply",
    "_run_skill_specs_from_reply",
    "_skill_pending_directives",
    "_skill_state_reply",
    "_split_run_policy",
    "_truncate_repeated_lines",
    "_xml_tool_calls_to_directives",
    "process_reply",
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


# 2026-10-06: SCREENSHOT directive support (ported from parked branch
# feat/screenshot-vision-directive — see the scan block inside
# process_reply for the full story).
_SCREENSHOT_DEFAULT_QUESTION = (
    "Describe what you see in this screenshot in detail: windows, "
    "overlays, applications, visible text, and UI elements."
)


def _capture_and_describe_screen(question: str) -> str:
    """Capture the local desktop and answer `question` about it via the
    local vision model (_ma().MODELS["vision"]). The missing link between
    SCREENSHOT: and actual vision. gnome-screenshot is tried first, scrot
    is the fallback; neither is hardcoded as the only option since either
    can be absent on a different machine this repo runs on."""
    import shutil

    _ma_obj = _ma()
    tmp_path = f"/tmp/master_ai_screenshot_{int(time.time())}.png"
    capture_cmds = [
        ["gnome-screenshot", "-f", tmp_path],
        ["scrot", tmp_path],
    ]
    captured = False
    last_err = ""
    for cmd in capture_cmds:
        if not shutil.which(cmd[0]):
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and os.path.isfile(tmp_path):
                captured = True
                break
            last_err = result.stderr.strip() or f"exit {result.returncode}"
        except Exception as e:
            last_err = str(e)
    if not captured:
        return f"Screenshot capture failed: {last_err or 'no screenshot tool available (tried gnome-screenshot, scrot)'}"
    try:
        # Cap output so CPU-only vision doesn't hold the chat turn for minutes.
        description = _ma_obj.ask_local(
            [{"role": "user", "content": question}],
            model=_ma_obj.MODELS.get("vision"),
            image_path=tmp_path,
            options={"num_predict": 250},
        )
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    return description or "Vision model returned no description of the screenshot."


def _parse_run_skill_payload(payload: Any) -> Any:
    raw = str(payload or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        name = _ma()._normalize_skill_name(obj.get("name") or obj.get("skill"))
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        session_id = str(obj.get("session_id") or "").strip() or None
        resume = bool(obj.get("resume"))
        return (
            {"name": name, "params": params, "session_id": session_id, "resume": resume}
            if name
            else None
        )
    parts = raw.split(None, 1)
    name = _ma()._normalize_skill_name(parts[0])
    if not name:
        return None
    params = {}
    session_id = None
    resume = False
    if len(parts) > 1:
        tail = parts[1].strip()
        if tail.startswith("{"):
            obj = json.loads(tail)
            if isinstance(obj, dict):
                if isinstance(obj.get("params"), dict):
                    params = obj.get("params") or {}
                    session_id = str(obj.get("session_id") or "").strip() or None
                    resume = bool(obj.get("resume"))
                else:
                    params = obj
        elif tail:
            session_id = tail
            resume = True
    return {"name": name, "params": params, "session_id": session_id, "resume": resume}


def _run_skill_specs_from_reply(reply: Any) -> Any:
    specs = []
    for line in str(reply or "").splitlines():
        if not _real_directive_line(line, "RUN_SKILL"):
            continue
        payload = re.split(r"\bRUN_SKILL:", line, maxsplit=1, flags=re.IGNORECASE)[1]
        try:
            spec = _parse_run_skill_payload(payload)
        except Exception as e:
            spec = {"error": f"invalid RUN_SKILL payload: {e}"}
        if spec:
            specs.append(spec)
    return specs


def _real_directive_line(line: Any, name: Any) -> bool:
    for m in re.finditer(rf"\b{name}:", str(line or ""), re.IGNORECASE):
        if str(line or "")[: m.start()].count("`") % 2 == 0:
            return True
    return False


def _skill_pending_directives(state: Any) -> Any:
    pending = (getattr(state, "data", {}) or {}).get("_pending_directives")
    if not isinstance(pending, list):
        return []
    out = []
    for item in pending:
        line = str(item or "").strip()
        if not line or _real_directive_line(line, "RUN_SKILL"):
            continue
        if re.match(
            r"^(?:RUNTERM|RUN|READ|CREATE|EDIT|REMEMBER|BROWSER_[A-Z_]+):",
            line,
            re.IGNORECASE,
        ):
            out.append(line)
    return out


def _append_skill_session_marker(history: list, state: Any) -> None:
    meta = {
        "name": getattr(state, "skill_name", ""),
        "session_id": getattr(state, "session_id", ""),
        "pending_step": (getattr(state, "data", {}) or {}).get("_pending_step")
        or getattr(state, "current_step", ""),
        "done": bool(getattr(state, "done", False)),
        "aborted": bool(getattr(state, "aborted", False)),
    }
    history.append(
        {
            "role": "user",
            "content": _SKILL_SESSION_MARKER + "\n" + json.dumps(meta, sort_keys=True),
        }
    )


def _latest_skill_session_marker(history: Any) -> Any:
    for msg in reversed(history or []):
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if _SKILL_SESSION_MARKER not in str(content):
            continue
        tail = str(content).split(_SKILL_SESSION_MARKER, 1)[1].strip()
        try:
            meta = json.loads(tail.splitlines()[0])
        except Exception:
            continue
        if (
            meta.get("name")
            and meta.get("session_id")
            and not meta.get("done")
            and not meta.get("aborted")
        ):
            return meta
    return None


def _skill_state_reply(state: Any, history: Any) -> Any:
    pending = _skill_pending_directives(state)
    if pending:
        _append_skill_session_marker(history, state)
        return "\n".join(pending)
    if getattr(state, "done", False):
        # 2026-08-29: state.data was being dropped here — every skill's actual
        # result (the "message" a step's report-style final step sets via
        # state_update) never reached the model, which then had nothing to
        # relay but a bare "DONE" line. Surface it when present.
        result_msg = (getattr(state, "data", {}) or {}).get("message")
        if result_msg:
            return (
                f"[SKILL RESULT — {getattr(state, 'skill_name', 'unknown')}]\n"
                f"{result_msg}"
            )
        return f"DONE: skill {getattr(state, 'skill_name', 'unknown')} completed"
    if getattr(state, "aborted", False):
        reason = (
            (getattr(state, "data", {}) or {}).get("_reason")
            or getattr(state, "interrupt_reason", "")
            or "aborted"
        )
        return f"Skill {getattr(state, 'skill_name', 'unknown')} aborted: {reason}"
    reason = (
        getattr(state, "interrupt_reason", "")
        or (getattr(state, "data", {}) or {}).get("_pending_action")
        or "operator input required"
    )
    _append_skill_session_marker(history, state)
    return f"Skill {getattr(state, 'skill_name', 'unknown')} paused: {reason}"


def _run_skill_reply_from_reply(reply: Any, history: Any) -> Any:
    specs = _run_skill_specs_from_reply(reply)
    if not specs:
        return None
    spec = specs[0]
    if spec.get("error"):
        return f"Skill dispatch failed: {spec['error']}"
    try:
        import skill_runtime as _sr

        state = _sr.run_skill(
            spec["name"],
            spec.get("params") or {},
            session_id=spec.get("session_id"),
            resume=bool(spec.get("resume") and spec.get("session_id")),
        )
        _ma().log(
            f"RUN_SKILL: {state.skill_name} session={state.session_id} step={state.current_step} pending={len(_skill_pending_directives(state))}"
        )
        return _skill_state_reply(state, history)
    except Exception as e:
        _ma().log(f"RUN_SKILL_ERROR: {type(e).__name__}: {e}")
        return f"Skill dispatch failed: {type(e).__name__}: {e}"


def _resume_skill_reply_from_turn(user_text: Any, history: Any) -> Any:
    if "[PREVIOUS ROUND RESULTS]" not in str(user_text or ""):
        return None
    meta = _latest_skill_session_marker(history)
    if not meta:
        return None
    try:
        import skill_runtime as _sr

        state = _sr.load_state(meta["name"], meta["session_id"])
        if state.done or state.aborted:
            return None
        pending_step = (state.data or {}).get("_pending_step") or meta.get(
            "pending_step"
        )
        state.data.setdefault("_last_directive_results_by_step", {})[
            pending_step or state.current_step
        ] = str(user_text or "")
        state.data["_last_directive_results"] = str(user_text or "")
        state.data["_last_directive_results_at"] = time.time()
        state.data.pop("_pending_directives", None)
        state.data.pop("_pending_step", None)
        if state.current_step == _sr.INTERRUPT and pending_step:
            state.current_step = pending_step
        state.interrupt_reason = None
        _sr.save_state(state)
        state = _sr.run_skill(
            state.skill_name,
            state.params,
            session_id=state.session_id,
            resume=True,
            step_budget=10,
        )
        _ma().log(
            f"RUN_SKILL_RESUME: {state.skill_name} session={state.session_id} step={state.current_step} pending={len(_skill_pending_directives(state))}"
        )
        return _skill_state_reply(state, history)
    except Exception as e:
        _ma().log(f"RUN_SKILL_RESUME_ERROR: {type(e).__name__}: {e}")
        return f"Skill resume failed: {type(e).__name__}: {e}"


def _json_tool_call_to_directive_line(name: str, arguments: dict) -> Any:
    """Shared conversion for both JSON-shaped formats above: a {"name":
    ..., "arguments": {...}} dict becomes one `X: payload` directive line.
    Prefers a "command" argument when present (matching the XML function
    format's same preference, for the same reason — an accompanying
    "description" argument is not the payload); otherwise falls back to
    the first argument value. Defensive by construction: any shape that
    doesn't parse into a usable (name, payload) pair returns None rather
    than guessing, consistent with this file's existing "safe default: no
    match" convention for widen-only heuristics — an execution safety
    concern, not just a style one."""
    if not isinstance(name, str) or not name.strip():
        return None
    directive_name = _XML_FUNCTION_NAME_ALIASES.get(
        name.strip().lower(), name.strip().upper()
    )
    if isinstance(arguments, dict):
        payload = arguments.get("command")
        if payload is None and arguments:
            payload = next(iter(arguments.values()))
    else:
        payload = arguments
    if not isinstance(payload, str) or not payload.strip():
        return None
    payload = " ".join(line.strip() for line in payload.splitlines() if line.strip())
    if not payload:
        return None
    return f"{directive_name}: {payload}"


def _xml_tool_calls_to_directives(reply: str) -> Any:
    """Translate native tool-call shapes into bare `X: payload` directives
    (one line, whitespace-collapsed inside the payload so a multi-line
    command body still satisfies the one-directive-per-line invariant).
    Handles four distinct shapes different model families are natively
    trained to emit, despite the system prompt never asking for any of
    them: Anthropic-style `<invoke name="X"><parameter name="Y">...
    </parameter></invoke>`, Llama/Hermes-style `<function=X>
    <parameter=Y>...</parameter></function>`, Hermes/NousResearch
    `<tool_call>{"name": X, "arguments": {...}}</tool_call>` (JSON), and
    Mistral `[TOOL_CALLS] [{"name": X, "arguments": {...}}, ...]` (JSON
    array, possibly more than one call). Unknown action names still
    convert — a malformed-but-visible directive line beats invisible raw
    tool-call syntax, because the directive-repair feedback loop can then
    teach the model the right shape. Blocks with no parameter/argument
    take the whole body as the payload where that's well-defined (the two
    XML shapes); the two JSON shapes require a parseable {"name",
    "arguments"} object and skip anything that doesn't fit that shape
    rather than guessing at a payload from malformed JSON."""
    if not reply:
        return reply

    # 2026-09-25: root-caused live via a delegated subagent call routed to
    # deepseek-v4-pro (opencode-go::deepseek-v4-pro): this model wraps the
    # exact same Anthropic-style <invoke name="X"><parameter name="Y">
    # shape _XML_INVOKE_RE already handles, but prefixes every tag name
    # with a literal "｜｜DSML｜｜" marker (fullwidth vertical bars, U+FF5C) --
    # <｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="run">
    # <｜｜DSML｜｜parameter name="cmd" string="true">...
    # </｜｜DSML｜｜parameter></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>.
    # Neither "<invoke" nor "<function" substring-matched against this, so
    # it sailed straight through every check below as opaque text: no
    # directive found, no stall language for _reply_claims_unexecuted_
    # action to catch either, so it fell through process_reply()'s
    # final-fallthrough and got accepted as a "successful" answer that was
    # actually just unexecuted raw tool-call markup. Strip the marker from
    # tag names up front so the existing _XML_INVOKE_RE/_XML_PARAM_RE
    # handling below sees the exact same shape it already knows.
    if "｜｜DSML｜｜" in reply:
        reply = re.sub(r"[｜]{2}DSML[｜]{2}", "", reply)
        # The outer <tool_calls>...</tool_calls> wrapper (plural -- distinct
        # from the singular <tool_call>{json} shape handled below) carries no
        # payload of its own. Left in place, it can land on the SAME line as
        # the invoke block's converted RUN:/etc. payload (</tool_calls> right
        # after the command text), silently corrupting what actually gets
        # executed rather than just being harmless clutter. Strip it outright
        # instead of just forcing a newline around it.
        reply = re.sub(r"</?tool_calls\s*>", "", reply, flags=re.IGNORECASE)

    if "<invoke" in reply:

        def _conv(m: Any) -> Any:
            name = m.group(1).strip().upper()
            body = m.group(2)
            params = _XML_PARAM_RE.findall(body)
            payload = params[0][1] if params else body
            # Collapse line breaks and indentation noise, but preserve intentional
            # spaces inside quoted strings and heredoc bodies.
            payload = " ".join(
                line.strip() for line in payload.splitlines() if line.strip()
            )
            if not name or not payload:
                return ""
            return f"{name}: {payload}"

        reply = _XML_INVOKE_RE.sub(_conv, reply)

    if "<function" in reply.lower():

        def _conv_fn(m: Any) -> Any:
            raw_name = m.group(1).strip().lower()
            name = _XML_FUNCTION_NAME_ALIASES.get(raw_name, raw_name.upper())
            body = m.group(2)
            params = _XML_FUNCTION_PARAM_RE.findall(body)
            # Prefer a "command" parameter when one exists — bash/shell-style
            # calls commonly carry an extra "description" parameter alongside
            # it, which is not the payload to execute.
            payload = next(
                (v for k, v in params if k.strip().lower() == "command"), None
            )
            if payload is None:
                payload = params[0][1] if params else body
            payload = " ".join(
                line.strip() for line in payload.splitlines() if line.strip()
            )
            if not name or not payload:
                return ""
            return f"{name}: {payload}"

        reply = _XML_FUNCTION_RE.sub(_conv_fn, reply)

    if "<tool_call>" in reply.lower() and "{" in reply:

        def _conv_json(m: Any) -> Any:
            try:
                obj = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                return m.group(0)  # leave malformed JSON untouched, don't guess
            if not isinstance(obj, dict):
                return m.group(0)
            line = _json_tool_call_to_directive_line(
                obj.get("name"), obj.get("arguments")
            )
            return line if line else m.group(0)

        reply = _JSON_TOOL_CALL_RE.sub(_conv_json, reply)

    if "[TOOL_CALLS]" in reply.upper():

        def _conv_mistral(m: Any) -> Any:
            try:
                calls = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                return m.group(0)
            if not isinstance(calls, list) or not calls:
                return m.group(0)
            lines = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                line = _json_tool_call_to_directive_line(
                    call.get("name"), call.get("arguments")
                )
                if line:
                    lines.append(line)
            return "\n".join(lines) if lines else m.group(0)

        reply = _MISTRAL_TOOL_CALLS_RE.sub(_conv_mistral, reply)

    return reply


def _join_bare_keyword_lines(reply: Any) -> Any:
    """A third malformed-directive shape, distinct from
    _xml_tool_calls_to_directives (native <invoke> XML) and
    _normalize_directive_lines (crammed same-line directives): some
    models put the bare keyword alone on its own line -- no colon at
    all, so _DIRECTIVE_KEYWORDS_RE's colon-attached match never fires --
    with the real argument on the NEXT line, wrapped in whatever
    tool-call punctuation the model's own template glues on. That
    wrapper is not a fixed contract -- it has shown up as 1-or-2
    leading colons, and separately as a second <tool_call> tag with no
    colon at all -- so this matches EITHER shape generically rather
    than pinning to whichever one was last seen live, which is exactly
    what let this same underlying bug keep reappearing as a new
    variant each time only the previously-seen shape got fixed. Three
    reproductions so far, all on nvidia::minimaxai/minimax-m3 /
    opencode-go::minimax-m3 (same underlying model, different provider
    lane):
        2026-09-09, double colon:
            <tool_call>RUN
            :: echo hi
            </tool_call>
        2026-09-12, single colon:
            <tool_call>RUN
            : ls -la ~/Desktop/AI_CONTEXT/
            </tool_call>
        2026-09-13, second <tool_call> tag, no colon at all -- and this
        time the model repeated the identical two-line pair ~40 times
        within a single reply before stopping, so fixing the shape
        alone is necessary but not sufficient; see the repetition
        guard this triggered elsewhere in process_reply:
            <tool_call>RUN
            <tool_call>ls ~/scripts/ 2>/dev/null; echo "===DONE==="
        2026-09-13, completely unwrapped -- reproduced repeatedly on
        opencode-go::deepseek-v4-pro, including immediately after a
        [Directive repair] message explicitly telling it the correct
        format, still without a colon:
            RUN
            ls ~/.master_ai_tasks/ 2>/dev/null && echo "---TASKS DIR---"
    Join the two lines into the bare directive grammar ("RUN: echo hi")
    so every downstream per-line parser sees what it already expects.
    A bare keyword followed by a colon/tag-wrapped line always joins
    (some wrapper punctuation, however it's shaped, is the model's own
    signal the line is a payload). A bare keyword followed by a
    completely UNWRAPPED line only joins if that line's own content
    carries positive shell-syntax evidence (_SHELL_SYNTAX_MARKER_RE --
    a path, a redirect, an operator); otherwise it's left alone, since
    ordinary prose has no such markers and joining it in would execute
    that prose as a command. This is a widen-only allowlist: it can
    never make a real command that already worked stop matching, only
    catch bare commands that previously fell through entirely."""
    lines = (reply or "").splitlines()
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _BARE_KEYWORD_LINE_RE.match(line)
        if m:
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            next_line = lines[j] if j < n else None
            arg_m = _BARE_KEYWORD_ARG_RE.match(next_line) if next_line else None
            if arg_m:
                out.append(f"{m.group(1).upper()}: {arg_m.group(1).strip()}")
                i = j + 1
                continue
            if (
                next_line
                and next_line.strip()
                and _SHELL_SYNTAX_MARKER_RE.search(next_line)
            ):
                out.append(f"{m.group(1).upper()}: {next_line.strip()}")
                i = j + 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _truncate_repeated_lines(reply: Any, max_repeats: Any = _MAX_LINE_REPEATS) -> Any:
    """Circuit-breaker for a model stuck regenerating the same broken
    line(s) instead of ever finishing a reply.

    Reproduced live 2026-09-13 on opencode-go::minimax-m3: a malformed
    <tool_call> shape that the parser didn't yet recognize (see
    _join_bare_keyword_lines) wasn't just emitted once and dropped --
    the model repeated the identical two-line pair ~40 times in a
    single reply before stopping on its own. Fixing that one shape
    closes THIS gap, but the next unrecognized shape a model invents
    would hit the exact same failure mode: unbounded repetition, no
    real work ever done, the user staring at a session that looks
    frozen.

    This is deliberately shape-agnostic -- it doesn't try to recognize
    tool-call syntax at all, just exact repeated lines, so it catches
    a parser gap AND plain model looping (e.g. repeating a sentence)
    with the same one mechanism, including future shapes nobody has
    seen yet. Counts each exact (stripped) non-blank line's occurrences
    across the whole reply; once any single line's count EXCEEDS
    max_repeats (i.e. the model is genuinely stuck, not just
    legitimately repeating something 2-3 times), every occurrence past
    the first is cut and replaced with one marker line. max_repeats=3
    matches this project's existing hard-cap-at-3 convention for
    repeated attempts (see retry_policy.yaml) for the DETECTION
    threshold, but only the first copy of a detected line survives --
    not up to max_repeats copies.

    2026-09-13: reproduced live why keeping multiple copies is actively
    harmful, not just wasteful, when the repeated line is a real
    directive: three identical `READ: ~/.master_ai_memory` survivors
    each executed and each injected their own copy of that file's
    content into history, so the model's next turn saw the same file
    three times over in one bloated context block -- and, faced with
    that redundant noise instead of a clean single copy, went off to
    read something else entirely rather than answering. By the time
    this function runs, lines are already directive-shaped
    (_normalize_directive_lines already ran), so a genuine repeat here
    is never "the model deliberately doing something 3 times" -- it is
    always the stuck-loop failure mode this guard exists to catch, and
    running it even once more than necessary just spends a tool call
    and pollutes context for nothing."""
    lines = (reply or "").splitlines()
    counts = {}
    for line in lines:
        stripped = line.strip()
        if stripped:
            counts[stripped] = counts.get(stripped, 0) + 1
    over_limit = {ln for ln, c in counts.items() if c > max_repeats}
    if not over_limit:
        return reply
    seen = {}
    out = []
    cut = False
    for line in lines:
        stripped = line.strip()
        if stripped in over_limit:
            seen[stripped] = seen.get(stripped, 0) + 1
            if seen[stripped] > 1:
                cut = True
                continue
        out.append(line)
    if cut:
        out.append(
            "[REPETITION DETECTED] The previous output repeated the same "
            "line(s) more than "
            f"{max_repeats} times without making progress -- truncated "
            "here. Stop and either report what actually blocked you, or "
            "try a genuinely different approach; repeating the same "
            "output again will be truncated again."
        )
    return "\n".join(out)


def _normalize_directive_lines(reply: Any) -> Any:
    """Give every parser downstream (_extract_directive, split-on-newline
    per-line matchers, _extract_browser_actions's line.strip()-anchored
    regex) what they all assume: one directive per line, at column 0.

    2026-08-30: some models (confirmed live on z-ai/glm-5.2:free, a
    typical failure mode for models trained on native <tool_call>
    XML-wrapped tool-calling) ignore the "each directive on its OWN
    line" instruction entirely — they emit prose glued directly to
    BROWSER_NAV:/SEARCH:/etc., multiple directives crammed onto one
    physical line, wrapped in <tool_call> tags nobody asked for. Two
    silent failure modes result, not just an ugly render:
      - _extract_browser_actions requires BROWSER_*: to be the first
        thing on the (stripped) line — a directive glued onto the end
        of a sentence, or prefixed with a leftover <tool_call> tag,
        never matches at all. Silently dropped, not partially run.
      - _extract_directive splits each line on the FIRST directive
        keyword only — a second directive crammed onto the same line
        gets swallowed whole as part of the first one's argument
        (e.g. a garbled URL containing literal "<tool_call>SEARCH: ...").
    Strip the tags (pure noise, not part of this app's directive
    grammar) and force a newline before every directive keyword that
    isn't already at the start of a line, so every existing per-line
    parser sees what it already assumes it's getting.

    2026-09-10: backtick parity must be tracked ACROSS the whole reply,
    not reset at every newline. A code span whose closing backtick lands
    on a different physical line than its opening backtick used to make
    the line containing the closing backtick look "outside" the span,
    causing a `RUN:` inside the span to be treated as a real directive.
    We now carry an open-backtick counter from line to line so spans
    that cross newlines are recognized correctly."""
    text = _TOOL_CALL_TAG_RE.sub("", reply or "")
    out = []
    pos = 0
    backtick_parity = 0  # 0 = outside a backtick span; 1 = inside
    for m in _DIRECTIVE_KEYWORDS_RE.finditer(text):
        start = m.start()
        # Update parity over the gap since the last processed position.
        # Only unescaped backticks flip parity; escaped backticks (`\\` followed by
        # a backtick) are treated as literal characters inside the span.
        for ch in text[pos:start]:
            if ch == "`":
                backtick_parity ^= 1
        # Inside a backtick span, a directive keyword is prose/quoted and
        # must not be forced onto its own line.
        if backtick_parity == 1:
            continue
        line_start = text.rfind("\n", 0, start) + 1
        before_on_line = text[line_start:start]
        if before_on_line.strip():
            out.append(text[pos:start])
            out.append("\n")
            pos = start
    out.append(text[pos:])
    return "".join(out)


def _reply_claims_unexecuted_action(reply_text: str) -> bool:
    """True if reply_text talks like it's taking/about to take an action
    (e.g. "executing now", "let me check", "running the scan") while the
    caller has already confirmed zero directives (RUN:/READ:/SEARCH:/etc.)
    were parsed out of it. False means treat it as a genuinely complete
    answer with nothing left to do.

    2026-09-24: root-caused live — a model replied "Ok, executing the
    service health checks now." with no directive attached. process_reply()
    only has failure-feedback paths for a directive that was found and then
    failed; a reply with NO directive at all just falls through to
    `return reply` and gets accepted as the final answer, even though
    nothing ran. This is the check that closes that gap.

    Get it wrong toward True and a legitimately finished answer that
    happens to use action-ish phrasing ("I ran the check and it passed")
    gets bounced into an unnecessary repair turn — annoying but recoverable
    (the model just restates itself). Get it wrong toward False and the
    exact bug above slips through again — the user sees a promise with no
    result and no error, silently.

    Implementation: look for present/future-tense "about to act" language
    ("executing", "let me", "i'll", "going to"...). If none, this isn't a
    stall claim at all — bail out False immediately (covers real questions
    like "should I proceed?" too, since those don't use action verbs).
    If a stall verb IS present, check for past-tense/result language
    ("ran", "found", "here's", "completed"...) anywhere in the same
    reply — a model that says "I ran the checks, here's what I found"
    used an action verb in service of a real answer, not a stalled one,
    so that overrides. Otherwise, a short reply (<=2 sentences, <200
    chars) that's nothing but the stall claim is exactly tonight's bug
    shape and returns True. Biased toward catching stalls over missing
    them: a false True costs one harmless extra repair turn where the
    model just restates itself; a false False reproduces the silent
    bug this function exists to catch.
    """
    text = (reply_text or "").strip()
    if not text:
        return False
    low = text.lower()

    stall_verbs = (
        "executing",
        "running",
        "checking",
        "verifying",
        "scanning",
        "let me",
        "i'll",
        "i will",
        "going to",
        "about to",
        "starting",
        "kicking off",
        "now check",
        "now runn",
    )
    if not any(v in low for v in stall_verbs):
        return False

    result_markers = (
        "here's",
        "here is",
        "found:",
        "result:",
        "results:",
        "done.",
        "completed",
        "finished",
        "passed",
        "failed:",
        "returned",
        " ran ",
        " checked ",
        " scanned ",
        "shows that",
        "confirms",
    )
    if any(m in low for m in result_markers):
        return False

    sentence_count = len([s for s in re.split(r"[.!?]+", text) if s.strip()])
    return sentence_count <= 2 and len(text) < 200


def _run_mcp_call_spec(spec: Any, history: list) -> None:
    """Invoke one `MCP_CALL: <server> <tool> {json}` and feed back the result.

    The spec has already been through _validation_gate, so the server is
    registered and enabled and the tool is one it exposes. The result goes
    back into history either way, success or failure, so the model can react
    instead of silently reporting a step it never performed -- the same
    contract as every other tool dispatch here.

    Never raises: a broken MCP call must not take down the turn.
    """
    try:
        import json as _json

        import sensei_mcp_client as _mcp

        parts = (spec or "").split(None, 2)
        if len(parts) < 2:
            return
        server, tool = parts[0], parts[1]
        args = _json.loads(parts[2].strip() or "{}") if len(parts) > 2 else {}

        print(f"\n🔌 {BOLD}MCP{X} {C}{server}{X} → {tool}")
        result = _mcp.call_tool(server, tool, args)

        if result.get("ok"):
            body = result.get("result")
            text = (
                body
                if isinstance(body, str)
                else _json.dumps(body, indent=2, default=str)
            )
            print(f"{D}{text[:2000]}{X}")
            _ma().log(f"MCP_CALL_OK: {server}/{tool}")
            history.append(
                {
                    "role": "user",
                    "content": f"[MCP RESULT] {server}/{tool}\n{text[:4000]}",
                }
            )
        else:
            err = result.get("error") or "unknown error"
            print(f"  {R}MCP call failed: {err}{X}")
            _ma().log(f"MCP_CALL_FAIL: {server}/{tool} {err}")
            history.append(
                {
                    "role": "user",
                    "content": (
                        f"[MCP BLOCKED] {server}/{tool} did not run: {err}\n"
                        "Do not report this step as done."
                    ),
                }
            )
    except Exception as e:  # noqa: BLE001
        _ma().log(f"MCP_CALL_ERROR: {e}")
        try:
            history.append(
                {
                    "role": "user",
                    "content": f"[MCP BLOCKED] call could not be made: {e}",
                }
            )
        except Exception:
            pass


def process_reply(
    reply: str,
    history: list,
    streamed: bool = False,
    continue_after_tools: bool = False,
) -> Any:
    """Parse RUN: / READ: / CREATE: directives from AI reply and execute."""
    _ma()._CHAIN_SUDO_ACKS = 0
    reply = _xml_tool_calls_to_directives(reply)
    reply = _join_bare_keyword_lines(reply)
    reply = _normalize_directive_lines(reply)
    reply = _truncate_repeated_lines(reply)
    raw_lines = reply.splitlines()

    def _join_shell_continuations(src_lines: Any) -> Any:
        """Join directive shell commands split with trailing backslashes.

        Models often emit:
          RUN: ffmpeg ... \
            -vf ... \
            output.mp4
        The old parser treated only the first physical line as RUN, so
        bash received a dangling backslash and ffmpeg saw '\' as output.
        """
        out = []
        i = 0
        directive_re = re.compile(r"^\s*(RUN|RUNTERM):\s*(.*)$", re.IGNORECASE)
        other_directive_re = re.compile(
            r"^\s*(RUN|RUNTERM|READ|CREATE|EDIT|ASK|DONE|SEARCH|REMEMBER):",
            re.IGNORECASE,
        )
        while i < len(src_lines):
            line = src_lines[i]
            m = directive_re.match(line)
            if not m:
                out.append(line)
                i += 1
                continue
            name, rest = m.group(1), m.group(2).rstrip()
            pieces = [rest[:-1].rstrip() if rest.endswith("\\") else rest]
            continued = rest.endswith("\\")
            # 2026-09-02: live stall reproduced and root-caused -- the model
            # emitted `<tool_call>RUN:` on its own line with the real command
            # on the NEXT line and no trailing backslash. _normalize_directive_lines
            # strips the `<tool_call>` tag upstream, leaving a bare `RUN:` with
            # an empty same-line payload; that fell through to the noop/empty
            # check below and got silently discarded, while the orphaned command
            # line (now missing its RUN: prefix) survived narrative construction
            # as plain text -- shown to the user, never executed, no audit entry,
            # no repair triggered (the malformed-<tool_call> detector runs against
            # narrative, and the tag was already gone by then). Treat an empty
            # same-line payload as an implicit continuation onto the next
            # non-blank, non-directive line, same as an explicit backslash would.
            empty_payload_pending = (not rest) and not continued
            i += 1
            while (continued or empty_payload_pending) and i < len(src_lines):
                nxt_raw = src_lines[i]
                nxt = nxt_raw.strip()
                if empty_payload_pending and (
                    not nxt or other_directive_re.match(nxt_raw)
                ):
                    break
                continued = nxt.endswith("\\")
                pieces.append(nxt[:-1].rstrip() if continued else nxt)
                i += 1
                empty_payload_pending = False
            out.append(f"{name}: {' '.join(p for p in pieces if p).strip()}")
        return out

    lines = _join_shell_continuations(raw_lines)

    skill_reply = _run_skill_reply_from_reply("\n".join(lines), history)
    if skill_reply is not None:
        # 2026-09-21: sibling of the same bug fixed in handle() at the
        # outer "[SKILL RESULT" check (a model's FIRST reply of a turn
        # dispatches a skill before ever reaching process_reply at all).
        # THIS site is process_reply's own internal RUN_SKILL handling —
        # reached when a skill directive shows up mid-chain, already
        # inside a reply process_reply is parsing. Recursing straight into
        # process_reply(skill_reply, ...) just re-parses skill_reply for
        # MORE directives (correctly finds none) and falls through to its
        # own `return reply` fallthrough — the raw "[SKILL RESULT — X]\n..."
        # text becomes this call's return value verbatim, same missing-
        # synthesis bug, different entry point. Mirror the SUBAGENT RESULT
        # pattern already used elsewhere in this same function: hand the
        # real result back to the model as context and return None so the
        # caller's continuation loop re-asks instead of standing pat on
        # the raw dump. Scoped to the same "[SKILL RESULT" shape only —
        # pending-directive/aborted/paused skill replies still recurse
        # normally, they're not raw data needing interpretation.
        if skill_reply.startswith("[SKILL RESULT"):
            history.append(
                {
                    "role": "user",
                    "content": (
                        skill_reply
                        + "\n\nThe skill result above is real. Answer the user's "
                        "original question using it — don't just repeat the raw "
                        "list back verbatim; say what it means for what they asked."
                    ),
                }
            )
            _ma().log(
                "CHAIN_SKILL_RESULT_FEEDBACK: forcing continuation to synthesize a real answer (internal path)"
            )
            return None
        return process_reply(
            skill_reply,
            history,
            streamed=streamed,
            continue_after_tools=continue_after_tools,
        )

    # Typed-tool-boundary migration, still phase-1 shadow for the TYPE
    # ENVELOPE only. What IS live since 2026-08-28/09-28: the pre-dispatch
    # validation gate (_validation_gate below -> validation_gate.gate) —
    # every extracted payload passes through action_validation before any
    # dispatch, blocked ones never execute. What is STILL shadow-only: this
    # typed_actions.parse_reply() call — its envelopes land in
    # _LAST_TYPED_ACTIONS for tests/observability; dispatch decisions are
    # made by the legacy extractor below. That legacy extractor is NOT yet
    # proven redundant — parity fixtures never import this module, a bare
    # "RUN:" diverges between the two parsers, and kinds the typed parser
    # cannot model (RUN_SKILL, MCP_CALL, REMEMBER, ...) pass the gate
    # untouched — so the selector swap stays future work. Full pinned
    # evidence: validation_gate.py module docstring.
    try:
        import typed_actions as _ta

        typed_shadow = _ta.parse_reply(
            "\n".join(lines),
            model=getattr(_ma(), "_LAST_MODEL", ""),
            cwd=os.getcwd(),
        )
        _ma()._LAST_TYPED_ACTIONS = [a.to_dict() for a in typed_shadow]
        _ma()._LAST_TYPED_ACTIONS_ERROR = ""
    except Exception as e:
        _ma()._LAST_TYPED_ACTIONS = []
        _ma()._LAST_TYPED_ACTIONS_ERROR = str(e)

    # Strip surrounding backticks/quotes from extracted commands — local
    # models sometimes wrap commands in `backticks` or 'quotes'. Shell
    # would mis-interpret those (backticks trigger command substitution).
    def _strip_command_wrap(s: str) -> Any:
        s = s.strip()
        for pair in (("`", "`"), ("'", "'"), ('"', '"'), ("“", "”"), ("‘", "’")):
            if s.startswith(pair[0]) and s.endswith(pair[1]) and len(s) >= 2:
                s = s[1:-1].strip()
                break
        return s

    def _extract_directive(line: Any, name: Any) -> Any:
        parts = re.split(rf"\b{name}:", line, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            return ""
        s = _strip_command_wrap(parts[1])
        arg_xml = _ARG_XML_TAG_RE.search(s)
        if arg_xml:
            s = s[: arg_xml.start()].rstrip()
        think_tag = _THINK_TAG_RE.search(s)
        if think_tag:
            s = s[: think_tag.start()].rstrip()
        # Drop bash no-ops / placeholder garbage (`:`, `true`, empty) so
        # the dispatch loop never spawns a terminal that runs nothing.
        return "" if _ma()._is_noop_cmd(s) else s

    # NAME: must appear OUTSIDE any backtick span in the reply. Backtick-
    # wrapped occurrences are prose (the model describing its own directives
    # by name) and must not fire. Backtick parity is tracked across all
    # physical lines because code spans legitimately cross newlines; the
    # old per-line count reset caused the closing-backtick line to look
    # "outside" the span and false-positive a directive there.
    # Count of unescaped backticks before the match is even → outside; odd →
    # inside an open backtick span.
    # Precompute prefix backtick parity once per reply so every directive check
    # is O(1) instead of O(n) over the full reply.
    _reply_len = len(reply)
    _prefix_backtick_parity = [0] * (_reply_len + 1)
    for _idx, _ch in enumerate(reply):
        _prefix_backtick_parity[_idx + 1] = _prefix_backtick_parity[_idx] ^ (
            1 if _ch == "`" else 0
        )

    def _real_directive(line: Any, name: Any, line_start: int = 0) -> bool:
        for m in re.finditer(rf"\b{name}:", line, re.IGNORECASE):
            global_pos = line_start + m.start()
            if _prefix_backtick_parity[global_pos] % 2 == 0:
                return True
        return False

    def _directive_payload(line: Any, name: Any, line_start: int = 0) -> Any:
        if not _real_directive(line, name, line_start=line_start):
            return ""
        return _strip_command_wrap(
            re.split(rf"\b{name}:", line, maxsplit=1, flags=re.IGNORECASE)[1]
        ).strip()

    # Use re.search with a word boundary — catches "RUN:" anywhere on the
    # line, not just at the start. This handles the 2026-04-20 case where
    # the local model echoed "PLAN ONLY: RUN: cmd" and the prior `re.match`
    # at start-of-line missed it entirely, leaving the command un-parsed.
    # \bRUN: deliberately does NOT match RUNTERM: — "RUN" is followed by "T"
    # in "RUNTERM:", not ":", so the regex skips it. RUNTERM: has its own
    # extraction below.
    # Walk the reply tracking the global character offset for each line so
    # _real_directive/_directive_payload can compute backtick parity across
    # newlines consistently. str.splitlines() consumes newlines; the +1 is
    # correct for "\n" separators and harmless for the final line.
    line_offsets = []
    _off = 0
    for _ln in lines:
        line_offsets.append(_off)
        _off += len(_ln) + 1

    read_paths = [
        p
        for p in (
            _extract_directive(l, "READ")
            for lo, l in zip(line_offsets, lines, strict=False)
            if _real_directive(l, "READ", line_start=lo)
        )
        if p
    ]
    run_cmds = [
        c
        for c in (
            _extract_directive(l, "RUN")
            for lo, l in zip(line_offsets, lines, strict=False)
            if _real_directive(l, "RUN", line_start=lo)
        )
        if c
    ]
    runterm_cmds = [
        c
        for c in (
            _extract_directive(l, "RUNTERM")
            for lo, l in zip(line_offsets, lines, strict=False)
            if _real_directive(l, "RUNTERM", line_start=lo)
        )
        if c
    ]

    # 2026-09-03: SUBAGENT: <goal> — model can delegate a focused task to
    # the internal delegate_runner, which runs isolated in a temp workdir
    # and returns a structured result back into the conversation.
    subagent_goals = [
        g
        for g in (
            _extract_directive(l, "SUBAGENT")
            for l in lines
            if _real_directive(l, "SUBAGENT")
        )
        if g
    ]

    # 2026-08-29: SEARCH: <query> — lightweight live-info lookup via
    # web_search(), no Chrome/tab required. Added because the model's only
    # taught tool for "find out X" was BROWSER_NAV — it kept opening a
    # Google Images tab and screenshotting it for plain lookups, which fails
    # outright whenever Chrome/the Sensei side panel isn't open. See
    # SEARCH_VS_BROWSER_SYSTEM_ADDITION for the usage split taught to the model.
    search_queries = [
        q
        for q in (
            _extract_directive(l, "SEARCH")
            for l in lines
            if _real_directive(l, "SEARCH")
        )
        if q
    ]

    # 2026-10-06: SCREENSHOT: [optional question] — capture the local
    # DESKTOP (not a browser tab — that's BROWSER_SCREENSHOT, which needs
    # the Chrome extension side panel and only sees web content) and
    # describe it via the local vision model. Ported from the parked
    # feat/screenshot-vision-directive branch (built 2026-09-17 after a
    # live session where the model, asked to look at the screen, could
    # only improvise ad-hoc RUN: commands and never actually saw the
    # image — no working path from "screenshot" to "vision").
    screenshot_requests = [
        _extract_directive(l, "SCREENSHOT") or _SCREENSHOT_DEFAULT_QUESTION
        for l in lines
        if _real_directive(l, "SCREENSHOT")
    ]

    # 2026-09-02: TASK_ADD: <text> / TASK_DONE: <text or number> — built
    # for large multi-part requests (an audit, a numbered checklist, "do
    # these 50 things") that don't reliably survive as one giant reply.
    # Reproduced live: a 50-question audit prompt truncated silently
    # mid-sentence with no error and no way to tell what had actually been
    # answered. Operator's own words: "it's not making a to-do list, and
    # it's not reflecting it -- it should definitely have a task list."
    # Gives the model a directive it can emit to decompose a big request
    # into the SAME persistent task list `task add`/`task list` already
    # show the user, and to check items off as it goes -- see TASK
    # DECOMPOSITION DISCIPLINE in the system prompt for when to use this.
    task_add_texts = [
        t
        for t in (
            _extract_directive(l, "TASK_ADD")
            for l in lines
            if _real_directive(l, "TASK_ADD")
        )
        if t
    ]
    task_done_targets = [
        t
        for t in (
            _extract_directive(l, "TASK_DONE")
            for l in lines
            if _real_directive(l, "TASK_DONE")
        )
        if t
    ]

    # 2026-05-17: SEND_EMAIL: to=<addr> subject="..." body="..." attach=<path>
    # Parses to a dict spec; dispatcher calls confirm_send_email which gates
    # by mode (plan refuses, review/auto always prompt — irreversible).
    def _parse_send_email_spec(line: Any) -> Any:
        payload = _extract_directive(line, "SEND_EMAIL")
        if not payload:
            return None
        spec = {}
        pat = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
        for m in pat.finditer(payload):
            k = m.group(1).lower()
            v = (
                m.group(2)
                if m.group(2) is not None
                else (m.group(3) if m.group(3) is not None else m.group(4))
            )
            spec[k] = v
        if not spec.get("to") or not spec.get("subject"):
            return None
        spec.setdefault("body", "")
        spec.setdefault("attach", None)
        return spec

    send_email_specs = [
        s
        for s in (
            _parse_send_email_spec(l)
            for lo, l in zip(line_offsets, lines, strict=False)
            if _real_directive(l, "SEND_EMAIL", line_start=lo)
        )
        if s
    ]

    # 2026-09-08: SEND_TELEGRAM: <chat_id> <message> — one-way outbound Telegram
    # from Sensei CLI. Uses TELEGRAM_BOT_TOKEN from ~/.master_ai_keys. Irreversible
    # send, so it follows the same confirm gate as SEND_EMAIL (plan refuses,
    # review/auto prompt once).
    def _parse_send_telegram_spec(line: Any) -> None:
        payload = _extract_directive(line, "SEND_TELEGRAM")
        if not payload:
            return None
        # If a default chat ID is configured, the directive can be just the message text.
        default_chat_id = None
        try:
            import telegram_client

            default_chat_id = telegram_client._get_default_chat_id()
        except Exception:
            pass
        parts = payload.split(None, 1)
        if not parts:
            return None
        if len(parts) == 1:
            if not default_chat_id:
                return None
            chat_id, text = default_chat_id, parts[0].strip()
        else:
            chat_id, text = parts[0], parts[1].strip()
        # Strip accidental surrounding quotes
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
            text = text[1:-1]
        if not chat_id or not text:
            return None
        return {"chat_id": chat_id, "text": text}

    send_telegram_specs = [
        s
        for s in (
            _parse_send_telegram_spec(l)
            for lo, l in zip(line_offsets, lines, strict=False)
            if _real_directive(l, "SEND_TELEGRAM", line_start=lo)
        )
        if s
    ]

    # 2026-08-27: BROWSER_* — see _extract_browser_actions()/confirm_browser_action()
    # above confirm_run. Long taught to the model, never executed until now.
    browser_actions = _extract_browser_actions(lines)

    # 2026-05-11: REMEMBER: <fact> — model-emitted memory write. Same
    # extraction shape as RUN/READ, BUT block-aware: REMEMBER lines that
    # appear INSIDE a <<<CONTENT>>>CONTENT / <<<FIND>>>FIND /
    # <<<REPLACE>>>REPLACE body must NOT fire — they're document/edit
    # content, not directives. Pre-existing RUN/READ extraction has the
    # same blindspot (rare in practice + gated by user confirm); REMEMBER
    # writes silently so the gate matters more here.
    _in_body, _eligible, _eligible_offsets = False, [], []
    for _lo, _ln in zip(line_offsets, lines, strict=False):
        _stripped_up = _ln.strip().upper()
        if _stripped_up in ("<<<CONTENT", "<<<FIND", "<<<REPLACE"):
            _in_body = True
            continue
        if _stripped_up in (">>>CONTENT", ">>>FIND", ">>>REPLACE"):
            _in_body = False
            continue
        if not _in_body:
            _eligible.append(_ln)
            _eligible_offsets.append(_lo)
    remember_facts = [
        f
        for f in (
            _directive_payload(l, "REMEMBER")
            for lo, l in zip(_eligible_offsets, _eligible, strict=False)
            if _real_directive(l, "REMEMBER", line_start=lo)
        )
        if f
    ]

    create_directive_paths = [
        os.path.expanduser(_directive_payload(l, "CREATE"))
        for l in lines
        if re.match(r"^\s*CREATE:", l, re.IGNORECASE)
        and _directive_payload(l, "CREATE")
    ]
    edit_directive_paths = [
        os.path.expanduser(_directive_payload(l, "EDIT"))
        for l in lines
        if re.match(r"^\s*EDIT:", l, re.IGNORECASE) and _directive_payload(l, "EDIT")
    ]
    # 2026-09-28: MCP_CALL: <server> <tool> {json args}
    mcp_call_specs = [
        _directive_payload(l, "MCP_CALL")
        for l in lines
        if _real_directive(l, "MCP_CALL") and _directive_payload(l, "MCP_CALL")
    ]

    # 2026-09-24: snapshot of "did this reply contain ANY directive at
    # all" — taken here, right after the raw per-directive extraction and
    # before any of these lists get consumed/reassigned below (run_cmds in
    # particular gets normalized further down), so the fallthrough check
    # near the end of this function reflects what the model actually
    # emitted rather than post-processing state. See
    # _reply_claims_unexecuted_action() for why this exists.
    _any_directive_found = bool(
        read_paths
        or run_cmds
        or runterm_cmds
        or subagent_goals
        or search_queries
        or task_add_texts
        or task_done_targets
        or send_email_specs
        or send_telegram_specs
        or remember_facts
        or create_directive_paths
        or edit_directive_paths
        or browser_actions
    )

    # Parse CREATE: ... <<<CONTENT ... >>>CONTENT blocks
    create_files = []
    # Parse EDIT: ... <<<FIND ... >>>FIND <<<REPLACE ... >>>REPLACE blocks
    edit_ops = []
    in_block, cur_path, cur_content = False, None, []
    cur_find, cur_replace, in_find, in_replace = None, None, False, False
    for line in lines:
        if re.match(r"^\s*CREATE:", line, re.IGNORECASE):
            cur_path = os.path.expanduser(
                re.split(r"CREATE:", line, maxsplit=1, flags=re.IGNORECASE)[1].strip()
            )
            cur_content = []
            in_block = False
        elif line.strip().upper() == "<<<CONTENT" and cur_path:
            in_block = True
        elif line.strip().upper() == ">>>CONTENT" and in_block:
            in_block = False
            create_files.append((cur_path, "\n".join(cur_content)))
            cur_path = None
        elif in_block:
            cur_content.append(line)
        elif re.match(r"^\s*EDIT:", line, re.IGNORECASE):
            cur_path = os.path.expanduser(
                re.split(r"EDIT:", line, maxsplit=1, flags=re.IGNORECASE)[1].strip()
            )
            cur_find = []
            cur_replace = []
            in_find = False
            in_replace = False
        elif line.strip().upper() == "<<<FIND" and cur_path:
            in_find = True
        elif line.strip().upper() == ">>>FIND" and in_find:
            in_find = False
        elif line.strip().upper() == "<<<REPLACE" and cur_path:
            in_replace = True
        elif line.strip().upper() == ">>>REPLACE" and in_replace:
            in_replace = False
            if cur_find is not None and cur_replace is not None:
                edit_ops.append((cur_path, "\n".join(cur_find), "\n".join(cur_replace)))
            cur_path = None
            cur_find = None
            cur_replace = None
        elif in_find and cur_find is not None:
            cur_find.append(line)
        elif in_replace and cur_replace is not None:
            cur_replace.append(line)

    # Salvage common local-model drift:
    #   CREATE: ~/Desktop/demo.html
    #   Here is the file:
    #   ```html
    #   ...
    #   ```
    # The strict directive shape above is preferred, but this fallback turns a
    # useful fenced file into the same create operation instead of silently
    # doing nothing.
    created_paths = {os.path.realpath(os.path.expanduser(p)) for p, _ in create_files}
    for m in re.finditer(r"(?im)^\s*CREATE:\s*(.+?)\s*$", reply):
        raw_path = _strip_command_wrap(m.group(1)).strip()
        if not raw_path:
            continue
        exp_path = os.path.expanduser(raw_path)
        real_path = os.path.realpath(exp_path)
        if real_path in created_paths:
            continue
        tail = reply[m.end() :]
        next_directive = re.search(
            r"(?im)^\s*(RUN|RUNTERM|READ|CREATE|EDIT|ASK|DONE):", tail
        )
        create_tail = tail[: next_directive.start()] if next_directive else tail
        reversed_block = re.search(
            r"(?is)^\s*>>>CONTENT\s*\n(.*?)\n\s*<<<CONTENT\s*",
            create_tail,
        )
        if reversed_block:
            content = reversed_block.group(1).strip("\n")
            if content:
                create_files.append((exp_path, content))
                created_paths.add(real_path)
            continue
        fences = list(
            re.finditer(r"```([A-Za-z0-9_-]+)?\s*\n(.*?)\n```", create_tail, re.DOTALL)
        )
        if fences:
            content = fences[0].group(2).strip("\n")
            if exp_path.lower().endswith((".html", ".htm")):
                css_chunks = []
                js_chunks = []
                for fm in fences[1:]:
                    lang = (fm.group(1) or "").lower()
                    body = fm.group(2).strip("\n")
                    if lang == "css":
                        css_chunks.append(body)
                    elif lang in ("js", "javascript"):
                        js_chunks.append(body)
                if css_chunks:
                    style_block = "<style>\n" + "\n\n".join(css_chunks) + "\n</style>"
                    content = re.sub(
                        r'\s*<link[^>]+href=["\']styles\.css["\'][^>]*>\s*',
                        "\n    " + style_block + "\n",
                        content,
                        flags=re.IGNORECASE,
                    )
                    if style_block not in content:
                        content = content.replace(
                            "</head>", f"    {style_block}\n</head>", 1
                        )
                if js_chunks:
                    script_block = "<script>\n" + "\n\n".join(js_chunks) + "\n</script>"
                    content = re.sub(
                        r'\s*<script[^>]+src=["\']scripts\.js["\'][^>]*>\s*</script>\s*',
                        "\n    " + script_block + "\n",
                        content,
                        flags=re.IGNORECASE,
                    )
                    if script_block not in content:
                        content = content.replace(
                            "</body>", f"    {script_block}\n</body>", 1
                        )
            if content:
                create_files.append((exp_path, content))
                created_paths.add(real_path)

    parsed_create_paths = {
        os.path.realpath(os.path.expanduser(p)) for p, _ in create_files
    }
    malformed_creates = [
        p
        for p in create_directive_paths
        if os.path.realpath(os.path.expanduser(p)) not in parsed_create_paths
    ]
    if malformed_creates:
        print(
            _ma()._pill(
                "BLOCKED",
                f"{D}malformed CREATE block: missing <<<CONTENT / >>>CONTENT{X}",
            )
        )
        _ma().log(f"DIRECTIVE_REPAIR_MALFORMED_CREATE: {malformed_creates[:5]}")
        history.append(
            {
                "role": "user",
                "content": (
                    "[Directive repair]\n"
                    "You emitted CREATE without a complete content block for:\n"
                    + "\n".join(f"- {p}" for p in malformed_creates[:5])
                    + "\n\nRepair the same task now. Emit CREATE on its own line, then "
                    "a full <<<CONTENT / >>>CONTENT block. Do not describe the file; "
                    "include the actual file contents. Keep the same filename."
                ),
            }
        )
        return None

    parsed_edit_paths = {
        os.path.realpath(os.path.expanduser(p)) for p, _, _ in edit_ops
    }
    malformed_edits = [
        p
        for p in edit_directive_paths
        if os.path.realpath(os.path.expanduser(p)) not in parsed_edit_paths
    ]
    if malformed_edits:
        print(
            _ma()._pill(
                "BLOCKED", f"{D}malformed EDIT block: missing FIND / REPLACE markers{X}"
            )
        )
        _ma().log(f"DIRECTIVE_REPAIR_MALFORMED_EDIT: {malformed_edits[:5]}")
        history.append(
            {
                "role": "user",
                "content": (
                    "[Directive repair]\n"
                    "You emitted EDIT without complete <<<FIND / >>>FIND and "
                    "<<<REPLACE / >>>REPLACE blocks for:\n"
                    + "\n".join(f"- {p}" for p in malformed_edits[:5])
                    + "\n\nRepair the same task now with a complete EDIT block, or READ "
                    "the target file first if you need exact text."
                ),
            }
        )
        return None

    has_directives = bool(
        read_paths
        or run_cmds
        or runterm_cmds
        or create_files
        or edit_ops
        or remember_facts
        or task_add_texts
        or task_done_targets
        or send_email_specs
        or send_telegram_specs
    )

    # ── Tier-1 pre-dispatch validation gate (2026-09-28) ──────────────
    # Placed here because every list is final: the per-directive extraction
    # above and the CREATE/EDIT block builders have all run. Anything that
    # fails validation is removed BEFORE the first dispatch consumes these
    # lists, which is the whole point -- the model must not be able to report
    # a step it never actually performed.
    _gate_input = {
        "read_paths": read_paths,
        "run_cmds": run_cmds,
        "runterm_cmds": runterm_cmds,
        "create_files": create_files,
        "edit_ops": edit_ops,
        "send_email_specs": send_email_specs,
        "send_telegram_specs": send_telegram_specs,
        "mcp_call_specs": mcp_call_specs,
    }
    _gate_kept, _gate_blocked = _ma()._validation_gate(_gate_input)
    if _gate_blocked:
        _ma()._LAST_BLOCKED_ACTION = {
            "count": len(_gate_blocked),
            "reasons": [f"{n}: {r}" for n, _p, r in _gate_blocked[:5]],
        }
        for _name, _payload, _reason in _gate_blocked:
            _ma().log(f"TOOL_BLOCKED_VALIDATION: {_name} {_reason}")
        print(
            _ma()._pill(
                "BLOCKED",
                f"{D}{len(_gate_blocked)} action(s) failed validation before "
                f"dispatch{X}",
            )
        )
        for _name, _payload, _reason in _gate_blocked:
            print(f"  {Y}{_name}: {_reason}{X}")
        _detail = "\n".join(
            f"- {_name}: {_reason}" for _name, _p, _reason in _gate_blocked[:5]
        )
        history.append(
            {
                "role": "user",
                "content": (
                    "[TOOL BLOCKED]\n"
                    "These actions were rejected by validation and did NOT run:\n"
                    f"{_detail}\n\n"
                    "Fix the payloads and re-emit them. Do not report a step as "
                    "done unless a directive for it actually executed.\n"
                    'If there is a one-line lesson here (e.g. "CREATE with a '
                    'body that does not parse"), emit a single '
                    "`REMEMBER: <one-line lesson>` directive in your next "
                    "reply so this doesn't repeat next turn."
                ),
            }
        )
        # Reassign so downstream dispatch sees only validated actions.
        read_paths = _gate_kept["read_paths"]
        run_cmds = _gate_kept["run_cmds"]
        runterm_cmds = _gate_kept["runterm_cmds"]
        create_files = _gate_kept["create_files"]
        edit_ops = _gate_kept["edit_ops"]
        send_email_specs = _gate_kept["send_email_specs"]
        send_telegram_specs = _gate_kept["send_telegram_specs"]
        mcp_call_specs = _gate_kept["mcp_call_specs"]
        if not (
            read_paths
            or run_cmds
            or runterm_cmds
            or create_files
            or edit_ops
            or remember_facts
            or task_add_texts
            or task_done_targets
            or send_email_specs
            or send_telegram_specs
        ):
            # Everything this reply proposed was invalid: stop the chain
            # rather than falling through and returning the narrative text as
            # if it were a completed answer.
            return None
    # REMEMBER: <fact> — fire first, before any tool dispatch. Memory
    # writes are inert text appends; no fence, no approval needed, same
    # path as the user `remember:` command. The model may emit multiple
    # REMEMBER lines in one reply; each gets validated + stored.
    for _fact in remember_facts:
        _ma().confirm_remember(_fact)

    # ── MCP_CALL: <server> <tool> {json} (2026-09-28) ─────────────────
    # Runs after the validation gate above has already checked that the
    # server is registered+enabled and the tool is one it exposes, so this
    # only has to invoke and report.
    for _spec in mcp_call_specs:
        _run_mcp_call_spec(_spec, history)

    # Print non-directive narrative text. Backtick-wrapped directive names
    # (e.g. "use `RUN:` for shell commands") are prose and must stay in the
    # narrative — same backtick-parity check as the directive parser above.
    def _line_is_directive(l: Any) -> bool:
        for m in re.finditer(r"\b(?:run|runterm|read|create|edit):", l, re.IGNORECASE):
            if l[: m.start()].count("`") % 2 == 0:
                return True
        return False

    skip_prefixes = (
        "<<<content",
        ">>>content",
        "<<<find",
        ">>>find",
        "<<<replace",
        ">>>replace",
    )
    narrative = "\n".join(
        l
        for l in lines
        if not _line_is_directive(l)
        and not any(l.strip().lower().startswith(p) for p in skip_prefixes)
    ).strip()

    # 2026-09-01: prompt-only fix (COMPLETION RULE in the system prompt)
    # did not hold — same night, same bug: "I'll set that up now. One
    # moment..." with zero directives, rendered as if it were the final
    # answer, turn over. narrative is non-empty here so the branch above
    # would have fired and shown it as done. Catch the announcement-only
    # shape structurally instead of trusting the model to stop doing it:
    # no directives at all + starts with a stock "about to work" lead-in +
    # short enough to be a stall rather than a real short answer. Force
    # another turn instead of ending on it. Bounded by the existing
    # max_continuation_turns cap in the caller's loop — can't hang forever.
    # Searches anywhere in the narrative, not just the start — real examples
    # tonight led with "Got it — ..." before the actual stall phrase.
    # "i'll <word> that/this/it/up/now" catches the generic case ("I'll set
    # that up now"); the explicit verb list catches "I'll <verb> <object>"
    # without a trailing filler word ("I'll check the logs").
    _stall_pattern = re.compile(
        r"\b(on it\b|on it\s+[🔍🚀⚙️✅👍]|i\'?ll\s+\w+\s+(?:that|this|it|up|now)\b|"
        r"i\'?ll (?:set|get|check|investigate|look|create|start|do|run|write|build|make|dig|take)|"
        r"let me (?:\w+\s+)?(?:check|see|look|investigate|create|dig|take|pivot)|"
        r"one moment|give me a (?:second|moment|sec)|working on it|hold on)\b"
        # 2026-09-15: gerund-lead announcements ("Checking environment
        # first.", "Verifying the setup now.") reproduced live — they
        # don't match any "i'll "/"let me " form above, so a bare backtick-
        # wrapped directive right after one slipped through both repair
        # paths and rendered as a finished answer with nothing executed.
        # A review pass caught a first version of this that matched the
        # gerund lead ANYWHERE via .search(), false-firing on legitimate
        # complete answers that merely open with or contain one of these
        # words ("Looking at the logs, the issue is the missing key.",
        # "Checking the config, it's fine."). A real stall IS the entire
        # narrative -- no comma-joined follow-on clause with actual
        # information -- so this alternative is anchored to the WHOLE
        # narrative and requires no comma anywhere after the gerund lead;
        # kept outside the shared \b(...)\b group above since anchoring
        # ^...$ inside it wouldn't compose the same way.
        r"|^\s*(?:checking|verifying|confirming|inspecting|scanning|looking at)\s+[^,\n]*$"
        # 2026-09-27: reproduced live — "Let me first examine and fix the
        # start-learning recipe.py: [now proceeding with the fix]" matched
        # NEITHER "i'll <verb>" list above ("examine" isn't enumerated) NOR
        # "let me <verb>" list ("examine" isn't there either), so is_stall
        # stayed False and the announcement rendered as a finished answer
        # with nothing dispatched. A closed verb list can never be complete
        # — the model can always phrase intent with a verb nobody
        # enumerated yet. Replace enumeration with the open-class shape
        # already proven safe for the gerund branch above: any verb after
        # the first-person modal, gated by the same "no comma, no
        # completed-action marker, to end of narrative" structural check —
        # a real stall IS the whole remaining narrative. "let me know/
        # clarify/explain/summarize/add/recap/reiterate/note" are excluded
        # because they're common discourse-framing openers for a real
        # answer ("Let me know if...", "Let me clarify: ..."), not a stall
        # — everything else after "i'll"/"let me" is intentionally open,
        # accepting the residual false-positive risk (one extra forced
        # retry turn, bounded by max_continuation_turns) over the worse
        # failure mode this branch exists to close: silently doing nothing
        # while looking done. Kept alongside the closed-list alternatives
        # above rather than replacing them — those already cover comma-
        # tailed cases this end-anchored branch can't match.
        r"|\b(?:i\'?ll\s+(?:\w+\s+){0,2}\w+\b|"
        r"let me(?!\s+(?:know|clarify|explain|summarize|add|recap|reiterate|note)\b)\s+(?:\w+\s+){0,2}\w+\b)"
        r"(?![^,\n]*\b(?:successfully|complete|completed|done|finished|fixed|ready|clean|passed|confirmed|ok|okay|resolved|sent|good|verified)\b)"
        r"[^,\n]*$",
        re.IGNORECASE,
    )
    # Second shape seen tonight: the model attempts directives but wraps
    # them as `<tool_call>RUN: ...` mid-line instead of a bare `RUN:` at
    # column 0 — the parser never recognizes these as real directives, so
    # has_directives is False here too, but this isn't a stall-phrase, it's
    # a malformed-syntax dump that got shown to the user as if it were an
    # answer. No length cap here — these dumps ran long.
    # 2026-09-08: reproduced live -- a third malformed shape, distinct from
    # the <tool_call> wrapper above: the model emits a directive keyword
    # with NO colon at all (`RUN find ...` instead of `RUN: find ...`),
    # often glued onto the end of a prose sentence ("Let me locate the
    # file first. RUN find ..."). _DIRECTIVE_KEYWORDS_RE requires the
    # colon specifically to tell a real directive from prose sharing a
    # substring, so this never even registers as a directive attempt --
    # has_directives stays False, nothing executes, and the model's own
    # leaked <arg_key>/<arg_value> XML fragments (from whatever native
    # tool-call format it was trained on) get shown to the user raw
    # instead of triggering repair. Reuse _ARG_XML_TAG_RE as a second
    # detector: any arg_key/arg_value fragment in undispatched narrative
    # is just as strong a "the model tried to make a real tool call and
    # botched the format" signal as a literal <tool_call> tag.
    _malformed_directive_pattern = re.compile(
        r"<tool_call>|\btool_call\b", re.IGNORECASE
    )
    is_malformed_directive = (
        not has_directives
        and narrative
        and (
            _malformed_directive_pattern.search(narrative)
            or _ARG_XML_TAG_RE.search(narrative)
        )
    )
    is_stall = (
        not has_directives
        and narrative
        and len(narrative) < 400
        and _stall_pattern.search(narrative)
    )
    if is_stall or is_malformed_directive:
        reason = (
            "malformed <tool_call> syntax"
            if is_malformed_directive
            else "announced work, emitted no directive"
        )
        print(_ma()._pill("WARN", f"{D}model {reason} — forcing a retry{X}"))
        _ma().log(f"STALL_REPAIR ({reason}): narrative={narrative[:120]!r}")
        repair_msg = (
            "[Directive repair]\n"
            "You emitted `<tool_call>RUN: ...` — that format isn't recognized. "
            "Directives are bare, one per line, at column 0: `RUN: <cmd>` (no "
            "XML tags, no wrapper). Emit the real directive now in that format."
            if is_malformed_directive
            else "[Directive repair]\n"
            "You said you'd do that but emitted no RUN/READ/CREATE/EDIT/"
            "RUNTERM directive — nothing actually happened. Emit the real "
            "directive now. Do not narrate intent again; either do the "
            "thing or say plainly why you can't."
        )
        history.append({"role": "user", "content": repair_msg})
        return None
    if narrative and not streamed:
        _ma().render_reply(narrative, prefix=f"\n{M}  🥋{X} ", suffix="")
    elif not has_directives and not streamed:
        _ma().render_reply(reply, prefix=f"\n{M}  🥋{X} ", suffix="")

    # 2026-09-02: reproduced live -- a model emitted
    # `READ: ~/.master_ai_facts.json 2>/dev/null || cat ... || find ...`,
    # RUN:-style shell fallback chaining glued onto a READ: target. READ:
    # only ever takes ONE bare path -- there is no shell here to interpret
    # `||`/`2>/dev/null` -- so the whole string became one literal,
    # obviously-nonexistent "path" and the read failed. Truncate at the
    # first shell-operator token so at least the first real candidate path
    # gets tried, the same "keep the clean part, discard the noise"
    # approach as the <arg_key>/<tool_call> stripping above.
    _READ_SHELL_NOISE_RE = re.compile(r"\s+(?:\|\||&&|\||[12]?>&?\d?|2>/dev/null)\s*")

    def _parse_read_target(raw: Any) -> tuple:
        """Return (path, start_line, end_line) for READ payloads.

        Models often emit code-review style locations like
        `READ: /path/file.py:120-180  # why`. Treat that as a file range, not
        as a literal filename containing colon/comment text.
        """
        target = re.sub(r"\s+#.*$", "", (raw or "").strip())
        target = _strip_command_wrap(target)
        noise = _READ_SHELL_NOISE_RE.search(target)
        if noise:
            target = target[: noise.start()].rstrip()
        # 2026-09-07: reproduced live — a READ target came back "not found"
        # for a file that genuinely exists (confirmed via direct ls). Root
        # cause: _extract_directive already truncates RUN payloads at the
        # same _ARG_XML_TAG_RE match (trailing <arg_key>/<arg_value>/
        # </tool_call> fragments a small model glues onto real output), but
        # this READ-specific parser never got that same protection — so a
        # genuine path with XML noise trailing it just silently fails to
        # match any real file on disk instead of being cleaned first.
        arg_xml = _ARG_XML_TAG_RE.search(target)
        if arg_xml:
            target = target[: arg_xml.start()].rstrip()
        think_tag = _THINK_TAG_RE.search(target)
        if think_tag:
            target = target[: think_tag.start()].rstrip()
        m = re.match(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$", target)
        if not m:
            return target, None, None
        start = max(1, int(m.group("start")))
        end = int(m.group("end") or start)
        if end < start:
            start, end = end, start
        return m.group("path"), start, end

    # READ: — inject file content and signal caller to re-ask
    if read_paths:
        injected_block = []
        # 2026-09-02: a READ that finds nothing (bad path, fence-blocked)
        # used to just print to the terminal and vanish -- nothing was ever
        # fed back to the model, unlike RUN/RUNTERM which both have a real
        # failure path (_append_tool_blocked_feedback / _append_exec_failure_
        # feedback). Reproduced live: a malformed READ ("not found") ended
        # the turn with no repair and no explanation reaching the model, the
        # same silent-stop shape as the CREATE/EDIT feedback gap fixed
        # earlier tonight -- just on the failure side instead of success.
        failed_reads = []
        for rpath in read_paths:
            parsed_path, start_line, end_line = _parse_read_target(rpath)
            exp = os.path.expanduser(parsed_path)
            # P2.3: enforce read path fence. Symlink escapes, secret
            # paths (.ssh, .aws/credentials, /etc/shadow, etc.), and
            # anything outside allowed roots get blocked + audited.
            _ok, _why = _ma()._read_path_ok(exp)
            if not _ok:
                print(f"{R}  🚫 READ fence: {exp}{X}")
                print(f"  {D}reason: {_why}{X}")
                _ma()._audit("READ-FENCE-BLOCK", f"{exp} :: {_why}")
                _ma()._record_blocked_action("read", exp, _why, "READ-FENCE-BLOCK")
                failed_reads.append((exp, _why))
                continue
            if os.path.isfile(exp):
                full_text = Path(exp).read_text(errors="replace")
                _priv_reason = _ma()._privacy_check_path_or_content(
                    exp, full_text[:4000]
                )
                if _priv_reason:
                    _ma()._mark_turn_private(f"{_priv_reason}: {exp}")
                    print(f"  {Y}🔒 Privacy: turn marked private ({_priv_reason}){X}")
                if start_line is not None:
                    file_lines = full_text.splitlines()
                    selected = file_lines[start_line - 1 : end_line]
                    numbered = "\n".join(
                        f"{lineno}: {line}"
                        for lineno, line in enumerate(selected, start=start_line)
                    )
                    content = numbered[:8000]
                    injected_block.append(
                        f"--- {exp}:{start_line}-{end_line} ---\n{content}"
                    )
                    print(
                        f"{C}  📄 Read: {Y}{exp}:{start_line}-{end_line}{C} ({len(content)} chars){X}"
                    )
                else:
                    # 2026-09-11: For framework source files, allow a larger
                    # whole-file read so audits don't stall on tiny chunks.
                    _read_cap = 64000 if _ma()._read_path_is_framework(exp) else 8000
                    content = full_text[:_read_cap]
                    injected_block.append(f"--- {exp} ---\n{content}")
                    print(f"{C}  📄 Read: {Y}{exp}{C} ({len(content)} chars){X}")
            elif os.path.isdir(exp):
                _priv_reason = _ma()._privacy_check_path_or_content(exp, "")
                if _priv_reason:
                    _ma()._mark_turn_private(f"{_priv_reason}: {exp}")
                    print(f"  {Y}🔒 Privacy: turn marked private ({_priv_reason}){X}")
                listing = subprocess.run(
                    ["ls", "-la", exp], capture_output=True, text=True
                ).stdout
                injected_block.append(f"--- {exp} (directory) ---\n{listing}")
                print(f"{C}  📁 Dir: {Y}{exp}{X}")
            else:
                print(f"{R}  ❌ READ: not found: {exp}{X}")
                failed_reads.append((exp, "not found"))
        if injected_block:
            content = "[File contents]\n" + "\n\n".join(injected_block)
            if failed_reads:
                content += "\n\n[Some READ targets also failed]\n" + "\n".join(
                    f"- {p}: {why}" for p, why in failed_reads[:6]
                )
            # P1.6 reconciliation (2026-09-20): a READ in a chain that also
            # edits the file it read must NOT end the turn here. The READ
            # contents already satisfied the READ→EDIT contract (the edit gate
            # below consults read_paths), and returning None would strand the
            # EDIT for a "re-ask" turn the model already answered — the exact
            # failure test_edit_markers_are_case_insensitive pinned at HEAD
            # (verified: red at every commit back to ca3f813, which introduced
            # the READ→EDIT contract in the same chain as this early return —
            # the two never agreed). Inject the content into history (so the
            # model still gets grounding for later turns) and fall through to
            # dispatch the edits in this same pass. Chains with no edits keep
            # the original re-ask behavior.
            if not edit_ops:
                history.append(
                    {"role": "user", "content": content + "\n\nNow proceed."}
                )
                return None  # caller re-asks AI with injected context
            history.append({"role": "user", "content": content + "\n\nNow proceed."})
        if failed_reads:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[READ FAILED]\n"
                        "Every READ target in this turn failed:\n"
                        + "\n".join(f"- {p}: {why}" for p, why in failed_reads[:6])
                        + "\n\nDo not repeat the same path. Either propose a corrected "
                        "path (check spelling, try a directory listing first with "
                        "RUN: ls, or search for the real filename), or if you "
                        "genuinely don't know where the right file is, say so plainly "
                        "as your closing answer instead of retrying blindly."
                    ),
                }
            )
            return None

    def _latest_user_turn() -> Any:
        for msg in reversed(history):
            if msg.get("role") == "user":
                return msg.get("content") or ""
        return ""

    def _creation_expected() -> bool:
        text = _latest_user_turn().lower()
        return bool(
            _ma()._is_tool_required(text)
            and re.search(
                r"\b(create|write|make|build|generate)\b.*\b(script|file|html|app|page|demo|animation|effect|video|clip|movie)\b",
                text,
            )
        )

    def _inline_python_generator(cmd: str) -> Any:
        low = cmd.lower()
        if not re.search(r"\bpython(?:3|\d(?:\.\d+)?)?\s+-c\b", low):
            return False
        if len(cmd) < 140:
            return False
        return any(
            tok in low
            for tok in (
                "from pil import",
                "import pil",
                "imagedraw",
                "imagefont",
                "subprocess.run(",
                "os.system(",
                "ffmpeg",
                "image.new(",
                "draw.",
                "frames",
                "generate",
                "animate",
                "render",
            )
        )

    def _is_bare_cd(cmd: str) -> Any:
        """True for a RUN/RUNTERM command that is ONLY `cd <dir>` with nothing
        chained after it — `cd X && Y` or `cd X; Y` are fine, only the
        standalone form is the problem (see the repair message below for why)."""
        stripped = cmd.strip()
        if not re.match(r"^cd\s+\S", stripped, re.IGNORECASE):
            return False
        return not re.search(r"&&|;|\|\|", stripped)

    def _visual_requested() -> bool:
        text = _latest_user_turn().lower()
        return bool(
            any(w in text for w in _VISUAL_RUN_WORDS)
            or "terminal effect" in text
            or "terminal animation" in text
            or "matrix style" in text
            or "matrix-style" in text
            or "matrix credit" in text
            or "matrix credits" in text
            or "credit screen" in text
            or "credit roll" in text
        )

    def _html_demo_expected() -> bool:
        text = _latest_user_turn().lower()
        return bool(
            re.search(
                r"\b(html|ui|browser|web|page|site|app|demo|dashboard|interface)\b",
                text,
            )
            and re.search(r"\b(create|write|make|build|generate|demo)\b", text)
        )

    def _html_demo_quality_issues(content: str) -> Any:
        issues = []
        low = content.lower()
        if not re.search(r"<!doctype\s+html|<html[\s>]", low):
            issues.append("missing complete HTML document skeleton")
        if "<style" not in low:
            issues.append("missing inline CSS")
        if "<script" not in low:
            issues.append("missing working JavaScript")
        if re.search(r'<link[^>]+href=["\'](?:styles?\.css|style\.css)["\']', low):
            issues.append("depends on missing external CSS")
        if re.search(
            r'<script[^>]+src=["\'](?:scripts?\.js|main\.js|app\.js)["\']', low
        ):
            issues.append("depends on missing external JavaScript")
        if re.search(
            r"\b(lorem ipsum|placeholder|todo:|coming soon|replace me)\b", low
        ):
            issues.append("contains placeholder copy")
        if not re.search(r"<button\b|<input\b|<select\b|<textarea\b|<form\b", low):
            issues.append("has no interactive controls")
        if not re.search(
            r"addEventListener|onclick\s*=|querySelector|localStorage|classList",
            content,
        ):
            issues.append("JavaScript has no visible interaction wiring")
        body_text = re.sub(
            r"<script.*?</script>|<style.*?</style>|<[^>]+>",
            " ",
            content,
            flags=re.I | re.S,
        )
        real_words = re.findall(r"[A-Za-z]{3,}", body_text)
        if len(real_words) < 45:
            issues.append("body copy is too thin for a polished demo")
        if "viewport" not in low or "@media" not in low:
            issues.append("missing responsive viewport/media styling")
        return issues[:4]

    # CREATE: / EDIT: run BEFORE RUN: — so RUN: bash <path> works on a file
    # the same reply just created. Prior order produced exit-127s when the
    # model emitted CREATE: + RUN: together.
    action_failed = False
    created_ok_paths = []
    # 2026-09-02: moved here from just above the RUN loop -- CREATE/EDIT
    # now append to this list too (see the CREATE/EDIT success branches
    # below), and both of those loops run BEFORE the RUN loop in this
    # function's execution order. The old single init point after EDIT's
    # blocked-feedback handling left CREATE and EDIT referencing this name
    # before it was ever assigned (UnboundLocalError, reproduced live:
    # "local variable 'tool_result_feedback' referenced before assignment"
    # right after a successful CREATE). One init, at the true first use.
    tool_result_feedback = []
    for filepath, content in create_files:
        if _html_demo_expected() and str(filepath).lower().endswith((".html", ".htm")):
            html_issues = _html_demo_quality_issues(content)
            if html_issues:
                print(
                    _ma()._pill(
                        "BLOCKED", f"{D}HTML demo below polish bar: {html_issues[0]}{X}"
                    )
                )
                _ma().log(f"HTML_QUALITY_REPAIR: {filepath} issues={html_issues}")
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "[Directive repair]\n"
                            f"The generated HTML demo for {filepath} is below the product-demo quality bar:\n"
                            + "\n".join(f"- {i}" for i in html_issues)
                            + "\n\nRegenerate the same file as a complete single-file HTML demo. "
                            "Required: full HTML skeleton, inline CSS, inline JavaScript, "
                            "responsive layout, real UI text, visible controls, and working interactions. "
                            "No placeholder copy and no missing external styles/scripts. "
                            "Then verify the file exists."
                        ),
                    }
                )
                return None
        if _visual_requested() and str(filepath).lower().endswith(".sh"):
            visual_issues = []
            low_content = content.lower()
            if "killall" in low_content or "pkill" in low_content:
                visual_issues.append("uses killall/pkill instead of a timed frame loop")
            if re.search(r"\bsleep\s+1[12]0\b", low_content):
                visual_issues.append("uses one long sleep instead of animation frames")
            if "trap " not in low_content:
                visual_issues.append("missing cleanup trap")
            if "tput" not in low_content and "stty size" not in low_content:
                visual_issues.append("not terminal-size aware")
            if "while" not in low_content and "for ((" not in low_content:
                visual_issues.append("missing animation loop")
            if visual_issues:
                print(
                    _ma()._pill(
                        "BLOCKED",
                        f"{D}visual script below quality bar: {visual_issues[0]}{X}",
                    )
                )
                _ma().log(f"VISUAL_QUALITY_REPAIR: {filepath} issues={visual_issues}")
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "[Directive repair]\n"
                            f"The generated visual script for {filepath} is below the product-demo quality bar:\n"
                            + "\n".join(f"- {i}" for i in visual_issues)
                            + "\n\nRegenerate the same file with a complete bash animation script. "
                            "Required: cleanup trap, hidden/restored cursor, clear screen, tput rows/cols, "
                            "timed frame loop using SECONDS/end time, multiple moving elements per frame, "
                            "color/depth variation, no killall/pkill, no long sleep shortcut, no static echo spam. "
                            "Then verify with bash -n, chmod, ls, and run the visual script with RUNTERM."
                        ),
                    }
                )
                return None
        if _ma().confirm_create(filepath, content):
            created_ok_paths.append(os.path.expanduser(filepath))
            # 2026-09-02: CREATE/EDIT success never fed tool_result_feedback,
            # unlike RUN/RUNTERM/SEARCH/BROWSER -- so a reply that ended in a
            # bare CREATE (no narrative text) produced an empty tool_result_
            # feedback list, skipped CHAIN_CONTINUE_AFTER_TOOL_RESULT entirely,
            # and the turn ended with no closing message and no chance for the
            # model to do a requested follow-up (e.g. "write X, then read it
            # back and show me"). Reproduced live: a search-then-write-then-
            # read task wrote the file correctly and just stopped -- the
            # "read it back" half was never attempted. Mirror the RUN/SEARCH
            # pattern so a CREATE-terminated reply gets a real continuation
            # turn instead of silently ending.
            if continue_after_tools:
                tool_result_feedback.append(
                    f"[CREATE RESULT]\nPath: {filepath}\nStatus: written ({len(content)} bytes)"
                )
        else:
            action_failed = True

    # P1.6: enforce READ → EDIT inside the same directive chain. Editing
    # files the model hasn't read this turn is a hallucination smell — the
    # find_text is likely drifted from current disk content, and the edit
    # will either no-op or land in the wrong place. Allow the model one
    # repair pass: send [Directive repair] back to history and abort the
    # current chain so the next turn can re-emit READ: + EDIT:. Files
    # CREATEd this chain are exempt (model just wrote them).
    if edit_ops:
        read_set = {os.path.realpath(os.path.expanduser(p)) for p in read_paths}
        created_set = {os.path.realpath(os.path.expanduser(p)) for p, _ in create_files}
        unread_edits = []
        for ep, _, _ in edit_ops:
            try:
                rp = os.path.realpath(os.path.expanduser(ep))
            except Exception:
                rp = os.path.expanduser(ep)
            if rp not in read_set and rp not in created_set:
                unread_edits.append(ep)
        if unread_edits:
            print(
                _ma()._pill(
                    "BLOCKED", f"{D}EDIT without prior READ: {unread_edits[0][:60]}{X}"
                )
            )
            _ma().log(f"DIRECTIVE_REPAIR_READ_BEFORE_EDIT: {unread_edits[:3]}")
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[Directive repair]\n"
                        "You emitted an EDIT: directive for files you have not READ this turn:\n"
                        + "\n".join(f"- {p}" for p in unread_edits[:6])
                        + "\n\nRead each one first (READ: <path>), then re-emit the EDIT "
                        "directive with the find/replace based on the actual current content. "
                        "This is the coding-task loop: READ → EDIT → verify. "
                        "Do not explain. Repair the directive chain now."
                    ),
                }
            )
            return None

    for filepath, find_text, replace_text in edit_ops:
        if _ma().confirm_edit(filepath, find_text, replace_text):
            # Same CREATE-feedback gap applies to EDIT -- see the comment
            # at the CREATE success branch above.
            if continue_after_tools:
                tool_result_feedback.append(
                    f"[EDIT RESULT]\nPath: {filepath}\nStatus: applied"
                )
        else:
            action_failed = True

    if action_failed:
        _fed_back = False
        denied = getattr(_ma(), "_LAST_DENIED_ACTION", None) or {}
        if denied:
            kind = denied.get("kind") or "action"
            path = denied.get("path") or ""
            cmd = denied.get("command") or ""
            details = path or cmd
            msg = f"[User declined {kind}{': ' + details if details else ''}] Do not repeat that action unless the user explicitly asks."
            history.append({"role": "user", "content": msg})
            _ma()._LAST_DENIED_ACTION = {}
            _fed_back = True
        # P1.4: surface hook blocks the same way denied actions surface —
        # so the next model turn sees the [HOOK BLOCKED] feedback and can
        # repair instead of marching forward assuming success.
        hook_block = getattr(_ma(), "_LAST_HOOK_BLOCK", None) or {}
        if hook_block:
            hkind = hook_block.get("kind") or "action"
            hpath = hook_block.get("path") or ""
            hid = hook_block.get("hook_id") or "?"
            hreason = hook_block.get("reason") or "blocked by Sensei hook"
            msg = (
                f"[HOOK BLOCKED] {hkind} on {hpath} was flagged by hook "
                f"'{hid}': {hreason}. The action did happen if it was a "
                "post-* hook, so the file may now be in a broken state — "
                "diagnose and fix before continuing the chain. "
                "If there is a one-line lesson here, emit a single "
                "`REMEMBER: <one-line lesson>` directive in your next "
                "reply so this doesn't repeat next turn."
            )
            history.append({"role": "user", "content": msg})
            # 2026-05-11: fire on_blocked hook for the [HOOK BLOCKED]
            # path too. Same async lesson-extract pipeline.
            _ma()._fire_on_blocked(
                hpath,
                hkind.upper(),
                f"{hid}: {hreason}",
                f"HOOK-BLOCK-{hkind.upper()}",
            )
            _ma()._LAST_HOOK_BLOCK = {}
            _ma().log(
                f"CHAIN_HOOK_BLOCK_FEEDBACK: appended [HOOK BLOCKED] for {hkind} {hpath}"
            )
            _fed_back = True
        if run_cmds or runterm_cmds:
            print(
                _ma()._pill(
                    "BLOCKED",
                    f"{D}CREATE/EDIT failed or was denied — skipped downstream RUN/RUNTERM for this turn{X}",
                )
            )
            _ma().log("CHAIN_ABORT: skipped RUN/RUNTERM after failed CREATE/EDIT")
        # 2026-09-01: this used to `return reply` unconditionally here, even
        # in the two branches above that DO append real feedback to
        # history - so the model never got a follow-up turn to actually
        # respond to that feedback, same dead-end bug as the RUN/RUNTERM/
        # SEND_EMAIL/BROWSER exec-failure branches (see
        # _append_exec_failure_feedback). Confirmed live in the exact
        # thread this session was debugging: a CREATE hit a real
        # syntax-check hook block, printed the BLOCKED pill, and the turn
        # just stopped - no closing answer. Neither denied nor hook_block
        # fired that time (a plain confirm_edit() failure, e.g. a bad
        # find/replace that matched nothing), so NOTHING was appended to
        # history at all - the fallback below covers that case too.
        if not _fed_back:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[TOOL FAILED]\n"
                        "CREATE/EDIT was refused or failed (not a hook block, "
                        "not a user denial - likely a bad find/replace match "
                        "or a fence/policy refusal with no structured detail "
                        "captured).\n"
                        "Give the user a short, honest closing answer: say what "
                        "failed, and either re-read the file and retry with a "
                        "corrected directive or ask for guidance. Do not "
                        "silently stop."
                    ),
                }
            )
            _ma().log(
                "CHAIN_EXEC_FAIL_FEEDBACK: appended [TOOL FAILED] for CREATE/EDIT (no structured detail)"
            )
        return None

    if (
        (run_cmds or runterm_cmds)
        and not create_files
        and not edit_ops
        and _creation_expected()
    ):
        missing = []
        for cmd in run_cmds + runterm_cmds:
            missing.extend(_ma()._missing_execution_targets(cmd))
        if missing:
            uniq_missing = sorted(set(missing))
            print(
                _ma()._pill(
                    "BLOCKED",
                    f"{D}model tried to use missing file before CREATE: {uniq_missing[0][:60]}{X}",
                )
            )
            _ma().log(f"DIRECTIVE_REPAIR_MISSING_CREATE: {uniq_missing}")
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[Directive repair]\n"
                        "You tried to run commands against missing file(s):\n"
                        + "\n".join(f"- {p}" for p in uniq_missing[:6])
                        + "\n\nThis is a file-creation task. First emit a complete CREATE block "
                        "for the required file path with <<<CONTENT and >>>CONTENT. Only after "
                        "the CREATE block may you emit chmod, ls, bash, or RUNTERM commands. "
                        "Do not explain. Repair the directive chain now."
                    ),
                }
            )
            return None

    if (run_cmds or runterm_cmds) and not create_files and not edit_ops:
        inline_python = [
            c for c in run_cmds + runterm_cmds if _inline_python_generator(c)
        ]
        if inline_python:
            print(
                _ma()._pill(
                    "BLOCKED", f"{D}inline python generator must be CREATEd first{X}"
                )
            )
            _ma().log(f"DIRECTIVE_REPAIR_INLINE_PYTHON: {inline_python[:3]}")
            # 2026-09-21: _inline_python_generator()'s own detection above fires on
            # ANY sufficiently long python3 -c one-liner containing generic tokens
            # like "generate", "subprocess.run(", "os.system(" — not actually
            # restricted to media at all, despite this repair message's original
            # wording ("generated images or video"). Reported live: the model was
            # writing a web scraper (a subprocess.run() call in an inline check
            # tripped this), got told to stop making "images or video" — completely
            # wrong framing for what it was actually doing — and the mismatched
            # feedback left it unable to act on the correction properly, so it just
            # re-announced roughly the same plan instead of emitting a real CREATE
            # block. The underlying policy (write it as a real file, don't inline
            # a long generator) is correct and worth keeping regardless of content
            # type; only the wording was media-specific. Made purpose-agnostic so
            # it's accurate for scrapers, data processors, or anything else that
            # happens to trip the same broad detector.
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[Directive repair]\n"
                        "You tried to run a long inline python generator with python3 -c. "
                        "Do not use a one-liner for this — it doesn't matter what the script does "
                        "(image/video generation, scraping, data processing, anything else). First "
                        "emit a CREATE block for a real .py or .sh file on Desktop, then verify it, "
                        "then run that file by path. Keep the filename stable through CREATE → "
                        "chmod/ls → RUN/RUNTERM. Do not explain. Repair the directive chain now."
                    ),
                }
            )
            return None

    # 2026-09-21: reproduced live on opencode-go::mimo-v2.5-pro, twice in a
    # row without self-correcting: the model split "cd DIR" and the command
    # it actually wanted to run there into two separate RUN: directives.
    # Each RUN/RUNTERM dispatches through its own subprocess.run() call with
    # no shared shell state between them, so a standalone cd succeeds, does
    # nothing visible, and the next RUN starts fresh from wherever the
    # process itself already is — not the directory just "cd'd" into. The
    # system prompt shows the correct one-line `cd X && command` pattern as
    # an example elsewhere but never states the actual constraint outright,
    # and the model wasn't generalizing from the example on its own — it
    # just retried the identical broken two-step split.
    bare_cds = [c for c in run_cmds + runterm_cmds if _is_bare_cd(c)]
    if bare_cds:
        print(
            _ma()._pill(
                "BLOCKED",
                f"{D}bare cd with no chained command — each RUN is its own subprocess{X}",
            )
        )
        _ma().log(f"DIRECTIVE_REPAIR_BARE_CD: {bare_cds[:3]}")
        history.append(
            {
                "role": "user",
                "content": (
                    "[Directive repair]\n"
                    "You emitted a RUN/RUNTERM that is only `cd <dir>` with nothing chained "
                    "after it. Each RUN/RUNTERM executes as its own separate subprocess with "
                    "no shared shell state — a standalone cd has no effect on any later "
                    "command, even one in the same reply. Either chain the real command onto "
                    "the SAME line with && (e.g. `cd ~/project && command`), or skip cd "
                    "entirely and use an absolute or ~-relative path directly in the command "
                    "itself. Do not explain. Repair the directive chain now."
                ),
            }
        )
        return None

    # Deterministic execution policy: setup stays captured, visual work runs
    # in a real terminal. Example model drift:
    #   RUN: chmod +x file.sh && file.sh
    # becomes:
    #   RUN: chmod +x file.sh
    #   RUNTERM: file.sh
    visual_requested = _visual_requested()
    normalized_run_cmds = []
    for cmd in run_cmds:
        setup_parts, visual_parts = _split_run_policy(
            cmd, visual_requested=visual_requested
        )
        if visual_parts:
            print(
                _ma()._pill(
                    "POLICY",
                    f"{D}split visual command into RUN setup + RUNTERM execution{X}",
                )
            )
            _ma().log(
                f"RUN_POLICY_SPLIT: {cmd!r} -> run={setup_parts!r} runterm={visual_parts!r}"
            )
        normalized_run_cmds.extend(setup_parts)
        runterm_cmds.extend(visual_parts)
    run_cmds = normalized_run_cmds

    def _append_tool_blocked_feedback(kind: Any, cmd: Any) -> bool:
        blocked = getattr(_ma(), "_LAST_BLOCKED_ACTION", None) or {}
        if not blocked:
            return False
        history.append(
            {
                "role": "user",
                "content": (
                    "[TOOL BLOCKED]\n"
                    f"{kind} command was refused by Sensei before execution.\n"
                    f"Command: {blocked.get('command', cmd)}\n"
                    f"Reason: {blocked.get('reason', 'safeguard refused')}.\n"
                    "Choose an already-installed alternative, propose a safer "
                    "implementation, or ask for explicit user approval where "
                    "appropriate. Do not assume the command succeeded.\n"
                    "If there is a one-line lesson here (e.g. \"X isn't "
                    'installed on this box, use Y"), emit a single '
                    "`REMEMBER: <one-line lesson>` directive in your next "
                    "reply so this doesn't repeat next turn."
                ),
            }
        )
        # 2026-05-11: fire on_blocked hook for auto-lesson extraction.
        # Async — the worker runs the small 3B model in a thread and
        # stores the lesson via confirm_remember() without blocking the
        # user. Rate-limited inside the hook itself (max 10/session).
        # Capture the blocked context BEFORE clearing the global.
        try:
            _ma()._fire_on_blocked(
                blocked.get("command") or cmd,
                (blocked.get("kind") or kind).upper(),
                blocked.get("reason", "safeguard refused"),
                blocked.get("audit_kind", "TOOL-BLOCKED"),
            )
        except Exception as e:
            _ma().log(f"ON_BLOCKED_HOOK_ERROR: {e}")
        _ma()._LAST_BLOCKED_ACTION = {}
        _ma().log(f"CHAIN_BLOCKED_FEEDBACK: appended [TOOL BLOCKED] for: {cmd}")
        return True

    def _append_exec_failure_feedback(kind: Any, cmd: Any, detail: Any) -> None:
        """Genuine runtime failure (the command ran, then failed) — NOT a
        pre-execution safeguard refusal, so _LAST_BLOCKED_ACTION is never
        set and _append_tool_blocked_feedback() can't fire for this case.
        Without this, every exec-fail branch below used to `return reply`
        with nothing appended to history — the turn dead-ended on the
        stale pre-execution text and the model never got another turn to
        produce a real closing answer. Confirmed live 2026-09-01: "make an
        ai video" -> RUN chain never checked for matplotlib -> generated
        script crashed with ModuleNotFoundError -> BLOCKED pill printed ->
        turn just stopped. Question, work, no answer.
        """
        history.append(
            {
                "role": "user",
                "content": (
                    "[TOOL FAILED]\n"
                    f"{kind} command ran but failed (not a safeguard refusal).\n"
                    f"{detail}\n"
                    "Give the user a short, honest closing answer: say what "
                    "failed and why, and propose a concrete fix (e.g. install "
                    "the missing dependency) or ask before retrying. Do not "
                    "silently stop — the user must see a real response, not "
                    "just the raw failure output."
                ),
            }
        )
        _ma().log(f"CHAIN_EXEC_FAIL_FEEDBACK: appended [TOOL FAILED] for: {cmd}")

    for cmd in run_cmds:
        result = _ma().confirm_run(cmd)
        chain_ok = _ma()._action_ok(result)
        if not chain_ok:
            # Informational commands (systemctl status etc.) return nonzero
            # exits as diagnostic answers, not failures. Let the chain
            # advance so a follow-up `systemctl start` can fire.
            if isinstance(result, _ma().RunResult) and _ma()._is_informational_cmd(
                cmd, result.exit_code
            ):
                _ma().log(f"CHAIN_CONTINUE: informational nonzero exit on {cmd}")
                continue
            # Feed safeguard-blocked directives back into history so the next
            # model turn sees the BLOCKED instead of hallucinating that the
            # command ran. Without this the LLM (esp. cloud lanes) would
            # answer the next user turn assuming success.
            if _append_tool_blocked_feedback("RUN", cmd):
                return None
            # 2026-05-11 (Codex finding 2): fire on_blocked on EXEC failures
            # too, not just safeguard refusals. fetchmail exit 127 is a real
            # learnable failure (command-not-found / hallucination) — the
            # auto-extract-lesson hook should see it. audit_kind tags the
            # source so the hook can filter (this is RUN-EXEC-FAIL, not a
            # POLICY/FENCE block).
            try:
                _exit = getattr(result, "exit_code", "?")
                _ma()._fire_on_blocked(
                    cmd,
                    "RUN",
                    f"command failed (exit {_exit})",
                    "RUN-EXEC-FAIL",
                )
            except Exception as e:
                _ma().log(f"ON_BLOCKED_HOOK_ERROR (exec-fail): {e}")
            print(
                _ma()._pill(
                    "BLOCKED",
                    f"{D}RUN failed or was refused — skipped remaining RUN/RUNTERM for this turn{X}",
                )
            )
            _ma().log(
                f"CHAIN_ABORT: skipped downstream commands after RUN failure: {cmd}"
            )
            _append_exec_failure_feedback(
                "RUN", cmd, _ma()._format_tool_result("RUN", cmd, result)
            )
            return None
        if continue_after_tools:
            tool_result_feedback.append(_ma()._format_tool_result("RUN", cmd, result))

    # RUNTERM: runs after RUN: — if the model pairs "build output" (RUN:) with
    # "now open the demo" (RUNTERM:), the demo spawns after the build finishes.
    for cmd in runterm_cmds:
        result = _ma().confirm_runterm(cmd)
        if not _ma()._action_ok(result):
            if _append_tool_blocked_feedback("RUNTERM", cmd):
                return None
            # 2026-05-11: same exec-fail on_blocked fire for RUNTERM.
            try:
                _exit = getattr(result, "exit_code", "?")
                _ma()._fire_on_blocked(
                    cmd,
                    "RUNTERM",
                    f"runterm failed (exit {_exit})",
                    "RUNTERM-EXEC-FAIL",
                )
            except Exception as e:
                _ma().log(f"ON_BLOCKED_EXEC_HOOK_ERROR (runterm): {e}")
            print(
                _ma()._pill(
                    "BLOCKED",
                    f"{D}RUNTERM failed or was refused — skipped remaining RUNTERM for this turn{X}",
                )
            )
            _ma().log(
                f"CHAIN_ABORT: skipped downstream commands after RUNTERM failure: {cmd}"
            )
            _append_exec_failure_feedback(
                "RUNTERM", cmd, _ma()._format_tool_result("RUNTERM", cmd, result)
            )
            return None
        if continue_after_tools:
            tool_result_feedback.append(
                _ma()._format_tool_result("RUNTERM", cmd, result)
            )

    # SEARCH: <query> — read-only live lookup, no confirmation gate needed
    # (same reasoning as _BROWSER_READONLY_KINDS: nothing on disk or the
    # network's write side changes). web_search() never raises — it
    # degrades to "Search unavailable: ..." when every engine is down —
    # so this always has something to feed back, never a chain-abort.
    for query in search_queries:
        _ma()._audit("SEARCH", query)
        print(f"\n  {BC}[thinking: SEARCH — {query}]{X}")
        results = _ma().web_search(query)
        if continue_after_tools:
            tool_result_feedback.append(
                _ma()._format_tool_result("SEARCH", query, results)
            )
        else:
            print(f"  {C}{results}{X}\n", flush=True)

    # TASK_ADD: / TASK_DONE: — see the extraction comment above for why
    # this exists. Reuses the exact same persistent list `task add`/`task
    # list`/`task done` already show the user (~/.master_ai_tasks.json via
    # load_tasks()/save_tasks()) so a task the model adds is immediately
    # visible with the plain `task list` command, and one the user adds
    # manually is immediately visible to the model on its next turn.
    if task_add_texts or task_done_targets:
        _tasks = _ma().load_tasks()
        _task_events = []
        for _t in task_add_texts:
            _t = _t.strip()
            if not _t:
                continue
            _tasks.append({"text": _t, "done": False})
            _task_events.append(f"added: {_t}")
        for _target in task_done_targets:
            _target = _target.strip()
            _matched = None
            # Accept a bare index ("3") or enough of the task text to be
            # unambiguous -- the model may not know the exact current
            # number if tasks were added earlier in a different turn.
            if _target.isdigit():
                _n = int(_target) - 1
                if 0 <= _n < len(_tasks):
                    _matched = _n
            if _matched is None:
                _hits = [
                    i
                    for i, t in enumerate(_tasks)
                    if not t.get("done")
                    and _target.lower() in (t.get("text", "") or "").lower()
                ]
                if len(_hits) == 1:
                    _matched = _hits[0]
            if _matched is not None:
                _tasks[_matched]["done"] = True
                _task_events.append(f"done: {_tasks[_matched]['text']}")
            else:
                _task_events.append(
                    f"could not match TASK_DONE target {_target!r} to a pending task"
                )
        _ma().save_tasks(_tasks)
        _pending = [t["text"] for t in _tasks if not t.get("done")]
        _done_count = sum(1 for t in _tasks if t.get("done"))
        print(
            f"\n  {BC}[tasks: {_done_count}/{len(_tasks)} done, {len(_pending)} pending]{X}"
        )
        if continue_after_tools:
            summary = (
                "[TASK LIST RESULT]\n"
                + "\n".join(f"- {e}" for e in _task_events)
                + f"\n\nProgress: {_done_count}/{len(_tasks)} done.\n"
                + (
                    "Pending:\n" + "\n".join(f"- {p}" for p in _pending[:15])
                    if _pending
                    else "All tasks done."
                )
            )
            tool_result_feedback.append(summary)

    # SEND_EMAIL: runs after RUN/RUNTERM — e.g. RUN: a report-gen command,
    # then SEND_EMAIL: ship the report. Each spec confirmed individually
    # via confirm_send_email; irreversible, no auto-mode bypass.
    for spec in send_email_specs:
        result = _ma().confirm_send_email(spec)
        if not (isinstance(result, dict) and result.get("ok")):
            err = (result or {}).get("error", "send_email refused or failed")
            if _append_tool_blocked_feedback(
                "SEND_EMAIL",
                f"to={spec.get('to', '')} subject={spec.get('subject', '')}",
            ):
                return None
            print(
                _ma()._pill(
                    "BLOCKED", f"{D}SEND_EMAIL failed or was refused — {err}{X}"
                )
            )
            _ma().log(f"CHAIN_ABORT: SEND_EMAIL to={spec.get('to', '')} err={err}")
            _append_exec_failure_feedback(
                "SEND_EMAIL",
                f"to={spec.get('to', '')}",
                f"To: {spec.get('to', '')}\nSubject: {spec.get('subject', '')}\nError: {err}",
            )
            return None
        if continue_after_tools:
            tool_result_feedback.append(
                f"[SEND_EMAIL RESULT]\nTo: {spec.get('to', '')}\nSubject: {spec.get('subject', '')}\nStatus: sent"
            )

    # SEND_TELEGRAM: runs after RUN/RUNTERM/SEND_EMAIL — one-way bot message.
    for spec in send_telegram_specs:
        result = _ma().confirm_send_telegram(spec)
        if not (isinstance(result, dict) and result.get("ok")):
            err = (result or {}).get("error", "send_telegram refused or failed")
            if _append_tool_blocked_feedback(
                "SEND_TELEGRAM",
                f"chat_id={spec.get('chat_id', '')} text={spec.get('text', '')[:80]}",
            ):
                return None
            print(
                _ma()._pill(
                    "BLOCKED", f"{D}SEND_TELEGRAM failed or was refused — {err}{X}"
                )
            )
            _ma().log(
                f"CHAIN_ABORT: SEND_TELEGRAM chat_id={spec.get('chat_id', '')} err={err}"
            )
            _append_exec_failure_feedback(
                "SEND_TELEGRAM",
                f"chat_id={spec.get('chat_id', '')}",
                f"Chat ID: {spec.get('chat_id', '')}\nText: {spec.get('text', '')}\nError: {err}",
            )
            return None
        if continue_after_tools:
            tool_result_feedback.append(
                f"[SEND_TELEGRAM RESULT]\nChat ID: {spec.get('chat_id', '')}\nMessage: {spec.get('text', '')}\nStatus: sent"
            )

    # BROWSER_* — dispatched through sensei_bridge.py's queue (same one
    # sensei_mcp_server.py's mcp__sensei__* tools use). Chain-aborts on
    # failure like RUN/RUNTERM so the model doesn't narrate a browser
    # action it never actually performed.
    for action in browser_actions:
        kind, target, value = action["kind"], action["target"], action["value"]
        label = f"{kind}: {target}"
        result = _ma().confirm_browser_action(kind, target, value)
        if not _ma()._action_ok(result):
            if _append_tool_blocked_feedback("BROWSER", label):
                return None
            print(_ma()._pill("BLOCKED", f"{D}{label} failed or was refused{X}"))
            _ma().log(f"CHAIN_ABORT: BROWSER action failed: {label}")
            _append_exec_failure_feedback(
                "BROWSER", label, _ma()._format_tool_result(kind, label, result)
            )
            return None
        if continue_after_tools:
            tool_result_feedback.append(_ma()._format_tool_result(kind, label, result))

    if tool_result_feedback:
        # 2026-09-02: operator's own words: "it should always refer to the task
        # list to see where it is, and to check what's done -- that will help
        # it with continuation and keep going." Reproduced live: mid-way
        # through a large task-decomposed request, a turn with no TASK_ADD/
        # TASK_DONE of its own had zero visibility into the real task list --
        # it fell back on its own memory of what it thought the tasks were,
        # which had drifted from actual state, and it started fabricating a
        # replacement list from scratch instead of checking the real one.
        # Fix: pull load_tasks() FRESH off disk and append it to every
        # continuation turn, not just the ones where this turn's own reply
        # touched TASK_ADD/TASK_DONE -- ground truth every turn, never
        # memory of an earlier turn's ground truth. Now shared with a fresh
        # turn's own injection point (_build_task_list_context, see 2026-09-03
        # note there) -- this branch alone didn't cover a message that starts
        # a turn instead of continuing one.
        _task_context = _ma()._build_task_list_context()
        history.append(
            {
                "role": "user",
                "content": (
                    "\n\n".join(tool_result_feedback)
                    + _task_context
                    + "\n\nContinue from the tool output. If more inspection is needed, "
                    "emit the next directive. If you have pending tasks, work the "
                    "next one from the list above -- do not re-add or re-guess tasks "
                    "that are already there. If the task is complete, give the final "
                    "answer as 1-3 short plain sentences: state the direct result "
                    "(found it / done / not found / here's the number), skip restating "
                    "the tool output back to the user, and if there's an obvious next "
                    "step end with a one-line yes/no question offering it."
                ),
            }
        )
        _ma().log(
            f"CHAIN_CONTINUE_AFTER_TOOL_RESULT: {len(tool_result_feedback)} tool result(s)"
        )
        return None

    if getattr(_ma(), "MODE", "plan") == "auto":
        opened = any(
            "xdg-open" in c or "open " in c.lower() for c in (run_cmds + runterm_cmds)
        )
        html_paths = [
            p for p in created_ok_paths if str(p).lower().endswith((".html", ".htm"))
        ]
        if html_paths and not opened:
            _ma()._open_file_preview(html_paths[-1])

    # Chain reached the end with no BLOCKED. If the user stepped out to a
    # second terminal for a sudo handoff, the work happened and they came
    # back with 'ok' — that ack IS the verify. Auto-mark the pinned task
    # done so the chain doesn't leave a stale "in progress" hanging.
    if getattr(_ma(), "_CHAIN_SUDO_ACKS", 0) > 0 and _ma().ACTIVE_TASK:
        proj = getattr(_ma(), "ACTIVE_PROJECT", "")
        task = _ma().ACTIVE_TASK
        flipped = _ma()._dojo_mark_done(proj, task) if proj else False
        try:
            _ma().ACTIVE_TASK_FILE.write_text("")
        except Exception:
            pass
        _ma()._ma().ACTIVE_TASK = ""
        suffix = " (PROJECTS.md updated)" if flipped else ""
        print(_ma()._pill("DONE", f"{BG}{task}{X}{D}{suffix}{X}"))
        _ma().log(f"AUTO-MARK-DONE: project={proj!r} task={task!r} flipped={flipped}")

    # 2026-09-03: dispatch SUBAGENT: goals before final return so delegated
    # work feeds back into the conversation like any other tool result.
    if subagent_goals:
        sub_feedback = []
        try:
            import delegate_runner as _dr

            for goal in subagent_goals[:3]:  # cap parallel-like bursts
                print(f"  {BC}[subagent: {goal[:60]}...]{X}")
                res = _dr.delegate_task(
                    goal=goal,
                    context={"cwd": str(Path.cwd()), "mode": _ma().MODE},
                    max_turns=10,
                    timeout_s=300,
                )
                summary = res.get("summary", "no summary")
                ok = res.get("ok", False)
                sub_feedback.append(
                    f"[SUBAGENT RESULT] goal={goal!r} ok={ok}\n{summary}\n"
                    f"workdir: {res.get('workdir', '')}\n"
                    f"stdout:\n{res.get('stdout', '')[:2000]}"
                )
        except Exception as e:
            sub_feedback.append(f"[SUBAGENT ERROR] {e}")
        if sub_feedback:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "\n\n".join(sub_feedback)
                        + "\n\nThe subagent results above are now part of the context. "
                        "If the task is complete, answer concisely. If more work is needed, "
                        "emit the next directive."
                    ),
                }
            )
            _ma().log(
                f"CHAIN_SUBAGENT_FEEDBACK: {len(sub_feedback)} subagent result(s)"
            )
            return None

    # 2026-10-06: SCREENSHOT: requests — capture desktop + local vision,
    # feed the description back into the conversation (same feedback-loop
    # shape as SUBAGENT above). Runs BEFORE the subagent check would be
    # equally fine; kept after it so heavier delegation preempts eyes.
    if screenshot_requests:
        screenshot_feedback = []
        for question in screenshot_requests[:3]:
            print(f"  {BC}[screenshot: {question[:60]}...]{X}")
            try:
                description = _capture_and_describe_screen(question)
            except Exception as e:
                description = f"screenshot vision call failed: {e}"
            screenshot_feedback.append(f"[SCREENSHOT RESULT]\n{description}")
        if screenshot_feedback:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "\n\n".join(screenshot_feedback)
                        + "\n\nThe screenshot description above is now part of the context. "
                        "Answer the user's question using it."
                    ),
                }
            )
            _ma().log(
                f"CHAIN_SCREENSHOT_FEEDBACK: {len(screenshot_feedback)} screenshot result(s)"
            )
            return None

    # 2026-09-24: root-caused live — a reply can claim it's taking an
    # action ("Ok, executing the service health checks now.") while
    # emitting zero parseable directives. Every branch above only fires
    # when a directive WAS found and then failed; a reply with no
    # directive at all used to fall straight through to `return reply`
    # below and get accepted as a complete, final answer — nothing had
    # actually run, and there was no error to explain why. Mirrors the
    # sibling repair branches above (e.g. the missing-execution-target
    # repair): append a repair prompt and let the existing chain-level
    # backstops (repetition truncation, MAX_CONTINUATION_TURNS) bound it —
    # no separate counter needed here either.
    if not _any_directive_found:
        # _reply_claims_unexecuted_action is implemented; the old
        # `except NotImplementedError` guard was stub-era scaffolding.
        claims_action = _reply_claims_unexecuted_action(reply)
        if claims_action:
            print(
                _ma()._pill(
                    "REPAIR",
                    f"{D}reply claimed an action but emitted no directive — re-asking{X}",
                )
            )
            _ma().log(
                "CHAIN_UNEXECUTED_ACTION_CLAIM: reply had no directive despite action language"
            )
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[Directive repair]\n"
                        "Your last reply described taking an action (e.g. "
                        '"executing now", "let me check") but contained no '
                        "actual RUN:/READ:/SEARCH:/etc. directive — nothing "
                        "ran. Either emit the real directive now, or if the "
                        "work is genuinely already done, say so plainly "
                        "without claiming an action still in progress."
                    ),
                }
            )
            return None

        # 2026-09-25: root-caused live via a delegated subagent — a reply
        # can also fail silently a THIRD way: not a stall claim in prose,
        # but a hallucinated pseudo-directive that LOOKS like real
        # directive syntax and isn't ("GREP: def process_reply" — this
        # system has no GREP: directive, the real one is SEARCH: or
        # RUN: grep ...). _any_directive_found is already False here (no
        # REAL directive matched), so this text fell straight through as
        # an accepted "answer" that was actually just a guessed, never-
        # dispatched command name. Mirrors _xml_tool_calls_to_directives'
        # own stated philosophy for malformed tool-call XML ("a malformed
        # -but-visible directive line beats invisible raw syntax, because
        # the directive-repair feedback loop can then teach the model the
        # right shape") — same idea, applied to a hallucinated ALLCAPS:
        # keyword instead of malformed XML.
        _fake_directive = re.match(r"^\s*([A-Z][A-Z_]{1,24}):\s*\S", reply.strip())
        if _fake_directive and _fake_directive.group(1) not in {
            "RUN",
            "RUNTERM",
            "READ",
            "CREATE",
            "EDIT",
            "SEARCH",
            "SUBAGENT",
            "TASK_ADD",
            "TASK_DONE",
            "SEND_EMAIL",
            "SEND_TELEGRAM",
            "REMEMBER",
        }:
            _bad_name = _fake_directive.group(1)
            print(
                _ma()._pill(
                    "REPAIR",
                    f"{D}reply used '{_bad_name}:' — not a real directive, teaching the real ones{X}",
                )
            )
            _ma().log(f"CHAIN_UNKNOWN_DIRECTIVE_REPAIR: {_bad_name!r}")
            history.append(
                {
                    "role": "user",
                    "content": (
                        "[Directive repair]\n"
                        f"'{_bad_name}:' is not a real directive here — nothing ran. "
                        "The real directives are RUN:, RUNTERM:, READ:, CREATE:, EDIT:, "
                        "SEARCH:, SUBAGENT:, TASK_ADD:, TASK_DONE:, SEND_EMAIL:, "
                        "SEND_TELEGRAM:, REMEMBER:. For a code/file search use SEARCH: "
                        "<query> or RUN: grep ... Emit the real directive now."
                    ),
                }
            )
            return None

    return reply


def _extract_browser_actions(lines: Any) -> Any:
    """Pull {kind, target, value} out of BROWSER_*: lines, same shape as
    sensei_bridge.py's parse_directives(). Separator matches what the
    system prompt already documents for BROWSER_FILL/BROWSER_UPLOAD_FILE
    (:: or => or :=) — no prompt change needed, just wiring execution
    behind a vocabulary the model already knows."""
    actions = []
    for line in lines:
        m = _BROWSER_DIRECTIVE_RE.match(line.strip())
        if not m:
            continue
        kind, payload = m.group(1), m.group(2).strip()
        if not payload:
            continue
        parts = _BROWSER_SEP_RE.split(payload, maxsplit=1)
        target = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        actions.append({"kind": kind, "target": target, "value": value})
    return actions


def _split_run_policy(cmd: Any, visual_requested: bool = False) -> tuple:
    """Return (run_parts, runterm_parts) for a model-emitted RUN command."""
    parts = _ma()._shell_and_parts(cmd)
    if len(parts) == 1:
        if _ma()._is_visual_command_part(parts[0], visual_requested=visual_requested):
            return [], [parts[0]]
        return [cmd], []

    run_parts, runterm_parts = [], []
    i = 0
    while i < len(parts):
        part = parts[i]
        if _ma()._is_visual_command_part(part, visual_requested=visual_requested):
            # Keep the visual command plus any following shell pieces together
            # in the terminal. Setup/check pieces before it stay captured.
            runterm_parts.append(" && ".join(parts[i:]))
            break
        run_parts.append(part)
        i += 1
    return run_parts, runterm_parts
