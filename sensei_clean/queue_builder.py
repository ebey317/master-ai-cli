from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .policy import requires_monitored_review
from .ranker import score_action, score_item
from .schemas import ActionRecord, CapabilityReport, ItemRecord


def lane_for_item(item: ItemRecord, capability: str) -> str:
    if requires_monitored_review(item, capability):
        return "monitored"
    if item.confidence >= 0.95 and item.risk <= 35 and item.reversible_actions:
        return "unattended"
    return "background"


def lane_for_action(action: ActionRecord) -> str:
    if action.approval_required or action.risk >= 60 or action.confidence < 0.8:
        return "monitored"
    if action.reversible and action.confidence >= 0.95 and action.risk <= 35:
        return "unattended"
    return "background"


def build_queue(
    items: Iterable[ItemRecord],
    actions: Iterable[ActionRecord],
    capabilities: Iterable[CapabilityReport],
) -> dict[str, list[dict]]:
    capability_map = {cap.adapter: cap for cap in capabilities}
    lanes: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        capability = capability_map.get(item.source["adapter"])
        lane = lane_for_item(
            item, capability.capability if capability else "unavailable"
        )
        lanes[lane].append(
            {
                "type": "item",
                "item_id": item.item_id,
                "score": score_item(item),
                "source": item.source["adapter"],
                "category_guess": item.category_guess,
            }
        )
    for action in actions:
        lane = lane_for_action(action)
        lanes[lane].append(
            {
                "type": "action",
                "action_id": action.action_id,
                "score": score_action(action),
                "adapter": action.adapter,
                "action_type": action.action_type,
            }
        )
    for lane_name in lanes:
        lanes[lane_name].sort(key=lambda entry: entry["score"], reverse=True)
    return dict(lanes)
