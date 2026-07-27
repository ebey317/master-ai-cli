"""
Tests for the open-in-app surface.

Pure-function coverage of `resolve_open_target`, `review_link_href`,
and `LocalFSAdapter.open_view`. `_xdg_open` itself is monkey-patched
so no real apps spawn during tests.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sensei_clean.adapters.local_fs import LocalFSAdapter
from sensei_clean.adapters.rclone_remote import RcloneRemoteAdapter
from sensei_clean.opener import (
    OpenTarget,
    open_item,
    resolve_open_target,
    review_link_href,
)
from sensei_clean.schemas import ItemRecord


def _local_item(path: str, name: str = "x.txt", size: int = 10) -> ItemRecord:
    return ItemRecord(
        schema_version="sensei.item.v1",
        run_id="r1",
        item_id=f"id:{name}",
        source={"adapter": "local_fs", "provider": "local", "capability": "local",
                "account_label": "u", "root": str(Path(path).parent)},
        identity={"path": path, "provider_id": path, "parent_id": str(Path(path).parent)},
        kind="file",
        display_name=name,
        mime="text/plain",
        size_bytes=size,
        timestamps={"created": None, "modified": "2026-01-01T00:00:00Z", "taken": None},
        hashes={"sha256": None, "md5": None, "provider_hash": None, "perceptual_hash": None},
        features={"extension": ".txt", "dimensions": None, "duration_seconds": None,
                  "text_snippet": None, "face_count": None, "screenshot_likely": False},
        sensitivity="documents",
        category_guess="Reading",
        confidence=1.0,
        risk=10,
        reversible_actions=["quarantine_move"],
        required_access=["read_metadata"],
        dependencies=[],
        notes=[],
    )


def _cloud_item(remote: str, provider_id: str, name: str) -> ItemRecord:
    rel = f"{name}"
    return ItemRecord(
        schema_version="sensei.item.v1",
        run_id="r1",
        item_id=f"rclone:{remote}:{provider_id}",
        source={"adapter": f"rclone:{remote}", "provider": f"rclone-{remote}",
                "capability": "api", "account_label": remote, "root": f"rclone:{remote}:"},
        identity={"path": f"rclone:{remote}:{rel}",
                  "provider_id": provider_id, "parent_id": "", "relative_path": rel},
        kind="file",
        display_name=name,
        mime="application/pdf",
        size_bytes=42,
        timestamps={"created": None, "modified": "2026-01-01T00:00:00Z", "taken": None},
        hashes={"sha256": None, "md5": None, "provider_hash": None, "perceptual_hash": None},
        features={"extension": ".pdf", "dimensions": None, "duration_seconds": None,
                  "text_snippet": None, "face_count": None, "screenshot_likely": False},
        sensitivity="documents",
        category_guess="Reading",
        confidence=0.9,
        risk=10,
        reversible_actions=["cloud_move"],
        required_access=["read_metadata"],
        dependencies=[],
        notes=[],
    )


class LocalOpenViewTests(unittest.TestCase):
    def test_local_adapter_returns_file_path(self):
        adapter = LocalFSAdapter(run_id="r1", roots=["/tmp"], quarantine_root="/tmp/q")
        item = _local_item("/home/user/foo.txt")
        self.assertEqual(adapter.open_view(item), "/home/user/foo.txt")

    def test_local_adapter_returns_empty_for_rclone_path(self):
        adapter = LocalFSAdapter(run_id="r1", roots=["/tmp"], quarantine_root="/tmp/q")
        item = _local_item("rclone:gdrive:foo.txt")
        self.assertEqual(adapter.open_view(item), "")


class ResolveOpenTargetTests(unittest.TestCase):
    def test_local_file_resolves_to_local_file_kind(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.txt"
            p.write_text("hi")
            item = _local_item(str(p))
            t = resolve_open_target(item)
            self.assertEqual(t.kind, "local_file")
            self.assertEqual(t.target, str(p))

    def test_local_dir_resolves_to_local_dir_kind(self):
        with TemporaryDirectory() as tmp:
            item = _local_item(tmp, name=os.path.basename(tmp))
            t = resolve_open_target(item)
            self.assertEqual(t.kind, "local_dir")

    def test_cloud_resolves_via_adapter_open_view(self):
        item = _cloud_item("gdrive", "FID123", "doc.pdf")
        adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
        t = resolve_open_target(item, adapter=adapter)
        self.assertEqual(t.kind, "cloud_url")
        self.assertTrue(t.target.startswith("https://drive.google.com/file/d/"))

    def test_cloud_without_adapter_falls_back_to_unknown(self):
        item = _cloud_item("gdrive", "FID123", "doc.pdf")
        t = resolve_open_target(item)  # no adapter
        self.assertEqual(t.kind, "unknown")


class ReviewLinkHrefTests(unittest.TestCase):
    def test_local_file_becomes_file_uri(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "with space.txt"
            p.write_text("hi")
            item = _local_item(str(p), name="with space.txt")
            href = review_link_href(item)
            self.assertTrue(href.startswith("file://"))
            self.assertIn("with%20space.txt", href)

    def test_cloud_becomes_provider_url(self):
        item = _cloud_item("gdrive", "FID123", "doc.pdf")
        adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
        href = review_link_href(item, adapter=adapter)
        self.assertTrue(href.startswith("https://drive.google.com/"))


class OpenItemSpawnTests(unittest.TestCase):
    def test_spawn_off_does_not_call_xdg_open(self):
        called = {"hit": False}
        with mock.patch("sensei_clean.opener._xdg_open",
                        side_effect=lambda a: (called.__setitem__("hit", True) or (True, "x"))):
            with TemporaryDirectory() as tmp:
                p = Path(tmp) / "x.txt"
                p.write_text("hi")
                item = _local_item(str(p))
                t, ok, _msg = open_item(item, spawn=False)
                self.assertEqual(t.kind, "local_file")
                self.assertTrue(ok)
                self.assertFalse(called["hit"])

    def test_spawn_on_calls_xdg_open_with_target(self):
        captured = {}
        def fake(arg):
            captured["arg"] = arg
            return True, "fake spawn"
        with mock.patch("sensei_clean.opener._xdg_open", side_effect=fake):
            with TemporaryDirectory() as tmp:
                p = Path(tmp) / "x.txt"
                p.write_text("hi")
                item = _local_item(str(p))
                _t, ok, _msg = open_item(item, spawn=True)
                self.assertTrue(ok)
                self.assertEqual(captured["arg"], str(p))

    def test_spawn_refuses_missing_local_file(self):
        with TemporaryDirectory() as tmp:
            item = _local_item(str(Path(tmp) / "does_not_exist.txt"))
            with mock.patch("sensei_clean.opener._xdg_open",
                            side_effect=lambda a: (True, "should not run")):
                _t, ok, msg = open_item(item, spawn=True)
                self.assertFalse(ok)
                self.assertIn("missing", msg)

    def test_cloud_url_spawn_calls_xdg_open_with_url(self):
        captured = {}
        def fake(arg):
            captured["arg"] = arg
            return True, "fake spawn"
        item = _cloud_item("gdrive", "FID123", "doc.pdf")
        adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
        with mock.patch("sensei_clean.opener._xdg_open", side_effect=fake):
            _t, ok, _msg = open_item(item, adapter=adapter, spawn=True)
            self.assertTrue(ok)
            self.assertTrue(captured["arg"].startswith("https://drive.google.com/"))


class GuiPickerHelpersTests(unittest.TestCase):
    """The dialogs themselves need a TTY, but the helpers that build
    the choice list and pick the right adapter per item are pure."""

    def test_open_picker_choices_sorted_by_size_desc_and_capped(self):
        from sensei_clean_app import _open_picker_choices
        items = [
            _local_item("/tmp/a.txt", "a.txt", size=5),
            _local_item("/tmp/b.txt", "b.txt", size=100),
            _local_item("/tmp/c.txt", "c.txt", size=50),
        ]
        choices = _open_picker_choices(items, n=10)
        # value is item_id, label includes size & category
        names_in_order = [c[1].split(" ", 1)[1].split(" ", 1)[0]
                          for c in choices]  # extract display name token
        self.assertEqual(names_in_order, ["b.txt", "c.txt", "a.txt"])
        self.assertEqual(len(choices), 3)
        # Cap honored
        many = [_local_item(f"/tmp/f{i}.txt", f"f{i}.txt", size=i + 1)
                for i in range(80)]
        capped = _open_picker_choices(many, n=20)
        self.assertEqual(len(capped), 20)

    def test_open_picker_choices_marks_cloud_items_with_cloud_glyph(self):
        from sensei_clean_app import _open_picker_choices
        cloud = _cloud_item("gdrive", "FID", "doc.pdf")
        choices = _open_picker_choices([cloud], n=10)
        self.assertEqual(len(choices), 1)
        label = choices[0][1]
        self.assertIn("☁", label)
        self.assertIn("doc.pdf", label)

    def test_open_picker_choices_flags_sensitive_in_label(self):
        from sensei_clean_app import _open_picker_choices
        item = _local_item("/tmp/resume.pdf", "resume.pdf", size=200)
        # Override sensitivity to one in the monitored set
        from dataclasses import replace
        item = replace(item, sensitivity="career")
        choices = _open_picker_choices([item], n=5)
        self.assertIn("private", choices[0][1])

    def test_adapter_for_local_item_returns_local_fs(self):
        from sensei_clean_app import _adapter_for_item
        item = _local_item("/tmp/foo.txt")
        adapter = _adapter_for_item(item, run_id="r1", quarantine_root="/tmp/q")
        self.assertEqual(adapter.name, "local_fs")

    def test_adapter_for_cloud_item_returns_rclone(self):
        from sensei_clean_app import _adapter_for_item
        item = _cloud_item("gdrive", "FID", "doc.pdf")
        adapter = _adapter_for_item(item, run_id="r1", quarantine_root="/tmp/q")
        self.assertEqual(adapter.name, "rclone:gdrive")


class ReviewHtmlClickableLinksTests(unittest.TestCase):
    def test_review_html_emits_file_uri_for_local_action(self):
        """Smoke: write_review_html renders an <a class="open-link">
        with a file:// href for a local quarantine_move action."""
        from sensei_clean.reports import write_review_html
        from sensei_clean.schemas import ActionRecord, CapabilityReport, FindingRecord
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            p = tmpdir / "dup.txt"
            p.write_text("hi")
            item = _local_item(str(p), name="dup.txt")
            cap = CapabilityReport(
                adapter="local_fs", provider="local", capability="local",
                account_label="u", root=str(tmpdir), available=True,
            )
            action = ActionRecord(
                schema_version="sensei.action.v1", run_id="r1", action_id="a1",
                action_type="quarantine_move", adapter="local_fs",
                item_id=item.item_id, source_path=str(p),
                destination_path=str(tmpdir / "q" / "dup.txt"),
                confidence=1.0, risk=10, reversible=True,
                lane="unattended", reason="dup", approval_required=False,
                metadata={},
            )
            out_html = tmpdir / "review.html"
            write_review_html(str(out_html), [cap], [item], [], [action])
            doc = out_html.read_text()
            self.assertIn("open-link", doc)
            self.assertIn(f"file://{str(p)}", doc)


if __name__ == "__main__":
    unittest.main()
