"""Headless task execution mode for master-ai-cli.

This module provides a non-interactive mode for master-ai-cli that can be
invoked from another AI system or script, similar to Claude Code CLI's
print mode (`claude -p`). It parses tool directives from the model and
executes them in a bounded loop.

Example:
    python3 master_ai.py --task "List the files in this repo" --headless
    python3 master_ai.py --task-file /tmp/task.md --headless --json

It intentionally does NOT import the interactive UI, banner, permission
wizard, or setup wizard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import subagent_registry
import typed_actions

HEADLESS_DEFAULT_MAX_TURNS = 10


HEADLESS_SYSTEM_PROMPT = (
    "You are Master AI running headless: complete the user's task by emitting "
    "tool directives, one per line, and nothing else. Available directives:\n"
    "  READ: <path>            Read a file; its content is returned to you.\n"
    "  RUN: <shell command>    Run a shell command. Use this to create or modify "
    "files (e.g. RUN: printf 'hello' > /tmp/out.txt) or to inspect the system.\n"
    "Rules: one directive per line, no prose, no markdown fences. "
    "Keep commands non-interactive and safe (never rm -rf, sudo, mkfs, dd). "
    "After each tool result, emit the next directive; when the task is complete, "
    "reply with a brief summary and no directives."
)


def _load_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _read_file(target: str) -> str:
    try:
        return f"Content of {target}:\n{_load_text(target)}"
    except Exception as e:
        return f"Error reading {target}: {e}"


def _create_file(target: str, content: str) -> str:
    try:
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Created {target}"
    except Exception as e:
        return f"Error creating {target}: {e}"


def _edit_file(target: str, old: str, new: str) -> str:
    try:
        p = Path(target)
        text = p.read_text(encoding="utf-8")
        if old not in text:
            return f"Edit skipped: old text not found in {target}"
        p.write_text(text.replace(old, new), encoding="utf-8")
        return f"Edited {target}"
    except Exception as e:
        return f"Error editing {target}: {e}"


def _run_shell(command: str, *, allow_destructive: bool = False) -> str:
    # Very basic safety gate; in a real deployment this should reuse
    # master_ai's confirm_run / approval_queue path.
    # NOTE: dangerous tokens match on word boundaries, not substrings --
    # a plain substring check false-positives on innocent paths
    # (e.g. "rm" inside "/tmp/norm_probe.txt").
    lowered = command.lower()
    if not allow_destructive:
        if ":(){" in lowered.replace(" ", ""):
            return f"Blocked command (dangerous): {command}"
        words = set(re.findall(r"[a-z0-9_]+", lowered))
        if words & {"rm", "sudo", "mkfs", "dd", "format"}:
            return f"Blocked command (dangerous): {command}"
    try:
        result = os.popen(command).read()
        return f"Output of `{command}`:\n{result}"
    except Exception as e:
        return f"Error running `{command}`: {e}"


def _normalize_action(action: Any) -> dict[str, Any]:
    """Normalize a parsed action to the legacy dict shape.

    typed_actions.parse_reply() returns TypedAction dataclass instances
    (fields kind/target/create_content/edit_old/edit_new, kind uppercased)
    while the legacy fallback parser returns plain dicts
    (type/target/content/old/new/command, type lowercased). Without
    normalization _execute_action crashes with AttributeError on action.get().
    Accepts both shapes.
    """
    if isinstance(action, dict):
        return action
    to_dict = getattr(action, "to_dict", None)
    if callable(to_dict):
        d = to_dict()
    else:
        d = dict(getattr(action, "__dict__", {}) or {})
    if not isinstance(d, dict):
        return {"type": "unknown"}
    kind = str(d.get("kind", d.get("type", ""))).upper()
    target = d.get("target", "")
    return {
        "type": kind.lower(),
        "target": target,
        "content": d.get("content", d.get("create_content", "")),
        "old": d.get("old", d.get("edit_old", d.get("find", ""))),
        "new": d.get("new", d.get("edit_new", d.get("replace", ""))),
        "command": d.get("command", "")
        or (target if kind in ("RUN", "RUNTERM") else ""),
        "name": d.get("name", "") or (target if kind == "SUBAGENT" else ""),
        "task": d.get("task", ""),
    }


def _execute_action(action: Any) -> str:
    d = _normalize_action(action)
    kind = d.get("type")
    if kind == "read":
        return _read_file(d.get("target", ""))
    if kind == "create":
        return _create_file(d.get("target", ""), d.get("content", ""))
    if kind == "edit":
        return _edit_file(
            d.get("target", ""),
            d.get("old", ""),
            d.get("new", ""),
        )
    if kind in ("run", "runterm"):
        return _run_shell(d.get("command", ""))
    if kind == "subagent":
        name = d.get("name", "")
        task = d.get("task", "")
        try:
            result = subagent_registry.run(name, task)
            return f"Subagent `{name}` result: {json.dumps(result)}"
        except Exception as e:
            return f"Subagent `{name}` error: {e}"
    return f"Unknown action type: {kind}"


class HeadlessRunner:
    """Runs a task non-interactively with a bounded tool loop."""

    def __init__(
        self,
        task: str | None = None,
        task_file: str | None = None,
        max_turns: int = HEADLESS_DEFAULT_MAX_TURNS,
        json_output: bool = False,
        model: str | None = None,
    ):
        self.task = task
        self.task_file = task_file
        self.max_turns = max(max_turns, 1)
        self.json_output = json_output
        self.model = model
        self.history: list[dict[str, str]] = []
        self.output: list[str] = []

    def _load_task(self) -> str:
        if self.task:
            return self.task
        if self.task_file:
            return _load_text(self.task_file)
        raise ValueError("No task provided")

    def _model_reply(self, history: list[dict[str, str]]) -> str:
        """Real model call via master_ai.ask_model_router.

        ask_model_router() is the same provider-agnostic router the TUI uses:
        it picks local Ollama or cloud models (OpenRouter, OpenCode, NVIDIA,
        etc.) based on the active/pinned model. This makes the headless runner
        behave like Sensei rather than always using the default local model.
        """
        import master_ai

        messages = [{"role": h["role"], "content": h["content"]} for h in history]
        text, _elapsed = master_ai.ask_model_router(messages, model=self.model)
        return text or ""

    def _parse_actions(self, reply: str) -> list[dict[str, Any]]:
        try:
            return typed_actions.parse_reply(reply)
        except Exception:
            return self._fallback_parse(reply)

    @staticmethod
    def _fallback_parse(reply: str) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for line in reply.splitlines():
            line = line.strip()
            if line.startswith("READ:"):
                actions.append({"type": "read", "target": line[5:].strip()})
            elif line.startswith("CREATE:"):
                actions.append({"type": "create", "target": line[7:].strip()})
            elif line.startswith("EDIT:"):
                actions.append({"type": "edit", "target": line[5:].strip()})
            elif line.startswith("RUN:"):
                actions.append({"type": "run", "command": line[4:].strip()})
            elif line.startswith("RUNTERM:"):
                actions.append({"type": "runterm", "command": line[8:].strip()})
            elif line.startswith("SUBAGENT:"):
                actions.append({"type": "subagent", "name": line[9:].strip()})
        return actions

    def run(self) -> str:
        task = self._load_task()
        # The model cannot emit parseable directives unless it is taught the
        # format: without this system prompt it emits markdown prose, zero
        # actions parse, and the loop silently no-ops.
        self.history.append({"role": "system", "content": HEADLESS_SYSTEM_PROMPT})
        self.history.append({"role": "user", "content": task})

        for _ in range(self.max_turns):
            reply = self._model_reply(self.history)
            self.history.append({"role": "assistant", "content": reply})
            self.output.append(reply)

            actions = self._parse_actions(reply)
            if not actions:
                break

            for action in actions:
                result = _execute_action(action)
                self.output.append(result)
                self.history.append({"role": "user", "content": result})

        final = "\n".join(self.output)
        if self.json_output:
            return json.dumps(
                {"status": "success", "result": final, "turns": len(self.history)},
                indent=2,
            )
        return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="master-ai", description="Master AI CLI")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--uninstall", action="store_true", help="Run uninstall wizard")
    parser.add_argument("--task", "-t", type=str, help="Task to run in headless mode")
    parser.add_argument("--task-file", type=str, help="File containing the task")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default=None,
        help="Model override for this headless task (passed to ask_model_router)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=HEADLESS_DEFAULT_MAX_TURNS,
        help="Maximum tool turns in headless mode",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON in headless mode"
    )
    args = parser.parse_args(argv)

    if not args.headless:
        print("Headless mode not enabled. Use --headless with --task or --task-file.")
        return 0

    if not args.task and not args.task_file:
        print("Error: --task or --task-file is required in headless mode.")
        return 1

    runner = HeadlessRunner(
        task=args.task,
        task_file=args.task_file,
        max_turns=args.max_turns,
        json_output=args.json,
        model=args.model,
    )
    print(runner.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
