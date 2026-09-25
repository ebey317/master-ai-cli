from __future__ import annotations

import os
import platform
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceConnector:
    connector_id: str
    label: str
    path: str
    kind: str
    available: bool
    notes: tuple[str, ...] = ()

    def to_choice(self) -> tuple[str, str]:
        suffix = "" if self.available else " (not found)"
        note = f" - {'; '.join(self.notes)}" if self.notes else ""
        return self.path, f"{self.label}{suffix}  ({self.path}){note}"


def _xdg_dir(name: str, fallback: Path, *, home: Path) -> Path:
    user_dirs = home / ".config" / "user-dirs.dirs"
    if not user_dirs.exists():
        return fallback
    try:
        for line in user_dirs.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line.startswith(f"XDG_{name}_DIR="):
                continue
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            raw = raw.replace("$HOME", str(home))
            return Path(raw).expanduser().resolve()
    except Exception:
        return fallback
    return fallback


def _home_folder_connectors(home: Path) -> list[SourceConnector]:
    specs = [
        ("downloads", "Downloads", "DOWNLOAD", "local", "most common cleanup target"),
        ("desktop", "Desktop", "DESKTOP", "local", "customer-visible files"),
        (
            "documents",
            "Documents",
            "DOCUMENTS",
            "local",
            "sensitive: asks for approval",
        ),
        (
            "pictures",
            "Photos / Pictures",
            "PICTURES",
            "photo_library",
            "regular photos on this computer",
        ),
        (
            "videos",
            "Videos",
            "VIDEOS",
            "media_library",
            "regular videos on this computer",
        ),
        ("music", "Music", "MUSIC", "local", "media"),
    ]
    out: list[SourceConnector] = []
    for cid, label, xdg, kind, note in specs:
        default = home / label
        path = _xdg_dir(xdg, default, home=home)
        out.append(
            SourceConnector(
                connector_id=cid,
                label=label,
                path=str(path),
                kind=kind,
                available=path.exists() and path.is_dir(),
                notes=(note,),
            )
        )
    out.extend(_photo_folder_connectors(home))
    return out


def _photo_folder_connectors(home: Path) -> list[SourceConnector]:
    """Common local photo import locations across Linux, Windows, and macOS.

    We only return paths that exist, so adding cross-OS names is harmless on
    Linux but lets the same detector behave when the app is ported.
    """
    system = platform.system().lower()
    names = [
        ("camera_uploads", "Camera Uploads", ["Camera Uploads", "CameraUploads"]),
        (
            "phone_imports",
            "Phone Imports",
            ["Phone Imports", "Imported Photos", "Photo Imports"],
        ),
        ("dcim_home", "Camera Roll / DCIM", ["DCIM", "Camera Roll"]),
        (
            "google_photos_folder",
            "Google Photos folder",
            ["Google Photos", "GooglePhotos"],
        ),
        (
            "icloud_photos_folder",
            "iCloud Photos folder",
            ["iCloud Photos", "Photos Library.photoslibrary"],
        ),
    ]
    if system == "darwin":
        names.append(
            (
                "mac_photos",
                "Mac Photos library",
                ["Pictures/Photos Library.photoslibrary"],
            )
        )
    elif system == "windows":
        names.extend(
            [
                (
                    "windows_camera_roll",
                    "Windows Camera Roll",
                    ["Pictures/Camera Roll"],
                ),
                (
                    "windows_saved_pictures",
                    "Saved Pictures",
                    ["Pictures/Saved Pictures"],
                ),
            ]
        )

    out: list[SourceConnector] = []
    for cid, label, rels in names:
        for rel in rels:
            path = home / rel
            if path.exists() and path.is_dir():
                out.append(
                    SourceConnector(
                        connector_id=cid,
                        label=label,
                        path=str(path),
                        kind="photo_library",
                        available=True,
                        notes=("photos", "safe scan first"),
                    )
                )
    return _dedupe(out)


def _synced_folder_connectors(home: Path) -> list[SourceConnector]:
    candidates = [
        ("google_drive", "Google Drive", ["Google Drive", "GoogleDrive", "Drive"]),
        (
            "onedrive",
            "OneDrive",
            [
                "OneDrive",
                "OneDrive - Personal",
                "OneDrive - Business",
                "OneDrive - Work",
            ],
        ),
        ("dropbox", "Dropbox", ["Dropbox"]),
        ("nextcloud", "Nextcloud", ["Nextcloud"]),
        ("icloud", "iCloud Drive", ["iCloud Drive", "iCloudDrive"]),
        ("syncthing", "Syncthing", ["Sync", "Syncthing"]),
        ("amazon_photos", "Amazon Photos", ["Amazon Photos", "Amazon Drive"]),
        ("pcloud", "pCloud", ["pCloud Drive", "pCloud"]),
        ("box", "Box", ["Box", "Box Sync"]),
    ]
    out: list[SourceConnector] = []
    for cid, label, names in candidates:
        for name in names:
            path = home / name
            if path.exists() and path.is_dir():
                out.append(
                    SourceConnector(
                        connector_id=cid,
                        label=label,
                        path=str(path),
                        kind="synced_cloud_folder",
                        available=True,
                        notes=("local sync folder", "not OAuth/API"),
                    )
                )
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.startswith("OneDrive"):
                out.append(
                    SourceConnector(
                        connector_id="onedrive",
                        label="OneDrive",
                        path=str(child),
                        kind="synced_cloud_folder",
                        available=True,
                        notes=("local sync folder", "not OAuth/API"),
                    )
                )
    except Exception:
        pass
    return _dedupe(out)


def _rclone_connectors() -> list[SourceConnector]:
    """Discover configured rclone remotes (gdrive:, onedrive:, dropbox:, ...).
    The remote being LISTED here does not imply it is currently
    authenticated — that's the adapter's probe() job. Listing only reads
    ~/.config/rclone/rclone.conf via `rclone listremotes`."""
    try:
        from .adapters.rclone_remote import rclone_listremotes
    except Exception:
        return []
    try:
        remotes = rclone_listremotes()
    except Exception:
        remotes = []
    out: list[SourceConnector] = []
    for r in remotes:
        provider_label, provider_kind, provider_notes = _rclone_remote_label(r)
        out.append(
            SourceConnector(
                connector_id=f"rclone_{r}",
                label=provider_label,
                path=f"rclone:{r}:",
                kind=provider_kind,
                available=True,  # listed; live auth is a separate probe
                notes=(
                    "real cloud API connector via rclone",
                    "file listing is optional",
                    "moves go to cloud quarantine and need extra approval",
                    *provider_notes,
                ),
            )
        )
    return out


def _rclone_remote_label(remote: str) -> tuple[str, str, tuple[str, ...]]:
    lowered = remote.lower()
    if "gphoto" in lowered or "googlephoto" in lowered or "photos" in lowered:
        return f"{remote} (Google Photos)", "cloud_photo_api", ("photo service",)
    if "gdrive" in lowered or "drive" in lowered:
        return f"{remote} (Google Drive)", "cloud_api", ()
    if "onedrive" in lowered or "microsoft" in lowered:
        return f"{remote} (OneDrive)", "cloud_api", ()
    if "dropbox" in lowered:
        return f"{remote} (Dropbox)", "cloud_api", ()
    if "box" in lowered:
        return f"{remote} (Box)", "cloud_api", ()
    if "pcloud" in lowered:
        return f"{remote} (pCloud)", "cloud_api", ()
    return f"{remote} (cloud)", "cloud_api", ()


def _android_connectors(*, gvfs_root: Path, media_root: Path) -> list[SourceConnector]:
    out: list[SourceConnector] = []
    if gvfs_root.exists():
        try:
            for child in gvfs_root.iterdir():
                name = child.name.lower()
                if "mtp" in name or "android" in name:
                    out.append(
                        SourceConnector(
                            connector_id="android_mtp",
                            label="Android device",
                            path=str(child),
                            kind="android_mounted_storage",
                            available=True,
                            notes=(
                                "phone storage",
                                "photos usually live in DCIM/Camera",
                            ),
                        )
                    )
                    out.extend(_mounted_photo_children(child, prefix="android_photos"))
        except Exception:
            pass
    if media_root.exists():
        try:
            for child in media_root.iterdir():
                if child.is_dir():
                    out.append(
                        SourceConnector(
                            connector_id="removable_media",
                            label="Removable/media storage",
                            path=str(child),
                            kind="removable_storage",
                            available=True,
                            notes=("mounted local storage",),
                        )
                    )
                    out.extend(
                        _mounted_photo_children(child, prefix="removable_photos")
                    )
        except Exception:
            pass
    return _dedupe(out)


def _mounted_photo_children(root: Path, *, prefix: str) -> list[SourceConnector]:
    candidates = [
        root / "DCIM",
        root / "DCIM" / "Camera",
        root / "Pictures",
        root / "Download",
        root / "Internal shared storage" / "DCIM",
        root / "Internal shared storage" / "DCIM" / "Camera",
        root / "Internal shared storage" / "Pictures",
    ]
    out: list[SourceConnector] = []
    for path in candidates:
        if path.exists() and path.is_dir():
            out.append(
                SourceConnector(
                    connector_id=f"{prefix}_{path.name.lower().replace(' ', '_')}",
                    label=f"Phone photos: {path.name}",
                    path=str(path),
                    kind="photo_library",
                    available=True,
                    notes=("photos from phone/camera",),
                )
            )
    return out


def supported_connector_catalog() -> list[dict[str, str]]:
    """Plain-language target list for the setup screen/docs.

    `detect_sources()` returns what is available now. This catalog is the
    broader consumer target across OSes and providers.
    """
    return [
        {
            "group": "This computer",
            "name": "Downloads/Desktop/Documents",
            "status": "scan local folders",
        },
        {
            "group": "Photos",
            "name": "Pictures, Camera Uploads, DCIM, phone photos",
            "status": "scan when folder/device is present",
        },
        {
            "group": "Cloud drive",
            "name": "Google Drive, OneDrive, Dropbox, Box, pCloud, Nextcloud/WebDAV",
            "status": "connect through rclone or local sync folder",
        },
        {
            "group": "Photo cloud",
            "name": "Google Photos, iCloud Photos, Amazon Photos",
            "status": "connect separately; Google Photos can use rclone",
        },
        {
            "group": "Phones",
            "name": "Android MTP, removable SD cards, USB drives",
            "status": "scan when mounted",
        },
        {
            "group": "Email",
            "name": "Gmail, Outlook, Yahoo/IMAP",
            "status": "separate mail connector; not rclone",
        },
        {
            "group": "Operating systems",
            "name": "Linux, Windows, macOS",
            "status": "same connector model; OS-specific folders detected",
        },
    ]


def _dedupe(connectors: Iterable[SourceConnector]) -> list[SourceConnector]:
    seen: set[tuple[str, str]] = set()
    out: list[SourceConnector] = []
    for conn in connectors:
        key = (conn.connector_id, conn.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(conn)
    return out


def detect_sources(
    *,
    home: Path | None = None,
    gvfs_root: Path | None = None,
    media_root: Path | None = None,
) -> list[SourceConnector]:
    home = (home or Path.home()).expanduser().resolve()
    uid = os.getuid()
    gvfs_root = gvfs_root or Path(f"/run/user/{uid}/gvfs")
    media_root = media_root or (Path("/media") / os.environ.get("USER", "user"))
    return _dedupe(
        _home_folder_connectors(home)
        + _synced_folder_connectors(home)
        + _android_connectors(gvfs_root=gvfs_root, media_root=media_root)
        + _rclone_connectors()
    )
