"""
Validate that tests/ mirrors the source tree layout.

This test prevents drift: every non-script source module should have a
corresponding test directory under tests/, and every test directory should
map to a source module. Run via `scripts/run_tests.sh tests/`.
"""
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT
TESTS_ROOT = REPO_ROOT / "tests"

# Source modules/packages that should have mirrored test directories.
# Add new entries here when adding source modules.
EXPECTED_SOURCE_TO_TEST = {
    "master_ai.py": "core",
    "hooks.py": "core",
    "typed_actions.py": "core",
    "learning_loop.py": "learning",
    "skill_runtime.py": "skills",
    "router.py": "router",
    "scripts": "scripts",  # scripts/ tests live under tests/scripts/
}

# Directories under tests/ that are allowed without a direct source counterpart
# (e.g., shared fixtures, integration tests, etc.)
ALLOWED_TEST_ONLY_DIRS = {
    "conftest.py",      # root conftest
    "__pycache__",
    ".pytest_cache",
    "fixtures",         # shared test fixtures
    "integration",      # cross-cutting integration tests
}

def test_test_directories_mirror_source():
    """Every source module in EXPECTED_SOURCE_TO_TEST has a test directory."""
    missing = []
    for src, test_subdir in EXPECTED_SOURCE_TO_TEST.items():
        src_path = SRC_ROOT / src
        test_path = TESTS_ROOT / test_subdir
        if src_path.exists() and not test_path.exists():
            missing.append(f"{src} -> tests/{test_subdir}/ (missing)")
    assert not missing, "Missing test directories:\n" + "\n".join(missing)

def test_no_orphan_test_directories():
    """Every directory in tests/ maps to a known source module or is explicitly allowed."""
    orphaned = []
    for entry in TESTS_ROOT.iterdir():
        if entry.name in ALLOWED_TEST_ONLY_DIRS:
            continue
        if entry.is_dir():
            # Check if this test dir maps to any expected source
            mapped = any(entry.name == v for v in EXPECTED_SOURCE_TO_TEST.values())
            if not mapped:
                orphaned.append(f"tests/{entry.name}/ (no source mapping)")
    assert not orphaned, "Orphaned test directories:\n" + "\n".join(orphaned)

def test_no_loose_test_files_at_root():
    """Test files should live in subdirectories, not directly under tests/."""
    loose_files = [
        f.name for f in TESTS_ROOT.iterdir()
        if f.is_file() and f.suffix == ".py" and f.name != "conftest.py"
    ]
    assert not loose_files, (
        "Loose test files at tests/ root (move into subdirectories):\n"
        + "\n".join(f"  tests/{f}" for f in loose_files)
    )

def test_scripts_tests_mirror_scripts_structure():
    """tests/scripts/ should mirror the scripts/ directory structure."""
    scripts_src = SRC_ROOT / "scripts"
    scripts_tests = TESTS_ROOT / "scripts"
    if not scripts_src.exists():
        pytest.skip("scripts/ source directory not found")
    if not scripts_tests.exists():
        pytest.fail("tests/scripts/ missing — should mirror scripts/")

    # Each subdir in scripts/ should have a corresponding test subdir
    for src_subdir in scripts_src.iterdir():
        if src_subdir.is_dir() and not src_subdir.name.startswith("."):
            test_subdir = scripts_tests / src_subdir.name
            if not test_subdir.exists():
                pytest.fail(f"tests/scripts/{src_subdir.name}/ missing for scripts/{src_subdir.name}/")

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
