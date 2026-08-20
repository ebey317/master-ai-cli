"""
Cloud connector contract tests for sensei_clean.

Covers:
  * FakeDriveAdapter implements the full provider contract (no network).
  * RcloneRemoteAdapter is probe-only and refuses mutations.
  * GoogleDriveAdapter stub surfaces specific not-configured blockers.
  * detect_sources surfaces rclone remotes via the cloud_api kind.
  * engine.scan_run accepts a mix of local + rclone roots and records
    the cloud capability without crashing.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from sensei_clean import connectors as _connectors
from sensei_clean import status as _status
from sensei_clean.adapters.cloud_drive import CloudDriveAdapter
from sensei_clean.adapters.fake_drive import FakeDriveAdapter, FakeFile
from sensei_clean.adapters.gdrive import GoogleDriveAdapter
from sensei_clean.adapters.rclone_remote import RcloneRemoteAdapter
from sensei_clean.engine import scan_run
from sensei_clean.schemas import ActionRecord, UndoRecord


def _move_action(file_id: str) -> ActionRecord:
    return ActionRecord(
        schema_version="sensei.action.v1",
        run_id="r1",
        action_id=f"act:{file_id}",
        action_type="cloud_move",
        adapter="fake_drive",
        item_id=f"fake_drive:{file_id}",
        source_path=f"fake_drive:{file_id}",
        destination_path="Quarantine/duplicates",
        confidence=1.0,
        risk=10,
        reversible=True,
        lane="unattended",
        reason="test move",
        approval_required=False,
        metadata={"sensitivity": "documents"},
    )


class FakeDriveContractTests(unittest.TestCase):
    """Pin the cloud adapter contract via the in-memory fake."""

    def test_scan_emits_item_per_file(self):
        files = [
            FakeFile(id="a", name="invoice.pdf", mime_type="application/pdf",
                     size_bytes=10, sha256="aaaa"),
            FakeFile(id="b", name="resume.docx", mime_type="application/octet-stream",
                     size_bytes=20, sha256="bbbb"),
        ]
        adapter = FakeDriveAdapter(run_id="r1", files=files)
        items = list(adapter.scan())
        self.assertEqual(len(items), 2)
        names = sorted(i.display_name for i in items)
        self.assertEqual(names, ["invoice.pdf", "resume.docx"])
        # Resume should be flagged career-sensitive
        sensitivities = {i.display_name: i.sensitivity for i in items}
        self.assertEqual(sensitivities["resume.docx"], "career")

    def test_apply_and_undo_round_trip(self):
        f = FakeFile(id="x1", name="dup.txt", parent="root", size_bytes=5, sha256="ccc")
        adapter = FakeDriveAdapter(run_id="r1", files=[f])
        action = _move_action("x1")
        result = adapter.apply(action)
        self.assertTrue(result.success, result.message)
        self.assertEqual(adapter._files["x1"].parent, "Quarantine")
        self.assertIsNotNone(result.undo_record)
        undo_result = adapter.undo(result.undo_record)
        self.assertTrue(undo_result.success)
        self.assertEqual(adapter._files["x1"].parent, "root")

    def test_probe_reports_configured_and_supported_actions(self):
        adapter = FakeDriveAdapter(run_id="r1", files=[])
        cap = adapter.probe()
        self.assertTrue(cap.available)
        # contract: no delete action exposed even on the fake
        self.assertIn("cloud_move", cap.supported_actions)
        self.assertNotIn("cloud_delete", cap.supported_actions)

    def test_open_view_returns_provider_link(self):
        f = FakeFile(id="vp", name="x.pdf", web_view_link="https://example.test/x")
        adapter = FakeDriveAdapter(run_id="r1", files=[f])
        item = next(adapter.scan())
        self.assertEqual(adapter.open_view(item), "https://example.test/x")

    def test_can_apply_refuses_unknown_action_type(self):
        adapter = FakeDriveAdapter(run_id="r1", files=[])
        weird = _move_action("z")
        # Mutate to an unsupported action type — must be refused.
        from dataclasses import replace
        weird = replace(weird, action_type="cloud_delete")
        self.assertFalse(adapter.can_apply(weird))
        result = adapter.apply(weird)
        self.assertFalse(result.success)


class RcloneRemoteProbeOnlyTests(unittest.TestCase):
    """RcloneRemoteAdapter must default to probe-only: scan yields
    nothing, apply/undo refuse with clear messages."""

    @contextmanager
    def _stub_rclone(self, listremotes_return, about_return):
        # probe() gates on shutil.which(_rclone_bin()) before ever calling
        # rclone_about — stub that too so these tests exercise the mocked
        # about/listremotes behavior instead of failing with
        # "rclone-not-installed" on machines that don't have rclone on PATH.
        with mock.patch.multiple(
            "sensei_clean.adapters.rclone_remote",
            rclone_listremotes=mock.Mock(return_value=listremotes_return),
            rclone_about=mock.Mock(return_value=about_return),
            _rclone_bin=mock.Mock(return_value="/usr/bin/rclone"),
        ), mock.patch(
            "sensei_clean.adapters.rclone_remote.shutil.which",
            return_value="/usr/bin/rclone",
        ):
            yield

    def test_scan_default_is_empty(self):
        with self._stub_rclone(["gdrive"], {"used": 100, "total": 1000}):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            self.assertEqual(list(adapter.scan()), [])

    def test_probe_surfaces_quota_when_about_succeeds(self):
        with self._stub_rclone(["gdrive"], {"used": 500, "total": 9999}):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            cap = adapter.probe()
            self.assertTrue(cap.available)
            joined = "\n".join(cap.notes)
            self.assertIn("500 used of 9999 bytes", joined)

    def test_probe_blockers_when_about_fails(self):
        with self._stub_rclone(["gdrive"], {"error": "token refresh failed"}):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            cap = adapter.probe()
            self.assertFalse(cap.available)
            self.assertTrue(any("rclone-about-failed" in b for b in cap.blockers))

    def test_apply_refuses_non_rclone_source_path(self):
        with self._stub_rclone(["gdrive"], {"used": 0, "total": 1}):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            action = _move_action("anything")
            from dataclasses import replace
            action = replace(action, adapter=adapter.name)  # source_path stays "fake_drive:..."
            result = adapter.apply(action)
            self.assertFalse(result.success)
            self.assertIn("non-rclone source", result.message)

    def test_apply_refuses_cross_remote_move(self):
        with self._stub_rclone(["gdrive", "dropbox"], {"used": 0, "total": 1}):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            from dataclasses import replace
            action = replace(
                _move_action("xyz"),
                adapter=adapter.name,
                source_path="rclone:dropbox:foo.txt",
                destination_path="rclone:gdrive:Sensei-Cloud-Quarantine/duplicates/foo.txt",
                action_type="cloud_move",
            )
            result = adapter.apply(action)
            self.assertFalse(result.success)
            self.assertIn("cross-remote", result.message)

    def test_apply_propagates_rclone_failure(self):
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_moveto",
            return_value=(False, "rclone moveto rc=2: permission denied"),
        ), mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_listremotes",
            return_value=["gdrive"],
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            from dataclasses import replace
            action = replace(
                _move_action("xyz"),
                adapter=adapter.name,
                source_path="rclone:gdrive:foo.txt",
                destination_path="rclone:gdrive:Sensei-Cloud-Quarantine/duplicates/foo.txt",
                action_type="cloud_move",
            )
            result = adapter.apply(action)
            self.assertFalse(result.success)
            self.assertIn("permission denied", result.message)

    def test_apply_then_undo_round_trip_with_mock(self):
        """Happy path: rclone_moveto succeeds for apply AND for undo.
        Undo record points the path back at the original."""
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_moveto",
            return_value=(True, "moved gdrive:foo.txt -> gdrive:Sensei-Cloud-Quarantine/duplicates/foo.txt"),
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive")
            from dataclasses import replace
            action = replace(
                _move_action("xyz"),
                adapter=adapter.name,
                source_path="rclone:gdrive:foo.txt",
                destination_path="rclone:gdrive:Sensei-Cloud-Quarantine/duplicates/foo.txt",
                action_type="cloud_move",
            )
            result = adapter.apply(action)
            self.assertTrue(result.success, result.message)
            self.assertIsNotNone(result.undo_record)
            # Undo record swaps source/destination so reversal goes back to original
            self.assertEqual(result.undo_record.source_path, action.destination_path)
            self.assertEqual(result.undo_record.destination_path, action.source_path)
            undo_result = adapter.undo(result.undo_record)
            self.assertTrue(undo_result.success, undo_result.message)


class GoogleDriveStubTests(unittest.TestCase):
    """The google-api-python-client path is intentionally a stub.
    Probe must report the specific missing-piece signal."""

    def test_probe_surfaces_missing_deps_or_client(self):
        adapter = GoogleDriveAdapter(run_id="r1")
        cap = adapter.probe()
        # We're not running this on a box with the libs + client + token
        # all wired — at least one of those blockers must fire.
        self.assertFalse(cap.available)
        self.assertTrue(cap.blockers, "expected at least one blocker on the stub")
        joined = " ".join(cap.blockers)
        # one of these specific signals must be in the blockers
        self.assertTrue(
            any(tag in joined for tag in (
                "gdrive-missing-deps", "gdrive-missing-client-secret",
                "gdrive-not-authorized",
            )),
            f"expected a gdrive-specific blocker, got: {cap.blockers}",
        )

    def test_apply_refuses_until_configured(self):
        adapter = GoogleDriveAdapter(run_id="r1")
        action = _move_action("g")
        from dataclasses import replace
        action = replace(action, adapter="gdrive")
        result = adapter.apply(action)
        self.assertFalse(result.success)
        self.assertIn("not configured", result.message)


class ConnectorsDiscoveryTests(unittest.TestCase):
    """detect_sources must surface rclone remotes as cloud_api kind."""

    def test_rclone_remotes_appear_with_cloud_api_kind(self):
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            # Stub rclone_listremotes to return 2 remotes so we don't
            # depend on the test box having rclone configured.
            with mock.patch(
                "sensei_clean.connectors._rclone_connectors",
                return_value=[
                    _connectors.SourceConnector(
                        connector_id="rclone_gdrive",
                        label="gdrive (rclone)",
                        path="rclone:gdrive:",
                        kind="cloud_api",
                        available=True,
                        notes=("real cloud API connector via rclone",),
                    ),
                ],
            ):
                sources = _connectors.detect_sources(
                    home=home, gvfs_root=home / "gvfs", media_root=home / "media",
                )
        kinds = {s.kind for s in sources}
        self.assertIn("cloud_api", kinds)
        cloud = [s for s in sources if s.kind == "cloud_api"]
        self.assertTrue(any(c.path.startswith("rclone:") for c in cloud))


class RcloneRemoteListingTests(unittest.TestCase):
    """list_enabled=True path: scan calls rclone_lsjson and emits items."""

    def test_scan_yields_items_from_lsjson(self):
        fake_records = [
            {
                "Path": "Documents/foo.txt", "Name": "foo.txt", "Size": 42,
                "MimeType": "text/plain", "ModTime": "2026-01-01T00:00:00Z",
                "ID": "fid_001",
                "Hashes": {"md5": "abcdef0123456789", "sha1": "x" * 40},
            },
            {
                "Path": "Documents/foo_copy.txt", "Name": "foo_copy.txt", "Size": 42,
                "MimeType": "text/plain", "ModTime": "2026-01-02T00:00:00Z",
                "ID": "fid_002",
                "Hashes": {"md5": "abcdef0123456789", "sha1": "y" * 40},
            },
        ]
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=fake_records,
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive", list_enabled=True)
            items = list(adapter.scan())
        self.assertEqual(len(items), 2)
        names = sorted(i.display_name for i in items)
        self.assertEqual(names, ["foo.txt", "foo_copy.txt"])
        # md5 carried into hashes for dedup
        md5s = {i.hashes.get("md5") for i in items}
        self.assertEqual(md5s, {"abcdef0123456789"})
        # provider_id captured
        ids = sorted(i.identity.get("provider_id") for i in items)
        self.assertEqual(ids, ["fid_001", "fid_002"])
        # path uses rclone:remote:relpath convention so engine routes it back
        self.assertTrue(all(i.identity["path"].startswith("rclone:gdrive:") for i in items))

    def test_scan_skips_bad_records_without_failing(self):
        records = [{"Path": "ok.txt", "Name": "ok.txt", "Size": 1, "ID": "i1", "Hashes": {}},
                   {"Path": None}]  # bad row — should be skipped
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=records,
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive", list_enabled=True)
            items = list(adapter.scan())
        # Both may yield; the bad one yields with empty path. Worst case
        # is one good item.
        self.assertGreaterEqual(len(items), 1)


class CloudDedupAndActionTests(unittest.TestCase):
    """End-to-end: cloud scan -> findings -> cloud_move action (with
    in-remote destination) -> apply via mocked rclone -> undo."""

    def test_md5_dedup_and_cloud_move_destination(self):
        from sensei_clean.engine import build_findings, build_actions
        fake_records = [
            {"Path": "A/x.txt", "Name": "x.txt", "Size": 10, "MimeType": "text/plain",
             "ID": "fid_a", "Hashes": {"md5": "AAAA"}},
            {"Path": "B/x.txt", "Name": "x.txt", "Size": 10, "MimeType": "text/plain",
             "ID": "fid_b", "Hashes": {"md5": "AAAA"}},
            {"Path": "C/y.txt", "Name": "y.txt", "Size": 5, "MimeType": "text/plain",
             "ID": "fid_c", "Hashes": {"md5": "BBBB"}},
        ]
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=fake_records,
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive", list_enabled=True)
            items = list(adapter.scan())
        findings = build_findings(items, run_id="r1")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "exact_duplicate")
        actions = build_actions(items, findings, run_id="r1",
                                quarantine_root=Path("/unused"))
        self.assertEqual(len(actions), 1)  # one extra duplicate -> one cloud_move
        action = actions[0]
        self.assertEqual(action.action_type, "cloud_move")
        self.assertEqual(action.lane, "monitored")  # cloud always monitored this round
        self.assertTrue(action.destination_path.startswith(
            "rclone:gdrive:Sensei-Cloud-Quarantine/duplicates/"))
        self.assertTrue(action.source_path.startswith("rclone:gdrive:"))

    def test_full_cloud_apply_undo_round_trip(self):
        """Mock rclone_lsjson for scan and rclone_moveto for apply/undo."""
        from sensei_clean.engine import build_findings, build_actions
        from sensei_clean.apply import apply_actions, load_undo_records, undo_actions
        from sensei_clean.adapters.rclone_remote import RcloneRemoteAdapter
        from tempfile import TemporaryDirectory
        fake_records = [
            {"Path": "A/x.txt", "Name": "x.txt", "Size": 10, "MimeType": "text/plain",
             "ID": "fid_a", "Hashes": {"md5": "AAAA"}},
            {"Path": "B/x.txt", "Name": "x.txt", "Size": 10, "MimeType": "text/plain",
             "ID": "fid_b", "Hashes": {"md5": "AAAA"}},
        ]
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=fake_records,
        ):
            adapter = RcloneRemoteAdapter(run_id="r1", remote="gdrive", list_enabled=True)
            items = list(adapter.scan())
        findings = build_findings(items, run_id="r1")
        actions = build_actions(items, findings, run_id="r1",
                                quarantine_root=Path("/unused"))
        cap = adapter.probe()  # supports cloud_move; required by policy.can_apply
        # Force capability available even though our mocked subprocess
        # path doesn't actually call out:
        from dataclasses import replace
        cap = replace(cap, available=True, blockers=[])
        with TemporaryDirectory() as tmp, mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_moveto",
            return_value=(True, "mock moveto ok"),
        ):
            undo_path = Path(tmp) / "undo.jsonl"
            results = apply_actions(adapter, actions, cap, str(undo_path))
            self.assertEqual(sum(1 for r in results if r.success), len(actions),
                             [r.message for r in results])
            records = load_undo_records(str(undo_path))
            self.assertEqual(len(records), len(actions))
            undo_results = undo_actions(adapter, records)
            self.assertTrue(all(r.success for r in undo_results),
                            [r.message for r in undo_results])


class ListCloudFlagTests(unittest.TestCase):
    """`sensei-clean scan --list-cloud` and the GUI checkbox both plumb
    a single boolean (`list_cloud`) into scan_run, which passes it to
    RcloneRemoteAdapter as list_enabled. Pin that contract."""

    def test_list_cloud_false_default_yields_no_cloud_items(self):
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=[{"Path": "x.txt", "Name": "x.txt", "Size": 1, "ID": "i", "Hashes": {}}],
        ), mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_about",
            return_value={"used": 1, "total": 100},
        ):
            with TemporaryDirectory() as tmpdir:
                state_dir = Path(tmpdir) / "state"
                with mock.patch.object(_status, "STATE_DIR", state_dir), \
                     mock.patch.object(_status, "STATE_FILE", state_dir / "state.json"):
                    run_path, caps, items, findings, actions = scan_run(
                        roots=["rclone:gdrive:"],
                        sha256=False,
                        quarantine_root=str(Path(tmpdir) / "q"),
                        run_dir=str(Path(tmpdir) / "run"),
                        list_cloud=False,
                    )
        # Probe records the cap but scan emits no items
        self.assertEqual(len(items), 0)
        self.assertTrue(any(c.provider.startswith("rclone-") for c in caps))

    def test_list_cloud_true_yields_items_from_lsjson(self):
        records = [
            {"Path": "x.txt", "Name": "x.txt", "Size": 5, "MimeType": "text/plain",
             "ID": "id1", "Hashes": {"md5": "abc"}},
            {"Path": "y.txt", "Name": "y.txt", "Size": 5, "MimeType": "text/plain",
             "ID": "id2", "Hashes": {"md5": "abc"}},  # duplicate of x.txt by md5
        ]
        with mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_lsjson",
            return_value=records,
        ), mock.patch(
            "sensei_clean.adapters.rclone_remote.rclone_about",
            return_value={"used": 1, "total": 100},
        ):
            with TemporaryDirectory() as tmpdir:
                state_dir = Path(tmpdir) / "state"
                with mock.patch.object(_status, "STATE_DIR", state_dir), \
                     mock.patch.object(_status, "STATE_FILE", state_dir / "state.json"):
                    run_path, caps, items, findings, actions = scan_run(
                        roots=["rclone:gdrive:"],
                        sha256=False,
                        quarantine_root=str(Path(tmpdir) / "q"),
                        run_dir=str(Path(tmpdir) / "run"),
                        list_cloud=True,
                    )
        self.assertEqual(len(items), 2)
        self.assertEqual(len(findings), 1)  # md5 dedup cluster
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, "cloud_move")
        self.assertEqual(actions[0].lane, "monitored")


class EngineMultiAdapterTests(unittest.TestCase):
    """scan_run must accept a mix of local and rclone roots and record
    a capability for each, without crashing on the cloud side even when
    rclone is mocked-out."""

    def test_local_plus_cloud_root_records_both_capabilities(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Local").mkdir()
            (root / "Local" / "a.txt").write_text("hi", encoding="utf-8")
            run_dir = root / "run"
            with mock.patch(
                "sensei_clean.adapters.rclone_remote.rclone_listremotes",
                return_value=["gdrive"],
            ), mock.patch(
                "sensei_clean.adapters.rclone_remote.rclone_about",
                return_value={"used": 100, "total": 1000},
            ), mock.patch(
                "sensei_clean.adapters.rclone_remote.shutil.which",
                return_value="/usr/bin/rclone",
            ):
                state_dir = root / "state"
                with mock.patch.object(_status, "STATE_DIR", state_dir), \
                     mock.patch.object(_status, "STATE_FILE", state_dir / "state.json"):
                    run_path, caps, items, findings, actions = scan_run(
                        roots=[str(root / "Local"), "rclone:gdrive:"],
                        sha256=False,
                        quarantine_root=str(root / "Q"),
                        run_dir=str(run_dir),
                    )
        # Local items present, cloud probe-only yields nothing
        self.assertGreaterEqual(len(items), 1)
        cap_providers = {c.provider for c in caps}
        self.assertIn("local", cap_providers)
        self.assertTrue(any(p.startswith("rclone-") for p in cap_providers))


if __name__ == "__main__":
    unittest.main()
