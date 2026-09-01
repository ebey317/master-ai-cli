"""Sensei learning loop — read-only failure-pattern analysis over real
skill run history. Claude's other half of the Skill Marketplace /
Learning Loop split (ROADMAP.md Phase 3.3b).

Data source, checked directly rather than assumed: skill_runtime.py
already persists one JSON file per skill session at
~/.master_ai_skills/<name>/sessions/<session_id>.json (see
skill_runtime.save_state / SkillState.to_dict) containing `history`
(append-only {step, result, ts}), `errors` (append-only {step, error,
ts}), `current_step`, `done`, `aborted`, `interrupt_reason`,
`step_count`. This is far richer than the skills' own `knowledge/*.jsonl`
files (which only log successful outcomes, e.g. web-search-ddgr's
searches.jsonl has no failure/abort record at all) — so this module reads
session files directly, not knowledge/*.jsonl. 25 real session files exist
across the 4 adapted skills as of 2026-09-01, confirmed on disk before
writing this.

Hard constraint carried over from every other piece of tonight's work:
this module is READ-ONLY. It produces a structured report and nothing
else — no mutation of any skill's files, no auto-applied fixes. Hermes'
`skill improve <name>` REPL command consumes this report and, for any
proposed fix, must route the actual diff through the existing typed EDIT
confirm gate — that logic lives in master_ai.py, not here.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from skill_runtime import SKILLS_ROOT


def _sessions_dir(name: str) -> Path:
    return SKILLS_ROOT / name / "sessions"


def _load_sessions(name: str, last_n: Optional[int] = None) -> list:
    """Full session records (not the summary-only skill_runtime.list_sessions()),
    sorted oldest-first, optionally truncated to the most recent last_n."""
    d = _sessions_dir(name)
    if not d.is_dir():
        return []
    records = []
    for f in sorted(d.glob("*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: r.get("updated_at", 0))
    if last_n is not None and len(records) > last_n:
        records = records[-last_n:]
    return records


@dataclass
class AnalysisReport:
    skill_name: str
    total_sessions: int
    sessions_analyzed: int
    success_count: int = 0
    aborted_count: int = 0
    incomplete_count: int = 0  # neither done nor aborted (stuck/interrupted)
    success_rate: float = 0.0
    step_abort_counts: dict = field(default_factory=dict)   # step -> count of sessions that ended aborted at that step
    step_error_counts: dict = field(default_factory=dict)   # step -> count of error entries logged for that step
    top_error_messages: list = field(default_factory=list)  # [(message, count), ...]
    steps_retried: dict = field(default_factory=dict)       # step -> count of sessions where it appears >1x in history
    first_run_ts: Optional[float] = None
    last_run_ts: Optional[float] = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def format_text(self) -> str:
        lines = [
            f"Learning-loop analysis: {self.skill_name}",
            f"  sessions: {self.sessions_analyzed} analyzed of {self.total_sessions} total",
        ]
        if self.note:
            lines.append(f"  {self.note}")
            return "\n".join(lines)
        lines.append(
            f"  outcomes: {self.success_count} success, {self.aborted_count} aborted, "
            f"{self.incomplete_count} incomplete  (success rate {self.success_rate:.0%})"
        )
        if self.step_abort_counts:
            lines.append("  aborts by step:")
            for step, n in sorted(self.step_abort_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {step}: {n}")
        if self.steps_retried:
            lines.append("  steps that retried within a session:")
            for step, n in sorted(self.steps_retried.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {step}: {n} session(s)")
        if self.top_error_messages:
            lines.append("  most common errors:")
            for msg, n in self.top_error_messages:
                lines.append(f"    ({n}x) {msg}")
        return "\n".join(lines)


def analyze_skill(name: str, last_n: int = 50) -> AnalysisReport:
    """Aggregate real session history for `name` into a failure-pattern
    report. No mutation anywhere in this function."""
    all_sessions = _load_sessions(name)
    sessions = all_sessions[-last_n:] if last_n else all_sessions

    if not sessions:
        return AnalysisReport(
            skill_name=name,
            total_sessions=0,
            sessions_analyzed=0,
            note="no session history yet — run the skill at least once before analyzing",
        )

    success = aborted = incomplete = 0
    abort_by_step = Counter()
    error_by_step = Counter()
    error_messages = Counter()
    retried_steps = Counter()

    for sess in sessions:
        if sess.get("done") and not sess.get("aborted"):
            success += 1
        elif sess.get("aborted"):
            aborted += 1
            abort_by_step[sess.get("current_step") or "?"] += 1
            # Gate-refusal message: the recipes store the instructive ABORT
            # text in state.data.message (dataclasses SkillState.data), not
            # in errors[]. Count it as an error-pattern source too — 2026-
            # 09-01, found by diffing real systematic-debugging sessions
            # (9 aborted, errors[] empty, data.message populated).
            gate_msg = str((sess.get("data") or {}).get("message") or "")[:160]
            if gate_msg:
                error_messages[gate_msg] += 1
                error_by_step[sess.get("current_step") or "?"] += 1
        else:
            incomplete += 1

        for err in sess.get("errors", []):
            step = err.get("step") or "?"
            error_by_step[step] += 1
            msg = str(err.get("error", ""))[:160]
            if msg:
                error_messages[msg] += 1

        # A step that appears more than once in history within the same
        # session means it re-ran — either a retry_on_fail loop or a
        # recovery_next jump landed back on it.
        step_hits = Counter(h.get("step") for h in sess.get("history", []) if h.get("step"))
        for step, count in step_hits.items():
            if count > 1:
                retried_steps[step] += 1

    total = len(sessions)
    timestamps = [s.get("updated_at") for s in sessions if s.get("updated_at")]

    return AnalysisReport(
        skill_name=name,
        total_sessions=len(all_sessions),
        sessions_analyzed=total,
        success_count=success,
        aborted_count=aborted,
        incomplete_count=incomplete,
        success_rate=(success / total) if total else 0.0,
        step_abort_counts=dict(abort_by_step),
        step_error_counts=dict(error_by_step),
        top_error_messages=error_messages.most_common(5),
        steps_retried=dict(retried_steps),
        first_run_ts=min(timestamps) if timestamps else None,
        last_run_ts=max(timestamps) if timestamps else None,
    )


def analyze_all() -> dict:
    """Convenience: analyze every adapted skill under SKILLS_ROOT."""
    out = {}
    if not SKILLS_ROOT.is_dir():
        return out
    for d in sorted(SKILLS_ROOT.iterdir()):
        if not d.is_dir() or d.name == "_staging" or not (d / "recipe.py").exists():
            continue
        out[d.name] = analyze_skill(d.name)
    return out
