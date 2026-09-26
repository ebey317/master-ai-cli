"""Master AI CLI native delegation runner.

2026-09-25: rewritten. `delegate_task`/`run_subagent` now route `delegate
<goal>` and `SUBAGENT:` directives to a REAL model-backed sub-conversation
running in the actual project directory, with real RUN:/READ:/CREATE:/
EDIT:/SEARCH: tool access -- the same shape as delegating to Claude,
Hermes, or OpenCode. Model/provider is never hardcoded: it defaults to
whatever's pinned in the main session (master_ai.PINNED_MODEL), falls
back to the free local model, and an optional `<provider>: <goal>` or
`higher: <goal>` prefix overrides that per call. See run_subagent()'s
block comment for the full routing table and the bug this replaces (the
old version ran in an empty tempfile.mkdtemp() sandbox with zero access
to real project files, gated to a fixed registry of 6 non-LLM functions).

The old deterministic, no-LLM, registry-sandboxed dispatch is kept as
run_subagent_sandboxed() for anyone who wants that specific behavior on
purpose -- it's still what the individual named subagents in
subagent_registry.py (file_finder, code_reviewer, ...) are built around.

Public API:
    delegate_task(goal: str, context: dict = None, toolsets: list = None,
                  max_turns: int = 6, timeout_s: int = 300) -> dict

Return dict always contains:
    {
      "ok": bool,
      "goal": str,
      "summary": str,
      "stdout": str,
      "stderr": str,
      "returncode": int,
      "workdir": str,           # the REAL project directory this ran in
      "toolsets": list,
      "route": str,             # which model/provider actually answered
      "turns": int,
      "result": dict,           # {"final_text": ...}
    }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_S = 300


def _make_child_script(
    workdir: str,
    registry_dir: str,
    toolsets_json: str,
    goal: str,
    context_json: str,
) -> str:
    """Return the Python source for the isolated child process."""
    return textwrap.dedent(
        f"""
        import json, os, re, subprocess, sys, traceback
        from pathlib import Path

        WORKDIR = Path({workdir!r}).resolve()
        REGISTRY_DIR = Path({registry_dir!r})
        TOOLSETS = set(json.loads({toolsets_json!r}))
        GOAL = {goal!r}
        CONTEXT = json.loads({context_json!r})

        # ── Sandbox policy ─────────────────────────────────────────
        ALLOWED_TOOLS = {{"subagent", "run", "read", "create", "edit", "done"}}
        if "terminal" not in TOOLSETS:
            ALLOWED_TOOLS.discard("run")
        if "file" not in TOOLSETS:
            ALLOWED_TOOLS.discard("read")
            ALLOWED_TOOLS.discard("create")
            ALLOWED_TOOLS.discard("edit")

        DANGEROUS_CMDS = {{
            "rm", "sudo", "mkfs", "dd", "format", "fdisk", "parted",
            "shutdown", "reboot", "poweroff",
            "chmod", "chown", "passwd", "su ", "su -", "ssh", "scp", "sftp",
            "nc ", "ncat", "nmap", "telnet", "ftp",
            "openssl",
        }}
        # curl/wget are only dangerous when network is not explicitly allowed.
        if "network" not in TOOLSETS:
            DANGEROUS_CMDS.update({{"curl", "wget"}})

        SECRET_RE = re.compile(
            r"(^|/)(\\.ssh|\\.aws|\\.gnupg|\\.master_ai_|\\.env|env\\.txt|"
            r"secrets?\\.txt|tokens?\\.json|credentials?\\.json)($|/)",
            re.IGNORECASE,
        )

        HOME = Path.home().resolve()

        def _path_ok(path: str, for_write: bool = False) -> tuple:
            try:
                p = Path(path).resolve()
            except Exception:
                return False, "invalid path"
            if for_write:
                if WORKDIR not in (p, *p.parents):
                    return False, f"write outside workdir: {{p}}"
            else:
                if not str(p).startswith(str(HOME)):
                    return False, f"read outside home: {{p}}"
            if SECRET_RE.search(str(p)):
                return False, f"secret path blocked: {{p}}"
            return True, ""

        def _tool_allowed(kind: str) -> bool:
            return kind in ALLOWED_TOOLS

        # ── Low-level tool implementations (used by subagents or directly) ──
        def run_cmd(cmd: str) -> str:
            if not _tool_allowed("run"):
                return "RUN BLOCKED: terminal toolset not granted"
            lowered = cmd.lower().strip()
            if any(d in lowered for d in DANGEROUS_CMDS):
                return f"RUN BLOCKED: dangerous pattern in {{cmd!r}}"
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                out = (r.stdout or "") + (r.stderr or "")
                return out[:4000] + ("\\n..." if len(out) > 4000 else "")
            except Exception as e:
                return f"RUN ERROR: {{e}}"

        def read_file(path: str) -> str:
            if not _tool_allowed("read"):
                return "READ BLOCKED: file toolset not granted"
            ok, why = _path_ok(path, for_write=False)
            if not ok:
                return f"READ BLOCKED: {{why}}"
            try:
                p = Path(path).resolve()
                if not p.is_file():
                    return f"READ ERROR: not found: {{path}}"
                text = p.read_text(errors="replace")
                return text[:8000] + ("\\n..." if len(text) > 8000 else "")
            except Exception as e:
                return f"READ ERROR: {{e}}"

        def _to_workdir_path(path: str) -> Path:
            rel = os.path.expanduser(path)
            if rel.startswith("/"):
                return WORKDIR / Path(rel).name
            return WORKDIR / rel

        def create_file(path: str, content: str) -> str:
            if not _tool_allowed("create"):
                return "CREATE BLOCKED: file toolset not granted"
            target = _to_workdir_path(path)
            ok, why = _path_ok(str(target), for_write=True)
            if not ok:
                return f"CREATE BLOCKED: {{why}}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return f"CREATE OK: {{target}}"
            except Exception as e:
                return f"CREATE ERROR: {{e}}"

        def edit_file(path: str, old: str, new: str) -> str:
            if not _tool_allowed("edit"):
                return "EDIT BLOCKED: file toolset not granted"
            target = _to_workdir_path(path)
            ok, why = _path_ok(str(target), for_write=True)
            if not ok:
                return f"EDIT BLOCKED: {{why}}"
            try:
                text = target.read_text(encoding="utf-8")
                if old not in text:
                    return f"EDIT ERROR: old text not found in {{target}}"
                target.write_text(text.replace(old, new), encoding="utf-8")
                return f"EDIT OK: {{target}}"
            except Exception as e:
                return f"EDIT ERROR: {{e}}"

        def fetch_url(url: str) -> str:
            if "network" not in TOOLSETS:
                return "FETCH BLOCKED: network toolset not granted"
            if not re.match(r"^https?://", url):
                return "FETCH BLOCKED: only http/https allowed"
            try:
                r = subprocess.run(["curl", "-sL", "--max-time", "30", url],
                                   capture_output=True, text=True, timeout=35)
                out = (r.stdout or "") + (r.stderr or "")
                return out[:4000] + ("\\n..." if len(out) > 4000 else "")
            except Exception as e:
                return f"FETCH ERROR: {{e}}"

        # ── Registered subagent dispatch ────────────────────────────
        def run_subagent(name: str, task: str, context: dict = None) -> dict:
            if not _tool_allowed("subagent"):
                return {{"error": "SUBAGENT BLOCKED: subagent toolset not granted"}}
            # Ensure subagent_registry and the registry dir are importable.
            sys.path.insert(0, str(REGISTRY_DIR.parent))
            sys.path.insert(0, str(REGISTRY_DIR))
            # Also make the parent repo dir importable for sibling modules
            # (e.g. url_grounding imported by master_ai when general tries to
            # import master_ai).
            repo_parent = str(Path(REGISTRY_DIR).parent)
            if repo_parent not in sys.path:
                sys.path.insert(0, repo_parent)
            try:
                import subagent_registry as _sr
                _sr.discover(REGISTRY_DIR)
                return _sr.run(name, task, context=context)
            except Exception:
                return {{"error": str(traceback.format_exc()), "subagent": name}}

        # ── Directive parser (single-turn, no chat loop) ──────────
        def parse_directives(text: str) -> list:
            actions = []
            lines = text.splitlines()
            i = 0
            while i < len(lines):
                raw = lines[i]
                line = raw.strip()
                if line.startswith("SUBAGENT:"):
                    rest = line[9:].strip()
                    parts = rest.split(None, 1)
                    name = parts[0]
                    task = parts[1] if len(parts) > 1 else ""
                    actions.append(("subagent", name, task))
                elif line.startswith("RUN:"):
                    actions.append(("run", line[4:].strip()))
                elif line.startswith("READ:"):
                    actions.append(("read", line[5:].strip()))
                elif line.startswith("FETCH:"):
                    actions.append(("fetch", line[6:].strip()))
                elif line.startswith("CREATE:"):
                    path = line[7:].strip()
                    content_lines = []
                    i += 1
                    if i < len(lines) and lines[i].strip() == "<<<CONTENT":
                        i += 1
                        while i < len(lines) and lines[i].strip() != ">>>CONTENT":
                            content_lines.append(lines[i])
                            i += 1
                    if not content_lines and " :: " in path:
                        path, content = path.split(" :: ", 1)
                    else:
                        content = "\\n".join(content_lines)
                    actions.append(("create", path, content))
                elif line.startswith("EDIT:"):
                    path = line[5:].strip()
                    old_lines, new_lines = [], []
                    i += 1
                    stage = None
                    while i < len(lines):
                        l = lines[i]
                        if l.strip() == "<<<FIND":
                            stage = "old"; i += 1; continue
                        elif l.strip() == "===":
                            stage = "new"; i += 1; continue
                        elif l.strip() == ">>>REPLACE":
                            i += 1; break
                        if stage == "old":
                            old_lines.append(l)
                        elif stage == "new":
                            new_lines.append(l)
                        i += 1
                    actions.append(("edit", path, "\\n".join(old_lines), "\\n".join(new_lines)))
                elif line.startswith("DONE:"):
                    actions.append(("done", line[5:].strip()))
                i += 1
            return actions

        # ── Execution ───────────────────────────────────────────────
        result_record = {{
            "goal": GOAL,
            "context": CONTEXT,
            "workdir": str(WORKDIR),
            "toolsets": sorted(TOOLSETS),
            "actions": [],
            "subagent_result": {{}},
            "done": None,
        }}

        def emit(obj):
            print(json.dumps(obj), flush=True)

        # The parent sends the delegation goal on stdin as a single line.
        goal_line = sys.stdin.readline().rstrip("\\n")
        if not goal_line:
            emit({{"error": "no goal on stdin"}})
            sys.exit(0)

        actions = parse_directives(goal_line)
        for action in actions:
            kind = action[0]
            if kind not in ALLOWED_TOOLS:
                record = {{"action": kind, "result": f"BLOCKED: tool '{{kind}}' not allowed"}}
                result_record["actions"].append(record)
                emit(record)
                continue
            if kind == "done":
                result_record["done"] = action[1]
                emit({{"action": "done", "result": action[1]}})
                break
            elif kind == "subagent":
                _, name, task = action
                res = run_subagent(name, task, context=CONTEXT)
                result_record["subagent_result"] = res
                record = {{"action": "subagent", "name": name, "result": res}}
            elif kind == "run":
                record = {{"action": "run", "result": run_cmd(action[1])}}
            elif kind == "read":
                record = {{"action": "read", "result": read_file(action[1])}}
            elif kind == "create":
                record = {{"action": "create", "result": create_file(action[1], action[2])}}
            elif kind == "edit":
                record = {{"action": "edit", "result": edit_file(action[1], action[2], action[3])}}
            elif kind == "fetch":
                record = {{"action": "fetch", "result": fetch_url(action[1])}}
            else:
                record = {{"action": kind, "result": "UNKNOWN"}}
            result_record["actions"].append(record)
            emit(record)

        emit({{"__final__": result_record}})
        """
    ).strip()


def _goal_to_directive(
    goal: str,
    available: list[str],
    context: dict[str, Any],
) -> str:
    """Convert a free-form goal into a single SUBAGENT: directive.

    If the goal already starts with an allowed directive prefix, pass it
    through. Otherwise try to match a registered subagent name, or fall back
    to `file_finder` with the goal as a name pattern.
    """
    prefixes = ("SUBAGENT:", "RUN:", "READ:", "CREATE:", "EDIT:", "FETCH:", "DONE:")
    if goal.strip().upper().startswith(prefixes):
        return goal

    words = goal.lower().split()
    # Try exact name match against registry. The first word must be an
    # exact registered name; otherwise we fall through to heuristic routing.
    if words:
        available_lower = dict((n.lower(), n) for n in available)
        if words[0] in available_lower:
            name = available_lower[words[0]]
            rest = goal[len(words[0]) :].strip()
            return f"SUBAGENT: {name} {rest}"

    # Heuristic: if goal looks like a file search, use file_finder.
    low = goal.lower()
    if any(w in low for w in ("find file", "search file", "look for file")):
        return f"SUBAGENT: file_finder name:{goal}"
    if "grep" in low:
        # strip a redundant leading "grep" before forming the task
        task = re.sub(r"^\\s*grep\\s+", "", goal, flags=re.IGNORECASE)
        return f"SUBAGENT: file_finder grep:{task}"

    # Default to the general executor for any bare goal; it can do whatever
    # the toolsets allow (read/create/edit/run/fetch/subagent).
    return f"SUBAGENT: general {goal}"


# 2026-09-25: root-caused live -- Elijah: "i can't delegate a sub agent...
# i want to be able to delegate to an agent to do whatever i wanted to do
# [the same way] i delegate with you [Claude], the same way with Hermes,
# the same way with OpenCode." The registry-sandboxed path below
# (run_subagent_sandboxed) was never that: its own module docstring says
# it "does NOT require an online LLM" -- it's a fixed set of 6 narrow
# Python functions (file_finder, code_reviewer, ...) with a regex-based
# "general" fallback that fakes free-form delegation via sentence
# templates ("read X", "create X with Y", ...), and every one of them ran
# inside a brand-new EMPTY tempfile.mkdtemp() workdir. Confirmed live:
# asking it to "find master_ai.py" while sitting in the project directory
# containing that exact file returned 0 matches -- the sandbox has no
# bridge to the real project at all. master_ai.py's own call site (see
# process_reply()) DOES pass context={"cwd": str(Path.cwd()), ...}, but
# nothing in this file ever read context["cwd"] -- grepped the whole file,
# zero matches. Dead data.
#
# Fix: `run_subagent`/`delegate_task` now run the goal as a REAL bounded
# sub-conversation using master_ai's own model-routing and directive-
# dispatch machinery (ask_local_stream/ask_cloud + process_reply) --
# operating in the REAL project directory context["cwd"] gives, with real
# RUN:/READ:/CREATE:/EDIT:/SEARCH: tool access, exactly the shape of a
# Claude/Hermes/OpenCode subagent delegation. Model selection is never
# hardcoded (Elijah: "i don't need my framework hard coding anything"):
#   - bare goal                    -> master_ai.PINNED_MODEL if set
#                                      ("the key that's pinned"), else the
#                                      free local model (cheap default)
#   - "higher: <goal>" / "escalate: <goal>"
#                                   -> PINNED_MODEL if set, else the
#                                      system's existing free cloud
#                                      default (no new hardcoded model)
#   - "local: <goal>"              -> force the free local model
#   - "<provider>: <goal>"         -> literal provider string handed
#                                      straight to ask_cloud(), which
#                                      already parses any curated name,
#                                      any "ns::model" pin, or any raw
#                                      OpenRouter catalog id containing
#                                      "/" -- e.g. "openrouter/qwen/
#                                      qwen3.8-27b:free: <goal>" reaches
#                                      that exact free model with zero new
#                                      plumbing, matching "i could be
#                                      using the open code model and say
#                                      use open router free and delegate
#                                      the most capable model to do this."
# The old registry-sandboxed implementation is kept below, renamed
# run_subagent_sandboxed, for anyone who wants the deterministic
# no-model dispatch on purpose (it's still what individual named
# subagents like code_reviewer/test_runner are built around).

_DELEGATE_PREFIX_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+(?:::[A-Za-z0-9_./\-]+)?(?:/[A-Za-z0-9_.:\-]+)*)\s*:\s*(.+)$",
    re.DOTALL,
)
_DELEGATE_HIGHER_WORDS = {"higher", "escalate", "strong", "best", "stronger"}
_DELEGATE_LOCAL_WORDS = {"local", "lesser", "cheap", "cheapest"}
# 2026-09-25: reproduced live -- a goal that happens to start with an
# ordinary capitalized word + colon ("Run: grep ...", meant as an
# imperative sentence, not a provider directive) was silently swallowed
# as an explicit provider override ("Run"), which fell through
# ask_cloud()'s own unrecognized-provider fallback to the free OpenCode
# relay and returned garbled token-soup output -- a real bug, not a
# hypothetical one. A bare word before a colon is NOT enough evidence it
# names a provider: require it to actually look like one of ask_cloud's
# real provider shapes (contains "::" or "/", matching a pin or an
# OpenRouter catalog id) or be one of its small set of curated bare
# names. Anything else -- including "Run:", "Note:", "Fix:" and similar
# ordinary sentence openers -- falls through to tier="default" with the
# goal left completely untouched.
# 2026-09-26: was a hand-copied duplicate of master_ai.ask_cloud()'s real
# fn_map keys — already caught drifting when Poolside landed in fn_map
# tonight but never got added here, so `delegate poolside-s: ...` would
# have silently fallen through to tier="default" instead of being
# recognized as a provider override. master_ai.py now exports the real
# set as ASK_CLOUD_BARE_PROVIDERS; read it lazily (see the existing lazy
# import a few functions below — master_ai imports this module too, so a
# module-level import here would be circular) with this literal set kept
# only as a last-resort fallback if master_ai can't be imported at all.
_DELEGATE_FALLBACK_BARE_PROVIDERS = {
    "opencode",
    "opencode-go",
    "glm-5.3-flash",
    "nvidia",
    "nvidia-nano",
    "hermes-405b",
    "gpt-oss-120b",
    "nemotron",
    "qwen3-coder",
    "deepseek-r1",
    "openrouter",
}


def _known_bare_providers():
    try:
        import master_ai as _ma

        return _ma.ASK_CLOUD_BARE_PROVIDERS
    except Exception:
        return _DELEGATE_FALLBACK_BARE_PROVIDERS


def _parse_delegate_goal(goal: str):
    """Split an optional leading `<provider-or-tier>:` prefix off a
    delegate goal. Returns (tier, explicit_provider, clean_goal):
      tier in {"default", "higher", "local", "explicit"}
      explicit_provider is a literal provider string (only set for
      tier == "explicit"), passed straight to master_ai.ask_cloud().
    Ordinary sentence goals never match: the prefix pattern requires no
    spaces before the colon, AND (for anything that isn't a recognized
    tier keyword) the head must actually look like a provider token --
    "read the config and summarize it" and "Run: grep ..." are both left
    untouched as tier="default", the whole original string as the goal."""
    g = (goal or "").strip()
    m = _DELEGATE_PREFIX_RE.match(g)
    if not m:
        return "default", None, g
    head, rest = m.group(1), m.group(2).strip()
    low = head.lower()
    if low in _DELEGATE_HIGHER_WORDS:
        return "higher", None, rest
    if low in _DELEGATE_LOCAL_WORDS:
        return "local", None, rest
    looks_like_provider = "::" in head or "/" in head or low in _known_bare_providers()
    if not looks_like_provider:
        return "default", None, g
    return "explicit", head, rest


def run_subagent(
    goal: str,
    context: dict[str, Any] | None = None,
    toolsets: list[str] | None = None,
    max_turns: int = 6,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Delegate `goal` to a real model-backed subagent operating on the
    real project directory (context["cwd"], falling back to os.getcwd()).
    See the block comment above for the routing/model-selection rules and
    the bug this replaces. `toolsets` is accepted for call-site
    compatibility but unused -- the delegated turn gets the same real
    RUN:/READ:/CREATE:/EDIT: tool access the main session has, gated the
    same way (hooks, path fences) since it runs through the same
    process_reply()."""
    import master_ai as _ma  # lazy: master_ai imports this module too

    context = context or {}
    real_cwd = context.get("cwd") or os.getcwd()
    tier, explicit_provider, clean_goal = _parse_delegate_goal(goal)

    pinned = getattr(_ma, "PINNED_MODEL", None)
    use_local = False
    provider = None
    local_model = None
    if tier == "local":
        use_local = True
        local_model = _ma.DEFAULT_LOCAL_MODEL
    elif tier == "explicit":
        use_local = False
        provider = explicit_provider
    elif tier == "higher":
        if pinned:
            use_local = False
            provider = pinned
        else:
            use_local = False
            provider = "opencode"  # system's existing free cloud default
    else:  # "default"
        if pinned:
            use_local = False
            provider = pinned
        else:
            use_local = True
            local_model = _ma.DEFAULT_LOCAL_MODEL

    history = [
        {
            "role": "system",
            "content": (
                "You are a focused subagent delegated ONE goal by the main "
                "session. Use RUN:/READ:/CREATE:/EDIT:/SEARCH: directives "
                "as needed to complete it for real against the actual "
                "project files in your current directory -- you have the "
                "same tool access the main session has. When the goal is "
                "genuinely complete, reply with a final answer that shows "
                "your work, not just a bare fact: for a code location give "
                "file:line plus the actual matched line/snippet, for a "
                "command give the relevant output, for a file give the "
                "relevant excerpt -- the same way you'd report a finding "
                "to a developer, not a one-word answer. Only answer that "
                "tersely if the goal explicitly asks for just a single "
                "value with nothing else. Do not claim an action is "
                "happening without emitting the directive for it."
            ),
        },
        {"role": "user", "content": clean_goal},
    ]

    old_cwd = os.getcwd()
    turn_log: list[str] = []
    result_text = None
    ok = True
    err = ""
    turns = 0
    start = time.monotonic()
    try:
        os.chdir(real_cwd)
        while result_text is None and turns < max_turns:
            remaining = timeout_s - (time.monotonic() - start)
            if remaining <= 0:
                err = f"delegation timed out after {timeout_s}s ({turns} turn(s) ran)"
                ok = False
                break
            if use_local:
                reply = _ma._call_with_hard_timeout(
                    _ma.ask_local_stream,
                    history,
                    model=local_model,
                    timeout=min(90, int(remaining)),
                )
                streamed = True
            else:
                reply = _ma.ask_cloud(history, provider=provider)
                streamed = False
            turns += 1
            if not reply:
                err = f"model returned no reply (route={'local:' + str(local_model) if use_local else provider})"
                ok = False
                break
            turn_log.append(f"[turn {turns}] {reply[:800]}")
            result_text = _ma.process_reply(
                reply, history, streamed=streamed, continue_after_tools=True
            )
        if result_text is None and ok:
            err = f"hit max_turns ({max_turns}) without a final answer"
            ok = False
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
    finally:
        os.chdir(old_cwd)

    summary = (result_text or err or "no result").strip()
    route_label = f"local:{local_model}" if use_local else str(provider)
    return {
        "ok": ok and result_text is not None,
        "goal": goal,
        "directive": clean_goal,
        "summary": summary[:2000],
        "stdout": "\n".join(turn_log)[:4000],
        "stderr": err,
        "returncode": 0 if (ok and result_text is not None) else 1,
        "workdir": real_cwd,
        "toolsets": ["run", "read", "create", "edit", "search"],
        "route": route_label,
        "turns": turns,
        "result": {"final_text": result_text},
        "final_record": {"goal": goal, "route": route_label, "turns": turns},
    }


# Convenience helper: directly run a registered subagent by name.
# 2026-09-25: kept for anyone who wants the deterministic, no-model,
# sandboxed registry dispatch on purpose -- no longer the public
# `run_subagent`/`delegate_task` path (see block comment above).
def run_subagent_sandboxed(
    goal: str,
    context: dict[str, Any] | None = None,
    toolsets: list[str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Dispatch a delegated task to a registered subagent, isolated.

    The runner itself does not call any LLM. It maps the goal to a
    SUBAGENT: directive, runs the child, and returns the subagent's
    structured result.
    """
    context = context or {}
    toolsets = list(set(toolsets or ["subagent"]))
    if "subagent" not in toolsets:
        toolsets.append("subagent")

    # Discover available subagents so we can route by name.
    import subagent_registry as _sr

    registry_dir = Path(_sr.SUBAGENTS_DIR)
    if not registry_dir.is_dir():
        registry_dir = Path(__file__).parent / "subagents"
    _sr.discover(registry_dir)
    available = [s.name for s in _sr.list_subagents()]

    workdir = Path(tempfile.mkdtemp(prefix="master_ai_delegate_"))
    directive = _goal_to_directive(goal, available, context)

    runner_path = workdir / "_delegate_runner.py"
    runner_path.write_text(
        _make_child_script(
            workdir=str(workdir),
            registry_dir=str(registry_dir),
            toolsets_json=json.dumps(toolsets),
            goal=goal,
            context_json=json.dumps(context, default=str),
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["MASTER_AI_DELEGATE_NETWORK"] = "1" if "network" in toolsets else "0"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path(__file__).parent),
            str(registry_dir.parent),
            str(registry_dir),
            env.get("PYTHONPATH", ""),
        ]
    )

    proc = subprocess.Popen(
        [sys.executable, str(runner_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(workdir),
        env=env,
    )

    outputs: list[str] = []
    errors: list[str] = []
    final_record: dict[str, Any] = {}

    try:
        if proc.stdin is not None:
            proc.stdin.write(directive + "\n")
            proc.stdin.flush()

        start = time.monotonic()
        while True:
            if time.monotonic() - start > timeout_s:
                errors.append("HARD TIMEOUT")
                break
            line = proc.stdout.readline()  # type: ignore
            if not line:
                break
            outputs.append(line.rstrip("\n"))
            if '"__final__"' in line:
                break

        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        except ValueError:
            # stdin already closed by us; just read remaining output
            stdout, stderr = "", ""
        if stderr:
            errors.append(stderr)
    except Exception as e:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            stdout, stderr = "", ""
        errors.append(str(e))

    # Parse __final__ record from output.
    all_stdout = "\n".join(outputs) + (stdout or "")
    all_stderr = "\n".join(errors)
    for line in reversed(outputs):
        try:
            obj = json.loads(line)
            if "__final__" in obj:
                final_record = obj["__final__"]
                break
        except Exception:
            continue

    subagent_result = final_record.get("subagent_result", {})
    done = final_record.get("done")

    # Build a clean summary from the structured result if available.
    if isinstance(subagent_result, dict) and subagent_result:
        summary = (
            subagent_result.get("summary")
            or subagent_result.get("result")
            or (all_stdout.splitlines()[-1] if all_stdout else "subagent returned")
        )
        # Treat the run as OK unless there is an explicit error key at the top
        # level of the subagent result, or stderr/errors were captured.
        has_error = "error" in subagent_result
        ok = proc.returncode == 0 and not errors and not has_error
    elif done:
        summary = done
        ok = proc.returncode == 0 and not errors
    else:
        summary = all_stdout.splitlines()[-1] if all_stdout else "no output"
        ok = False

    return {
        "ok": ok,
        "goal": goal,
        "directive": directive,
        "summary": str(summary),
        "stdout": all_stdout,
        "stderr": all_stderr,
        "returncode": proc.returncode if proc.returncode is not None else -1,
        "workdir": str(workdir),
        "toolsets": toolsets,
        "result": subagent_result,
        "final_record": final_record,
    }


# Backward-compatible alias: delegate_task is the public name.
def delegate_task(
    goal: str,
    context: dict[str, Any] | None = None,
    toolsets: list[str] | None = None,
    max_turns: int = 10,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Alias for run_subagent. max_turns now genuinely bounds the delegated
    model conversation (see run_subagent's block comment, 2026-09-25) --
    it used to be accepted-but-ignored back when this ran a single
    non-LLM registry dispatch."""
    return run_subagent(
        goal,
        context=context,
        toolsets=toolsets,
        max_turns=max_turns,
        timeout_s=timeout_s,
    )


if __name__ == "__main__":
    result = delegate_task(
        goal="find master_ai.py",
        context={"test": True},
        max_turns=1,
    )
    print(json.dumps(result, indent=2, default=str))
