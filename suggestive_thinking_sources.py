"""Real GATHER and RECALL sources for the Suggestive Thinking loop.

Kept separate from suggestive_thinking.py because these touch real files and
git — the loop itself (run_loop, passes_relevance_filter) stays pure and
testable against injected stubs. This module is what a real caller wires in.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from suggestive_thinking import LOG, Candidate

# Deliberately NOT Path(__file__).resolve().parent. This script's scheduled
# copy runs from a pinned worktree (~/.master_ai_scheduled_jobs, see
# HANDOFF.md "DONE 2026-10-19 -- scheduled, natively") so a branch switch in
# the live repo can't break the schedule -- but that means __file__'s own
# directory is NOT the live repo, and the real upstream-integration-log /
# upstream-plans / git history this module needs to read only exist in the
# live one. Found 2026-10-20: this cost a full day of zero real GATHER
# results (log file didn't exist at the worktree path at all, and the
# worktree's own .git is a file, not a directory, so the old
# REPO-as-master-ai-cli stale-check silently skipped every run).
LIVE_REPO = Path.home() / "master-ai-cli"
UPSTREAM_LOG = LIVE_REPO / "memory" / "upstream-integration-log.jsonl"
UPSTREAM_PLANS = LIVE_REPO / "memory" / "upstream-plans"

# Operator's rule (2026-10-19): suggestive thinking stays silent by default.
# Voice is opt-in, toggled by this touch-file's presence -- "vocalize
# suggestive thinking" -- not a setting buried in a config file.
VOCALIZE_FLAG = Path.home() / ".master_ai_vocalize_suggestive_thinking"

# NOTE: CLAUDE.md and feedback_attention_signal_tiers.md both reference
# ~/scripts/speak.sh as the voice channel -- that file does not exist on
# disk (verified 2026-10-19). The real, working mechanism is notify_voice.sh,
# which POSTs to the local TTS server on :5050.
NOTIFY_VOICE = Path.home() / "scripts" / "notify_voice.sh"

# Scope-locked to exactly the two in-progress finish-line items (HANDOFF.md
# "CURRENT FOCUS"), NOT every repo on disk. Scanning the other businesses
# (BioVega, Fair Chance, etc.) for staleness would violate the operator's own
# standing rules: don't surface the other businesses while this lock is
# active, and BioVega specifically stays untouched unless the operator asks
# (feedback_leave_biovega_alone.md). Widen this list only when the operator
# says the lock is lifted.
STALE_PROJECT_REPOS = [
    LIVE_REPO,  # master-ai-cli -- the live repo, not wherever this script runs from
    Path.home() / "ai-controller",  # the Linux reference implementation
]
STALE_AFTER_DAYS = 3


def gather_upstream_proposals() -> list[Candidate]:
    """GATHER: unsurfaced proposal docs from the proposal-only
    perpetual-watcher cron — one candidate per "status": "proposal" line in
    the integration log that hasn't already produced a candidate before
    (see _already_gathered_shas; a 30-day window, not forever — the
    integrator's own 3-failed-attempt retry logic is what actually retires
    a dead idea from the queue)."""
    if not UPSTREAM_LOG.exists():
        return []
    already = _already_gathered_shas()
    candidates: list[Candidate] = []
    for line in UPSTREAM_LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("status") != "proposal":
            continue
        sha = entry.get("sha", "")
        if not sha or sha[:8] in already:
            continue
        plan_path = UPSTREAM_PLANS / f"{sha[:8]}.md"
        candidates.append(
            Candidate(
                source=f"upstream-proposal:{sha[:8]}",
                headline=f"upstream idea proposed, not yet reviewed: {sha[:8]}",
                detail=(
                    f"plan: {plan_path}"
                    if plan_path.exists()
                    else entry.get("detail", "")
                ),
                novelty=1.0,
                actionable=1.0,
                timely=0.8,
                interruption_worthy=0.7,
            )
        )
    return candidates


def gather_stale_projects(
    repos: list[Path] | None = None, stale_after_days: int = STALE_AFTER_DAYS
) -> list[Candidate]:
    """GATHER: locked-scope repos with no commit in stale_after_days+ days.
    Deliberately NOT a scan of every repo on disk — see module docstring."""
    repos = STALE_PROJECT_REPOS if repos is None else repos
    candidates: list[Candidate] = []
    now = datetime.now()
    for repo in repos:
        if not (repo / ".git").is_dir():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        last_commit = datetime.fromtimestamp(int(out.stdout.strip()))
        days_stale = (now - last_commit).days
        if days_stale < stale_after_days:
            continue
        candidates.append(
            Candidate(
                source=f"stale-project:{repo.name}",
                headline=(
                    f"haven't touched {repo.name} in {days_stale} days — "
                    "pick it back up or let it go?"
                ),
                detail=f"last commit {last_commit.isoformat()}",
                novelty=1.0,
                actionable=1.0,
                timely=min(1.0, days_stale / 7),
                interruption_worthy=0.65,
            )
        )
    return candidates


def gather_all() -> list[Candidate]:
    return gather_upstream_proposals() + gather_stale_projects()


def _recent_log_entries(within_days: int) -> list[dict]:
    if not LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(days=within_days)
    out = []
    for line in LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
        except Exception:
            continue
        if ts >= cutoff:
            out.append(entry)
    return out


def _already_gathered_shas(within_days: int = 30) -> set[str]:
    seen = set()
    for entry in _recent_log_entries(within_days):
        source = entry.get("source", "")
        if source.startswith("upstream-proposal:"):
            seen.add(source.split(":", 1)[1])
    return seen


def recall_suppress_recent_repeats(
    candidates: Iterable[Candidate], within_days: int = 3
) -> list[Candidate]:
    """RECALL: per the spec's own guardrail — "a declined suggestion is not
    re-raised unless the facts change" — drop any candidate whose source was
    already logged (surfaced OR suppressed) within the recall window. Keyed
    on source alone, not source+headline: a stale-project headline's day
    count changes daily, but it's still the same underlying fact being
    reconsidered, not a new one."""
    recent_sources = {e.get("source") for e in _recent_log_entries(within_days)}
    return [c for c in candidates if c.source not in recent_sources]


def vocalization_enabled() -> bool:
    """Off by default. On only when VOCALIZE_FLAG exists -- a plain
    `touch`/`rm`, not a setting buried in config, so turning voice on or off
    is a literal one-line command from anywhere."""
    return VOCALIZE_FLAG.exists()


def set_vocalization(on: bool) -> None:
    if on:
        VOCALIZE_FLAG.touch()
    else:
        VOCALIZE_FLAG.unlink(missing_ok=True)


def deliver_default(candidate: Candidate) -> None:
    """Always: print to stdout (run_loop's own _log_decision already writes
    every verdict to the jsonl log regardless of this function). Only
    additionally speaks it aloud when vocalization_enabled() -- silent
    otherwise, per the operator's explicit rule."""
    print(f"[SUGGEST] {candidate.headline}")
    if vocalization_enabled() and NOTIFY_VOICE.exists():
        try:
            subprocess.run([str(NOTIFY_VOICE), candidate.headline], timeout=10)
        except Exception:
            pass  # voice is a nice-to-have; never let it break the loop


def main() -> int:
    """Manual/cron entrypoint: one real wake cycle using the real sources
    above, delivering through deliver_default (silent unless vocalization
    is turned on). No scheduler/cron unit is installed by this file; it's
    invokable, not yet automatically run.

    Usage:
        python3 suggestive_thinking_sources.py                 # run one cycle
        python3 suggestive_thinking_sources.py --vocalize on    # turn voice on
        python3 suggestive_thinking_sources.py --vocalize off   # turn voice off
    """
    import sys

    from suggestive_thinking import run_loop

    if "--vocalize" in sys.argv:
        idx = sys.argv.index("--vocalize")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if arg not in ("on", "off"):
            print("usage: --vocalize on|off")
            return 1
        set_vocalization(arg == "on")
        print(f"vocalization {'enabled' if arg == 'on' else 'disabled'}")
        return 0

    results = run_loop(
        gather=gather_all,
        recall=lambda cs: recall_suppress_recent_repeats(cs),
        deliver=deliver_default,
    )
    surfaced = sum(1 for r in results if r.surfaced)
    print(
        f"suggestive-thinking: {len(results)} candidate(s) considered, {surfaced} surfaced"
        f" (voice {'on' if vocalization_enabled() else 'off'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
