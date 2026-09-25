from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from ..schemas import (
    AccessGrant,
    ActionRecord,
    ApplyResult,
    CapabilityReport,
    ItemRecord,
    UndoRecord,
)
from .base import BaseAdapter


class LocalFSAdapter(BaseAdapter):
    name = "local_fs"

    def __init__(self, run_id: str, roots: list[str], quarantine_root: str) -> None:
        self.run_id = run_id
        self.roots = [Path(root).expanduser().resolve() for root in roots]
        self.quarantine_root = Path(quarantine_root).expanduser().resolve()

    def probe(self) -> CapabilityReport:
        available_roots = [str(root) for root in self.roots if root.exists()]
        blockers = []
        if not available_roots:
            blockers.append("no-roots-available")
        return CapabilityReport(
            adapter=self.name,
            provider="local",
            capability="local",
            account_label=os.environ.get("USER", "local-user"),
            root=";".join(available_roots) or "local",
            available=bool(available_roots),
            supported_actions=["archive_move", "quarantine_move"],
            blockers=blockers,
            notes=["read-only scan unless apply is explicitly requested"],
        )

    def authorize(self, mode: str) -> AccessGrant:
        return AccessGrant(mode=mode, granted=True, details={"capability": "local"})

    def scan(self, cursor: str | None = None) -> Iterator[ItemRecord]:
        del cursor
        for root in self.roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                yield self._item_for_path(path, root)

    def enrich(self, item: ItemRecord, jobs: list[str]) -> ItemRecord:
        path = Path(item.identity["path"])
        hashes = dict(item.hashes)
        features = dict(item.features)
        notes = list(item.notes)

        if "sha256" in jobs and hashes.get("sha256") is None:
            hashes["sha256"] = self._sha256(path)
        if "screenshot" in jobs:
            features["screenshot_likely"] = self._is_screenshot(item.display_name)
        if "text_snippet" in jobs and path.suffix.lower() in {".txt", ".md"}:
            try:
                features["text_snippet"] = path.read_text(errors="ignore")[:280]
            except OSError as exc:
                notes.append(f"text-snippet-failed:{exc}")

        return replace(item, hashes=hashes, features=features, notes=notes)

    def can_apply(self, action: ActionRecord) -> bool:
        return action.adapter == self.name and action.action_type in {
            "archive_move",
            "quarantine_move",
        }

    def apply(self, action: ActionRecord) -> ApplyResult:
        if not self.can_apply(action):
            return ApplyResult(
                action_id=action.action_id, success=False, message="unsupported action"
            )

        source = Path(action.source_path)
        if action.destination_path is None:
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message="missing destination_path",
            )
        if not source.exists():
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"source missing: {source}",
            )
        intended_dest = Path(action.destination_path)
        intended_dest.parent.mkdir(parents=True, exist_ok=True)
        # Uniquify: never overwrite an existing file at the destination.
        # Same-basename duplicates from the same quarantine pass need a
        # numeric suffix so they don't collide silently.
        actual_dest = self._unique_destination(intended_dest)
        try:
            shutil.move(str(source), str(actual_dest))
        except (OSError, shutil.Error) as exc:
            return ApplyResult(
                action_id=action.action_id, success=False, message=f"move failed: {exc}"
            )
        undo = UndoRecord(
            schema_version="sensei.undo.v1",
            run_id=action.run_id,
            undo_id=f"undo:{action.action_id}",
            adapter=self.name,
            action_id=action.action_id,
            source_path=str(actual_dest),
            destination_path=str(source),
        )
        return ApplyResult(
            action_id=action.action_id,
            success=True,
            message=f"moved to {actual_dest}",
            undo_record=undo,
        )

    def _unique_destination(self, dest: Path) -> Path:
        """If dest doesn't exist, return it unchanged. Otherwise return
        a sibling path with a numeric suffix: foo.txt -> foo (2).txt ->
        foo (3).txt etc. Bounded at 9999 to prevent runaway loops on a
        pathological directory."""
        if not dest.exists():
            return dest
        stem = dest.stem
        suffix = dest.suffix
        parent = dest.parent
        for n in range(2, 10000):
            candidate = parent / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
        # Pathological case — return a hash-suffixed name as last resort
        return (
            parent
            / f"{stem}.{hashlib.sha1(str(dest).encode()).hexdigest()[:8]}{suffix}"
        )

    def open_view(self, item: ItemRecord) -> str:
        """Local 'view' is the file's own path. The CLI's `open`
        subcommand and the review.html link generator use this to
        spawn xdg-open. Returns "" when the item has no resolvable
        local path."""
        path = (item.identity or {}).get("path", "")
        if path and not path.startswith(("rclone:", "http://", "https://")):
            return path
        return ""

    def undo(self, undo_record: UndoRecord) -> ApplyResult:
        source = Path(undo_record.source_path)
        destination = Path(undo_record.destination_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return ApplyResult(
            action_id=undo_record.action_id,
            success=True,
            message="undone",
        )

    def _item_for_path(self, path: Path, root: Path) -> ItemRecord:
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rel_path = path.relative_to(root)
        sensitivity, category_guess = self._classify(path, mime)
        confidence = self._confidence(path, mime, category_guess)
        risk = self._risk(sensitivity, confidence)
        return ItemRecord(
            schema_version="sensei.item.v1",
            run_id=self.run_id,
            item_id=self._stable_id(path),
            source={
                "adapter": self.name,
                "provider": "local",
                "capability": "local",
                "account_label": os.environ.get("USER", "local-user"),
                "root": str(root),
            },
            identity={
                "path": str(path),
                "provider_id": str(path),
                "parent_id": str(path.parent),
                "relative_path": str(rel_path),
            },
            kind="file",
            display_name=path.name,
            mime=mime,
            size_bytes=stat.st_size,
            timestamps={
                "created": None,
                "modified": self._iso(stat.st_mtime),
                "taken": None,
            },
            hashes={
                "sha256": None,
                "md5": None,
                "provider_hash": None,
                "perceptual_hash": None,
            },
            features={
                "extension": path.suffix.lower(),
                "dimensions": None,
                "duration_seconds": None,
                "text_snippet": None,
                "face_count": None,
                "screenshot_likely": self._is_screenshot(path.name),
            },
            sensitivity=sensitivity,
            category_guess=category_guess,
            confidence=confidence,
            risk=risk,
            reversible_actions=["archive_move", "quarantine_move"],
            required_access=["read_metadata"],
            dependencies=[],
            notes=[],
        )

    def _classify(self, path: Path, mime: str) -> tuple[str, str]:
        name = path.name.lower()
        full_path = str(path).lower()
        if any(
            token in full_path
            for token in ("resume", "cv", "career", "cover_letter", "transcript")
        ):
            return "career", "Career"
        if any(
            token in full_path
            for token in (
                "w2",
                "w-2",
                "tax",
                "paystub",
                "pay stub",
                "passport",
                "license",
            )
        ):
            return "financial", "Forms"
        if any(token in full_path for token in ("poem", "poetry", "lyrics")):
            return "creative", "Poetry"
        if any(token in full_path for token in ("private", "intimate", "nsfw", "nude")):
            return "private", "Private"
        if mime.startswith("image/"):
            return "photos", "Photos"
        if mime.startswith("video/"):
            return "media", "Videos"
        if mime.startswith("audio/"):
            return "media", "Music"
        if mime in {"application/pdf", "text/plain", "text/markdown"}:
            return "documents", "Reading"
        if path.suffix.lower() in {".doc", ".docx", ".odt", ".rtf"}:
            return "documents", "Office"
        if path.suffix.lower() in {".xls", ".xlsx", ".ods", ".csv"}:
            return "documents", "Spreadsheets"
        if path.suffix.lower() in {".ppt", ".pptx", ".odp"}:
            return "documents", "Presentations"
        if path.suffix.lower() in {".apk", ".apks", ".xapk"}:
            return "media", "Android-Apps"
        return "unknown", "Manual-Review"

    def _confidence(self, path: Path, mime: str, category_guess: str) -> float:
        if category_guess in {"Photos", "Videos", "Music"} and "/" in mime:
            return 0.96
        if category_guess in {
            "Reading",
            "Office",
            "Spreadsheets",
            "Presentations",
            "Android-Apps",
        }:
            return 0.85
        if category_guess in {"Career", "Forms", "Poetry"}:
            return 0.7
        if category_guess == "Private":
            return 0.6
        return 0.4

    def _risk(self, sensitivity: str, confidence: float) -> int:
        base = {
            "private": 90,
            "financial": 80,
            "career": 55,
            "creative": 45,
            "documents": 35,
            "photos": 40,
            "media": 30,
            "unknown": 50,
        }.get(sensitivity, 50)
        return min(100, max(0, int(base + (1.0 - confidence) * 20)))

    def _stable_id(self, path: Path) -> str:
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
        return f"{self.name}:{digest}"

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _iso(self, timestamp: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _is_screenshot(self, name: str) -> bool:
        lowered = name.lower()
        return any(
            token in lowered
            for token in ("screenshot", "screen shot", "screen_shot", "img_20")
        )
