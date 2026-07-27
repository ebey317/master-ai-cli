#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

import harvest


class HarvestPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_harvest = harvest.HARVEST_FILE
        self.old_skip = harvest.PRIVATE_SKIP_FILE
        harvest.HARVEST_FILE = Path(self.tmp.name) / "harvest.jsonl"
        harvest.PRIVATE_SKIP_FILE = Path(self.tmp.name) / "private_skips.jsonl"
        harvest._entries = None
        harvest._last_mtime = 0

    def tearDown(self):
        harvest.HARVEST_FILE = self.old_harvest
        harvest.PRIVATE_SKIP_FILE = self.old_skip
        harvest._entries = None
        harvest._last_mtime = 0
        self.tmp.cleanup()

    def test_record_skips_private_paths(self):
        harvest.record(
            "Summarize /home/user/Pictures/family.jpg",
            "local",
            "This is a private photo.",
            task_type="local",
        )
        self.assertFalse(harvest.HARVEST_FILE.exists())
        self.assertTrue(harvest.PRIVATE_SKIP_FILE.exists())

    def test_few_shot_excludes_private_legacy_entries(self):
        harvest.HARVEST_FILE.write_text(
            '{"ts":1,"prompt":"change /home/user/Documents/tax.txt","model":"x","response":"EDIT: private","task_type":"local"}\n'
            '{"ts":2,"prompt":"write a bash status script","model":"x","response":"CREATE: /tmp/status.sh","task_type":"local"}\n'
        )
        examples = harvest.few_shot("write a bash script", max_examples=5, min_similarity=0.1)
        prompts = [e["prompt"] for e in examples]
        self.assertIn("write a bash status script", prompts)
        self.assertNotIn("change /home/user/Documents/tax.txt", prompts)

    def test_format_redacts_basic_contact_data(self):
        text = harvest.format_few_shot([{
            "prompt": "email me at person@example.com",
            "response": "call 555-123-4567",
            "similarity": 1.0,
            "model": "x",
        }])
        self.assertIn("[email]", text)
        self.assertIn("[phone]", text)
        self.assertNotIn("person@example.com", text)


if __name__ == "__main__":
    unittest.main()
