"""
Validate that tests/ stays mapped to the source tree.

Reality-first convention of this repo:
- Most tests are loose files at tests/ root:  test_<module>.py must map to
  <module>.py at the repo root or scripts/.
- Tool tests (scripts/tools/*) live under tests/tools/.
- Genuinely unmatched loose files are listed in KNOWN_UNMAPPED (a cleanup
  backlog — shrink this set over time).
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

# Loose test files with no matching source module yet (cleanup backlog).
KNOWN_UNMAPPED = {
    "test_headless.py",  # tests headless_runner.py via sys.path dance
    "test_oauth_callback_rfc9207.py",  # extends scripts/oauth_callback.py (RFC variant)
    "test_skill_blueprint.py",  # tests scripts/skill_metadata.py (multi-class)
    "test_model_switch_reasoning.py",  # tests scripts/model_reasoning.py + model_switch.py
    "test_tests_tree_layout.py",  # this file
    "test_verifiers_extra.py",  # supplementary cases for verifiers.py
    "test_master_ai_core.py",  # smoke/e2e driver for master_ai.py
}

# Subdirectories under tests/ that are allowed (not name-mapped to a module).
ALLOWED_TEST_DIRS = {
    "__pycache__",
    ".pytest_cache",
    "fixtures",  # shared test fixtures
    "integration",  # cross-cutting integration tests
    "tools",  # mirrors scripts/tools/
    "_misc",  # tiny placeholder/self-describing tests
}


def _expected_module_for(test_name: str) -> str | None:
    m = re.match(r"test_(.+)\.py$", test_name)
    if not m:
        return None
    base = m.group(1)
    for c in (REPO_ROOT / f"{base}.py", REPO_ROOT / "scripts" / f"{base}.py"):
        if c.exists():
            return str(c.relative_to(REPO_ROOT))
    return None


def test_every_root_test_file_maps_to_a_source_module():
    """test_<module>.py at tests/ root maps to <module>.py (root or scripts/)."""
    unmapped = []
    for f in TESTS_ROOT.iterdir():
        if not f.is_file() or f.suffix != ".py" or f.name == "conftest.py":
            continue
        if f.name in KNOWN_UNMAPPED:
            continue
        if _expected_module_for(f.name) is None:
            unmapped.append(f.name)
    assert not unmapped, (
        "Loose test files with no matching source module:\n"
        + "\n".join(f"  tests/{n}" for n in unmapped)
        + "\nMove tool tests into tests/tools/ or fix the module name, "
        + "or add to KNOWN_UNMAPPED with a comment."
    )


def test_no_unexpected_directories():
    """Directories under tests/ must be an allowed subdir (no test-dir sprawl)."""
    unexpected = [
        e.name
        for e in TESTS_ROOT.iterdir()
        if e.is_dir() and e.name not in ALLOWED_TEST_DIRS
    ]
    assert not unexpected, (
        "Unexpected directories under tests/:\n"
        + "\n".join(f"  tests/{n}/" for n in unexpected)
        + "\nAdd to ALLOWED_TEST_DIRS with a comment, or remove."
    )


def test_tools_tests_live_in_tools_dir():
    """Tests importing scripts.tools.* live under tests/tools/."""
    misfiles = []
    for f in TESTS_ROOT.iterdir():
        if (
            not f.is_file()
            or f.suffix != ".py"
            or f.name
            in (
                "conftest.py",
                __name__.rsplit(".", 1)[-1] + ".py"
                if "." in __name__
                else "test_tests_tree_layout.py",
            )
        ):
            continue
        if "scripts.tools." in f.read_text(encoding="utf-8", errors="replace"):
            misfiles.append(f.name)
    assert not misfiles, (
        "These tests import scripts.tools.* but sit at tests/ root "
        "(move into tests/tools/):\n" + "\n".join(f"  tests/{n}" for n in misfiles)
    )


def test_tools_test_files_reference_real_modules():
    """Every tests/tools/test_*.py maps to an existing scripts/tools/*.py
    or an existing repo-root module (legacy placement kept on purpose)."""
    missing = []
    for f in (TESTS_ROOT / "tools").glob("test_*.py"):
        m = re.match(r"test_(.+)\.py$", f.name)
        if not m:
            continue
        base = m.group(1)
        if (
            not (REPO_ROOT / "scripts" / "tools" / f"{base}.py").exists()
            and not (REPO_ROOT / f"{base}.py").exists()
            and not (REPO_ROOT / "scripts" / f"{base}.py").exists()
        ):
            missing.append(f"{f.name} -> scripts/tools/{base}.py")
    assert not missing, "Missing tool source modules:\n" + "\n".join(missing)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
