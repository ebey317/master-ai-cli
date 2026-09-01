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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typed_actions
import subagent_registry


HEADLESS_DEFAULT_MAX_TURNS = 10


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
    dangerous = {"rm", "sudo", "mkfs", "dd", "format", ":(){"}
    lowered = command.lower()
    if not allow_destructive and any(d in lowered for d in dangerous):
        return f"Blocked command (dangerous): {command}"
    try:
        result = os.popen(command).read()
        return f"Output of `{command}`:\n{result}"
    except Exception as e:
        return f"Error running `{command}`: {e}"


def _execute_action(action: Dict[str, Any]) -> str:
    kind = action.get("type")
    if kind == "read":
        return _read_file(action.get("target", ""))
    if kind == "create":
        return _create_file(action.get("target", ""), action.get("content", ""))
    if kind == "edit":
        return _edit_file(
            action.get("target", ""),
            action.get("old", action.get("find", "")),
            action.get("new", action.get("replace", "")),
        )
    if kind in ("run", "runterm"):
        return _run_shell(action.get("command", ""))
    if kind == "subagent":
        name = action.get("name", "")
        task = action.get("task", "")
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
        task: Optional[str] = None,
        task_file: Optional[str] = None,
        max_turns: int = HEADLESS_DEFAULT_MAX_TURNS,
        json_output: bool = False,
    ):
        self.task = task
        self.task_file = task_file
        self.max_turns = max(max_turns, 1)
        self.json_output = json_output
        self.history: List[Dict[str, str]] = []
        self.output: List[str] = []

    def _load_task(self) -> str:
        if self.task:
            return self.task
        if self.task_file:
            return _load_text(self.task_file)
        raise ValueError("No task provided")

    @staticmethod
    def _model_reply(history: List[Dict[str, str]]) -> str:
        """Real model call via master_ai.ask_local.

        2026-09-01: this used to be a placeholder that never called any
        model. ask_local() is the same local/cloud-routing layer the rest
        of master_ai.py (and CLAF escalation) is built on -- the lower
        level api_handle() in stt_server.py itself ultimately calls into.
        Deliberately NOT routing through api_handle() directly: that
        function is heavily specialized for the Chrome-extension browser-
        action *proposal* contract (every action tagged executed=False by
        design, classified by page_url/sensitivity tier) and pulls in a
        lot of incidental machinery (capabilities/verifiers/prompt_versions,
        module-global patching) just to extract a text reply -- real
        coupling risk for a general RUN/READ/CREATE/EDIT headless loop.
        ask_local() is the minimal, correct integration point.

        Local import (not module-level) matches this file's own stated
        design goal of not pulling in interactive-UI state at import time
        -- master_ai only gets imported once a task actually needs a
        model turn.
        """
        import master_ai
        messages = [{"role": h["role"], "content": h["content"]} for h in history]
        return master_ai.ask_local(messages) or ""

    def _parse_actions(self, reply: str) -> List[Dict[str, Any]]:
        try:
            return typed_actions.parse_reply(reply)
        except Exception:
            return self._fallback_parse(reply)

    @staticmethod
    def _fallback_parse(reply: str) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="master-ai", description="Master AI CLI")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--uninstall", action="store_true", help="Run uninstall wizard")
    parser.add_argument("--task", "-t", type=str, help="Task to run in headless mode")
    parser.add_argument("--task-file", type=str, help="File containing the task")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument(
        "--max-turns", type=int, default=HEADLESS_DEFAULT_MAX_TURNS,
        help="Maximum tool turns in headless mode",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON in headless mode")
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
    )
    print(runner.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
