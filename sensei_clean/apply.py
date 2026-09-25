"""
Sensei cleanup apply / undo orchestrator.

Safety contract (was the three highest-severity gaps from the briefing):

  1. Every action goes through policy.can_apply() before any mutation.
     Lane/sensitivity/capability gating is enforced here, not just at
     queue time.
  2. The undo record is appended to the journal AND fsync'd to disk
     immediately after each successful mutation. A crash between
     actions leaves a complete trail for everything done so far.
  3. The adapter is responsible for resolving a unique destination
     before moving, so same-basename duplicates do not collide. The
     UndoRecord returned by the adapter reflects the actual (possibly
     uniquified) destination, not the planned one.

The journal file (undo_path) is JSONL — one UndoRecord per line, append-
only. `undo_actions()` reads the same JSONL back.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from . import policy
from .adapters.base import BaseAdapter
from .schemas import ActionRecord, ApplyResult, CapabilityReport, UndoRecord


def apply_actions(
    adapter: BaseAdapter,
    actions: Iterable[ActionRecord],
    capability: CapabilityReport,
    undo_path: str,
) -> list[ApplyResult]:
    """Apply each action only if policy.can_apply allows it. After every
    successful mutation, append the adapter's UndoRecord to undo_path and
    fsync so a crash leaves a complete reverse trail.

    Returns the full results list (one ApplyResult per input action).
    Policy- or adapter-refused actions get a success=False ApplyResult
    with a clear message; the journal is unaffected for those entries.
    """
    results: list[ApplyResult] = []
    undo_dir = Path(undo_path).parent
    if undo_dir:
        undo_dir.mkdir(parents=True, exist_ok=True)
    # JSONL append-only. Open in line-buffered mode so a crash mid-write
    # leaves at most one partial line, which json.loads() will skip.
    with open(undo_path, "a", buffering=1) as journal:
        for action in actions:
            if not policy.can_apply(capability, action):
                results.append(
                    ApplyResult(
                        action_id=action.action_id,
                        success=False,
                        message=f"policy refused: capability={capability.capability} "
                        f"action_type={action.action_type} "
                        f"sensitivity={action.metadata.get('sensitivity', 'unknown')}",
                    )
                )
                continue
            if not adapter.can_apply(action):
                results.append(
                    ApplyResult(
                        action_id=action.action_id,
                        success=False,
                        message=f"adapter refused: {adapter.name} cannot run {action.action_type}",
                    )
                )
                continue
            result = adapter.apply(action)
            results.append(result)
            if result.success and result.undo_record is not None:
                journal.write(json.dumps(result.undo_record.to_dict()) + "\n")
                journal.flush()
                try:
                    os.fsync(journal.fileno())
                except OSError:
                    pass  # best-effort durability; not a fatal error
    return results


def load_undo_records(undo_path: str) -> list[UndoRecord]:
    """Read the JSONL undo journal back into UndoRecord objects.
    Malformed/partial lines are skipped (forward-compatibility + crash
    tolerance)."""
    records: list[UndoRecord] = []
    p = Path(undo_path)
    if not p.exists():
        return records
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            records.append(UndoRecord(**obj))
        except TypeError:
            continue
    return records


def undo_actions(
    adapter: BaseAdapter, undo_records: Iterable[UndoRecord]
) -> list[ApplyResult]:
    """Reverse each move in the journal. Caller decides order (newest-first
    is the usual call for partial undos)."""
    return [adapter.undo(record) for record in undo_records]
