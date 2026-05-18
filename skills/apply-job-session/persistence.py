"""V0 persistence for the task model — designed in dual-agent dialogue
with browser-Claude 2026-05-18.

Scope: persist Task + nested TaskTabBinding state to disk so a session
that crashes mid-flow can be resumed. Out of scope for v0: index
compaction, cross-task referential integrity, encryption-at-rest.

Design:
  - Manual save, centralized in dispatcher. `task.persist_to_disk` gates
    it. Dispatcher is the only caller of `save_task()` — adapters and
    executors don't touch disk.
  - One JSON file per task at
    ``<base>/<task_id>.json`` (default base
    ``~/.master_ai_skills/apply-job-session/tasks/``).
  - Append-only discoverability log at ``<base>/_index.jsonl``, one line
    per save with ISO 8601 UTC microsecond timestamp + task_id + state +
    terminated_reason (for tail-the-log debugging without globbing the
    directory).
  - Atomic write: tmp file in same dir + ``os.replace()``. No partial JSON
    survives a crash mid-write.
  - Best-effort load with NO SILENT RECOVERY:
      * Unknown fields → warning entry on Task.persistence_warnings
      * Missing required fields → warning entry, field defaulted
      * Type mismatch on required field → load FAILS LOUD, no partial
        Task returned
  - Dispatcher precondition: any task with non-empty
    ``persistence_warnings`` is blocked from advance() until cleared via
    ``confirm_recovered``.

Deferred to a later cycle (per browser-Claude pacing):
  - Index file compaction / rotation
  - Cross-task referential integrity (task A references task B that's
    missing)
  - Encryption-at-rest — separate cycle, threat model first
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import List, Optional

from task_model import (
    Task,
    TaskTabBinding,
    _BINDING_ROLES,
    _BINDING_SOURCES,
    _TASK_STATES,
    _TASK_TERMINATED_REASONS,
)


# Default storage location. Per-test isolation: tests pass an explicit
# base_path; production code lets it default.
DEFAULT_BASE_PATH = Path.home() / ".master_ai_skills" / "apply-job-session" / "tasks"
INDEX_FILENAME = "_index.jsonl"


# ─── Serialization helpers ───────────────────────────────────────────


def _task_to_dict(task: Task) -> dict:
    """Convert a Task (with nested TaskTabBindings) into a JSON-safe dict.

    Done explicitly rather than via dataclasses.asdict() so we control the
    shape — in particular, list[TaskTabBinding] becomes list[dict], not
    list[(field,value) tuples], and we can add a schema marker without
    confusing the dataclass machinery."""
    return {
        "_schema": "task_v0",
        "task_id": task.task_id,
        "task_type": task.task_type,
        "state": task.state,
        "target": task.target,
        "params": task.params,
        "artifacts": task.artifacts,
        "tab_bindings": [
            {
                "task_id": b.task_id,
                "tab_id": b.tab_id,
                "role": b.role,
                "binding_source": b.binding_source,
                "added_ts": b.added_ts,
                "last_observed_url": b.last_observed_url,
            }
            for b in task.tab_bindings
        ],
        "terminated_reason": task.terminated_reason,
        "spawned_at": task.spawned_at,
        "persist_to_disk": task.persist_to_disk,
        "persistence_warnings": list(task.persistence_warnings),
    }


_REQUIRED_TASK_FIELDS = {"task_id", "task_type"}
_REQUIRED_BINDING_FIELDS = {"task_id", "tab_id", "role", "binding_source"}


class TaskLoadError(Exception):
    """Load failed loudly — type mismatch on a required field, malformed
    JSON, or other unrecoverable shape error. Caller must NOT use a
    partial Task; the file should be inspected before re-attempting."""


def _build_binding(raw: dict, warnings: List[str], idx: int) -> Optional[TaskTabBinding]:
    """Build a TaskTabBinding from a raw dict. Type mismatches on required
    fields raise TaskLoadError. Unknown fields produce warnings; missing
    optional fields default cleanly. Returns the binding, or raises."""
    if not isinstance(raw, dict):
        raise TaskLoadError(
            f"tab_bindings[{idx}] is {type(raw).__name__}, expected dict"
        )

    missing = _REQUIRED_BINDING_FIELDS - set(raw)
    if missing:
        raise TaskLoadError(
            f"tab_bindings[{idx}] missing required fields: {sorted(missing)}"
        )

    task_id = raw["task_id"]
    tab_id = raw["tab_id"]
    role = raw["role"]
    binding_source = raw["binding_source"]
    added_ts = raw.get("added_ts", "") or ""
    last_observed_url = raw.get("last_observed_url")

    if not isinstance(task_id, str):
        raise TaskLoadError(
            f"tab_bindings[{idx}].task_id is {type(task_id).__name__}, expected str"
        )
    if not isinstance(tab_id, int) or isinstance(tab_id, bool):
        raise TaskLoadError(
            f"tab_bindings[{idx}].tab_id is {type(tab_id).__name__}, expected int"
        )
    if role not in _BINDING_ROLES:
        raise TaskLoadError(
            f"tab_bindings[{idx}].role={role!r} not in {sorted(_BINDING_ROLES)}"
        )
    if binding_source not in _BINDING_SOURCES:
        raise TaskLoadError(
            f"tab_bindings[{idx}].binding_source={binding_source!r} not in "
            f"{sorted(_BINDING_SOURCES)}"
        )
    if last_observed_url is not None and not isinstance(last_observed_url, str):
        raise TaskLoadError(
            f"tab_bindings[{idx}].last_observed_url is "
            f"{type(last_observed_url).__name__}, expected str or null"
        )

    known = {
        "task_id", "tab_id", "role", "binding_source",
        "added_ts", "last_observed_url",
    }
    for key in raw:
        if key not in known:
            warnings.append(f"tab_bindings[{idx}].unknown_field:{key}")

    return TaskTabBinding(
        task_id=task_id,
        tab_id=tab_id,
        role=role,
        binding_source=binding_source,
        added_ts=added_ts,
        last_observed_url=last_observed_url,
    )


def _dict_to_task(raw: dict) -> Task:
    """Rebuild a Task from a raw dict. Populates Task.persistence_warnings
    for unknown fields and missing-but-defaulted optional fields. Raises
    TaskLoadError for missing required fields or type mismatches."""
    if not isinstance(raw, dict):
        raise TaskLoadError(f"top-level is {type(raw).__name__}, expected dict")

    missing_required = _REQUIRED_TASK_FIELDS - set(raw)
    if missing_required:
        raise TaskLoadError(
            f"task JSON missing required fields: {sorted(missing_required)}"
        )

    task_id = raw["task_id"]
    task_type = raw["task_type"]

    if not isinstance(task_id, str):
        raise TaskLoadError(
            f"task_id is {type(task_id).__name__}, expected str"
        )
    if not isinstance(task_type, str):
        raise TaskLoadError(
            f"task_type is {type(task_type).__name__}, expected str"
        )

    warnings: List[str] = []

    # Optional fields with defaults. Track which ones were missing so we
    # surface the recovery decision rather than silently restoring defaults.
    state = raw.get("state")
    if state is None:
        warnings.append("missing_field_defaulted:state")
        state = "spawned"
    elif state not in _TASK_STATES:
        raise TaskLoadError(
            f"state={state!r} not in {sorted(_TASK_STATES)}"
        )

    terminated_reason = raw.get("terminated_reason")
    if terminated_reason is not None and terminated_reason not in _TASK_TERMINATED_REASONS:
        raise TaskLoadError(
            f"terminated_reason={terminated_reason!r} not in "
            f"{sorted(_TASK_TERMINATED_REASONS)}"
        )

    target = raw.get("target")
    if target is not None and not isinstance(target, dict):
        raise TaskLoadError(
            f"target is {type(target).__name__}, expected dict or null"
        )

    params = raw.get("params")
    if params is None:
        warnings.append("missing_field_defaulted:params")
        params = {}
    elif not isinstance(params, dict):
        raise TaskLoadError(
            f"params is {type(params).__name__}, expected dict"
        )

    artifacts = raw.get("artifacts")
    if artifacts is None:
        warnings.append("missing_field_defaulted:artifacts")
        artifacts = []
    elif not isinstance(artifacts, list):
        raise TaskLoadError(
            f"artifacts is {type(artifacts).__name__}, expected list"
        )

    spawned_at = raw.get("spawned_at", "") or ""
    if not isinstance(spawned_at, str):
        raise TaskLoadError(
            f"spawned_at is {type(spawned_at).__name__}, expected str"
        )

    persist_to_disk = raw.get("persist_to_disk", False)
    if not isinstance(persist_to_disk, bool):
        raise TaskLoadError(
            f"persist_to_disk is {type(persist_to_disk).__name__}, expected bool"
        )

    raw_warnings = raw.get("persistence_warnings", [])
    if not isinstance(raw_warnings, list):
        raise TaskLoadError(
            f"persistence_warnings is {type(raw_warnings).__name__}, expected list"
        )
    for w in raw_warnings:
        if not isinstance(w, str):
            raise TaskLoadError(
                "persistence_warnings entries must be str; "
                f"found {type(w).__name__}"
            )
    # Pre-existing warnings from a prior load that wasn't acknowledged
    # carry forward. They join any new ones from this load.
    warnings = list(raw_warnings) + warnings

    # Tab bindings — typed rebuild with their own validation.
    raw_bindings = raw.get("tab_bindings", [])
    if not isinstance(raw_bindings, list):
        raise TaskLoadError(
            f"tab_bindings is {type(raw_bindings).__name__}, expected list"
        )
    tab_bindings: List[TaskTabBinding] = []
    for i, raw_b in enumerate(raw_bindings):
        b = _build_binding(raw_b, warnings, i)
        if b is not None:
            tab_bindings.append(b)

    # Surface unknown top-level fields. Schema marker is expected and ignored.
    known_top = {
        "_schema", "task_id", "task_type", "state", "target", "params",
        "artifacts", "tab_bindings", "terminated_reason", "spawned_at",
        "persist_to_disk", "persistence_warnings",
    }
    for key in raw:
        if key not in known_top:
            warnings.append(f"unknown_field:{key}")

    return Task(
        task_id=task_id,
        task_type=task_type,
        state=state,
        target=target,
        params=params,
        artifacts=artifacts,
        tab_bindings=tab_bindings,
        terminated_reason=terminated_reason,
        spawned_at=spawned_at,
        persist_to_disk=persist_to_disk,
        persistence_warnings=warnings,
    )


# ─── Atomic file write ───────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to ``path`` atomically: serialize to a temp file in the
    same directory, fsync, then ``os.replace()`` onto the final name. A
    crash mid-write leaves the previous file (or no file) intact — never
    a half-written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # On any failure, scrub the tmp file so we don't leave debris.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_index_line(base_path: Path, task: Task) -> None:
    """Append a single line to ``<base>/_index.jsonl`` recording this
    save. One JSON object per line — task_id, state, terminated_reason,
    ts (ISO 8601 UTC microsecond). Append-only; never rewritten in v0
    (rotation/compaction is a later cycle).

    Soft-failure: an index write failure does NOT block the save. The
    primary task JSON is already on disk; the index is a debugging
    convenience."""
    ts_iso = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
    )
    entry = {
        "ts": ts_iso,
        "task_id": task.task_id,
        "state": task.state,
        "terminated_reason": task.terminated_reason,
    }
    try:
        base_path.mkdir(parents=True, exist_ok=True)
        with open(base_path / INDEX_FILENAME, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Soft-fail — primary save already landed.
        pass


# ─── Public API ──────────────────────────────────────────────────────


def save_task(task: Task, base_path: Optional[Path] = None) -> Path:
    """Persist a task to disk. Returns the path the JSON was written to.

    Atomic. Writes ``<base>/<task_id>.json`` then appends a line to
    ``<base>/_index.jsonl``. Caller (dispatcher) gates on
    ``task.persist_to_disk`` — this function will save regardless of that
    flag, so the gate decision lives at the call site, not here."""
    base = Path(base_path) if base_path is not None else DEFAULT_BASE_PATH
    path = base / f"{task.task_id}.json"
    payload = _task_to_dict(task)
    _atomic_write_json(path, payload)
    _append_index_line(base, task)
    return path


def load_task(task_id: str, base_path: Optional[Path] = None) -> Optional[Task]:
    """Load a task from disk. Returns the rebuilt Task, or None if no
    file exists for ``task_id``. Raises ``TaskLoadError`` on type
    mismatch / missing-required-field / malformed JSON.

    Any unknown fields or missing-but-defaulted optional fields surface
    via the returned Task's ``persistence_warnings`` list. The dispatcher
    precondition will block advance() until those are explicitly cleared
    via ``confirm_recovered``."""
    base = Path(base_path) if base_path is not None else DEFAULT_BASE_PATH
    path = base / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise TaskLoadError(f"malformed JSON in {path}: {e}") from e
    return _dict_to_task(raw)


def confirm_recovered(
    task: Task,
    acknowledged_warnings: List[str],
    base_path: Optional[Path] = None,
) -> List[str]:
    """Clear the warnings from ``task.persistence_warnings`` that exactly
    match entries in ``acknowledged_warnings``. Returns the list of
    warnings still outstanding after the clear.

    Re-saves the task if anything was cleared and ``persist_to_disk`` is
    on. Exact-match only — partial / pattern matches are deliberately not
    supported in v0. If the warning text changes shape later, the
    confirm call has to be updated alongside; surfacing that drift is the
    point."""
    ack_set = set(acknowledged_warnings or [])
    before = list(task.persistence_warnings)
    cleared_any = False
    remaining: List[str] = []
    for w in before:
        if w in ack_set:
            cleared_any = True
        else:
            remaining.append(w)
    task.persistence_warnings = remaining

    if cleared_any and task.persist_to_disk:
        save_task(task, base_path=base_path)
    return remaining


__all__ = [
    "DEFAULT_BASE_PATH",
    "INDEX_FILENAME",
    "TaskLoadError",
    "save_task",
    "load_task",
    "confirm_recovered",
]
