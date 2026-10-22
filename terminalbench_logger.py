"""Terminal-Bench / Harbor evaluation logger for Master AI CLI.

Master AI CLI has no existing token/cost accounting (ask_model_router()
returns only (text, elapsed_seconds) — see master_ai.py:7276) and
observability.py's summarize() tracks routes/models/blocks, not tokens or
$ cost. This module is new instrumentation, not a rewire of something that
already tracked this.

Two separate jobs, because Harbor computes pass/fail (reward) in a
verifier step that runs *after* our agent's run() returns — our own code
never sees it:

  1. record_trial() — called from harbor_agent.py's run(), once per trial,
     while the trial is still in flight. Captures what Master AI CLI
     itself knows: turns used, actions executed/failed, elapsed time,
     estimated token counts. Written to TB_LOG_FILE as it happens so a
     killed/timed-out trial still leaves a record.

  2. build_comparison_report() — run after a Harbor job finishes. Reads
     Harbor's own jobs/<job-id>/ output (which DOES carry reward and, for
     metered agents like claude-code/codex, real cost/token usage) and
     joins it against our TB_LOG_FILE by task name, producing one
     comparison table usable across any agent Harbor ran (Master AI CLI,
     Claude Code, OpenCode, ...).

Token counts from estimate_tokens() are a ~4-chars-per-token approximation
(no tiktoken dependency added — it's only present inside Harbor's own uv
venv, not the system python3 master_ai.py runs under). Treat them as a
rough proxy for comparing turn-to-turn cost trends, not as billing-accurate
figures.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

TB_LOG_FILE = Path.home() / ".master_ai_terminalbench_log.jsonl"

CHARS_PER_TOKEN_ESTIMATE = 4.0


def estimate_tokens(text: str) -> int:
    """Rough token count: len(text) / 4, the standard English-text BPE
    rule of thumb. Not provider-exact — see module docstring."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN_ESTIMATE))


def record_trial(
    *,
    task_name: str,
    model: str | None,
    turns_used: int,
    actions_executed: int,
    actions_failed: int,
    elapsed_sec: float,
    est_input_tokens: int,
    est_output_tokens: int,
    trial_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one trial's execution-side telemetry as a JSON line.

    Best-effort: a logging failure must never break the benchmark run, so
    write errors are swallowed (matching observability.py's own
    best-effort posture for its telemetry streams).
    """
    record = {
        "ts": time.time(),
        "task_name": task_name,
        "trial_id": trial_id,
        "model": model,
        "turns_used": turns_used,
        "actions_executed": actions_executed,
        "actions_failed": actions_failed,
        "elapsed_sec": round(elapsed_sec, 3),
        "est_input_tokens": est_input_tokens,
        "est_output_tokens": est_output_tokens,
        "est_total_tokens": est_input_tokens + est_output_tokens,
    }
    if extra:
        record["extra"] = extra
    try:
        TB_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TB_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _tail_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _load_our_trials(log_file: Path = TB_LOG_FILE) -> list[dict]:
    """Our own per-trial records, most recent last (a re-run's record
    comes after the earlier attempt's, so fuzzy-matching below prefers
    it when multiple records could match the same trial directory)."""
    return _tail_jsonl(log_file)


def _match_our_trial(trial_dir_name: str, our_trials: list[dict]) -> dict:
    """Find the telemetry record for a Harbor trial directory.

    Exact match on trial_id (Harbor's own context_id, when we had one at
    record time) is tried first — that's the reliable key. Falls back to
    a substring match against task_name, since Harbor's on-disk naming
    convention (per BaseAgent's docstring example,
    "hello-world__bZZeEkw__agent") embeds an id suffix we don't control
    and isn't confirmed against a live job on this machine yet (Docker
    isn't installed here — see harbor_agent.py's module docstring). If
    the exact format turns out to differ once a real job runs, this is
    the one function to adjust."""
    for rec in reversed(our_trials):
        if rec.get("trial_id") and rec["trial_id"] in trial_dir_name:
            return rec
    for rec in reversed(our_trials):
        task = rec.get("task_name") or ""
        leaf = task.rsplit("/", 1)[-1]
        if leaf and leaf in trial_dir_name:
            return rec
    return {}


def _find_trial_results(job_dir: Path) -> list[Path]:
    """Locate per-trial result.json files under a Harbor job directory.

    Harbor's documented layout is jobs/<job-id>/trials/<task>/result.json
    (see Harbor docs "Understanding Results" / "Analyzing results"). Globs
    defensively since this hasn't been verified against a live job (Docker
    isn't installed on this machine yet — see the CLAUDE.md note this was
    built under)."""
    candidates = list(job_dir.glob("trials/*/result.json"))
    if not candidates:
        candidates = list(job_dir.glob("trials/*/trial_result.json"))
    if not candidates:
        candidates = list(job_dir.rglob("result.json"))
    return candidates


def build_comparison_report(
    job_dir: str | Path, our_log_file: Path = TB_LOG_FILE
) -> dict[str, Any]:
    """Join a finished Harbor job's per-trial results with our own
    execution telemetry, keyed by task name (the trial directory name).

    Returns a dict with per-task rows and aggregate stats (pass rate, mean
    reward, mean elapsed time, mean estimated tokens, and real cost/tokens
    when Harbor's result.json carries usage_info for a metered agent).
    """
    job_dir = Path(job_dir)
    our_trials = _load_our_trials(our_log_file)

    rows: list[dict[str, Any]] = []
    for result_path in sorted(_find_trial_results(job_dir)):
        task_name = result_path.parent.name
        try:
            harbor_result = json.loads(result_path.read_text())
        except Exception as e:
            rows.append({"task_name": task_name, "error": f"unreadable result.json: {e}"})
            continue

        reward = harbor_result.get("reward")
        usage = harbor_result.get("usage_info", {}) or {}
        timing = harbor_result.get("timing", {}) or {}

        ours = _match_our_trial(task_name, our_trials)

        rows.append(
            {
                "task_name": task_name,
                "reward": reward,
                "passed": bool(reward) if reward is not None else None,
                "harbor_cost_usd": usage.get("total_cost"),
                "harbor_prompt_tokens": usage.get("prompt_tokens"),
                "harbor_completion_tokens": usage.get("completion_tokens"),
                "harbor_agent_time_sec": timing.get("agent_time_sec"),
                "our_turns_used": ours.get("turns_used"),
                "our_actions_executed": ours.get("actions_executed"),
                "our_actions_failed": ours.get("actions_failed"),
                "our_elapsed_sec": ours.get("elapsed_sec"),
                "our_est_total_tokens": ours.get("est_total_tokens"),
            }
        )

    n = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    rewards = [r["reward"] for r in rows if isinstance(r.get("reward"), (int, float))]
    elapsed = [r["our_elapsed_sec"] for r in rows if isinstance(r.get("our_elapsed_sec"), (int, float))]
    tokens = [r["our_est_total_tokens"] for r in rows if isinstance(r.get("our_est_total_tokens"), (int, float))]
    costs = [r["harbor_cost_usd"] for r in rows if isinstance(r.get("harbor_cost_usd"), (int, float))]

    return {
        "job_dir": str(job_dir),
        "n_trials": n,
        "pass_rate": (passed / n) if n else None,
        "mean_reward": (sum(rewards) / len(rewards)) if rewards else None,
        "mean_elapsed_sec": (sum(elapsed) / len(elapsed)) if elapsed else None,
        "mean_est_total_tokens": (sum(tokens) / len(tokens)) if tokens else None,
        "mean_cost_usd": (sum(costs) / len(costs)) if costs else None,
        "rows": rows,
    }


def format_comparison_report(report: dict[str, Any]) -> str:
    """Plain-text rendering of build_comparison_report()'s output."""
    lines = [
        f"Terminal-Bench job: {report['job_dir']}",
        f"Trials: {report['n_trials']}",
        f"Pass rate: {_pct(report['pass_rate'])}",
        f"Mean reward: {_fmt(report['mean_reward'])}",
        f"Mean elapsed (our telemetry): {_fmt(report['mean_elapsed_sec'])}s",
        f"Mean estimated tokens (our telemetry): {_fmt(report['mean_est_total_tokens'])}",
        f"Mean cost (Harbor usage_info, when present): {_fmt(report['mean_cost_usd'], prefix='$')}",
        "",
        "Per-task:",
    ]
    for r in report["rows"]:
        if "error" in r:
            lines.append(f"  {r['task_name']}: ERROR — {r['error']}")
            continue
        status = "PASS" if r.get("passed") else "FAIL" if r.get("passed") is False else "?"
        lines.append(
            f"  [{status}] {r['task_name']}  "
            f"turns={r.get('our_turns_used')}  "
            f"actions={r.get('our_actions_executed')}/{r.get('our_actions_failed')}f  "
            f"time={_fmt(r.get('our_elapsed_sec'))}s  "
            f"tokens~{r.get('our_est_total_tokens')}"
        )
    return "\n".join(lines)


def _fmt(value: Any, prefix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{prefix}{value:.3f}"
    return f"{prefix}{value}"


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 terminalbench_logger.py <harbor-job-dir>")
        raise SystemExit(1)
    report = build_comparison_report(sys.argv[1])
    print(format_comparison_report(report))
