"""
Google Drive adapter — stub.

HONEST STATE (2026-05-11): this file ships the contract a real Google
Drive connector will fulfill, plus a probe() that surfaces a clear
"not-configured" signal in the UI. It does NOT make any real Google
Drive API calls yet. The real wiring needs:

  1. The user registers an OAuth client at
     https://console.cloud.google.com/apis/credentials and downloads
     client_secret.json to ~/.config/sensei-clean/gdrive_client.json
  2. `pip install --user google-api-python-client google-auth-oauthlib`
     (the imports below are guarded so we don't crash if these are not
     installed yet — probe() reports the missing dep).
  3. Run `sensei-clean connect gdrive` (TODO — UI hook), which does the
     OAuth consent flow once and writes the refresh token to
     ~/.config/sensei-clean/gdrive.token (0600).

Until those three steps land, this adapter is intentionally inert:

  * probe() returns available=False with the specific missing piece
    (no deps / no client / no token).
  * scan() yields nothing.
  * apply() refuses with a clear message.

Once auth lands, scan() will use Drive v3's files.list with fields like
id,name,parents,mimeType,size,modifiedTime,md5Checksum,trashed and
yield ItemRecords; apply() will use files.update with addParents/
removeParents for cloud_move. No delete is wired by design.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..schemas import (
    AccessGrant,
    ActionRecord,
    ApplyResult,
    CapabilityReport,
    ItemRecord,
    UndoRecord,
)
from .cloud_drive import CONFIG_DIR, CloudDriveAdapter

CLIENT_SECRET_FILE = CONFIG_DIR / "gdrive_client.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.file",  # only files the app touches
]


def _deps_present() -> bool:
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401

        return True
    except Exception:
        return False


class GoogleDriveAdapter(CloudDriveAdapter):
    name = "gdrive"
    provider_id = "gdrive"
    provider_label = "Google Drive"

    @classmethod
    def is_configured(cls) -> bool:
        return _deps_present() and cls._token_path().exists()

    def probe(self) -> CapabilityReport:
        blockers: list[str] = []
        if not _deps_present():
            blockers.append(
                "gdrive-missing-deps: pip install --user "
                "google-api-python-client google-auth-oauthlib"
            )
        if not CLIENT_SECRET_FILE.exists():
            blockers.append(
                f"gdrive-missing-client-secret: place file at {CLIENT_SECRET_FILE}"
            )
        if not self._token_path().exists():
            blockers.append("gdrive-not-authorized: run `sensei-clean connect gdrive`")
        return CapabilityReport(
            adapter=self.name,
            provider=self.provider_id,
            capability="api",
            account_label=self._account_label() or "(not connected)",
            root=self.root,
            available=not blockers,
            supported_actions=["cloud_move"],
            blockers=blockers,
            notes=[
                "Real Google Drive API connector (Drive v3).",
                "No delete actions in v1 — move/organize only.",
            ],
        )

    def authorize(self, mode: str) -> AccessGrant:
        # Real OAuth flow goes here. Until the deps and client-secret are
        # present, return granted=False with the specific blocker.
        if not _deps_present():
            return AccessGrant(
                mode=mode,
                granted=False,
                details={
                    "blocker": "missing-deps",
                    "hint": "pip install --user google-api-python-client google-auth-oauthlib",
                },
            )
        if not CLIENT_SECRET_FILE.exists():
            return AccessGrant(
                mode=mode,
                granted=False,
                details={
                    "blocker": "missing-client-secret",
                    "hint": f"place OAuth client_secret.json at {CLIENT_SECRET_FILE}",
                },
            )
        # Real flow (kept commented until creds are wired so this file
        # doesn't accidentally pop a browser on import):
        #
        # from google_auth_oauthlib.flow import InstalledAppFlow
        # flow = InstalledAppFlow.from_client_secrets_file(
        #     str(CLIENT_SECRET_FILE), SCOPES)
        # creds = flow.run_local_server(port=0)
        # self._save_token(json.loads(creds.to_json()))
        # return AccessGrant(mode=mode, granted=True, details={"provider": "gdrive"})
        return AccessGrant(
            mode=mode,
            granted=False,
            details={
                "blocker": "oauth-flow-not-wired",
                "hint": "OAuth flow stub exists in gdrive.py; uncomment when client.json is in place",
            },
        )

    def scan(self, cursor: str | None = None) -> Iterator[ItemRecord]:
        # Real implementation would iterate files.list pages. Until auth
        # is configured we yield nothing — the engine handles that as
        # "zero items from this source", and the UI surfaces the blocker
        # via probe().
        if not self.is_configured():
            return
            yield  # pragma: no cover — keeps function a generator

    def enrich(self, item: ItemRecord, jobs: list[str]) -> ItemRecord:
        # No-op until real auth lands. Real impl would fetch md5Checksum
        # for sha256-equivalent dedup, plus thumbnailLink for preview.
        return item

    def apply(self, action: ActionRecord) -> ApplyResult:
        return ApplyResult(
            action_id=action.action_id,
            success=False,
            message="gdrive not configured: run setup, then re-run apply",
        )

    def undo(self, undo_record: UndoRecord) -> ApplyResult:
        return ApplyResult(
            action_id=undo_record.action_id,
            success=False,
            message="gdrive not configured: nothing to undo",
        )

    def open_view(self, item: ItemRecord) -> str:
        fid = item.identity.get("provider_id", "")
        return f"https://drive.google.com/file/d/{fid}/view" if fid else ""
