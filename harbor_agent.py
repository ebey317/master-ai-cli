"""Harbor BaseAgent adapter for Master AI CLI.

Lets `harbor run -a harbor_agent:MasterAICliAgent -t <task>` evaluate
Master AI CLI on Terminal-Bench the same way Harbor evaluates Claude Code,
OpenCode, etc.

Design choice — BaseAgent (external), not BaseInstalledAgent: Harbor's docs
(docs.harborframework.com/agents/custom-agents) distinguish an "installed"
agent (CLI pip-installed and run *inside* the task's container) from an
"external" agent (the agent loop stays outside the container and drives it
through BaseEnvironment). Master AI CLI assumes a specific host — DISPLAY,
tmux, the Sensei browser bridge, TTS/STT servers, ~/.master_ai_* files —
none of which exist inside a disposable Terminal-Bench container, and none
of which are needed for a terminal/coding task anyway. So the agent loop
(model calls + action parsing) runs here, on the host, exactly like it
already does in headless_runner.HeadlessRunner, and only the actions
themselves (RUN/RUNTERM/READ/CREATE/EDIT) get redirected into the task
container via environment.exec/upload_file/download_file.

This reuses headless_runner.py's existing model-call and action-parsing
logic unchanged (HeadlessRunner._model_reply / ._parse_actions and the
module-level _normalize_action) — only *where actions execute* changes.
headless_runner's own RUN/READ/CREATE/EDIT handlers stay as they are, for
its original use case (a real task on Elijah's actual machine).

Deliberately does NOT reapply headless_runner._run_shell's rm/sudo/mkfs/dd
word-blocklist here: that blocklist exists to protect Elijah's real
machine, and inside Harbor's sandboxed, disposable container it would only
make Master AI CLI fail legitimate Terminal-Bench tasks (deleting a build
directory, running a service as a designated non-root user, etc.) that
the sandbox itself already makes safe to run. The container IS the safety
boundary here, not Master AI CLI's own gates.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

import headless_runner
import terminalbench_logger as tb_logger

DEFAULT_EXEC_TIMEOUT_SEC = 120


class MasterAICliAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "master-ai-cli"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        # Agent loop runs on the host (see module docstring) — nothing to
        # install inside the task container.
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        trial_start = time.monotonic()
        history: list[dict[str, str]] = [
            {"role": "system", "content": headless_runner.HEADLESS_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]

        runner = headless_runner.HeadlessRunner(model=self.model_name)
        max_turns = headless_runner.HEADLESS_DEFAULT_MAX_TURNS

        turns_used = 0
        actions_executed = 0
        actions_failed = 0
        est_input_tokens = 0
        est_output_tokens = 0

        for turn in range(max_turns):
            turns_used = turn + 1

            est_input_tokens += tb_logger.estimate_tokens(
                "\n".join(m["content"] for m in history)
            )
            reply = runner._model_reply(history)
            est_output_tokens += tb_logger.estimate_tokens(reply)

            history.append({"role": "assistant", "content": reply})

            actions = runner._parse_actions(reply)
            if not actions:
                break

            for action in actions:
                result, failed = await self._execute_in_environment(
                    action, environment
                )
                actions_executed += 1
                if failed:
                    actions_failed += 1
                history.append({"role": "user", "content": result})

        elapsed = time.monotonic() - trial_start

        context.n_input_tokens = est_input_tokens
        context.n_output_tokens = est_output_tokens
        context.metadata = {
            "turns_used": turns_used,
            "actions_executed": actions_executed,
            "actions_failed": actions_failed,
            "elapsed_sec": round(elapsed, 2),
            "token_estimation": (
                "~4 chars/token approximation — ask_model_router() does "
                "not return provider usage; see terminalbench_logger.py"
            ),
        }

        tb_logger.record_trial(
            task_name=context.metadata.get("task_name") or instruction[:80],
            model=self.model_name,
            turns_used=turns_used,
            actions_executed=actions_executed,
            actions_failed=actions_failed,
            elapsed_sec=elapsed,
            est_input_tokens=est_input_tokens,
            est_output_tokens=est_output_tokens,
            trial_id=str(self.context_id) if self.context_id else None,
        )

    async def _execute_in_environment(
        self, action: Any, environment: BaseEnvironment
    ) -> tuple[str, bool]:
        """Run one parsed action against the task container. Returns
        (result_text_fed_back_to_history, failed)."""
        d = headless_runner._normalize_action(action)
        kind = d.get("type")

        if kind in ("run", "runterm"):
            command = d.get("command", "")
            if not command:
                return "Error: empty command", True
            result = await environment.exec(command, timeout_sec=DEFAULT_EXEC_TIMEOUT_SEC)
            out = result.stdout or ""
            if result.stderr:
                out += f"\n[stderr]\n{result.stderr}"
            text = f"Output of `{command}` (exit {result.return_code}):\n{out}"
            return text, result.return_code != 0

        if kind == "read":
            target = d.get("target", "")
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "read_target"
                try:
                    await environment.download_file(target, local)
                    content = local.read_text(errors="replace")
                    return f"Content of {target}:\n{content}", False
                except Exception as e:
                    return f"Error reading {target}: {e}", True

        if kind == "create":
            target = d.get("target", "")
            content = d.get("content", "")
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "create_source"
                local.write_text(content, encoding="utf-8")
                try:
                    await environment.upload_file(local, target)
                    return f"Created {target}", False
                except Exception as e:
                    return f"Error creating {target}: {e}", True

        if kind == "edit":
            target = d.get("target", "")
            old = d.get("old", "")
            new = d.get("new", "")
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "edit_target"
                try:
                    await environment.download_file(target, local)
                    text = local.read_text(errors="replace")
                    if old not in text:
                        return f"Edit skipped: old text not found in {target}", True
                    local.write_text(text.replace(old, new), encoding="utf-8")
                    await environment.upload_file(local, target)
                    return f"Edited {target}", False
                except Exception as e:
                    return f"Error editing {target}: {e}", True

        if kind == "subagent":
            # Master AI CLI's subagent_registry is a host-side feature
            # (filesystem scans, local test runners) with no meaning
            # inside a Terminal-Bench container — refuse rather than
            # silently no-op.
            return f"subagent `{d.get('name', '')}` not available in Harbor trials", True

        return f"Unknown action type: {kind}", True
