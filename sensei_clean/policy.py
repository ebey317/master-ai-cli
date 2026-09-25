from __future__ import annotations

from .schemas import ActionRecord, CapabilityReport, ItemRecord

MONITORED_SENSITIVITIES = {
    "private",
    "financial",
    "medical",
    "identity",
    "credential",
    "career",
}


def allowed_actions(capability: str, sensitivity: str) -> list[str]:
    if capability == "api":
        # cloud_move is in-provider (Sensei-Cloud-Quarantine/) only.
        # No delete, no cross-remote moves — those are refused in the
        # adapter regardless of this list.
        return ["scan", "enrich", "organize", "cloud_move"]
    if capability == "local":
        return ["scan", "enrich", "archive_move", "quarantine_move"]
    if capability == "export":
        return ["scan", "enrich", "archive_move"]
    if capability == "browser":
        return ["scan", "export"]
    return []


def requires_monitored_review(item: ItemRecord, capability: str) -> bool:
    if capability == "browser":
        return True
    if capability == "api":
        # All cloud actions stay in the monitored lane this round — the
        # customer must explicitly approve mutations against their cloud.
        return True
    if item.sensitivity in MONITORED_SENSITIVITIES:
        return True
    if item.confidence < 0.75:
        return True
    return False


# Any action that contains the substring "delete" is refused outright,
# regardless of capability. This is the project-wide hard rule: no
# destructive deletes in v1. Subclasses with a tested delete path would
# need to override this contract explicitly.
_DELETE_ACTION_TYPES = {"delete", "cloud_delete", "trash", "permanent_delete"}


def can_apply(capability: CapabilityReport, action: ActionRecord) -> bool:
    if not capability.available:
        return False
    if action.action_type in _DELETE_ACTION_TYPES:
        return False
    return action.action_type in allowed_actions(
        capability.capability, action.metadata.get("sensitivity", "unknown")
    )
