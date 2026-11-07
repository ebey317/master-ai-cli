"""Frontier coding benchmark runner for master-ai-cli.

Usage:
    python3 eval/frontier/run.py --model nvidia/nemotron-3-ultra-550b-a55b:free \
        --tasks all --timeout 600 --k 1

The runner copies each task's repo/ into a temp directory, hands the task prompt
to a model, and lets the model edit the repo through the existing headless
action format (READ / CREATE / EDIT / RUN). After the turn budget is exhausted,
it runs the hidden verify/ pytest suite against the modified repo and records
pass/fail, turns, wall seconds, and the verifier tail.

Only API keys are read from the host; no live Sensei state is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# Make repo-root imports work when the script is invoked as
# `python3 eval/frontier/run.py`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the existing headless action parser/system prompt without importing
# the rest of the interactive runtime. The runner uses the full body-aware
# parser (parse_reply_with_bodies) so CREATE/EDIT directives actually carry
# content; headless_runner's own _parse_actions uses parse_reply and only
# extracts single-line directives.
import headless_runner
import typed_actions

FRONTIER_DIR = Path(__file__).resolve().parent
RESULTS_DIR = FRONTIER_DIR / "results"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

FRONTIER_SYSTEM_PROMPT = (
    "You are Master AI running headless in a benchmark: complete the user's coding task by emitting "
    "tool directives, one per line, and nothing else. Available directives:\n"
    "  READ: <path>                          Read a file; its content is returned to you.\n"
    "  CREATE: <path>                        Create/replace a file. Put its body on the following lines:\n"
    "    <<<CONTENT\n"
    "    <file body>\n"
    "    >>>CONTENT\n"
    "  EDIT: <path>                          Patch a file. Next two blocks are find→replace text:\n"
    "    <<<FIND\n"
    "    <exact old text>\n"
    "    >>>FIND\n"
    "    <<<REPLACE\n"
    "    <replacement text>\n"
    "    >>>REPLACE\n"
    "  RUN: <shell command>                  Run a shell command (pytest, python, grep, etc.).\n"
    "Rules: one directive per line, no prose, no markdown fences. "
    "Keep commands non-interactive and safe. "
    "After each tool result, emit the next directive; when the task is complete, "
    "reply with a brief summary and no directives."
)


def _load_keychain_openrouter_key() -> str | None:
    """Read OpenRouter key from the same keychain master-ai-cli uses."""
    try:
        import keychain_kv

        text = Path.home().joinpath(".master_ai_keys").read_text()
        keys = keychain_kv.parse_kv_keys(text)
        return keys.get("openrouter")
    except Exception:
        return os.environ.get("OPENROUTER_API_KEY") or None


def _openrouter_chat(
    model: str, messages: list[dict[str, str]], api_key: str, timeout: float = 120.0
) -> str:
    """Call OpenRouter chat completions and return the assistant content."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {result}")
    return str(choices[0].get("message", {}).get("content", ""))


def _execute_action_in_repo(
    action: Any, repo: Path, allow_destructive: bool = True
) -> str:
    """Run one parsed action against files inside `repo`."""
    d = headless_runner._normalize_action(action)
    kind = d.get("type")
    if kind == "read":
        target = repo / d.get("target", "").lstrip("/")
        try:
            return f"Content of {target}:\n{target.read_text(encoding='utf-8')}"
        except Exception as e:
            return f"Error reading {target}: {e}"

    if kind == "create":
        target = repo / d.get("target", "").lstrip("/")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(d.get("content", ""), encoding="utf-8")
            return f"Created {target}"
        except Exception as e:
            return f"Error creating {target}: {e}"

    if kind == "edit":
        target = repo / d.get("target", "").lstrip("/")
        try:
            text = target.read_text(encoding="utf-8")
            old = d.get("old", "")
            if old not in text:
                return f"Edit skipped: old text not found in {target}"
            target.write_text(text.replace(old, d.get("new", "")), encoding="utf-8")
            return f"Edited {target}"
        except Exception as e:
            return f"Error editing {target}: {e}"

    if kind in ("run", "runterm"):
        command = d.get("command", "")
        if not command:
            return "Error: empty command"
        if not allow_destructive:
            lowered = command.lower()
            if ":(){" in lowered.replace(" ", ""):
                return f"Blocked command (dangerous): {command}"
            words = set(re.findall(r"[a-z0-9_]+", lowered))
            if words & {"rm", "sudo", "mkfs", "dd", "format"}:
                return f"Blocked command (dangerous): {command}"
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = result.stdout or ""
            if result.stderr:
                out += f"\n[stderr]\n{result.stderr}"
            return f"Output of `{command}` (exit {result.returncode}):\n{out}"
        except subprocess.TimeoutExpired:
            return f"Error running `{command}`: timeout after 300s"
        except Exception as e:
            return f"Error running `{command}`: {e}"

    if kind == "subagent":
        return "Error: SUBAGENT is not available in the frontier harness"

    return f"Unknown action type: {kind}"


def _run_verify(repo: Path, verify: Path, timeout: float = 120.0) -> tuple[int, str]:
    """Run the hidden verify tests against `repo`. Returns (exit_code, tail)."""
    # Copy verify into the same temp tree so pytest can resolve imports via
    # the sys.path insertion in the test file (parents[1]/repo).
    tmp_root = repo.parent
    local_verify = tmp_root / "verify"
    shutil.copytree(verify, local_verify, dirs_exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(local_verify)],
        cwd=str(tmp_root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    tail = (result.stdout + result.stderr).strip()
    lines = tail.splitlines()
    return result.returncode, "\n".join(lines[-30:])


def _discover_tasks(tasks_arg: str) -> list[str]:
    """Return sorted task ids from 'all' or a comma-separated list."""
    if tasks_arg == "all":
        return sorted(
            p.name
            for p in FRONTIER_DIR.iterdir()
            if p.is_dir() and (p / "repo").exists() and (p / "PROMPT.md").exists()
        )
    return [t.strip() for t in tasks_arg.split(",") if t.strip()]


def run_task(
    task_id: str,
    model: str,
    api_key: str,
    max_turns: int = 10,
    timeout: float = 600.0,
    k: int = 1,
) -> dict[str, Any]:
    """Run one task up to `k` attempts. Return result dict."""
    task_dir = FRONTIER_DIR / task_id
    repo_src = task_dir / "repo"
    verify_src = task_dir / "verify"
    prompt = (task_dir / "PROMPT.md").read_text(encoding="utf-8")

    attempts: list[dict[str, Any]] = []
    overall_start = time.monotonic()

    for attempt in range(k):
        tmp = Path(f"/tmp/frontier_{task_id}_{attempt}_{int(time.time())}")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(repo_src, tmp / "repo")
        repo = tmp / "repo"

        history: list[dict[str, str]] = [
            {"role": "system", "content": FRONTIER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        turns_used = 0
        actions_executed = 0
        action_errors = 0

        turn_start = time.monotonic()
        for turn in range(max_turns):
            turns_used = turn + 1
            if time.monotonic() - turn_start > timeout:
                break
            try:
                reply = _openrouter_chat(model, history, api_key, timeout=120.0)
            except Exception as e:
                attempts.append(
                    {
                        "attempt": attempt + 1,
                        "turns": turns_used,
                        "status": "error",
                        "error": f"model call failed: {e}",
                        "seconds": round(time.monotonic() - overall_start, 2),
                    }
                )
                break

            history.append({"role": "assistant", "content": reply})
            actions = typed_actions.parse_reply_with_bodies(reply)
            if not actions:
                break
            for action in actions:
                result = _execute_action_in_repo(action, repo)
                actions_executed += 1
                if "Error" in result or "skipped" in result.lower():
                    action_errors += 1
                history.append({"role": "user", "content": result})
        else:
            turns_used = max_turns

        exit_code, tail = _run_verify(repo, verify_src)
        attempts.append(
            {
                "attempt": attempt + 1,
                "turns": turns_used,
                "actions_executed": actions_executed,
                "action_errors": action_errors,
                "status": "pass" if exit_code == 0 else "fail",
                "verifier_exit_code": exit_code,
                "verifier_tail": tail,
                "seconds": round(time.monotonic() - overall_start, 2),
            }
        )
        if exit_code == 0:
            break

    passed = any(a["status"] == "pass" for a in attempts)
    return {
        "task": task_id,
        "model": model,
        "passed": passed,
        "attempts": attempts,
        "total_seconds": round(time.monotonic() - overall_start, 2),
    }


def _print_table(results: list[dict[str, Any]]) -> None:
    """Print a simple ASCII table of results."""
    header = f"{'task':<22} {'model':<45} {'status':<8} {'turns':>6} {'seconds':>8}"
    print(header)
    print("-" * len(header))
    for r in results:
        attempts = r["attempts"]
        last = attempts[-1] if attempts else {}
        print(
            f"{r['task']:<22} {r['model']:<45} "
            f"{'PASS' if r['passed'] else 'FAIL':<8} {last.get('turns', 0):>6} "
            f"{r['total_seconds']:>8.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frontier coding benchmark runner")
    parser.add_argument(
        "--model",
        required=True,
        help="OpenRouter model pin (e.g. nvidia/nemotron-3-ultra-550b-a55b:free)",
    )
    parser.add_argument(
        "--tasks", default="all", help="Comma-separated task ids or 'all'"
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="Per-task wall timeout in seconds"
    )
    parser.add_argument(
        "--k", type=int, default=1, help="Attempts per task (stops on first pass)"
    )
    parser.add_argument(
        "--max-turns", type=int, default=10, help="Max tool turns per attempt"
    )
    parser.add_argument(
        "--retries", type=int, default=2, help="Model-call retries on error"
    )
    args = parser.parse_args(argv)

    api_key = _load_keychain_openrouter_key()
    if not api_key:
        print(
            "No OpenRouter key found in ~/.master_ai_keys or OPENROUTER_API_KEY",
            file=sys.stderr,
        )
        return 1

    task_ids = _discover_tasks(args.tasks)
    if not task_ids:
        print(f"No tasks matched: {args.tasks}", file=sys.stderr)
        return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"{timestamp}.json"

    all_results: list[dict[str, Any]] = []
    for task_id in task_ids:
        print(f"\nRunning {task_id} with {args.model} ...")
        result = run_task(
            task_id,
            args.model,
            api_key,
            max_turns=args.max_turns,
            timeout=args.timeout,
            k=args.k,
        )
        all_results.append(result)

    summary = {
        "model": args.model,
        "tasks": args.tasks,
        "timestamp": timestamp,
        "results": all_results,
        "pass_rate": sum(1 for r in all_results if r["passed"]) / len(all_results),
    }
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nResults written to {out_path}")
    _print_table(all_results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
