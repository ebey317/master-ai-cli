"""Suggestive Thinking loop — the proactivity module Osiris spec'd 2026-10-19.

WAKE -> GATHER -> RECALL -> COMPARE -> FILTER -> DELIVER (or stay silent).
Default to silence: a run that surfaces nothing is a successful run — the
operator's attention is the scarcest resource in the system (see HANDOFF.md,
"2026-10-19 — SUGGESTIVE THINKING").

This module ships the loop skeleton and the relevance filter as pure,
independently testable functions. GATHER/RECALL/COMPARE/DELIVER are injected
by the caller and start as thin stubs here — real sources (the
perpetual-watcher's upstream-plans/, memory/, feedback_attention_signal_tiers'
speak.sh/TV/Jazz-Gospel delivery tiers) get wired in as a follow-up, per the
spec's own "loop first, schedulers second" ordering.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOG = Path.home() / "master-ai-cli" / "memory" / "suggestive-thinking-log.jsonl"


@dataclass
class Candidate:
    """One thing the loop is considering surfacing to the operator."""

    source: str  # e.g. "upstream-proposal", "stale-project", "cron-failure"
    headline: str  # the one-line nudge, e.g. "haven't touched the RV in 6 days"
    detail: str = ""  # supporting context, shown only if surfaced
    novelty: float = 0.0  # 0-1: does the operator already know this?
    actionable: float = 0.0  # 0-1: is there something to do/decide/approve?
    timely: float = 0.0  # 0-1: does it matter now vs. stale?
    interruption_worthy: float = 0.0  # 0-1: worth breaking silence for?
    time_critical: bool = False  # money/legal/safety/deadline-today override


@dataclass
class FilterResult:
    candidate: Candidate
    surfaced: bool
    reason: str


RELEVANCE_THRESHOLD = 0.6  # each of the four scores must clear this to surface


def passes_relevance_filter(candidate: Candidate) -> tuple[bool, str]:
    """Strict AND-gate, per the spec's own wording: a candidate "passes only
    if it meets ALL of" novelty/actionable/timely/worth-the-interruption —
    that's already an AND of four conditions in the spec text, not a design
    choice between AND and a weighted blend. The one real call is the
    threshold and the time-critical bypass, both made here:

    - time_critical (money/legal/safety/deadline-today) always surfaces,
      regardless of the other four scores — the clock makes it matter, not
      how well-understood or well-timed it is to the operator.
    - RELEVANCE_THRESHOLD=0.6 is a deliberately conservative default. Per the
      spec's attention-budget rule ("default to silence... a silent run
      that found nothing worth surfacing is a successful run"), it's
      cheaper to miss a borderline nudge than to train the operator to
      tune out the channel. Tune this one constant if it's surfacing too
      much or too little; the AND-gate shape itself follows the spec text
      directly and shouldn't need to change.
    """
    if candidate.time_critical:
        return True, "time_critical override"

    scores = {
        "novelty": candidate.novelty,
        "actionable": candidate.actionable,
        "timely": candidate.timely,
        "interruption_worthy": candidate.interruption_worthy,
    }
    failing = {k: v for k, v in scores.items() if v < RELEVANCE_THRESHOLD}
    if failing:
        worst = min(failing, key=lambda k: failing[k])
        return False, f"{worst}={failing[worst]:.2f} below {RELEVANCE_THRESHOLD}"
    return True, "all four scores clear threshold"


def _log_decision(result: FilterResult) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now().isoformat(),
                    "source": result.candidate.source,
                    "headline": result.candidate.headline,
                    "surfaced": result.surfaced,
                    "reason": result.reason,
                    "scores": {
                        "novelty": result.candidate.novelty,
                        "actionable": result.candidate.actionable,
                        "timely": result.candidate.timely,
                        "interruption_worthy": result.candidate.interruption_worthy,
                        "time_critical": result.candidate.time_critical,
                    },
                }
            )
            + "\n"
        )


def run_loop(
    gather: Callable[[], Iterable[Candidate]],
    recall: Callable[[Iterable[Candidate]], Iterable[Candidate]] = lambda c: c,
    compare: Callable[[Iterable[Candidate]], Iterable[Candidate]] = lambda c: c,
    deliver: Callable[[Candidate], None] = lambda c: print(f"[SUGGEST] {c.headline}"),
) -> list[FilterResult]:
    """One wake cycle: GATHER -> RECALL -> COMPARE -> FILTER -> DELIVER/silent.

    gather/recall/compare/deliver are injected so this stays testable without
    touching real repos, memory files, or notification channels. Real
    callers wire in: gather = read upstream-plans/ + a stale-project scan,
    recall = pull operator context from memory/, deliver = route through
    feedback_attention_signal_tiers.md's existing urgency tiers (speak.sh /
    TV / Jazz-Gospel) instead of a new channel.
    """
    candidates = list(gather())
    candidates = list(recall(candidates))
    candidates = list(compare(candidates))

    results: list[FilterResult] = []
    for c in candidates:
        surfaced, reason = passes_relevance_filter(c)
        result = FilterResult(candidate=c, surfaced=surfaced, reason=reason)
        _log_decision(result)
        if surfaced:
            deliver(c)
        results.append(result)
    return results
