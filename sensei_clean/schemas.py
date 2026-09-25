from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _require(value: Any, name: str) -> None:
    if value is None or value == "":
        raise ValueError(f"{name} is required")


@dataclass
class AccessGrant:
    mode: str
    granted: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityReport:
    adapter: str
    provider: str
    capability: str
    account_label: str
    root: str
    available: bool
    supported_actions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(self.adapter, "adapter")
        _require(self.provider, "provider")
        _require(self.capability, "capability")
        _require(self.root, "root")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class ItemRecord:
    schema_version: str
    run_id: str
    item_id: str
    source: dict[str, Any]
    identity: dict[str, Any]
    kind: str
    display_name: str
    mime: str
    size_bytes: int
    timestamps: dict[str, Any]
    hashes: dict[str, Any]
    features: dict[str, Any]
    sensitivity: str
    category_guess: str
    confidence: float
    risk: int
    reversible_actions: list[str]
    required_access: list[str]
    dependencies: list[str]
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(self.schema_version, "schema_version")
        _require(self.run_id, "run_id")
        _require(self.item_id, "item_id")
        _require(self.kind, "kind")
        _require(self.display_name, "display_name")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be >= 0")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.risk <= 100:
            raise ValueError("risk must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class FindingRecord:
    schema_version: str
    run_id: str
    finding_id: str
    finding_type: str
    item_ids: list[str]
    confidence: float
    risk: int
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(self.schema_version, "schema_version")
        _require(self.run_id, "run_id")
        _require(self.finding_id, "finding_id")
        _require(self.finding_type, "finding_type")
        if not self.item_ids:
            raise ValueError("item_ids must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.risk <= 100:
            raise ValueError("risk must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class ActionRecord:
    schema_version: str
    run_id: str
    action_id: str
    action_type: str
    adapter: str
    item_id: str
    source_path: str
    destination_path: str | None
    confidence: float
    risk: int
    reversible: bool
    lane: str
    reason: str
    approval_required: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _require(self.schema_version, "schema_version")
        _require(self.run_id, "run_id")
        _require(self.action_id, "action_id")
        _require(self.action_type, "action_type")
        _require(self.adapter, "adapter")
        _require(self.item_id, "item_id")
        _require(self.source_path, "source_path")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.risk <= 100:
            raise ValueError("risk must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class UndoRecord:
    schema_version: str
    run_id: str
    undo_id: str
    adapter: str
    action_id: str
    source_path: str
    destination_path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _require(self.schema_version, "schema_version")
        _require(self.run_id, "run_id")
        _require(self.undo_id, "undo_id")
        _require(self.adapter, "adapter")
        _require(self.action_id, "action_id")
        _require(self.source_path, "source_path")
        _require(self.destination_path, "destination_path")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class ApplyResult:
    action_id: str
    success: bool
    message: str
    undo_record: UndoRecord | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.undo_record is not None:
            data["undo_record"] = self.undo_record.to_dict()
        return data
