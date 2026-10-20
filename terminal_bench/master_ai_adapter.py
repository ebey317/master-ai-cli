"""
Master AI Terminal-Bench adapter — Option A: drives the live /chat HTTP server.

Install:
  pip install -e ~/master-ai-cli          # once
  tb run --agent terminal_bench.master_ai_adapter:MasterAIAgent --model <ignored>

Contract:
  * Implements terminal_bench.agents.base_agent.BaseAgent
  * perform_task() POSTs the TB instruction to the host's Master AI /chat
    (stt_server.py on :8080, same path eval/ev2_driver validated at 96%)
  * For each turn, the adapter executes the returned typed actions INSIDE the
    TB Docker container via TmuxSession, then POSTs /chat/continue with results
    until done/budget.

Network: harness runs on host, so http://127.0.0.1:8080 is reachable. If you
run tb inside Docker-in-Docker, set MASTER_AI_HOST=http://host.docker.internal:8080.

Requires the HEADLESS_SYSTEM_PROMPT fix (all 6 directives) on the host.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.terminal.tmux_session import TmuxSession

HOST = os.environ.get("MASTER_AI_HOST", "http://127.0.0.1:8080")
TOKEN_PATH = Path.home() / ".master_ai_extension_token"
_TIMEOUT = 420


def _token() -> str:
    if TOKEN_PATH.is_file():
        return TOKEN_PATH.read_text().strip()
    return ""


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        HOST + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Master-AI-Token": _token(),
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


class MasterAIAgent(BaseAgent):
    """TB adapter that delegates to a live Master AI instance."""

    @staticmethod
    def name() -> str:
        return "master-ai"

    def __init__(self, model_name: str = "master-ai", max_episodes: int = 50, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model_name
        self._max_episodes = max_episodes

    # ---- session helpers ----

    def _exec_in_container(
        self, session: TmuxSession, cmd: str, timeout: float = 30.0
    ) -> str:
        """Run a shell command inside the TB container via TmuxSession."""
        # Escape for tmux send - use the session's exec_run if available, else send_keys trick
        # TmuxSession in TB wraps docker exec; we use its public API
        try:
            # Try the container direct exec (fast, non-interactive)
            result = session.container.exec_run(
                ["bash", "-lc", cmd],
                workdir="/tmp",
            )
            out = (
                result.output.decode(errors="ignore")
                if hasattr(result.output, "decode")
                else str(result.output)
            )
            return out.strip()
        except Exception:
            pass
        # Fallback: tmux send
        session.send_keys(cmd, block=True, timeout_sec=timeout)
        # Capture last pane output via session helper if available
        try:
            return session.capture_pane()  # type: ignore
        except Exception:
            return ""

    def _handle_create(self, session: TmuxSession, path: str, content: str) -> str:
        # Use printf to avoid shell quoting hell for small bodies; for large, use heredoc
        escaped = content.replace("'", "'\"'\"'")
        return self._exec_in_container(
            session,
            f"mkdir -p $(dirname '{path}') && printf '%s' '{escaped}' > '{path}' && echo ok",
        )

    def _handle_read(self, session: TmuxSession, path: str) -> str:
        return self._exec_in_container(session, f"cat '{path}' 2>&1 | head -c 20000")

    def _handle_edit(self, session: TmuxSession, path: str, old: str, new: str) -> str:
        # Use python to do exact replace to avoid sed escaping
        py = (
            "import pathlib, sys;"
            f"p=pathlib.Path('{path}');"
            "t=p.read_text() if p.exists() else '';"
            f"o={repr(old)}; n={repr(new)};"
            "p.write_text(t.replace(o,n)) if o in t else sys.exit(2)"
        )
        out = self._exec_in_container(
            session, f'python3 -c "{py}" && echo ok || echo miss'
        )
        return out

    # ---- main entry ----

    def perform_task(
        self, instruction: str, session: TmuxSession, logging_dir: Path | None = None
    ) -> AgentResult:
        session_id = f"tbench-{int(time.time())}"
        # The instruction TB gives already contains the workdir context (often /tmp or /workspace)
        # We forward it as-is; Master AI's prompt builder handles it
        try:
            payload = _post(
                "/chat",
                {
                    "prompt": instruction,
                    "mode": "auto",
                    "source": "terminal_bench",
                    "session_id": session_id,
                    "round_budget": self._max_episodes,
                },
            )
        except (urllib.error.URLError, OSError) as e:
            # Host not reachable — fail fast with marker TB dashboard understands
            return AgentResult(
                failure_mode="API_ERROR",
                timestamped_markers=[(0, f"host unreachable {HOST}: {e}")],
            )

        turns = 1
        markers: list[tuple[float, str]] = []

        while True:
            actions = payload.get("actions") or []
            done = bool(payload.get("done"))
            reply = payload.get("reply") or ""

            if logging_dir is not None:
                (logging_dir / f"turn-{turns}.json").write_text(
                    json.dumps(payload, indent=2)
                )

            # If Master AI is done and sent no actions, we're finished
            if done and not actions:
                markers.append((time.time(), reply[:500]))
                break

            # Execute each typed action inside the TB container
            results: list[dict[str, Any]] = []
            for act in actions:
                kind = (act.get("kind") or act.get("type") or "").upper()
                target = act.get("target") or ""
                out = ""
                if kind == "READ":
                    out = self._handle_read(session, target)
                elif kind == "CREATE":
                    content = act.get("create_content") or act.get("content") or ""
                    out = self._handle_create(session, target, content)
                elif kind == "EDIT":
                    old = act.get("edit_old") or act.get("old") or ""
                    new = act.get("edit_new") or act.get("new") or ""
                    out = self._handle_edit(session, target, old, new)
                elif kind in ("RUN", "RUNTERM"):
                    cmd = act.get("command") or target
                    out = self._exec_in_container(session, cmd)
                else:
                    out = f"skipped kind={kind}"

                results.append(
                    {
                        "action_id": act.get("id"),
                        "kind": kind,
                        "target": target,
                        "verdict": "accept",
                        "result": "success",
                        "output": out[:4000],
                        "final_state": {"ok": True},
                    }
                )
                markers.append((time.time(), f"{kind} {target} -> {out[:200]}"))

            # If no more continuations allowed or nothing was dispatched, stop
            if (
                payload.get("round_remaining") is not None
                and payload["round_remaining"] <= 0
            ):
                break
            if not actions and not done:
                break
            if done:
                break

            # Ask Master AI for next turn with execution results
            try:
                payload = _post(
                    "/chat/continue",
                    {
                        "parent_turn_id": payload.get("turn_id"),
                        "source": "terminal_bench",
                        "mode": "auto",
                        "session_id": session_id,
                        "action_results": results,
                    },
                )
            except Exception as e:
                return AgentResult(
                    failure_mode="API_ERROR",
                    timestamped_markers=[(time.time(), f"continue failed: {e}")],
                )

            turns += 1
            if turns > self._max_episodes:
                break

        return AgentResult(timestamped_markers=markers)
