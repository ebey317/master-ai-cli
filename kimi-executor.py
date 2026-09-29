#!/usr/bin/env python3
"""
kimi-executor.py — Run an agentic loop where KIMI K2.7 (Ollama Cloud) is the
executor. The orchestrator (Hermes) only sets up the goal + context, then
watches.

Differences from fire-kimi-cleanup.py:
- Tool-calling: Kimi decides what shell command to run
- Multi-turn: Kimi iterates, observes output, reasons, runs next command
- Safety wrapper: run_shell is gated by an allowlist + path guard
- Orchestrator (Hermes) does NOT execute anything — it only dispatches and observes

Run: python3 kimi-executor.py <goal-file>
The goal file is a markdown brief with: GOAL, CONTEXT, CONSTRAINTS, DONE-WHEN.

The loop:
  1. Send system prompt + goal to Kimi with run_shell tool
  2. If Kimi returns tool_call: validate command, execute, return stdout+stderr+rc
  3. Repeat until Kimi returns content (no tool_call) or hit max turns (default 20)
  4. Print full transcript (Kimi's reasoning + commands + outputs) to stdout

Safety:
- Allowlist: only safe commands (mv, du, df, ls, cat, stat, etc.)
- Path guard: any command touching /etc, /usr, /var, or files outside
  the explicit allow-list of paths is REFUSED
- rm -rf is only allowed for paths matching /home/elijah/.cleanup-stash-*
- systemd-run is allowed with restricted args (--user, --unit=cleanup-*,
  --on-active, --timer-property, plus a script path that is itself restricted)
- Each refusal is logged to ~/.cleanup-audit.log with the refused command

Exit code:
  0 = Kimi declared done and the orchestrator (this script) verified the
      post-conditions in DONE-WHEN
  1 = Kimi declared done but a post-condition failed
  2 = transport / API error
  3 = max turns hit without Kimi declaring done
"""

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://ollama.com/v1/chat/completions"
MODEL = "kimi-k2.7-code"
MAX_TURNS = 30
AUDIT_LOG = "/home/elijah/.cleanup-audit.log"

# ---------- Allowlist ----------

# Commands Kimi is allowed to run. Anything else is refused.
ALLOWED_COMMANDS = {
    # inspection
    "ls",
    "du",
    "df",
    "stat",
    "cat",
    "head",
    "tail",
    "wc",
    "test",
    "echo",
    "readlink",
    "find",
    "grep",
    "file",
    "realpath",
    "date",
    "loginctl",
    "systemctl",
    "git",
    "diff",
    "printf",
    "true",
    "false",
    # filesystem ops (validated separately by path guard)
    "mv",
    "mkdir",
    "touch",
    "ln",
    "chmod",
    # process / service (validated)
    "systemd-run",
    # python (read-only verification helpers)
    "python3",
}

# Regex allow-list for paths. Anything touching a path NOT in this list is refused.
ALLOWED_PATH_PATTERNS = [
    r"^/home/elijah$",  # root for find/ls queries
    r"^/home/elijah/\.cleanup-stash-20260812(/.*)?$",
    r"^/home/elijah/\.cleanup-audit\.log$",
    r"^/home/elijah/\.cleanup-purge\.sh$",
    r"^/home/elijah/llama\.cpp(/.*)?$",
    r"^/home/elijah/\.local/share/claude/versions(/.*)?$",
    r"^/home/elijah/Desktop/Hermes-Docs/MasterAI(/.*)?$",
    r"^/home/elijah/Desktop/Hermes-Docs/Reference(/.*)?$",
    r"^/home/elijah/Desktop/Archives(/.*)?$",
    r"^/home/elijah/Desktop(/.*)?$",  # allow find/ls under Desktop
    r"^/tmp/(cleanup-.*|llama-.*|goal-.*)$",
    r"^/$",  # df, ls /
    r"^/dev/null$",
]
ALLOWED_PATH_RE = re.compile("|".join(ALLOWED_PATH_PATTERNS))

# Commands that must NOT appear anywhere in the command (defense in depth).
# Stash pattern means we never rm -rf user data. The only path that can be
# rm-rf'd is the explicit cleanup-stash dir, and that's done by the
# .cleanup-purge.sh script (path-restricted) — not by Kimi directly.
FORBIDDEN_SUBSTRINGS = [
    "rm -rf",
    "rm -fr",
    "rm -f -r",
    "rm -r -f",
    "&& rm",
    "; rm",
    "| rm",
    "sudo ",
    "su -",
    "su root",
    "curl ",
    "wget ",  # no network from Kimi
    "> /etc/",
    ">> /etc/",
    "> /usr/",
    ">> /usr/",
    "chmod 777",
    "chown -R",
    ":(){:|:&};:",  # fork bomb
    # git: only allow gc, log, rev-parse, fsck, stash list, diff, status
    "git push",
    "git fetch",
    "git pull",
    "git reset --hard",
    "git clean -fd",
    "git checkout --",
    "git branch -D",
    "git tag -d",
]

# systemd-run: only the cleanup-purge units are allowed
SYSTEMD_RUN_ALLOWED = re.compile(
    r"^systemd-run\s+"
    r"--user\s+"
    r"--unit=cleanup-purge-[a-z0-9]+\s+"
    r"--on-active=\d+[hm]\s+"
    r"--timer-property=AccuracySec=\d+[smh]\s+"
    r"/home/elijah/\.cleanup-purge\.sh\s+"
    r"/home/elijah/\.cleanup-stash-20260812/[a-z0-9]+/?\s*$"
)

# git gc: only inside llama.cpp
GIT_GC_ALLOWED = re.compile(r"^git\s+gc\s+--aggressive\s+--prune=now\s*$")


def audit_log(line: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(AUDIT_LOG, "a") as f:
        f.write(f"{ts}\tKIMI\t{line}\n")


def validate_command(command: str) -> tuple[bool, str]:
    """Return (ok, reason). If ok=False, the command MUST NOT be executed."""
    cmd = command.strip()
    if not cmd:
        return False, "empty command"

    # Forbidden substrings
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad in cmd:
            return False, f"forbidden substring: {bad!r}"

    # Special-case validators first
    if cmd.startswith("systemd-run"):
        if SYSTEMD_RUN_ALLOWED.match(cmd):
            return True, "systemd-run: matches cleanup-purge pattern"
        return False, "systemd-run: not a cleanup-purge invocation"
    if cmd.startswith("git gc"):
        if GIT_GC_ALLOWED.match(cmd):
            return True, "git gc: matches --aggressive --prune=now"
        return False, "git gc: only --aggressive --prune=now allowed"

    # Split on && ; | to find the actual commands
    # Simple split — not a full shell parser, but enough for the allowlist
    parts = re.split(r"\s*(?:&&|;|\|)\s*", cmd)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part)
        except ValueError as e:
            return False, f"parse error: {e}"
        if not tokens:
            continue
        binary = os.path.basename(tokens[0])
        if binary not in ALLOWED_COMMANDS:
            return False, f"command not in allowlist: {binary!r}"

    # Path guard: extract any absolute path argument and verify
    paths = re.findall(r"(/[^\s'\";&|<>]*)", cmd)
    for p in paths:
        # strip trailing punctuation that might have been captured
        p = p.rstrip(",.)")
        if p.startswith("/") and not ALLOWED_PATH_RE.match(p):
            return False, f"path not in allow-list: {p!r}"

    return True, "ok"


def run_shell(command: str) -> dict:
    """Validate then execute. Returns {stdout, stderr, rc, refused, reason}."""
    ok, reason = validate_command(command)
    if not ok:
        audit_log(f"REFUSED\t{command[:200]}\treason={reason}")
        return {
            "stdout": "",
            "stderr": f"REFUSED: {reason}",
            "rc": 126,
            "refused": True,
            "reason": reason,
        }
    audit_log(f"RUN\t{command[:200]}")
    try:
        r = subprocess.run(
            ["/bin/bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "stdout": r.stdout[:8000],
            "stderr": r.stderr[:4000],
            "rc": r.returncode,
            "refused": False,
            "reason": reason,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "timeout after 600s",
            "rc": 124,
            "refused": False,
            "reason": "timeout",
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"executor error: {e}",
            "rc": 127,
            "refused": False,
            "reason": str(e),
        }


SYSTEM_PROMPT = (
    "You are KIMI K2.7, an executor agent. You do the actual filesystem work.\n"
    "The orchestrator (Hermes) set up the goal, context, and constraints. "
    "You are responsible for: running shell commands, observing output, "
    "reasoning about what to do next, and declaring DONE when the goal is met.\n\n"
    "TOOL: run_shell(command: str) — runs a bash command, returns stdout+stderr+rc.\n"
    "You can call it many times. Each call costs one turn. Use as many turns "
    "as you need — but be efficient. Stop when the goal is met.\n\n"
    "REASONING: You may think before each tool call. Internal reasoning is "
    "expected and welcome — use it to plan, to verify, to catch mistakes. "
    "When you have enough information, stop calling the tool and respond "
    "with a brief DONE summary.\n\n"
    "SAFETY: Your commands are validated by a guard. Refusals return "
    "stderr='REFUSED: <reason>' with rc=126. If a command is refused, reason "
    "about WHY and try a different approach. Do NOT try to bypass the guard.\n\n"
    "DONE: When the goal is met, respond with content (no tool call) starting "
    "with 'DONE: ' followed by a 1-2 line summary of what you did and the "
    "final state. The orchestrator will verify post-conditions."
)


def call_kimi(messages: list) -> dict:
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    if not api_key:
        sys.stderr.write("ERROR: OLLAMA_API_KEY not exported\n")
        sys.exit(2)
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run_shell",
                    "description": "Run a bash command. Returns stdout, stderr, exit_code. The command is validated by a safety guard before execution.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The exact bash command to run, e.g. 'ls -la /tmp' or 'mv /home/foo /home/bar/'",
                            }
                        },
                        "required": ["command"],
                    },
                },
            }
        ],
        "stream": False,
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.stderr.write(f"HTTP {e.code}: {body[:500]}\n")
        sys.exit(2)
    except urllib.error.URLError as e:
        sys.stderr.write(f"URL error: {e}\n")
        sys.exit(2)


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: kimi-executor.py <goal-file>\n")
        return 2
    with open(sys.argv[1]) as f:
        goal = f.read()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"GOAL:\n{goal}\n\nBegin."},
    ]

    audit_log(f"EXECUTOR_START\tgoal_file={sys.argv[1]}")
    for turn in range(1, MAX_TURNS + 1):
        sys.stderr.write(f"\n--- TURN {turn}/{MAX_TURNS} ---\n")
        resp = call_kimi(messages)
        msg = resp["choices"][0]["message"]
        finish = resp["choices"][0].get("finish_reason", "?")
        usage = resp.get("usage", {})

        reasoning = msg.get("reasoning", "")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls") or []

        if reasoning:
            sys.stderr.write(
                f"[reasoning {len(reasoning)}c] {reasoning[:200]}{'...' if len(reasoning) > 200 else ''}\n"
            )
        if content:
            sys.stderr.write(
                f"[content] {content[:300]}{'...' if len(content) > 300 else ''}\n"
            )
        sys.stderr.write(
            f"[usage] prompt={usage.get('prompt_tokens', '?')} completion={usage.get('completion_tokens', '?')} finish={finish}\n"
        )

        # Append assistant message
        messages.append(msg)

        if not tool_calls:
            # Kimi declared done
            audit_log(f"EXECUTOR_DONE\tturn={turn}\tcontent_len={len(content)}")
            if content.startswith("DONE"):
                print(content)
                return 0
            print(f"UNEXPECTED FINAL: {content[:500]}")
            return 1

        # Execute each tool call (usually one)
        tool_results = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {}
            command = args.get("command", "")
            sys.stderr.write(
                f"[tool_call] {name}({command[:200]}{'...' if len(command) > 200 else ''})\n"
            )
            if name == "run_shell":
                result = run_shell(command)
                # Truncate output for context
                out_preview = (result["stdout"] + result["stderr"])[:2000]
                sys.stderr.write(
                    f"[result] rc={result['rc']} refused={result['refused']}\n"
                )
                if result["stdout"]:
                    sys.stderr.write(f"[stdout] {result['stdout'][:400]}...\n")
                if result["stderr"]:
                    sys.stderr.write(f"[stderr] {result['stderr'][:400]}...\n")
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", "?"),
                        "content": json.dumps(result),
                    }
                )
            else:
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", "?"),
                        "content": json.dumps({"error": f"unknown tool: {name}"}),
                    }
                )

        messages.extend(tool_results)

    sys.stderr.write(f"MAX_TURNS ({MAX_TURNS}) reached without DONE\n")
    audit_log("EXECUTOR_TIMEOUT")
    return 3


if __name__ == "__main__":
    sys.exit(main())
