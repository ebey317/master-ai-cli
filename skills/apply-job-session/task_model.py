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
from typing import Callable, Dict, List, Optional


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


# ─── Cross-tab routing — tab role + binding source constants ─────────
#
# Per browser-Claude cross-tab v0 design 2026-05-18.
#
# A tab's role within a task says what the task is allowed to DO on
# that tab. "Reference" is read-only (we look but don't act);
# "monitor" is observe-only (we don't even read on demand — the tab
# emits signals we listen for); "primary" is the driving tab where
# fill / click / submit happen.

BINDING_ROLE_PRIMARY = "primary"
BINDING_ROLE_REFERENCE = "reference"
BINDING_ROLE_MONITOR = "monitor"

_BINDING_ROLES = {
    BINDING_ROLE_PRIMARY,
    BINDING_ROLE_REFERENCE,
    BINDING_ROLE_MONITOR,
}

# Provenance — where this binding came from. Useful for audit / debug
# (why is this tab bound to this task?) and for trust decisions
# (operator-added bindings are more authoritative than adapter-promoted).

BINDING_SOURCE_OPERATOR_ADDED = "operator_added"
BINDING_SOURCE_ADAPTER_PROMOTED = "adapter_promoted"
BINDING_SOURCE_INHERITED = "inherited"  # carried from a prior task/session

_BINDING_SOURCES = {
    BINDING_SOURCE_OPERATOR_ADDED,
    BINDING_SOURCE_ADAPTER_PROMOTED,
    BINDING_SOURCE_INHERITED,
}

# Routing outcomes — the four branches per BC's spec (one_primary,
# multiple_primary, zero_primary, stale_primary).

ROUTING_OK = "ok"                           # exactly one valid primary binding
ROUTING_AMBIGUOUS = "ambiguous"             # multiple primary bindings; operator picks
ROUTING_NO_PRIMARY = "no_primary"           # zero primary bindings; operator designates
ROUTING_STALE_PRIMARY = "stale_primary"     # primary binding exists but tab drifted


# ─── Task dataclass ──────────────────────────────────────────────────

@dataclass
class TaskTabBinding:
    """Links a Chrome tab to a task. Per browser-Claude cross-tab v0 design
    2026-05-18. In-memory only in v0 (persistence is a later cycle).

    role tells the executor what it's allowed to DO on this tab:
      primary   → fill / click / submit (the driving tab)
      reference → read-only (data source like Drive doc, prior tab)
      monitor   → observe-only (status / signal source like email)

    binding_source carries provenance: operator_added is most
    authoritative, adapter_promoted (e.g., adapter clicking Apply opens
    smartapply in a new tab and auto-binds it) is implicit, inherited
    is carried from a prior task or session.

    last_observed_url enables stale-binding detection: when route_for_task
    sees a binding whose tab is currently on a different URL than recorded
    here, the binding flags as STALE rather than silently routing to a
    drifted tab. Per BC's refinement — stale gets surfaced as its own
    interrupt branch rather than auto-removed (a closed-by-accident tab
    can be recovered; a silent drop loses information)."""
    task_id: str
    tab_id: int
    role: str
    binding_source: str
    added_ts: str = ""
    last_observed_url: Optional[str] = None

    def __post_init__(self):
        if not self.added_ts:
            self.added_ts = (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="microseconds")
            )
        if self.role not in _BINDING_ROLES:
            raise ValueError(
                f"invalid binding role {self.role!r}; "
                f"must be one of {sorted(_BINDING_ROLES)}"
            )
        if self.binding_source not in _BINDING_SOURCES:
            raise ValueError(
                f"invalid binding_source {self.binding_source!r}; "
                f"must be one of {sorted(_BINDING_SOURCES)}"
            )


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
    tab_bindings: List[TaskTabBinding] = field(default_factory=list)
                                         # cross-tab v0 — bindings linking
                                         # this task to specific Chrome tabs
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
        # Apply any side effects the phase emitted (e.g., new tab
        # bindings from adapter auto-promote on redirect).
        side_effects = (outcome_dict.get("details") or {}).get("_side_effects")
        if side_effects:
            applied = apply_side_effects(task, side_effects)
            outcome_dict["details"]["_side_effects_applied"] = applied
    else:
        # Unknown outcome — treat as failure rather than silently advance.
        # Better to fail loud than let the state machine drift.
        task.state = TASK_STATE_TERMINATED
        task.terminated_reason = TASK_TERMINATED_FAILED
        outcome_dict.setdefault("details", {})[
            "_dispatcher_note"
        ] = f"unknown outcome {outcome!r}, terminated as failed"

    return outcome_dict


# ─── Side-effect protocol — phase → task mutations via dispatcher ────
#
# Per browser-Claude design lock 2026-05-18 (b-plus shape). Phase
# functions don't mutate the task directly. Instead, they return outcome
# dicts with an optional `_side_effects` list in `details`. Each entry is
# a self-describing dict with a `kind` field plus kind-specific payload.
# The dispatcher iterates the list and applies each effect to the task.
#
# Why a list rather than a TaskPatch dataclass:
#   - Append-only, ordered, each entry self-describing (matches the
#     pattern audit logs and event sources use)
#   - Flexible enough that adding a new side-effect kind doesn't require
#     a schema migration
#   - Discoverable enough at v0 scale — at five-plus side-effect types
#     we'd promote to a typed TaskPatch dataclass (browser-Claude flagged
#     this as the future shape if the list gets unstructured)
#
# Why side-effects rather than magic keys in outcome details:
#   - Outcome details are currently a free-for-all (debugging info,
#     directive payloads, anything a phase wants to communicate). Adding
#     `_tab_binding_update` as a sentinel the dispatcher reads creates
#     an implicit protocol — the dispatcher has to know which keys
#     are state mutations vs ordinary debug detail. Side-effects are
#     EXPLICITLY mutations. Single-purpose surface.
#
# v0 side-effect kinds:
#   tab_binding_add       — append a new TaskTabBinding to task.tab_bindings
#   tab_binding_demote    — change an existing binding's role and update
#                           its last_observed_url to the URL the tab was
#                           on at demotion time

SIDE_EFFECT_TAB_BINDING_ADD = "tab_binding_add"
SIDE_EFFECT_TAB_BINDING_DEMOTE = "tab_binding_demote"

_KNOWN_SIDE_EFFECTS = {
    SIDE_EFFECT_TAB_BINDING_ADD,
    SIDE_EFFECT_TAB_BINDING_DEMOTE,
}


def apply_side_effects(task: Task, side_effects: list) -> list:
    """Apply each side-effect entry to the task in order. Returns a list
    of effects actually applied (skipping unknown kinds with a note —
    fail-loud-not-silent on the count mismatch).

    For tab_binding_add: entry["binding"] is appended to task.tab_bindings.
    For tab_binding_demote: entry["tab_id"] looked up in task.tab_bindings;
        if found, its role and last_observed_url are updated to
        entry["new_role"] and entry["new_last_observed_url"]; if not
        found, recorded as skipped (the tab may have been unbound earlier
        in the same dispatch — not a hard failure)."""
    applied = []
    for effect in side_effects or []:
        kind = effect.get("kind")
        if kind == SIDE_EFFECT_TAB_BINDING_ADD:
            binding = effect.get("binding")
            if isinstance(binding, TaskTabBinding):
                task.tab_bindings.append(binding)
                applied.append({"kind": kind, "tab_id": binding.tab_id})
            else:
                applied.append({"kind": kind, "skipped": "invalid_binding_payload"})
        elif kind == SIDE_EFFECT_TAB_BINDING_DEMOTE:
            tab_id = effect.get("tab_id")
            new_role = effect.get("new_role")
            new_url = effect.get("new_last_observed_url")
            matched = None
            for b in task.tab_bindings:
                if b.tab_id == tab_id:
                    matched = b
                    break
            if matched is None:
                applied.append({"kind": kind, "tab_id": tab_id,
                               "skipped": "binding_not_found"})
            elif new_role not in _BINDING_ROLES:
                applied.append({"kind": kind, "tab_id": tab_id,
                               "skipped": f"invalid_new_role:{new_role}"})
            else:
                matched.role = new_role
                if new_url is not None:
                    matched.last_observed_url = new_url
                applied.append({"kind": kind, "tab_id": tab_id,
                               "new_role": new_role})
        else:
            # Unknown side-effect kind — fail loud, don't silently drop.
            applied.append({"kind": kind or "<missing>",
                           "skipped": "unknown_side_effect_kind"})
    return applied


def detect_redirect_and_promote(
    task: Task,
    previous_tab_urls: Dict[int, str],
    current_tab_urls: Dict[int, str],
    redirect_host_substring: str,
) -> list:
    """Helper for adapters: detect that a click caused a new tab to open
    on a redirect target (e.g., adapter_indeed click_apply landing the
    operator on smartapply.indeed.com in a new tab). Returns a side-effects
    list ready to attach to a phase outcome.

    What it does:
      - Identifies new tab IDs present in current_tab_urls but not in
        previous_tab_urls
      - For each new tab whose URL contains redirect_host_substring,
        builds a tab_binding_add side effect (role=primary, source=
        adapter_promoted)
      - For each existing primary binding in the task whose tab is still
        present in current_tab_urls, builds a tab_binding_demote side
        effect (role=reference, last_observed_url=current_url at the
        time of demotion)

    Pure function — does not mutate the task. Caller attaches the returned
    list as `_side_effects` in the phase outcome, and the dispatcher's
    apply_side_effects does the actual mutation."""
    side_effects = []
    new_tab_ids = set(current_tab_urls) - set(previous_tab_urls)

    # Demote existing primaries that are still live — they had been
    # primary before the redirect; now they're reference.
    for b in task.tab_bindings:
        if b.role != BINDING_ROLE_PRIMARY:
            continue
        if b.tab_id in current_tab_urls:
            side_effects.append({
                "kind": SIDE_EFFECT_TAB_BINDING_DEMOTE,
                "tab_id": b.tab_id,
                "new_role": BINDING_ROLE_REFERENCE,
                "new_last_observed_url": current_tab_urls[b.tab_id],
            })

    # Promote each new tab on the redirect host as a fresh primary.
    for tab_id in sorted(new_tab_ids):
        url = current_tab_urls[tab_id]
        if redirect_host_substring in url:
            new_binding = TaskTabBinding(
                task_id=task.task_id,
                tab_id=tab_id,
                role=BINDING_ROLE_PRIMARY,
                binding_source=BINDING_SOURCE_ADAPTER_PROMOTED,
                last_observed_url=url,
            )
            side_effects.append({
                "kind": SIDE_EFFECT_TAB_BINDING_ADD,
                "binding": new_binding,
            })

    return side_effects


# ─── Cross-tab routing function ──────────────────────────────────────

def route_for_task(
    task: Task,
    current_tab_urls: Dict[int, str],
) -> dict:
    """Decide which tab the task should act on right now. Pure function —
    takes the task plus a snapshot of {tab_id: current_url} (caller fetches
    via tabs_context_mcp or equivalent) and returns a routing outcome.

    Four branches per browser-Claude cross-tab v0 design 2026-05-18:

      ROUTING_OK             — exactly one primary binding, its tab is
                               still on the bound URL → proceed
      ROUTING_AMBIGUOUS      — multiple primary bindings active → operator
                               picks (interrupt for disambiguation)
      ROUTING_NO_PRIMARY     — zero primary bindings → operator designates
                               (interrupt for assignment)
      ROUTING_STALE_PRIMARY  — primary binding exists but tab closed, or
                               tab navigated away from the bound URL →
                               operator recovers (interrupt asking whether
                               to drop the binding or re-confirm). Per BC's
                               refinement, drift gets its own branch
                               rather than silent fallback to NO_PRIMARY.

    Reference/monitor bindings are NOT considered for routing — the
    function returns the primary tab the task is supposed to ACT on.
    Read-only / observe-only bindings live in their own slot."""
    primaries = [b for b in task.tab_bindings if b.role == BINDING_ROLE_PRIMARY]

    if not primaries:
        return {
            "outcome": ROUTING_NO_PRIMARY,
            "details": {
                "reason": "no_primary_binding_for_task",
                "task_id": task.task_id,
            },
        }

    # Classify each primary as live or stale.
    live = []
    stale = []
    for b in primaries:
        current_url = current_tab_urls.get(b.tab_id)
        if current_url is None:
            # Tab closed
            stale.append({"binding": b, "reason": "tab_closed"})
        elif b.last_observed_url is not None and current_url != b.last_observed_url:
            # Tab navigated away
            stale.append({"binding": b, "reason": "tab_url_drifted",
                         "current_url": current_url})
        else:
            live.append(b)

    if not live:
        return {
            "outcome": ROUTING_STALE_PRIMARY,
            "details": {
                "reason": "all_primary_bindings_stale",
                "task_id": task.task_id,
                "stale": stale,
            },
        }

    if len(live) > 1:
        return {
            "outcome": ROUTING_AMBIGUOUS,
            "details": {
                "reason": "multiple_live_primary_bindings",
                "task_id": task.task_id,
                "candidates": [{"tab_id": b.tab_id} for b in live],
            },
        }

    # Exactly one live primary.
    chosen = live[0]
    return {
        "outcome": ROUTING_OK,
        "details": {
            "task_id": task.task_id,
            "tab_id": chosen.tab_id,
            "binding_source": chosen.binding_source,
        },
    }


__all__ = [
    "Task",
    "TaskTabBinding",
    "task_dispatch",
    "route_for_task",
    "apply_side_effects",
    "detect_redirect_and_promote",
    "SIDE_EFFECT_TAB_BINDING_ADD",
    "SIDE_EFFECT_TAB_BINDING_DEMOTE",
    "TASK_STATE_SPAWNED",
    "TASK_STATE_RESOLVING_TARGET",
    "TASK_STATE_RUNNING",
    "TASK_STATE_TERMINATED",
    "TASK_TERMINATED_APPLIED",
    "TASK_TERMINATED_SKIPPED",
    "TASK_TERMINATED_INTERRUPTED",
    "TASK_TERMINATED_FAILED",
    "BINDING_ROLE_PRIMARY",
    "BINDING_ROLE_REFERENCE",
    "BINDING_ROLE_MONITOR",
    "BINDING_SOURCE_OPERATOR_ADDED",
    "BINDING_SOURCE_ADAPTER_PROMOTED",
    "BINDING_SOURCE_INHERITED",
    "ROUTING_OK",
    "ROUTING_AMBIGUOUS",
    "ROUTING_NO_PRIMARY",
    "ROUTING_STALE_PRIMARY",
]
