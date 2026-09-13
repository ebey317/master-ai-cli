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
        self.assertIn(
            "- [ ] Review each commit for applicability", text
        )
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


if __name__ == "__main__":
    unittest.main()
