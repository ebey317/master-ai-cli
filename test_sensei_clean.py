from __future__ import annotations

import unittest
from pathlib import Path

from sensei_clean.adapters.local_fs import LocalFSAdapter
from sensei_clean.queue_builder import build_queue
from sensei_clean.schemas import ActionRecord, CapabilityReport, ItemRecord


def make_item(
    path: str, confidence: float = 1.0, risk: int = 20, sensitivity: str = "documents"
) -> ItemRecord:
    return ItemRecord(
        schema_version="sensei.item.v1",
        run_id="run1",
        item_id=f"id:{path}",
        source={
            "adapter": "local_fs",
            "provider": "local",
            "capability": "local",
            "account_label": "me",
            "root": "/tmp",
        },
        identity={
            "path": path,
            "provider_id": path,
            "parent_id": str(Path(path).parent),
        },
        kind="file",
        display_name=Path(path).name,
        mime="text/plain",
        size_bytes=10,
        timestamps={"created": None, "modified": "2026-01-01T00:00:00Z", "taken": None},
        hashes={
            "sha256": None,
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
        category_guess="Reading",
        confidence=confidence,
        risk=risk,
        reversible_actions=["archive_move", "quarantine_move"],
        required_access=["read_metadata"],
        dependencies=[],
        notes=[],
    )


class SenseiCleanTests(unittest.TestCase):
    def test_schema_validation_rejects_bad_confidence(self) -> None:
        item = make_item("/tmp/a.txt", confidence=2.0)
        with self.assertRaises(ValueError):
            item.validate()

    def test_local_scan_is_read_only(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "Documents"
            source.mkdir()
            sample = source / "resume.txt"
            sample.write_text("resume")

            adapter = LocalFSAdapter(
                run_id="run1",
                roots=[str(source)],
                quarantine_root=str(Path(tmpdir) / "quarantine"),
            )
            items = list(adapter.scan())

            self.assertEqual(len(items), 1)
            self.assertTrue(sample.exists())
            self.assertEqual(items[0].identity["path"], str(sample))

    def test_local_apply_and_undo(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "Downloads"
            source.mkdir()
            sample = source / "dup.txt"
            sample.write_text("hello")
            destination = root / "Sensei-Quarantine" / "duplicates" / "dup.txt"

            adapter = LocalFSAdapter(
                run_id="run1",
                roots=[str(source)],
                quarantine_root=str(root / "Sensei-Quarantine"),
            )
            action = ActionRecord(
                schema_version="sensei.action.v1",
                run_id="run1",
                action_id="act1",
                action_type="quarantine_move",
                adapter="local_fs",
                item_id="item1",
                source_path=str(sample),
                destination_path=str(destination),
                confidence=1.0,
                risk=10,
                reversible=True,
                lane="unattended",
                reason="test",
                approval_required=False,
                metadata={"sensitivity": "documents"},
            )

            result = adapter.apply(action)
            self.assertTrue(result.success)
            self.assertTrue(destination.exists())
            self.assertFalse(sample.exists())

            undo = result.undo_record
            assert undo is not None
            undo_result = adapter.undo(undo)
            self.assertTrue(undo_result.success)
            self.assertTrue(sample.exists())

    def test_queue_keeps_sensitive_items_monitored(self) -> None:
        capability = CapabilityReport(
            adapter="local_fs",
            provider="local",
            capability="local",
            account_label="me",
            root="/tmp",
            available=True,
        )
        item = make_item(
            "/tmp/private.txt", confidence=0.95, risk=20, sensitivity="private"
        )
        queue = build_queue([item], [], [capability])
        self.assertEqual(queue["monitored"][0]["item_id"], item.item_id)


def _make_action(
    action_id,
    source,
    destination,
    sensitivity="documents",
    action_type="quarantine_move",
    adapter="local_fs",
):
    return ActionRecord(
        schema_version="sensei.action.v1",
        run_id="run1",
        action_id=action_id,
        action_type=action_type,
        adapter=adapter,
        item_id=f"item:{action_id}",
        source_path=str(source),
        destination_path=str(destination),
        confidence=1.0,
        risk=10,
        reversible=True,
        lane="unattended",
        reason="test",
        approval_required=False,
        metadata={"sensitivity": sensitivity},
    )


def _local_capability(tmpdir):
    return CapabilityReport(
        adapter="local_fs",
        provider="local",
        capability="local",
        account_label="me",
        root=str(tmpdir),
        available=True,
        supported_actions=["archive_move", "quarantine_move"],
    )


class SenseiCleanSafetyTests(unittest.TestCase):
    """Pin the three high-severity gaps from the briefing."""

    def test_apply_enforces_policy_can_apply(self):
        """apply_actions must refuse actions policy.can_apply rejects."""
        from tempfile import TemporaryDirectory

        from sensei_clean.apply import apply_actions

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "src"
            src.mkdir()
            f = src / "x.txt"
            f.write_text("data")
            adapter = LocalFSAdapter(
                run_id="r1", roots=[str(src)], quarantine_root=str(root / "Q")
            )
            # capability with available=False should block apply
            cap = CapabilityReport(
                adapter="local_fs",
                provider="local",
                capability="local",
                account_label="me",
                root=str(root),
                available=False,
            )
            action = _make_action("a1", f, root / "Q/duplicates/x.txt")
            results = apply_actions(adapter, [action], cap, str(root / "undo.jsonl"))
            self.assertFalse(results[0].success)
            self.assertIn("policy refused", results[0].message)
            self.assertTrue(f.exists(), "source must not have been touched")

    def test_apply_writes_undo_per_action_immediately(self):
        """Journal must have one line per successful action, not a single
        batch write at the end."""
        from tempfile import TemporaryDirectory

        from sensei_clean.apply import apply_actions

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "src"
            src.mkdir()
            files = [src / f"f{i}.txt" for i in range(3)]
            for f in files:
                f.write_text(f.name)
            adapter = LocalFSAdapter(
                run_id="r1", roots=[str(src)], quarantine_root=str(root / "Q")
            )
            cap = _local_capability(root)
            actions = [
                _make_action(f"a{i}", f, root / f"Q/duplicates/{f.name}")
                for i, f in enumerate(files)
            ]
            undo_path = root / "undo.jsonl"
            results = apply_actions(adapter, actions, cap, str(undo_path))
            self.assertEqual(sum(1 for r in results if r.success), 3)
            lines = [ln for ln in undo_path.read_text().splitlines() if ln.strip()]
            self.assertEqual(
                len(lines), 3, f"expected 3 journal lines, got {len(lines)}"
            )

    def test_apply_uniquifies_same_basename_destinations(self):
        """Same-basename quarantine targets must not collide; second gets
        a numeric suffix."""
        from tempfile import TemporaryDirectory

        from sensei_clean.apply import apply_actions

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a").mkdir()
            (root / "b").mkdir()
            f1 = root / "a" / "dup.txt"
            f2 = root / "b" / "dup.txt"
            f1.write_text("one")
            f2.write_text("two")
            quarantine = root / "Q"
            adapter = LocalFSAdapter(
                run_id="r1", roots=[str(root)], quarantine_root=str(quarantine)
            )
            cap = _local_capability(root)
            actions = [
                _make_action("a1", f1, quarantine / "duplicates" / "dup.txt"),
                _make_action("a2", f2, quarantine / "duplicates" / "dup.txt"),
            ]
            results = apply_actions(adapter, actions, cap, str(root / "undo.jsonl"))
            for r in results:
                self.assertTrue(r.success, r.message)
            survivors = sorted(p.name for p in (quarantine / "duplicates").iterdir())
            # both files survived with distinct names
            self.assertEqual(survivors, ["dup (2).txt", "dup.txt"])

    def test_apply_then_undo_roundtrip(self):
        """End-to-end: scan-like actions -> apply -> undo restores
        originals at original paths."""
        from tempfile import TemporaryDirectory

        from sensei_clean.apply import apply_actions, load_undo_records, undo_actions

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "src"
            src.mkdir()
            f = src / "doc.txt"
            f.write_text("hello")
            quarantine = root / "Q"
            adapter = LocalFSAdapter(
                run_id="r1", roots=[str(src)], quarantine_root=str(quarantine)
            )
            cap = _local_capability(root)
            action = _make_action("a1", f, quarantine / "duplicates" / "doc.txt")
            undo_path = root / "undo.jsonl"
            apply_actions(adapter, [action], cap, str(undo_path))
            self.assertFalse(f.exists())
            self.assertTrue((quarantine / "duplicates" / "doc.txt").exists())

            records = load_undo_records(str(undo_path))
            self.assertEqual(len(records), 1)
            undo_results = undo_actions(adapter, records)
            self.assertTrue(all(r.success for r in undo_results))
            self.assertTrue(f.exists())
            self.assertFalse((quarantine / "duplicates" / "doc.txt").exists())


if __name__ == "__main__":
    unittest.main()
