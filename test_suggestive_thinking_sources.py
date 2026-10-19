"""Tests for the real GATHER/RECALL sources (suggestive_thinking_sources.py).

All filesystem/git state is faked under tmp_path -- never touches the real
memory/ directory or real repos.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import suggestive_thinking_sources as src
from suggestive_thinking import Candidate


def _write_log(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n" if entries else "")


def test_gather_upstream_proposals_reads_only_proposal_status(tmp_path, monkeypatch):
    log = tmp_path / "upstream-integration-log.jsonl"
    _write_log(
        log,
        [
            {"ts": datetime.now().isoformat(), "sha": "a" * 40, "status": "proposal"},
            {"ts": datetime.now().isoformat(), "sha": "b" * 40, "status": "failed"},
            {"ts": datetime.now().isoformat(), "sha": "c" * 40, "status": "skipped"},
        ],
    )
    monkeypatch.setattr(src, "UPSTREAM_LOG", log)
    monkeypatch.setattr(src, "LOG", tmp_path / "suggestive-thinking-log.jsonl")
    monkeypatch.setattr(src, "UPSTREAM_PLANS", tmp_path / "upstream-plans")

    candidates = src.gather_upstream_proposals()
    assert [c.source for c in candidates] == [f"upstream-proposal:{'a' * 8}"]
    assert candidates[0].actionable == 1.0


def test_gather_upstream_proposals_skips_already_gathered_shas(tmp_path, monkeypatch):
    sha = "d" * 40
    log = tmp_path / "upstream-integration-log.jsonl"
    _write_log(
        log, [{"ts": datetime.now().isoformat(), "sha": sha, "status": "proposal"}]
    )
    st_log = tmp_path / "suggestive-thinking-log.jsonl"
    _write_log(
        st_log,
        [
            {
                "ts": datetime.now().isoformat(),
                "source": f"upstream-proposal:{sha[:8]}",
                "headline": "x",
                "surfaced": False,
                "reason": "stub",
            }
        ],
    )
    monkeypatch.setattr(src, "UPSTREAM_LOG", log)
    monkeypatch.setattr(src, "LOG", st_log)
    monkeypatch.setattr(src, "UPSTREAM_PLANS", tmp_path / "upstream-plans")

    assert src.gather_upstream_proposals() == []


def _init_repo(path, commit_age_days=None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=path, check=True)
    env = None
    if commit_age_days is not None:
        import os

        ts = (datetime.now() - timedelta(days=commit_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        env = dict(os.environ, GIT_AUTHOR_DATE=ts, GIT_COMMITTER_DATE=ts)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)


def test_gather_stale_projects_flags_only_repos_past_threshold(tmp_path):
    fresh = tmp_path / "fresh-repo"
    stale = tmp_path / "stale-repo"
    _init_repo(fresh, commit_age_days=0)
    _init_repo(stale, commit_age_days=10)

    candidates = src.gather_stale_projects(repos=[fresh, stale], stale_after_days=3)
    assert [c.source for c in candidates] == ["stale-project:stale-repo"]
    assert "10 days" in candidates[0].headline


def test_gather_stale_projects_skips_non_git_dirs(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    assert src.gather_stale_projects(repos=[not_a_repo]) == []


def test_recall_suppress_recent_repeats_drops_recently_logged_sources(
    tmp_path, monkeypatch
):
    log = tmp_path / "suggestive-thinking-log.jsonl"
    _write_log(
        log,
        [
            {
                "ts": datetime.now().isoformat(),
                "source": "stale-project:master-ai-cli",
                "headline": "old",
                "surfaced": False,
                "reason": "stub",
            }
        ],
    )
    monkeypatch.setattr(src, "LOG", log)
    candidates = [
        Candidate(
            source="stale-project:master-ai-cli", headline="new headline, same source"
        ),
        Candidate(source="stale-project:ai-controller", headline="different source"),
    ]
    result = src.recall_suppress_recent_repeats(candidates, within_days=3)
    assert [c.source for c in result] == ["stale-project:ai-controller"]


def test_recall_suppress_recent_repeats_ignores_entries_outside_window(
    tmp_path, monkeypatch
):
    log = tmp_path / "suggestive-thinking-log.jsonl"
    old_ts = (datetime.now() - timedelta(days=10)).isoformat()
    _write_log(
        log,
        [
            {
                "ts": old_ts,
                "source": "stale-project:master-ai-cli",
                "headline": "old",
                "surfaced": False,
                "reason": "stub",
            }
        ],
    )
    monkeypatch.setattr(src, "LOG", log)
    candidates = [Candidate(source="stale-project:master-ai-cli", headline="x")]
    result = src.recall_suppress_recent_repeats(candidates, within_days=3)
    assert len(result) == 1


def test_gather_all_combines_both_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "UPSTREAM_LOG", tmp_path / "nope.jsonl")
    monkeypatch.setattr(
        src, "gather_stale_projects", lambda: [Candidate(source="s", headline="h")]
    )
    assert len(src.gather_all()) == 1
