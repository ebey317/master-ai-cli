"""
rclone-backed cloud adapter.

Wraps any rclone-configured remote (gdrive:, onedrive:, dropbox:, s3:,
b2:, ...) so we leverage the user's existing OAuth/auth in
~/.config/rclone/rclone.conf instead of re-implementing each provider's
SDK. rclone is treated as a stable subprocess surface; no network is
required to LIST configured remotes — only to probe quota or list
files.

# Default mode: PROBE ONLY
The instruction from this session was: "probe only account/availability
metadata first, not list private file names." This adapter honors that
explicitly. scan() yields nothing unless `list_enabled=True` is passed
at construction time. probe() runs `rclone about <remote>:` which
returns ONLY quota / account metadata — no file names, no IDs.

# Listing (opt-in via list_enabled=True)
With list_enabled=True, scan() calls
`rclone lsjson <remote>:<path> --files-only --recursive --hash`
and yields one ItemRecord per file. Hashes use whatever the provider
exposes (md5 for Drive, sha1 for many others). Provider IDs land in
identity.provider_id so apply() can resolve back to the file.

# Mutations (apply/undo)
Scope is intentionally narrow: only `cloud_move` from the source path
to `<remote>:Sensei-Cloud-Quarantine/duplicates/<basename>` (a sibling
folder INSIDE the same remote). Nothing ever leaves the provider; no
deletes. apply() uses `rclone moveto` and writes a per-action undo
record. undo() reverses the moveto using stored source/destination.

# Tests
The rclone binary call is wrapped in `_run_rclone()`. Tests can
monkey-patch that or set SENSEI_CLEAN_RCLONE to point at a fake binary
shim so the test suite never depends on real network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator

from ..schemas import (
    AccessGrant,
    ActionRecord,
    ApplyResult,
    CapabilityReport,
    ItemRecord,
    UndoRecord,
)
from .cloud_drive import CloudDriveAdapter

RCLONE_BIN_ENV = "SENSEI_CLEAN_RCLONE"
DEFAULT_TIMEOUT = 15  # seconds — network call may be slow


def _rclone_bin() -> str:
    return os.environ.get(RCLONE_BIN_ENV) or shutil.which("rclone") or "rclone"


def _run_rclone(
    argv: list[str], timeout: int = DEFAULT_TIMEOUT
) -> tuple[int, str, str]:
    """Run rclone with the given args. Returns (returncode, stdout, stderr).
    Exit code 127 + empty stdout/stderr means rclone wasn't found."""
    bin_ = _rclone_bin()
    if not shutil.which(bin_):
        return 127, "", "rclone not on PATH"
    try:
        out = subprocess.run(
            [bin_, *argv], capture_output=True, text=True, timeout=timeout
        )
        return out.returncode, out.stdout, out.stderr
    except subprocess.TimeoutExpired as e:
        return 124, "", f"rclone timed out after {timeout}s: {e}"
    except OSError as e:
        return 1, "", f"rclone exec failed: {e}"


def rclone_listremotes(timeout: int = 5) -> list[str]:
    """Return configured rclone remote names (without trailing colon).
    Empty list when rclone is missing or fails — never raises."""
    rc, out, _ = _run_rclone(["listremotes"], timeout=timeout)
    if rc != 0:
        return []
    return [line.rstrip(":") for line in out.splitlines() if line.endswith(":")]


def rclone_lsjson(remote: str, path: str = "", timeout: int = 120) -> list[dict]:
    """Run `rclone lsjson <remote>:<path> --files-only --recursive --hash`
    and return the parsed list. Empty list on any error — never raises.
    Caller is the only path that touches actual file names; the rest of
    the adapter is probe-only."""
    target = f"{remote.rstrip(':')}:{path}".rstrip(":")
    if not target.endswith(":"):
        # Restore the colon-suffix that rclone expects for the root.
        if ":" not in target:
            target = f"{target}:"
    rc, out, _err = _run_rclone(
        ["lsjson", target, "--files-only", "--recursive", "--hash"],
        timeout=timeout,
    )
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def rclone_moveto(src_spec: str, dst_spec: str, timeout: int = 120) -> tuple[bool, str]:
    """Run `rclone moveto SRC DST`. SRC and DST must be full
    `<remote>:<path>` specs. Returns (ok, message)."""
    rc, _out, err = _run_rclone(["moveto", src_spec, dst_spec], timeout=timeout)
    if rc == 0:
        return True, f"moved {src_spec} -> {dst_spec}"
    return False, f"rclone moveto rc={rc}: {(err or '').strip()[:240]}"


def rclone_about(remote: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run `rclone about <remote>: --json` and return parsed JSON. On
    any error returns a dict with an 'error' key — never raises. Reads
    only account/quota metadata, never file names."""
    rc, out, err = _run_rclone(
        ["about", f"{remote.rstrip(':')}:", "--json"], timeout=timeout
    )
    if rc != 0:
        return {"error": (err or out).strip()[:240] or f"rc={rc}"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "non-json output from rclone about"}


class RcloneRemoteAdapter(CloudDriveAdapter):
    """One adapter per rclone remote. Probe-only by default."""

    def __init__(
        self,
        run_id: str,
        remote: str,
        *,
        list_enabled: bool = False,
        path_in_remote: str = "",
    ) -> None:
        clean_remote = remote.rstrip(":")
        self.remote = clean_remote
        self.list_enabled = list_enabled
        self.path_in_remote = path_in_remote
        super().__init__(run_id=run_id, root=f"rclone:{clean_remote}:{path_in_remote}")
        # Per-instance name so action.adapter routes back to the right remote.
        self.name = f"rclone:{clean_remote}"
        self.provider_id = f"rclone-{clean_remote}"
        self.provider_label = f"rclone:{clean_remote}"

    @classmethod
    def is_configured(cls) -> bool:
        # Class-level cheap check: rclone is present and at least one
        # remote is configured. Per-instance probe verifies the specific
        # remote actually authenticates.
        return bool(shutil.which(_rclone_bin())) and bool(rclone_listremotes())

    def probe(self) -> CapabilityReport:
        blockers: list[str] = []
        if not shutil.which(_rclone_bin()):
            blockers.append("rclone-not-installed")
            account_info: dict = {}
        else:
            account_info = rclone_about(self.remote)
            if "error" in account_info:
                blockers.append(f"rclone-about-failed: {account_info['error']}")
        used = account_info.get("used")
        total = account_info.get("total")
        quota_note = (
            f"Quota: {used} used of {total} bytes"
            if isinstance(used, int) and isinstance(total, int) and total
            else "Quota unknown"
        )
        return CapabilityReport(
            adapter=self.name,
            provider=self.provider_id,
            capability="api",
            account_label=self.remote,
            root=self.root,
            available=not blockers,
            supported_actions=["cloud_move"],
            blockers=blockers,
            notes=[
                f"rclone remote {self.remote!r} (uses ~/.config/rclone/rclone.conf).",
                "Probe-only: scan yields nothing unless list_enabled=True at construction.",
                "cloud_move is wired through rclone moveto and always requires monitored approval.",
                "Deletes are not wired.",
                quota_note,
            ],
        )

    def authorize(self, mode: str) -> AccessGrant:
        info = rclone_about(self.remote)
        granted = "error" not in info and bool(info)
        return AccessGrant(
            mode=mode,
            granted=granted,
            details={
                "provider": self.provider_id,
                "remote": self.remote,
                "rclone_about_keys": sorted(info.keys()) if info else [],
            },
        )

    # ── scan ─────────────────────────────────────────────────────

    def scan(self, cursor: str | None = None) -> Iterator[ItemRecord]:
        """Probe-only by default. With list_enabled=True, calls
        `rclone lsjson` and yields one ItemRecord per file."""
        if not self.list_enabled:
            return
            yield  # pragma: no cover — keeps generator shape
        records = rclone_lsjson(self.remote, self.path_in_remote)
        for r in records:
            try:
                yield self._record_to_item(r)
            except Exception:
                # Bad row from rclone — skip rather than fail the whole scan.
                continue

    def _record_to_item(self, r: dict) -> ItemRecord:
        rel_path = str(r.get("Path", "") or "")
        name = str(r.get("Name", "") or rel_path.rsplit("/", 1)[-1])
        size = int(r.get("Size", 0) or 0)
        mime = str(r.get("MimeType", "") or "application/octet-stream")
        mod = str(r.get("ModTime", "") or "")
        provider_id = str(r.get("ID", "") or rel_path)
        hashes_in = r.get("Hashes") or {}
        md5 = hashes_in.get("md5") or hashes_in.get("MD5")
        sha1 = hashes_in.get("sha1") or hashes_in.get("SHA-1")
        hashes_out = {
            "sha256": None,
            "md5": md5,
            "provider_hash": md5 or sha1,
            "perceptual_hash": None,
        }
        if sha1:
            hashes_out["sha1"] = sha1
        sensitivity, category = _classify_cloud(name, mime, rel_path)
        return ItemRecord(
            schema_version="sensei.item.v1",
            run_id=self.run_id,
            item_id=f"rclone:{self.remote}:{provider_id}",
            source={
                "adapter": self.name,
                "provider": self.provider_id,
                "capability": "api",
                "account_label": self.remote,
                "root": self.root,
            },
            identity={
                "path": f"rclone:{self.remote}:{rel_path}",
                "provider_id": provider_id,
                "parent_id": rel_path.rsplit("/", 1)[0] if "/" in rel_path else "",
                "relative_path": rel_path,
            },
            kind="file",
            display_name=name,
            mime=mime,
            size_bytes=size,
            timestamps={"created": None, "modified": mod or None, "taken": None},
            hashes=hashes_out,
            features={
                "extension": _suffix(name),
                "dimensions": None,
                "duration_seconds": None,
                "text_snippet": None,
                "face_count": None,
                "screenshot_likely": False,
            },
            sensitivity=sensitivity,
            category_guess=category,
            confidence=0.9 if (md5 or sha1) else 0.5,
            risk=10,
            reversible_actions=["cloud_move"],
            required_access=["read_metadata"],
            dependencies=[],
            notes=[],
        )

    def enrich(self, item: ItemRecord, jobs: list[str]) -> ItemRecord:
        # Cloud hashes already came from lsjson --hash; no further
        # enrichment required this round.
        return item

    # ── apply / undo ─────────────────────────────────────────────

    QUARANTINE_FOLDER = "Sensei-Cloud-Quarantine/duplicates"

    @classmethod
    def cloud_quarantine_destination(cls, remote: str, basename: str) -> str:
        """Canonical cloud destination spec for engine.build_actions.
        Keeps the quarantine folder name in one place."""
        return f"rclone:{remote.rstrip(':')}:{cls.QUARANTINE_FOLDER}/{basename}"

    def apply(self, action: ActionRecord) -> ApplyResult:
        if not self.can_apply(action):
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"{self.name} cannot run {action.action_type}",
            )
        if not action.source_path.startswith("rclone:"):
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"non-rclone source: {action.source_path}",
            )
        if not (action.destination_path or "").startswith("rclone:"):
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"non-rclone destination: {action.destination_path}",
            )
        # Refuse cross-remote moves and any move that would leave the
        # configured remote — nothing leaves the provider.
        src_remote = action.source_path[len("rclone:") :].split(":", 1)[0]
        dst_remote = action.destination_path[len("rclone:") :].split(":", 1)[0]
        if src_remote != self.remote or dst_remote != self.remote:
            return ApplyResult(
                action_id=action.action_id,
                success=False,
                message=f"cross-remote move refused: {src_remote}->{dst_remote}",
            )
        src_spec = action.source_path[len("rclone:") :]
        dst_spec = action.destination_path[len("rclone:") :]
        ok, msg = rclone_moveto(src_spec, dst_spec)
        if not ok:
            return ApplyResult(action_id=action.action_id, success=False, message=msg)
        undo = UndoRecord(
            schema_version="sensei.undo.v1",
            run_id=action.run_id,
            undo_id=f"undo:{action.action_id}",
            adapter=self.name,
            action_id=action.action_id,
            source_path=action.destination_path,
            destination_path=action.source_path,
            metadata={"remote": self.remote},
        )
        return ApplyResult(
            action_id=action.action_id, success=True, message=msg, undo_record=undo
        )

    def undo(self, undo_record: UndoRecord) -> ApplyResult:
        if not undo_record.source_path.startswith("rclone:"):
            return ApplyResult(
                action_id=undo_record.action_id,
                success=False,
                message=f"non-rclone undo source: {undo_record.source_path}",
            )
        if not undo_record.destination_path.startswith("rclone:"):
            return ApplyResult(
                action_id=undo_record.action_id,
                success=False,
                message=f"non-rclone undo destination: {undo_record.destination_path}",
            )
        src_spec = undo_record.source_path[len("rclone:") :]
        dst_spec = undo_record.destination_path[len("rclone:") :]
        ok, msg = rclone_moveto(src_spec, dst_spec)
        if not ok:
            return ApplyResult(
                action_id=undo_record.action_id, success=False, message=msg
            )
        return ApplyResult(
            action_id=undo_record.action_id, success=True, message=f"restored: {msg}"
        )

    def open_view(self, item: ItemRecord) -> str:
        if self.remote in {"gdrive", "drive"}:
            fid = item.identity.get("provider_id", "")
            if fid and "/" not in fid:
                return f"https://drive.google.com/file/d/{fid}/view"
        return ""


# ── module-level helpers ──────────────────────────────────────────


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _classify_cloud(name: str, mime: str, rel_path: str) -> tuple[str, str]:
    """Same shape as LocalFSAdapter._classify but path-aware for
    cloud-style relative paths (no /home prefix)."""
    lower = (name + "/" + rel_path).lower()
    if any(
        t in lower for t in ("resume", "cv", "career", "cover_letter", "transcript")
    ):
        return "career", "Career"
    if any(
        t in lower
        for t in ("tax", "w2", "w-2", "paystub", "1099", "passport", "license")
    ):
        return "financial", "Forms"
    if any(t in lower for t in ("private", "intimate", "nsfw")):
        return "private", "Private"
    if mime.startswith("image/"):
        return "photos", "Photos"
    if mime.startswith("video/"):
        return "media", "Videos"
    if mime.startswith("audio/"):
        return "media", "Music"
    if mime in {"application/pdf", "text/plain", "text/markdown"}:
        return "documents", "Reading"
    suf = _suffix(name)
    if suf in {".doc", ".docx", ".odt", ".rtf"}:
        return "documents", "Office"
    return "unknown", "Manual-Review"
