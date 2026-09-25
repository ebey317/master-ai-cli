#!/usr/bin/env python3
"""Regression tests for perpetual_review.py -- the Sensei-side review gate
for perpetual-watcher's generated proposals.

Runs entirely against a temp proposals directory; never touches the real
~/.master_ai_proposals/.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import perpetual_review as pr

SAMPLE_PROPOSAL = """# Proposal: {name} Updates (2026-09-13)

**Source**: {name}
**Commits**: aaaa111..bbbb222
**Category**: framework-improvement
**Priority**: high

## Summary
Some commits happened.

## Integration Plan
- [ ] Review each commit for applicability
- [ ] Create skill: `~/.master_ai_skills/<name>/` (for new skills)
- [ ] Update config: `~/scripts/howwework.txt` or `~/.master_ai_settings`
- [ ] Modify core: `~/scripts/master_ai.py` (specific functions)
- [ ] Add tests: `~/tests/...`

## Testing
- How to verify the integration works
- Regression risks

## Decision
- [ ] Approve
- [ ] Reject
- [ ] Defer
- [ ] Modify: <notes>
"""


class PerpetualReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_dir = pr.PROPOSALS_DIR
        pr.PROPOSALS_DIR = Path(self._tmpdir)

    def tearDown(self):
        pr.PROPOSALS_DIR = self._orig_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, name):
        path = pr.PROPOSALS_DIR / f"2026-09-13_{name}_proposal.md"
        path.write_text(SAMPLE_PROPOSAL.format(name=name))
        return path.stem

    def test_list_pending_returns_undecided_proposals(self):
        self._write("alpha")
        self._write("beta")
        pending = pr.list_pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual({e["priority"] for e in pending}, {"high"})

    def test_approve_checks_only_the_decision_box(self):
        stem = self._write("gamma")
        ok, msg = pr.approve(stem)
        self.assertTrue(ok)
        self.assertIn("marked approve", msg)

        text = (pr.PROPOSALS_DIR / f"{stem}.md").read_text()
        # Decision box checked...
        self.assertIn("- [x] Approve", text)
        self.assertIn("- [ ] Reject", text)
        # ...but the unrelated Integration Plan checkboxes are untouched.
        self.assertIn("- [ ] Review each commit for applicability", text)
        self.assertIn("- [ ] Create skill:", text)

    def test_decided_proposal_is_no_longer_pending(self):
        stem = self._write("delta")
        pr.approve(stem)
        self.assertEqual(pr.list_pending(), [])

    def test_second_decision_is_refused(self):
        stem = self._write("epsilon")
        pr.approve(stem)
        ok, msg = pr.reject(stem)
        self.assertFalse(ok)
        self.assertIn("already decided", msg)

    def test_partial_id_resolves_when_unique(self):
        self._write("zeta")
        p = pr.get("zeta")
        self.assertIsNotNone(p)
        self.assertEqual(p["source"], "zeta")

    def test_ambiguous_partial_id_does_not_resolve(self):
        self._write("eta-one")
        self._write("eta-two")
        self.assertIsNone(pr.get("eta"))

    def test_unknown_id_returns_none_not_an_error(self):
        self.assertIsNone(pr.get("does-not-exist"))
        ok, msg = pr.approve("does-not-exist")
        self.assertFalse(ok)
        self.assertIn("no proposal matching", msg)

    def test_checkbox_outside_decision_section_is_ignored(self):
        # CodeRabbit finding, 2026-09-13: decision matching used to search
        # the whole file. A stray "- [x] Reject"-shaped line anywhere else
        # (e.g. someone's own note in the Summary, or a future template
        # change) must not be mistaken for an actual decision, and
        # approve()/reject() must never touch it.
        stem = self._write("theta")
        path = pr.PROPOSALS_DIR / f"{stem}.md"
        text = path.read_text()
        text = text.replace(
            "## Summary\nSome commits happened.",
            "## Summary\nSome commits happened.\n- [x] Reject (this is just a note, not a decision)",
        )
        path.write_text(text)

        self.assertIsNone(pr.get(stem)["decision"])
        pending = pr.list_pending()
        self.assertEqual(len(pending), 1)

        ok, msg = pr.approve(stem)
        self.assertTrue(ok, msg)
        text_after = path.read_text()
        # The real Decision section got the approve mark...
        self.assertIn("- [x] Approve", text_after)
        # ...and the stray note-line above it was never touched.
        self.assertIn("- [x] Reject (this is just a note, not a decision)", text_after)

    def test_path_traversal_via_absolute_id_is_rejected(self):
        # CodeRabbit finding, 2026-09-13 (CWE-22): Path(base) / "/abs/x"
        # silently discards `base` in pathlib, so an absolute proposal_id
        # used to let _find() escape PROPOSALS_DIR entirely.
        outside = Path(tempfile.mkdtemp()) / "not-a-real-proposal"
        outside.write_text("not a proposal, just a decoy file")
        try:
            self.assertIsNone(pr.get(str(outside)))
            ok, msg = pr.approve(str(outside))
            self.assertFalse(ok)
            self.assertIn("no proposal matching", msg)
            # And the decoy file itself must be untouched.
            self.assertEqual(outside.read_text(), "not a proposal, just a decoy file")
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

    def test_path_traversal_via_dotdot_is_rejected(self):
        stem = self._write("iota")
        outside_name = f"../{Path(tempfile.mkdtemp()).name}/escaped"
        self.assertIsNone(pr.get(outside_name))

    def test_symlink_escaping_proposals_dir_is_rejected(self):
        outside_dir = Path(tempfile.mkdtemp())
        outside_file = outside_dir / "real_secret.md"
        outside_file.write_text("outside content")
        link = pr.PROPOSALS_DIR / "2026-09-13_sneaky_proposal.md"
        try:
            link.symlink_to(outside_file)
            self.assertIsNone(pr.get("sneaky"))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_decision_write_is_atomic_no_temp_file_left_behind(self):
        stem = self._write("kappa")
        pr.approve(stem)
        leftovers = list(pr.PROPOSALS_DIR.glob(f".{stem}.md.*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
