"""
Cloud drive adapter contract.

Defines the surface every cloud connector (Google Drive, OneDrive,
Dropbox, ...) must implement so the engine can dispatch scan / enrich /
preview / plan_actions / apply / undo against a real API workflow.

Read-first contract:

  * No connector deletes by default. The base class' apply() only
    accepts "cloud_move" (rename / move-folder) and refuses
    "cloud_delete" unless the subclass explicitly overrides AND the
    action carries an `allow_delete=True` in metadata. Even then, the
    engine wires no UI surface for delete — that is intentional.
  * Tokens are stored under ~/.config/sensei-clean/<provider>.token
    with 0600 permissions. Subclasses must use _token_path() and
    _save_token()/_load_token() instead of rolling their own storage.
  * Probe must report `available=False` with a clear blocker when the
    connector is not configured. The customer-facing UI relies on this
    to distinguish "configurable cloud account" from "ready to scan".
  * open_view(item) returns a provider URL (e.g. drive.google.com link)
    or a local export path. It must never silently download private
    content; the export must be explicit.
"""

from __future__ import annotations

import json
import os
from abc import abstractmethod
from collections.abc import Iterator
from pathlib import Path

from ..schemas import (
    AccessGrant,
    ActionRecord,
    CapabilityReport,
    ItemRecord,
)
from .base import BaseAdapter

CONFIG_DIR = Path.home() / ".config" / "sensei-clean"


class CloudDriveAdapter(BaseAdapter):
    """Abstract cloud drive connector. Real providers (GoogleDriveAdapter,
    OneDriveAdapter, ...) subclass this; FakeDriveAdapter implements it
    in-memory for tests."""

    provider_id: str = "cloud"
    provider_label: str = "Cloud drive"

    def __init__(self, run_id: str, root: str = "") -> None:
        self.run_id = run_id
        # Roots in cloud are typically provider folder IDs, not paths.
        # Format: "<provider>:<folder_id_or_root>"
        self.root = root or f"{self.provider_id}:root"

    # ── token storage ────────────────────────────────────────────

    @classmethod
    def _token_path(cls) -> Path:
        return CONFIG_DIR / f"{cls.provider_id}.token"

    @classmethod
    def _save_token(cls, payload: dict) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        p = cls._token_path()
        p.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    @classmethod
    def _load_token(cls) -> dict:
        p = cls._token_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def is_configured(cls) -> bool:
        """Override per provider — should return True only when the
        connector can actually make API calls right now."""
        return bool(cls._load_token())

    # ── BaseAdapter required surface ─────────────────────────────

    def probe(self) -> CapabilityReport:
        configured = self.is_configured()
        blockers: list[str] = []
        if not configured:
            blockers.append(f"{self.provider_id}-not-configured")
        return CapabilityReport(
            adapter=self.name,
            provider=self.provider_id,
            capability="api",
            account_label=self._account_label() or "(not connected)",
            root=self.root,
            available=configured,
            supported_actions=["cloud_move"],  # never "cloud_delete" by default
            blockers=blockers,
            notes=[
                "API connector — runs over network, requires OAuth/token.",
                "No delete actions in the default pipeline; move/organize only.",
                "Item paths use the provider's file id, not a local filesystem path.",
            ],
        )

    @abstractmethod
    def scan(self, cursor: str | None = None) -> Iterator[ItemRecord]:
        """Yield ItemRecords for files in the configured root. Cursor
        is opaque pagination state."""
        raise NotImplementedError

    def authorize(self, mode: str) -> AccessGrant:
        """Default returns the configured status. Subclasses may override
        to run an OAuth flow."""
        return AccessGrant(
            mode=mode,
            granted=self.is_configured(),
            details={"provider": self.provider_id},
        )

    def can_apply(self, action: ActionRecord) -> bool:
        if action.adapter != self.name:
            return False
        return action.action_type == "cloud_move"

    # ── New surface for cloud-specific behavior ──────────────────

    @abstractmethod
    def open_view(self, item: ItemRecord) -> str:
        """Return a viewable URL (e.g. https://drive.google.com/...)
        or local exported preview path. Must not exfiltrate content
        silently — only metadata or explicitly-exported preview."""
        raise NotImplementedError

    def _account_label(self) -> str:
        """Optional: email or display name of the connected account.
        Subclasses override; default reads from saved token if present."""
        tok = self._load_token()
        return tok.get("account_label", "")
