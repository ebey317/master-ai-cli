from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .adapters.base import BaseAdapter
from .adapters.local_fs import LocalFSAdapter
from .apply import apply_actions, undo_actions
from .schemas import ActionRecord, ApplyResult, CapabilityReport, UndoRecord

DEFAULT_LOCAL_ROOTS = [
    "~/Desktop",
    "~/Downloads",
    "~/Documents",
    "~/Pictures",
    "~/Videos",
]


def build_adapter(adapter_name: str, run_id: str) -> BaseAdapter:
    if adapter_name.startswith("rclone:"):
        from .adapters.rclone_remote import RcloneRemoteAdapter

        remote = adapter_name.split(":", 1)[1]
        return RcloneRemoteAdapter(run_id=run_id, remote=remote, list_enabled=True)
    return LocalFSAdapter(
        run_id=run_id,
        roots=DEFAULT_LOCAL_ROOTS,
        quarantine_root=str(Path("~/Sensei-Quarantine").expanduser().resolve()),
    )


def capability_for(
    adapter_name: str, capabilities: list[CapabilityReport]
) -> CapabilityReport | None:
    for capability in capabilities:
        if capability.adapter == adapter_name:
            return capability
    return capabilities[0] if capabilities else None


def apply_per_adapter(
    actions: Iterable[ActionRecord],
    capabilities: list[CapabilityReport],
    undo_path: str,
) -> list[ApplyResult]:
    groups: dict[str, list[ActionRecord]] = defaultdict(list)
    for action in actions:
        groups[action.adapter].append(action)

    results: list[ApplyResult] = []
    for adapter_name, group_actions in groups.items():
        cap = capability_for(adapter_name, capabilities)
        if cap is None:
            results.extend(
                [
                    ApplyResult(
                        action_id=action.action_id,
                        success=False,
                        message=f"no source connection for {adapter_name}",
                    )
                    for action in group_actions
                ]
            )
            continue
        adapter = build_adapter(adapter_name, group_actions[0].run_id)
        results.extend(apply_actions(adapter, group_actions, cap, undo_path))
    return results


def undo_per_adapter(records: Iterable[UndoRecord]) -> list[ApplyResult]:
    adapters: dict[str, BaseAdapter] = {}
    results: list[ApplyResult] = []
    for record in records:
        if record.adapter not in adapters:
            adapters[record.adapter] = build_adapter(record.adapter, record.run_id)
        adapter = adapters[record.adapter]
        results.extend(undo_actions(adapter, [record]))
    return results
