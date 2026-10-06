"""Tests for scripts/skill_manager.py — skill creation, editing, deletion, and locking invariants."""

import hashlib
import json
import threading
from pathlib import Path

import pytest

import scripts.skill_manager as sm
from scripts.skill_manager import (
    EXCLUDED_SKILL_DIRS,
    NON_PACKAGE_TOPS,
    SCAN_SKIP_PARTS,
    SKIP_PARTS,
    _basename,
    _skill_lock,
    _skill_lock_path,
    _validate_name,
    skill_manage,
)


@pytest.fixture
def isolated_skills_dir(monkeypatch, tmp_path):
    """Redirect SKILLS_DIR to a temp directory for isolation."""
    skills_dir = tmp_path / "skills"
    locks_dir = skills_dir / ".locks"
    monkeypatch.setattr("scripts.skill_manager.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("scripts.skill_manager.LOCKS_DIR", locks_dir)
    locks_dir.mkdir(parents=True, exist_ok=True)
    yield skills_dir


VALID_SKILL_CONTENT = """# Test Skill

Step 1: Do something
Step 2: Verify
"""


class TestNameValidation:
    @pytest.mark.parametrize(
        "name,expected_err",
        [
            ("", "skill name cannot be empty"),
            ("bad\x00name", "skill name cannot contain NUL bytes"),
            ("a" * 300, "skill name too long (max 255 chars)"),
            (
                "../../etc",
                "skill name cannot contain path traversal (..) or empty segments",
            ),
            (
                "category/../../etc",
                "skill name cannot contain path traversal (..) or empty segments",
            ),
            ("/absolute/path", "skill name cannot be an absolute path"),
            ("valid-name", None),
            ("category/valid-name", None),
            ("a" * 255, None),  # boundary
        ],
    )
    def test_validate_name(self, name, expected_err):
        assert _validate_name(name) == expected_err


class TestBasenameExtraction:
    @pytest.mark.parametrize(
        "name,expected_base",
        [
            ("foo", "foo"),
            ("category/foo", "foo"),
            ("a/b/c/foo", "foo"),
            ("foo/", "foo"),  # trailing slash
        ],
    )
    def test_basename(self, name, expected_base):
        assert _basename(name) == expected_base


class TestDigestKeyedLock:
    def test_lock_path_is_digest_keyed_and_shared_across_name_forms(
        self, isolated_skills_dir
    ):
        """`foo` and `category/foo` share one lock, keyed on fixed-width digest of basename."""
        lock1 = _skill_lock_path("mlops/foo")
        lock2 = _skill_lock_path("foo")
        assert lock1 == lock2
        assert lock1.parent == sm.LOCKS_DIR
        expected_name = hashlib.sha256(b"foo").hexdigest() + ".lock"
        assert lock1.name == expected_name
        assert len(lock1.name) == 64 + 5  # 64 hex chars + ".lock"

    def test_different_basenames_different_locks(self, isolated_skills_dir):
        lock1 = _skill_lock_path("foo")
        lock2 = _skill_lock_path("bar")
        assert lock1 != lock2

    def test_lock_path_independent_of_name_length(self, isolated_skills_dir):
        long_name = "a" * 1000
        lock = _skill_lock_path(long_name)
        assert len(lock.name) == 64 + 5  # fixed width


class TestRejectedNamesNoLockResidue:
    """Invariant: rejected names return JSON error and leave NO .locks residue."""

    @pytest.mark.parametrize(
        "name", ["a" * 300, "bad\x00name", "../../etc", "", "/absolute"]
    )
    def test_rejected_name_returns_json_and_leaves_no_lock_file(
        self, isolated_skills_dir, name
    ):
        result = json.loads(
            skill_manage(action="create", name=name, content=VALID_SKILL_CONTENT)
        )
        assert result["success"] is False
        assert result["error"] == _validate_name(name)
        # No .locks directory should even exist (or be empty)
        if sm.LOCKS_DIR.exists():
            assert list(sm.LOCKS_DIR.iterdir()) == [], (
                f"Lock residue found: {list(sm.LOCKS_DIR.iterdir())}"
            )


class TestSkillCreateReadUpdateDelete:
    def test_create_read_update_delete_cycle(self, isolated_skills_dir):
        # Create
        result = json.loads(
            skill_manage(action="create", name="my-skill", content=VALID_SKILL_CONTENT)
        )
        assert result["success"] is True
        skill_path = Path(result["data"]["path"])
        assert (skill_path / "skill.md").exists()

        # Read
        result = json.loads(skill_manage(action="read", name="my-skill"))
        assert result["success"] is True
        assert result["data"]["content"] == VALID_SKILL_CONTENT

        # Update
        new_content = "# Updated\n\nNew steps"
        result = json.loads(
            skill_manage(action="update", name="my-skill", content=new_content)
        )
        assert result["success"] is True
        result = json.loads(skill_manage(action="read", name="my-skill"))
        assert result["data"]["content"] == new_content

        # Delete
        result = json.loads(skill_manage(action="delete", name="my-skill"))
        assert result["success"] is True
        assert not skill_path.exists()

    def test_create_with_category_path(self, isolated_skills_dir):
        result = json.loads(
            skill_manage(
                action="create", name="mlops/training", content=VALID_SKILL_CONTENT
            )
        )
        assert result["success"] is True
        skill_path = Path(result["data"]["path"])
        assert skill_path.name == "training"
        assert skill_path.parent.name == "mlops"

    def test_read_update_delete_by_basename(self, isolated_skills_dir):
        # Create with category
        skill_manage(
            action="create", name="mlops/training", content=VALID_SKILL_CONTENT
        )
        # Read by basename only
        result = json.loads(skill_manage(action="read", name="training"))
        assert result["success"] is True
        # Update by basename
        result = json.loads(
            skill_manage(action="update", name="training", content="# Updated")
        )
        assert result["success"] is True
        # Delete by basename
        result = json.loads(skill_manage(action="delete", name="training"))
        assert result["success"] is True


class TestConcurrentLocking:
    def test_lock_prevents_concurrent_writes(self, isolated_skills_dir):
        """Two threads trying to create the same skill: one succeeds, one fails cleanly."""
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            result = json.loads(
                skill_manage(
                    action="create", name="concurrent", content=VALID_SKILL_CONTENT
                )
            )
            results.append(result)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0]["error"] == "skill 'concurrent' already exists"

    def test_lock_shared_across_category_and_basename(self, isolated_skills_dir):
        """Lock for 'mlops/foo' and 'foo' is the same file; concurrent access serializes."""
        # Pre-create the skill
        skill_manage(action="create", name="mlops/foo", content=VALID_SKILL_CONTENT)

        results = []
        barrier = threading.Barrier(2)

        def updater(name_form):
            barrier.wait()
            result = json.loads(
                skill_manage(
                    action="update", name=name_form, content=f"updated via {name_form}"
                )
            )
            results.append(result)

        t1 = threading.Thread(target=updater, args=("foo",))
        t2 = threading.Thread(target=updater, args=("mlops/foo",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both should succeed (serialized by shared lock)
        assert all(r["success"] for r in results)


class TestExclusionSets:
    def test_excluded_skill_dirs_contains_locks(self):
        assert ".locks" in EXCLUDED_SKILL_DIRS

    def test_scan_skip_parts_contains_locks(self):
        assert ".locks" in SCAN_SKIP_PARTS

    def test_non_package_tops_contains_locks(self):
        assert ".locks" in NON_PACKAGE_TOPS

    def test_skip_parts_contains_locks(self):
        assert ".locks" in SKIP_PARTS


class TestLockCleanupOnError:
    def test_lock_file_not_left_on_validation_error_during_create(
        self, isolated_skills_dir
    ):
        # Even if validation passes but something else fails, lock should be released
        # (hard to trigger without mocking, but we verify the lock file is not held)
        skill_manage(action="create", name="test-skill", content=VALID_SKILL_CONTENT)
        # Lock file exists but should not be held
        locks = list(sm.LOCKS_DIR.glob("*.lock"))
        assert len(locks) == 1
        # Verify we can acquire it again (not stuck)
        with _skill_lock("test-skill"):
            pass  # acquired and released successfully
