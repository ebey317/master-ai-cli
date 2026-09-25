"""
Tests for the Storage Waste analytics + last-clean status tracker +
the full system-scan path that writes them.

Covers:
  * waste.reclaimable_bytes / biggest_files / oldest_files / by_category
    return what the report formatter expects.
  * waste.human_bytes rounds correctly across the unit boundaries.
  * status.record_full_scan persists + load_state round-trips the
    payload (with a redirected STATE_FILE so tests don't touch the
    user's real ~/.config).
  * engine.scan_run writes waste.json AND updates the status state
    after a full scan (using monkeypatched STATE_FILE).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sensei_clean import status as _status
from sensei_clean import waste as _waste
from sensei_clean.engine import scan_run
from sensei_clean.schemas import FindingRecord, ItemRecord


def _item(
    name="x.txt",
    size=10,
    sha=None,
    modified="2026-01-01T00:00:00Z",
    category="Reading",
    sensitivity="documents",
):
    return ItemRecord(
        schema_version="sensei.item.v1",
        run_id="r1",
        item_id=f"id:{name}",
        source={
            "adapter": "local_fs",
            "provider": "local",
            "capability": "local",
            "account_label": "u",
            "root": "/tmp",
        },
        identity={
            "path": f"/tmp/{name}",
            "provider_id": f"/tmp/{name}",
            "parent_id": "/tmp",
        },
        kind="file",
        display_name=name,
        mime="text/plain",
        size_bytes=size,
        timestamps={"created": None, "modified": modified, "taken": None},
        hashes={
            "sha256": sha,
            "md5": None,
            "provider_hash": None,
            "perceptual_hash": None,
        },
        features={
            "extension": ".txt",
            "dimensions": None,
            "duration_seconds": None,
            "text_snippet": None,
            "face_count": None,
            "screenshot_likely": False,
        },
        sensitivity=sensitivity,
        category_guess=category,
        confidence=1.0,
        risk=10,
        reversible_actions=["quarantine_move"],
        required_access=["read_metadata"],
        dependencies=[],
        notes=[],
    )


def _finding(item_ids, sha):
    return FindingRecord(
        schema_version="sensei.finding.v1",
        run_id="r1",
        finding_id=f"f:{sha}",
        finding_type="exact_duplicate",
        item_ids=item_ids,
        confidence=1.0,
        risk=10,
        summary=f"{len(item_ids)} dups",
        evidence={"sha256": sha},
        notes=[],
    )


class WasteAnalyticsTests(unittest.TestCase):
    def test_human_bytes_boundaries(self):
        self.assertEqual(_waste.human_bytes(0), "0 B")
        self.assertEqual(_waste.human_bytes(999), "999 B")
        self.assertEqual(_waste.human_bytes(1024), "1.0 KB")
        self.assertEqual(_waste.human_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertIn("GB", _waste.human_bytes(2 * 1024 * 1024 * 1024))

    def test_reclaimable_bytes_keeps_largest_per_cluster(self):
        items = [
            _item("a.txt", size=100, sha="X"),
            _item("b.txt", size=80, sha="X"),
            _item("c.txt", size=50, sha="X"),
            _item("d.txt", size=10, sha="Y"),
        ]
        findings = [
            _finding([items[0].item_id, items[1].item_id, items[2].item_id], "X")
        ]
        # cluster has 100, 80, 50 — keep 100, reclaim 80+50 = 130
        self.assertEqual(_waste.reclaimable_bytes(items, findings), 130)

    def test_biggest_files_sorts_and_excludes_empty(self):
        items = [
            _item("small.txt", size=1),
            _item("empty.txt", size=0),
            _item("huge.bin", size=1_000_000),
            _item("medium.txt", size=500),
        ]
        big = _waste.biggest_files(items, n=2)
        self.assertEqual([i.display_name for i in big], ["huge.bin", "medium.txt"])

    def test_oldest_files_excludes_items_with_no_timestamp(self):
        good = _item("kept.txt", modified="2024-01-01T00:00:00Z")
        none = _item("notime.txt", modified="")
        newer = _item("newer.txt", modified="2026-05-01T00:00:00Z")
        result = _waste.oldest_files([newer, good, none], n=5)
        # 'none' must not appear; oldest first
        names = [i.display_name for i in result]
        self.assertNotIn("notime.txt", names)
        self.assertEqual(names[0], "kept.txt")

    def test_by_category_sorts_by_bytes_descending(self):
        items = [
            _item("a", size=10, category="Photos"),
            _item("b", size=200, category="Videos"),
            _item("c", size=5, category="Photos"),
        ]
        cats = _waste.by_category(items)
        keys = list(cats.keys())
        self.assertEqual(keys[0], "Videos")
        # (count, bytes) tuple
        self.assertEqual(cats["Photos"], (2, 15))

    def test_summary_shape_for_report(self):
        items = [
            _item("a", size=100, sha="X"),
            _item("b", size=100, sha="X"),
            _item("c", size=50),
        ]
        findings = [_finding([items[0].item_id, items[1].item_id], "X")]
        s = _waste.summary(items, findings)
        self.assertEqual(s["total_items"], 3)
        self.assertEqual(s["duplicate_clusters"], 1)
        self.assertEqual(s["reclaim_bytes"], 100)
        self.assertEqual(s["reclaim_bytes_human"], "100 B")
        self.assertTrue(s["biggest"])
        self.assertTrue(s["by_category"])


class StatusTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._real_state_file = _status.STATE_FILE
        self._real_state_dir = _status.STATE_DIR
        _status.STATE_DIR = Path(self._tmp.name)
        _status.STATE_FILE = Path(self._tmp.name) / "state.json"

    def tearDown(self):
        _status.STATE_FILE = self._real_state_file
        _status.STATE_DIR = self._real_state_dir
        self._tmp.cleanup()

    def test_format_status_empty_returns_helpful_hint(self):
        msg = _status.format_status()
        self.assertIn("No Sensei Clean runs recorded", msg)

    def test_record_full_scan_persists_and_loads(self):
        _status.record_full_scan(
            run_dir="/tmp/run1",
            total_items=1234,
            total_bytes=999_999,
            reclaim_bytes=12345,
            duplicate_clusters=7,
            sources=["/home/me/Downloads", "rclone:gdrive:"],
        )
        loaded = _status.load_state()
        self.assertEqual(loaded["last_total_items"], 1234)
        self.assertEqual(loaded["last_reclaim_bytes"], 12345)
        self.assertEqual(loaded["last_duplicate_clusters"], 7)
        self.assertEqual(
            loaded["last_sources"], ["/home/me/Downloads", "rclone:gdrive:"]
        )
        msg = _status.format_status()
        self.assertIn("1,234", msg)
        self.assertIn("rclone:gdrive:", msg)

    def test_record_apply_layered_onto_existing_state(self):
        _status.record_full_scan(
            run_dir="/tmp/run1",
            total_items=10,
            total_bytes=10,
            reclaim_bytes=0,
            duplicate_clusters=0,
            sources=[],
        )
        _status.record_apply(run_dir="/tmp/run1", applied=3, failed=0)
        loaded = _status.load_state()
        self.assertEqual(loaded["last_apply_applied"], 3)
        self.assertEqual(loaded["last_apply_failed"], 0)


class EngineWritesWasteAndStatusTests(unittest.TestCase):
    """Full pipeline: scan_run writes waste.json AND updates the
    redirected status state."""

    def test_waste_json_and_state_after_scan(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello world", encoding="utf-8")
            (src / "b.txt").write_text("hello world", encoding="utf-8")  # dup of a
            (src / "c.txt").write_text("unique", encoding="utf-8")

            state_dir = root / "state"
            with (
                mock.patch.object(_status, "STATE_DIR", state_dir),
                mock.patch.object(_status, "STATE_FILE", state_dir / "state.json"),
            ):
                run_path, caps, items, findings, actions = scan_run(
                    roots=[str(src)],
                    sha256=True,
                    quarantine_root=str(root / "q"),
                    run_dir=str(root / "run"),
                )

                waste_json = json.loads((run_path / "waste.json").read_text())
                self.assertEqual(waste_json["total_items"], 3)
                self.assertGreater(waste_json["duplicate_clusters"], 0)
                self.assertIn("biggest", waste_json)
                self.assertIn("oldest", waste_json)

                loaded = _status.load_state()
                self.assertEqual(loaded["last_total_items"], 3)
                self.assertGreater(loaded["last_reclaim_bytes"], 0)
                self.assertEqual(loaded["last_sources"], [str(src)])


if __name__ == "__main__":
    unittest.main()
