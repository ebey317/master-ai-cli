"""V0 task abstraction — designed in dual-agent dialogue with browser-Claude
2026-05-18.

Single task instance. No persistence, no multi-task scheduling, no cross-task
dependencies. Tight v0 scope deliberately, per browser-Claude design review:
"Designing the task abstraction floating in space risks producing a shape that
doesn't fit the things we already shipped" — so this is grounded in the
existing apply-job-session adapter as the concrete use case.

Lifecycle states:
    SPAWNED         — task created, target not yet resolved
    RESOLVING_TARGET — task is identifying / selecting its target (e.g.,
                       picking a listing from search results)
    RUNNING         — target locked, executing the phase sequence
    TERMINATED      — reached an outcome; check `terminated_reason`

Terminated reasons:
    APPLIED     — happy path, task achieved its goal (e.g., application
                  submitted successfully)
    SKIPPED     — task voluntarily stopped (e.g., external redirect,
                  background-check filter, skip-company match)
    INTERRUPTED — task is paused awaiting operator action (e.g., CAPTCHA,
                  human keypress on a sensitive field, final submit)
    FAILED      — task hit an unrecoverable error

The dispatcher (`task_dispatch`) advances the task by one phase. Caller is
responsible for re-entering until state == TERMINATED. No internal loop;
keeps the model composable with the existing skill_runtime's interrupt-
resume cycle.

What this module deliberately does NOT do (v0 scope, per BC):
- No persistence — task state is in-memory only; serialization is a future
  cycle when there's something worth persisting
- No multi-task scheduling — one task at a time; no TaskRunner / queue
- No cross-task dependencies — tasks don't reference other tasks
- No automatic phase chaining — caller drives re-entry, not the dispatcher
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─── Lifecycle state constants ───────────────────────────────────────

TASK_STATE_SPAWNED = "spawned"
TASK_STATE_RESOLVING_TARGET = "resolving_target"
TASK_STATE_RUNNING = "running"
TASK_STATE_TERMINATED = "terminated"

_TASK_STATES = {
    TASK_STATE_SPAWNED,
    TASK_STATE_RESOLVING_TARGET,
    TASK_STATE_RUNNING,
    TASK_STATE_TERMINATED,
}


# ─── Terminated-reason constants ─────────────────────────────────────

TASK_TERMINATED_APPLIED = "applied"
TASK_TERMINATED_SKIPPED = "skipped"
TASK_TERMINATED_INTERRUPTED = "interrupted"
TASK_TERMINATED_FAILED = "failed"

_TASK_TERMINATED_REASONS = {
    TASK_TERMINATED_APPLIED,
    TASK_TERMINATED_SKIPPED,
    TASK_TERMINATED_INTERRUPTED,
    TASK_TERMINATED_FAILED,
}


# ─── Task dataclass ──────────────────────────────────────────────────

@dataclass
class Task:
    """Single task instance, in-memory only. Composes with the existing
    phase pattern by holding state and being driven by a dispatcher.

    Task HAS-A state and dispatches to phase functions, rather than IS-A
    wrapper around a phase sequence (per browser-Claude design — matches
    the existing state.data phase-keying pattern instead of re-inventing
    it)."""
    task_id: str
    task_type: str                      # "apply_one_job" for v0; future
                                         # types extend without schema break
    state: str = TASK_STATE_SPAWNED
    target: Optional[dict] = None        # e.g., {"url": "...", "jk": "..."}
                                         # for an apply task
    params: dict = field(default_factory=dict)   # task-type-specific config
    artifacts: list = field(default_factory=list)  # accumulated outputs —
                                                    # audit entries, descriptors,
                                                    # confirmation refs
    terminated_reason: Optional[str] = None  # set when state == TERMINATED
    spawned_at: str = ""                 # ISO 8601 UTC (matches audit-log
                                         # timestamp shape)

    def __post_init__(self):
        if not self.spawned_at:
            self.spawned_at = (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="microseconds")
            )
        if self.state not in _TASK_STATES:
            raise ValueError(
                f"invalid task state {self.state!r}; "
                f"must be one of {sorted(_TASK_STATES)}"
            )


# ─── Dispatcher ──────────────────────────────────────────────────────

def task_dispatch(task: Task, phase_fn: Callable[[Task], dict]) -> dict:
    """Advance the task by one phase call. Updates task.state based on the
    outcome dict the phase function returns, then returns that same outcome
    so the caller can act on _pending_directives / reasons / state_updates.

    Outcome → state mapping (canonical):
      "applied"   → TERMINATED + reason APPLIED
      "skipped"   → TERMINATED + reason SKIPPED
      "failed"    → TERMINATED + reason FAILED
      "interrupt" → RUNNING (paused awaiting operator; not terminated —
                    operator resume re-enters the dispatcher)

    Phase functions match the existing executor-framework shape — they
    return {outcome, details}. The dispatcher reads `outcome` only;
    everything else passes through untouched."""
    if task.state == TASK_STATE_TERMINATED:
        raise RuntimeError(
            f"task_dispatch called on terminated task {task.task_id} "
            f"(reason={task.terminated_reason}); re-entry on terminated "
            "tasks would create a state-machine violation"
        )

    # If task hasn't started running yet, this dispatch transitions it.
    if task.state in (TASK_STATE_SPAWNED, TASK_STATE_RESOLVING_TARGET):
        task.state = TASK_STATE_RUNNING

    outcome_dict = phase_fn(task)
    outcome = outcome_dict.get("outcome")

    if outcome == "applied":
        task.state = TASK_STATE_TERMINATED
        task.terminated_reason = TASK_TERMINATED_APPLIED
    elif outcome == "skipped":
        task.state = TASK_STATE_TERMINATED
        task.terminated_reason = TASK_TERMINATED_SKIPPED
    elif outcome == "failed":
        task.state = TASK_STATE_TERMINATED
        task.terminated_reason = TASK_TERMINATED_FAILED
    elif outcome == "interrupt":
        # Paused for operator action — stay RUNNING. The next dispatch
        # re-enters the phase function (operator-driven resume).
        task.state = TASK_STATE_RUNNING
    else:
        # Unknown outcome — treat as failure rather than silently advance.
        # Better to fail loud than let the state machine drift.
        task.state = TASK_STATE_TERMINATED
        task.terminated_reason = TASK_TERMINATED_FAILED
        outcome_dict.setdefault("details", {})[
            "_dispatcher_note"
        ] = f"unknown outcome {outcome!r}, terminated as failed"

    return outcome_dict


__all__ = [
    "Task",
    "task_dispatch",
    "TASK_STATE_SPAWNED",
    "TASK_STATE_RESOLVING_TARGET",
    "TASK_STATE_RUNNING",
    "TASK_STATE_TERMINATED",
    "TASK_TERMINATED_APPLIED",
    "TASK_TERMINATED_SKIPPED",
    "TASK_TERMINATED_INTERRUPTED",
    "TASK_TERMINATED_FAILED",
]
