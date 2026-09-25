from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sensei_clean import status as _status
from sensei_clean.adapters.local_fs import LocalFSAdapter
from sensei_clean.apply import apply_actions, load_undo_records, undo_actions
from sensei_clean.connectors import detect_sources, supported_connector_catalog
from sensei_clean.engine import scan_run


def _write_docx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>""",
        )


class SenseiCleanConnectorTests(unittest.TestCase):
    def test_detects_os_cloud_and_android_sources(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            gvfs = root / "gvfs"
            media = root / "media"
            for folder in [
                home / "Downloads",
                home / "Desktop",
                home / "Documents",
                home / "Pictures",
                home / "Camera Uploads",
                home / "Google Drive",
                home / "OneDrive - Work",
                home / "Dropbox",
                home / "Nextcloud",
                gvfs / "mtp:host=Android_123",
                gvfs / "mtp:host=Android_123" / "DCIM" / "Camera",
                media / "PhoneStorage",
                media / "PhoneStorage" / "DCIM",
            ]:
                folder.mkdir(parents=True, exist_ok=True)

            sources = detect_sources(home=home, gvfs_root=gvfs, media_root=media)
            labels = {s.label for s in sources if s.available}
            kinds = {s.kind for s in sources if s.available}

            self.assertIn("Downloads", labels)
            self.assertIn("Google Drive", labels)
            self.assertIn("OneDrive", labels)
            self.assertIn("Dropbox", labels)
            self.assertIn("Nextcloud", labels)
            self.assertIn("Android device", labels)
            self.assertIn("Removable/media storage", labels)
            self.assertIn("Camera Uploads", labels)
            self.assertIn("synced_cloud_folder", kinds)
            self.assertIn("android_mounted_storage", kinds)
            self.assertIn("photo_library", kinds)

    def test_connector_catalog_names_photos_cloud_email_and_all_os(self) -> None:
        catalog_text = " ".join(
            f"{row['group']} {row['name']} {row['status']}"
            for row in supported_connector_catalog()
        )
        self.assertIn("Google Drive", catalog_text)
        self.assertIn("OneDrive", catalog_text)
        self.assertIn("Google Photos", catalog_text)
        self.assertIn("Gmail", catalog_text)
        self.assertIn("Linux", catalog_text)
        self.assertIn("Windows", catalog_text)
        self.assertIn("macOS", catalog_text)

    def test_scan_organize_apply_undo_and_preview(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            downloads = root / "Downloads"
            cloud = root / "Google Drive"
            android = root / "gvfs" / "mtp:host=Android_123"
            for folder in [downloads, cloud, android]:
                folder.mkdir(parents=True, exist_ok=True)

            (downloads / "invoice.txt").write_text(
                "consumer invoice preview text", encoding="utf-8"
            )
            (downloads / "copy_a.txt").write_text("duplicate payload", encoding="utf-8")
            (downloads / "copy_b.txt").write_text("duplicate payload", encoding="utf-8")
            _write_docx(downloads / "office.docx", "office preview text")
            (cloud / "sheet.xlsx").write_text(
                "not a real xlsx but should be inventoried", encoding="utf-8"
            )
            (android / "photo.jpg").write_bytes(b"fakejpg")

            run_dir = root / "run"
            organize_root = root / "organized"
            quarantine_root = root / "quarantine"
            state_dir = root / "state"
            with (
                mock.patch.object(_status, "STATE_DIR", state_dir),
                mock.patch.object(_status, "STATE_FILE", state_dir / "state.json"),
            ):
                run_path, caps, items, findings, actions = scan_run(
                    roots=[str(downloads), str(cloud), str(android)],
                    sha256=True,
                    quarantine_root=str(quarantine_root),
                    run_dir=str(run_dir),
                    include_previews=True,
                    organize=True,
                    organize_root=str(organize_root),
                )
            cap = next((c for c in caps if c.capability == "local"), caps[0])

            self.assertGreaterEqual(len(items), 6)
            self.assertGreaterEqual(len(findings), 1)
            self.assertTrue((run_path / "reports" / "summary.md").exists())
            self.assertTrue((run_path / "reports" / "previews.md").exists())
            self.assertTrue((run_path / "reports" / "review.html").exists())
            review_html = (run_path / "reports" / "review.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("Sensei Clean Review", review_html)
            self.assertIn("What Sensei Wants To Move", review_html)
            self.assertIn("Move extra copy", review_html)
            self.assertIn("Safe Quarantine", review_html)
            previews = json.loads((run_path / "previews.json").read_text())
            preview_text = json.dumps(previews)
            self.assertIn("consumer invoice preview text", preview_text)
            self.assertIn("office preview text", preview_text)

            adapter = LocalFSAdapter(
                run_id="apply",
                roots=[str(downloads), str(cloud), str(android)],
                quarantine_root=str(quarantine_root),
            )
            selected = [a for a in actions if a.lane != "monitored"]
            self.assertTrue(selected, "expected unattended actions for Downloads")
            results = apply_actions(
                adapter, selected, cap, str(run_path / "undo.jsonl")
            )
            self.assertTrue(
                all(r.success for r in results), [r.message for r in results]
            )

            records = load_undo_records(str(run_path / "undo.jsonl"))
            undo_results = undo_actions(adapter, list(reversed(records)))
            self.assertTrue(
                all(r.success for r in undo_results), [r.message for r in undo_results]
            )
            self.assertTrue((downloads / "invoice.txt").exists())


if __name__ == "__main__":
    unittest.main()
