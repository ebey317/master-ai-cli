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


def test_gather_stale_projects_caps_to_most_stale(tmp_path, monkeypatch):
    # Widened scope (2026-10-20: operator lifted the finish-line lock,
    # "all my repositories") means dozens of repos can be stale at once --
    # the cap keeps one run from flooding with the entire backlog.
    monkeypatch.setattr(src, "MAX_STALE_SURFACED_PER_RUN", 2)
    repos = []
    for i, age in enumerate((4, 20, 10, 5)):
        repo = tmp_path / f"repo-{i}"
        _init_repo(repo, commit_age_days=age)
        repos.append(repo)
    candidates = src.gather_stale_projects(repos=repos, stale_after_days=3)
    assert len(candidates) == 2
    assert "20 days" in candidates[0].headline  # most stale first
    assert "10 days" in candidates[1].headline


def test_discover_all_repos_finds_git_dirs_one_level_deep(tmp_path, monkeypatch):
    monkeypatch.setattr(src.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "repo-a" / ".git").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "projects" / "repo-b" / ".git").mkdir(parents=True)
    # Dotfile tool install with its own git clone (e.g. .nvm) must not be
    # mistaken for one of the operator's actual repos.
    (tmp_path / ".nvm" / ".git").mkdir(parents=True)
    found = {p.name for p in src._discover_all_repos()}
    assert found == {"repo-a", "repo-b"}


def test_gather_gmail_unread_builds_one_candidate_from_json(tmp_path, monkeypatch):
    # Path objects are slotted -- can't monkeypatch .exists() on an
    # instance. Point the module's constants at real (empty) tmp files
    # instead, which naturally makes .exists() True.
    fake_py = tmp_path / "google_api.py"
    fake_py.touch()
    fake_python = tmp_path / "python"
    fake_python.touch()
    monkeypatch.setattr(src, "GOOGLE_API_PY", fake_py)
    monkeypatch.setattr(src, "GOOGLE_API_PYTHON", fake_python)
    payload = json.dumps(
        [
            {"subject": "Newest one", "from": "a@example.com"},
            {"subject": "Older one", "from": "b@example.com"},
        ]
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    candidates = src.gather_gmail_unread()
    assert len(candidates) == 1
    assert candidates[0].source == "gmail-unread:2"
    assert "Newest one" in candidates[0].headline
    assert "Older one" not in candidates[0].headline  # no body/snippet leakage


def test_gather_gmail_unread_empty_inbox_surfaces_nothing(tmp_path, monkeypatch):
    fake_py = tmp_path / "google_api.py"
    fake_py.touch()
    fake_python = tmp_path / "python"
    fake_python.touch()
    monkeypatch.setattr(src, "GOOGLE_API_PY", fake_py)
    monkeypatch.setattr(src, "GOOGLE_API_PYTHON", fake_python)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert src.gather_gmail_unread() == []


def test_gather_gmail_unread_missing_binaries_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "GOOGLE_API_PY", tmp_path / "does-not-exist.py")
    assert src.gather_gmail_unread() == []


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
    monkeypatch.setattr(src, "gather_gmail_unread", lambda: [])
    assert len(src.gather_all()) == 1


def test_vocalization_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")
    assert src.vocalization_enabled() is False


def test_set_vocalization_toggles_the_flag_file(tmp_path, monkeypatch):
    flag = tmp_path / "flag"
    monkeypatch.setattr(src, "VOCALIZE_FLAG", flag)
    src.set_vocalization(True)
    assert flag.exists()
    assert src.vocalization_enabled() is True
    src.set_vocalization(False)
    assert not flag.exists()
    assert src.vocalization_enabled() is False


def test_deliver_default_always_fires_desktop_notify_even_with_voice_off(
    tmp_path, monkeypatch, capsys
):
    # Found 2026-10-20: desktop notify must be unconditional -- it's the
    # actual delivery channel now, not gated on the voice toggle (see
    # deliver_default's docstring for why print()-alone was effectively
    # silent under a systemd --user unit).
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")  # does not exist
    desktop = tmp_path / "sensei-notify.sh"
    desktop.touch()
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", desktop)
    called = []
    monkeypatch.setattr(src.subprocess, "run", lambda *a, **k: called.append(a))
    src.deliver_default(Candidate(source="s", headline="quiet nudge"))
    assert len(called) == 1
    assert "quiet nudge" in called[0][0]
    assert "quiet nudge" in capsys.readouterr().out


def test_deliver_default_calls_voice_when_enabled(tmp_path, monkeypatch, capsys):
    flag = tmp_path / "flag"
    flag.touch()
    monkeypatch.setattr(src, "VOCALIZE_FLAG", flag)
    monkeypatch.setattr(src, "NOTIFY_VOICE", tmp_path / "notify_voice.sh")
    (tmp_path / "notify_voice.sh").touch()
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop-notify.sh")
    called = []
    monkeypatch.setattr(src.subprocess, "run", lambda *a, **k: called.append(a))
    src.deliver_default(Candidate(source="s", headline="loud nudge"))
    assert len(called) == 1
    assert "loud nudge" in called[0][0]


def test_deliver_default_voice_failure_never_raises(tmp_path, monkeypatch, capsys):
    flag = tmp_path / "flag"
    flag.touch()
    monkeypatch.setattr(src, "VOCALIZE_FLAG", flag)
    monkeypatch.setattr(src, "NOTIFY_VOICE", tmp_path / "notify_voice.sh")
    (tmp_path / "notify_voice.sh").touch()

    def _boom(*a, **k):
        raise OSError("tts server down")

    monkeypatch.setattr(src.subprocess, "run", _boom)
    src.deliver_default(Candidate(source="s", headline="should not crash"))  # no raise
