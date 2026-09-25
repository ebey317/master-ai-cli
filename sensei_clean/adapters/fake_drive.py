"""
In-memory fake cloud drive adapter.

Mirrors the shape that a real Google Drive / OneDrive / Dropbox
connector takes so the engine can be tested end-to-end without
hitting any network or requiring OAuth setup. The spec's
"adapter contract must match the real API workflow" — this file
is the proof of that contract.

Test usage:

    from sensei_clean.adapters.fake_drive import FakeDriveAdapter, FakeFile

    files = [
        FakeFile(id="a1", name="invoice.pdf", parent="root", mime_type="application/pdf",
                 size_bytes=1234, modified="2026-04-01T12:00:00Z", sha256="abc..."),
        FakeFile(id="a2", name="invoice.pdf", parent="root", mime_type="application/pdf",
                 size_bytes=1234, modified="2026-04-02T12:00:00Z", sha256="abc..."),
    ]
    adapter = FakeDriveAdapter(run_id="r1", files=files, account_label="test@example.com")

Tokens for the fake are kept in-memory only; nothing is written to disk.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..schemas import (
    AccessGrant,
    ActionRecord,
    ApplyResult,
    ItemRecord,
    UndoRecord,
)
from .cloud_drive import CloudDriveAdapter


@dataclass
class FakeFile:
    id: str
    name: str
    parent: str = "root"
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    modified: str = "2026-01-01T00:00:00Z"
    sha256: str | None = None
    trashed: bool = False
    web_view_link: str = ""
    content: bytes = b""

    def to_item(self, run_id: str, root_label: str) -> ItemRecord:
        sensitivity, category_guess = _classify(self.name, self.mime_type)
        return ItemRecord(
            schema_version="sensei.item.v1",
            run_id=run_id,
            item_id=f"fake_drive:{self.id}",
            source={
                "adapter": "fake_drive",
                "provider": "fake_drive",
                "capability": "api",
                "account_label": "fake@example.com",
                "root": root_label,
            },
            identity={
                "path": f"fake_drive:{self.id}",
                "provider_id": self.id,
                "parent_id": self.parent,
                "relative_path": self.name,
            },
            kind="file",
            display_name=self.name,
            mime=self.mime_type,
            size_bytes=self.size_bytes,
            timestamps={"created": None, "modified": self.modified, "taken": None},
            hashes={
                "sha256": self.sha256,
                "md5": None,
                "provider_hash": self.sha256,
                "perceptual_hash": None,
            },
            features={
                "extension": _suffix(self.name),
                "dimensions": None,
                "duration_seconds": None,
                "text_snippet": None,
                "face_count": None,
                "screenshot_likely": False,
            },
            sensitivity=sensitivity,
            category_guess=category_guess,
            confidence=0.9 if self.sha256 else 0.6,
            risk=10,
            reversible_actions=["cloud_move"],
            required_access=["read_metadata"],
            dependencies=[],
            notes=[],
        )


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _classify(name: str, mime: str) -> tuple[str, str]:
    lower = name.lower()
    if any(
        t in lower for t in ("resume", "cv", "career", "cover_letter", "transcript")
    ):
        return "career", "Career"
    if any(t in lower for t in ("tax", "w2", "w-2", "paystub", "1099")):
        return "financial", "Forms"
    if mime.startswith("image/"):
        return "photos", "Photos"
    if mime.startswith("video/"):
        return "media", "Videos"
    if mime in {"application/pdf", "text/plain", "text/markdown"}:
        return "documents", "Reading"
    return "unknown", "Manual-Review"


class FakeDriveAdapter(CloudDriveAdapter):
    """Concrete in-memory provider. Implements the real-API workflow
    without any network."""

    name = "fake_drive"
    provider_id = "fake_drive"
    provider_label = "Fake Drive (test only)"

    def __init__(
        self,
        run_id: str,
        files: list[FakeFile] | None = None,
        account_label: str = "fake@example.com",
        root: str = "fake_drive:root",
    ) -> None:
        super().__init__(run_id=run_id, root=root)
        self._files: dict[str, FakeFile] = {f.id: f for f in (files or [])}
        self._account = account_label
        self._configured = True

    @classmethod
    def is_configured(cls) -> bool:
        # Class-level default. Per-instance overrides via constructor.
        return True

    def _account_label(self) -> str:
        return self._account

    def authorize(self, mode: str) -> AccessGrant:
        return AccessGrant(
            mode=mode,
            granted=True,
            details={
                "provider": self.provider_id,
                "account_label": self._account,
            },
        )

    def scan(self, cursor: str | None = None) -> Iterator[ItemRecord]:
        for f in self._files.values():
            if f.trashed:
                continue
            yield f.to_item(self.run_id, self.root)

    def enrich(self, item: ItemRecord, jobs: list[str]) -> ItemRecord:
        # For the fake, we already populated sha256 and metadata at
        # construction time. Real providers would call out to the API
        # here (e.g. Drive's get(fileId=..., fields="md5Checksum,..."))
        return item

    def apply(self, action: ActionRecord) -> ApplyResult:
        if not self.can_apply(action):
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"fake_drive does not support {action.action_type}",
            )
        file_id = action.source_path.split(":", 1)[-1]
        if file_id not in self._files:
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"file not found in fake drive: {file_id}",
            )
        original_parent = self._files[file_id].parent
        new_parent = (action.destination_path or "").split("/", 1)[0] or "Quarantine"
        self._files[file_id].parent = new_parent
        undo = UndoRecord(
            schema_version="sensei.undo.v1",
            run_id=action.run_id,
            undo_id=f"undo:{action.action_id}",
            adapter=self.name,
            action_id=action.action_id,
            source_path=action.source_path,
            destination_path=original_parent,
            metadata={"reverse_parent": original_parent},
        )
        return ApplyResult(
            action_id=action.action_id,
            success=True,
            message=f"moved {file_id} to {new_parent}",
            undo_record=undo,
        )

    def undo(self, undo_record: UndoRecord) -> ApplyResult:
        file_id = undo_record.source_path.split(":", 1)[-1]
        if file_id not in self._files:
            return ApplyResult(
                action_id=undo_record.action_id,
                success=False,
                message=f"file gone: {file_id}",
            )
        target = (undo_record.metadata or {}).get(
            "reverse_parent"
        ) or undo_record.destination_path
        self._files[file_id].parent = target
        return ApplyResult(
            action_id=undo_record.action_id,
            success=True,
            message=f"restored {file_id} to {target}",
        )

    def open_view(self, item: ItemRecord) -> str:
        fid = item.identity.get("provider_id", "")
        return (
            self._files.get(fid, FakeFile(id=fid, name="?")).web_view_link
            or f"fake://view/{fid}"
        )
