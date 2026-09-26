"""Tests for project-root-keyed skill registry."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from scripts.skill_registry import (
    get_skills,
    invalidate_cache,
    scan_skills,
    _resolve_project_tag,
)


def _write_skill(skill_dir: Path, name: str, entry: str = "main.py", desc: str = "") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.json").write_text(
        json.dumps({"name": name, "entry": entry, "description": desc})
    )


def test_project_skill_isolation(tmp_path: Path):
    """Two repos in one process each see only their own project skills."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    for r in (repo1, repo2):
        (r / ".git").mkdir(parents=True)
        (r / ".sensei" / "skills").mkdir(parents=True)

    _write_skill(repo1 / ".sensei" / "skills" / "skill_a", "skill_a")
    _write_skill(repo2 / ".sensei" / "skills" / "skill_b", "skill_b")

    # Session in repo1
    with tempfile.TemporaryDirectory() as _:
        import scripts.session_cwd as sc
        with sc.session_cwd_scope(repo1):
            invalidate_cache()
            skills = get_skills()
            assert "/skill_a" in skills
            assert "/skill_b" not in skills
            assert skills["/skill_a"]["source"] == "project"

    # Session in repo2
    with tempfile.TemporaryDirectory() as _:
        import scripts.session_cwd as sc
        with sc.session_cwd_scope(repo2):
            invalidate_cache()
            skills = get_skills()
            assert "/skill_b" in skills
            assert "/skill_a" not in skills
            assert skills["/skill_b"]["source"] == "project"


def test_home_skills_always_visible(tmp_path: Path):
    """Home skills visible regardless of project context."""
    home = tmp_path / "home"
    (home / ".sensei" / "skills" / "home_skill").mkdir(parents=True)
    _write_skill(home / ".sensei" / "skills" / "home_skill", "home_skill")

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    # Patch home tag
    import scripts.skill_registry as sr
    original_home = sr._resolve_home_tag
    sr._resolve_home_tag = lambda: str(home)

    try:
        with tempfile.TemporaryDirectory() as _:
            import scripts.session_cwd as sc
            with sc.session_cwd_scope(repo):
                invalidate_cache()
                skills = get_skills()
                assert "/home_skill" in skills
                assert skills["/home_skill"]["source"] == "home"
    finally:
        sr._resolve_home_tag = original_home


def test_cache_invalidation_on_project_change(tmp_path: Path):
    """Switching project root triggers rescan."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    for r in (repo1, repo2):
        (r / ".git").mkdir(parents=True)
        (r / ".sensei" / "skills").mkdir(parents=True)

    _write_skill(repo1 / ".sensei" / "skills" / "one", "one")
    _write_skill(repo2 / ".sensei" / "skills" / "two", "two")

    import scripts.session_cwd as sc

    with sc.session_cwd_scope(repo1):
        invalidate_cache()
        skills1 = get_skills()
        assert "/one" in skills1

    with sc.session_cwd_scope(repo2):
        # Cache should be stale (different project tag) -> rescan
        skills2 = get_skills()
        assert "/two" in skills2
        assert "/one" not in skills2


def test_resolve_project_tag_finds_git_root(tmp_path: Path):
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    subdir = repo / "sub" / "dir"
    subdir.mkdir(parents=True)

    import scripts.session_cwd as sc
    import scripts.skill_registry as sr

    with sc.session_cwd_scope(subdir):
        assert sr._resolve_project_tag() == str(repo.resolve())

    # Outside any repo
    with sc.session_cwd_scope(tmp_path / "nowhere"):
        assert sr._resolve_project_tag() is None
