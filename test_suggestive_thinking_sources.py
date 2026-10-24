"""Tests for the real GATHER/RECALL sources (suggestive_thinking_sources.py).

All filesystem/git state is faked under tmp_path -- never touches the real
memory/ directory or real repos.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

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


def _write_bridge_creds(path, accounts):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"accounts": accounts}))


def test_gather_email_bridge_unread_skips_gmail_and_reports_others(
    tmp_path, monkeypatch
):
    creds = tmp_path / "credentials.json"
    _write_bridge_creds(
        creds,
        {
            "gmail": {"provider": "gmail", "username": "a@gmail.com", "password": "x"},
            "outlook": {
                "provider": "outlook",
                "username": "b@outlook.com",
                "password": "x",
                "display_name": "Elijah",
            },
            "aol": {"provider": "aol", "username": "c@aol.com", "password": "x"},
        },
    )
    monkeypatch.setattr(src, "EMAIL_BRIDGE_CREDS", creds)

    def fake_count(acct):
        # gmail must never be asked -- it's skipped before this is called.
        assert acct.get("provider") != "gmail"
        return 3 if acct.get("provider") == "outlook" else 0

    monkeypatch.setattr(src, "_email_bridge_unread_count", fake_count)
    candidates = src.gather_email_bridge_unread()
    assert len(candidates) == 1
    assert candidates[0].source == "email-bridge:outlook:3"
    assert "Elijah" in candidates[0].headline


def test_gather_email_bridge_unread_no_creds_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "EMAIL_BRIDGE_CREDS", tmp_path / "missing.json")
    assert src.gather_email_bridge_unread() == []


def test_email_bridge_unread_count_returns_none_on_connect_failure(monkeypatch):
    class _BoomIMAP:
        def __init__(self, *a, **k):
            raise OSError("host unreachable")

    import imaplib

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BoomIMAP)
    acct = {
        "provider": "aol",
        "username": "c@aol.com",
        "password": "x",
        "imap_host": "imap.aol.com",
        "imap_port": 993,
    }
    assert src._email_bridge_unread_count(acct) is None


def test_gather_sensei_chats_flags_stale_newest_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "SENSEI_CHATS_DIR", tmp_path)
    old_chat = tmp_path / "old-session.chat"
    old_chat.write_text("transcript")
    old_ts = (datetime.now() - timedelta(days=5)).timestamp()
    import os

    os.utime(old_chat, (old_ts, old_ts))
    candidates = src.gather_sensei_chats()
    assert len(candidates) == 1
    assert candidates[0].source == "sensei-chat-stale"
    assert "5 days" in candidates[0].headline
    assert "old-session" in candidates[0].headline


def test_gather_sensei_chats_recent_chat_surfaces_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "SENSEI_CHATS_DIR", tmp_path)
    (tmp_path / "fresh.chat").write_text("transcript")
    assert src.gather_sensei_chats() == []


def test_gather_sensei_chats_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "SENSEI_CHATS_DIR", tmp_path / "does-not-exist")
    assert src.gather_sensei_chats() == []


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
    monkeypatch.setattr(src, "gather_email_bridge_unread", lambda: [])
    monkeypatch.setattr(src, "gather_sensei_chats", lambda: [])
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


# ── 2026-10-23 fix order: proposals-not-facts, outcome gate, real execution ──


def test_gather_stale_projects_produces_a_proposal_shaped_candidate(tmp_path):
    stale = tmp_path / "stale-repo"
    _init_repo(stale, commit_age_days=10)
    [candidate] = src.gather_stale_projects(repos=[stale], stale_after_days=3)
    assert (
        candidate.question
        == "Want me to run stale-repo's test suite and report what's red?"
    )
    assert "pass/fail report" in candidate.deliverable
    assert candidate.action_type == "repo_test_check"
    assert candidate.action_payload == {"repo_path": str(stale)}
    assert candidate.outcome == 0.8


def test_repo_path_allowed_accepts_a_repo_directly_under_an_allowed_root(tmp_path):
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert src._repo_path_allowed(repo, roots=[tmp_path]) is True


def test_repo_path_allowed_rejects_a_path_outside_the_allowed_roots(tmp_path):
    outside = tmp_path / "elsewhere" / "toy-repo"
    outside.mkdir(parents=True)
    (outside / ".git").mkdir()
    assert src._repo_path_allowed(outside, roots=[tmp_path]) is False


def test_repo_path_allowed_rejects_traversal_out_of_an_allowed_root(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    escape = allowed_root / ".." / "not-allowed"
    escape.mkdir(parents=True)
    (escape / ".git").mkdir()
    assert src._repo_path_allowed(escape, roots=[allowed_root]) is False


def test_repo_path_allowed_rejects_a_directory_with_no_git(tmp_path):
    not_a_repo = tmp_path / "just-a-folder"
    not_a_repo.mkdir()
    assert src._repo_path_allowed(not_a_repo, roots=[tmp_path]) is False


def test_repo_path_allowed_rejects_dotfile_directories(tmp_path):
    """OCR finding 2026-10-23: the original version only checked the
    parent directory, which would have accepted something like ~/.ssh if
    it happened to contain a .git dir. Dotfile-named targets are refused
    outright, mirroring _discover_all_repos()'s own skip rule."""
    dotdir = tmp_path / ".ssh"
    dotdir.mkdir()
    (dotdir / ".git").mkdir()
    assert src._repo_path_allowed(dotdir, roots=[tmp_path]) is False


def test_repo_test_check_handler_reports_green_for_passing_pytest_suite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: True)
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / "test_toy.py").write_text("def test_ok():\n    assert True\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert result.startswith("GREEN (exit 0)")


def test_repo_test_check_handler_reports_red_for_failing_pytest_suite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: True)
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / "test_toy.py").write_text("def test_fail():\n    assert False\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert result.startswith("RED")


def test_repo_test_check_handler_reports_no_runner_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: True)
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert "no recognized test runner" in result


def test_repo_test_check_handler_raises_on_missing_payload_key():
    import pytest

    with pytest.raises(ValueError, match="missing repo_path"):
        src._run_repo_test_check({"payload": {}})


def test_repo_test_check_handler_raises_on_disallowed_path(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: False)
    import pytest

    with pytest.raises(ValueError, match="outside the allowed repo roots"):
        src._run_repo_test_check({"payload": {"repo_path": str(tmp_path / "x")}})


def test_repo_test_check_handler_raises_on_missing_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: True)
    import pytest

    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        src._run_repo_test_check({"payload": {"repo_path": str(missing)}})


def test_repo_test_check_handler_raises_when_subprocess_cannot_start(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(src, "_repo_path_allowed", lambda repo: True)
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n")

    def _boom(*a, **k):
        raise FileNotFoundError("python3 not found")

    monkeypatch.setattr(src.subprocess, "run", _boom)
    import pytest

    with pytest.raises(FileNotFoundError):
        src._run_repo_test_check({"payload": {"repo_path": str(repo)}})


def test_load_handlers_registers_suggestive_thinking_sources_in_a_fresh_process():
    """OCR finding 2026-10-23: approval_queue._load_handlers() must import
    this module, or repo_test_check is never in _HANDLERS in a fresh
    process and approve() marks it APPROVED_NO_HANDLER instead of running
    it. Spawns a genuinely separate Python process (not just a fresh
    import within this test process, which this module is already loaded
    into) -- that's the actual scenario OCR flagged: `python3
    approval_queue.py approve <id>` run later, on its own."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import approval_queue; approval_queue._load_handlers(); "
            "assert 'repo_test_check' in approval_queue._HANDLERS, "
            "sorted(approval_queue._HANDLERS)",
        ],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_deliver_default_speaks_the_question_not_the_headline(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")  # does not exist
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop.sh")
    candidate = Candidate(
        source="s",
        headline="fact-shaped headline, should not be spoken",
        question="Want me to do the real thing?",
        deliverable="a finished real thing",
    )
    src.deliver_default(candidate)
    out = capsys.readouterr().out
    assert "Want me to do the real thing?" in out
    assert "a finished real thing" in out
    assert "fact-shaped headline" not in out


def test_deliver_default_queues_an_approval_entry_when_action_type_set(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop.sh")
    monkeypatch.setattr(src.approval_queue, "list_pending", lambda: [])
    queued = []
    monkeypatch.setattr(
        src.approval_queue,
        "queue",
        lambda **kw: queued.append(kw) or "fake-id",
    )
    candidate = Candidate(
        source="stale-project:foo",
        headline="headline",
        question="Want me to run foo's test suite?",
        deliverable="a pass/fail report",
        action_type="repo_test_check",
        action_payload={"repo_path": "/tmp/foo"},
    )
    src.deliver_default(candidate)
    assert len(queued) == 1
    assert queued[0]["entry_type"] == "repo_test_check"
    assert queued[0]["payload"] == {"repo_path": "/tmp/foo"}
    assert queued[0]["who"] == "suggestive_thinking"


def test_deliver_default_skips_queueing_a_duplicate_pending_entry(
    tmp_path, monkeypatch
):
    """OCR finding 2026-10-23: without this check, a candidate that keeps
    re-surfacing after its 3-day recall window lapses (e.g. a repo that's
    still stale) would pile up a new PENDING entry every wake cycle for
    the same underlying thing."""
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop.sh")
    monkeypatch.setattr(
        src.approval_queue,
        "list_pending",
        lambda: [
            {
                "status": "PENDING",
                "type": "repo_test_check",
                "where": "stale-project:foo",
            }
        ],
    )
    queued = []
    monkeypatch.setattr(
        src.approval_queue, "queue", lambda **kw: queued.append(kw) or "fake-id"
    )
    candidate = Candidate(
        source="stale-project:foo",
        headline="headline",
        action_type="repo_test_check",
        action_payload={"repo_path": "/tmp/foo"},
    )
    src.deliver_default(candidate)
    assert queued == []


def test_deliver_default_survives_approval_queue_raising(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop.sh")
    monkeypatch.setattr(src.approval_queue, "list_pending", lambda: [])

    def _boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(src.approval_queue, "queue", _boom)
    candidate = Candidate(
        source="stale-project:foo",
        headline="headline",
        action_type="repo_test_check",
        action_payload={"repo_path": "/tmp/foo"},
    )
    src.deliver_default(candidate)  # must not raise
    assert "approval_queue.queue failed" in capsys.readouterr().out


def test_deliver_default_does_not_queue_when_action_type_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "VOCALIZE_FLAG", tmp_path / "flag")
    monkeypatch.setattr(src, "NOTIFY_DESKTOP", tmp_path / "no-desktop.sh")
    queued = []
    monkeypatch.setattr(src.approval_queue, "queue", lambda **kw: queued.append(kw))
    src.deliver_default(Candidate(source="s", headline="pure info, no action"))
    assert queued == []


def test_gmail_unread_candidate_fails_the_outcome_gate_by_default(
    tmp_path, monkeypatch
):
    """Mechanical proof of the 'zero email-count suggestions' requirement:
    gather_gmail_unread never sets outcome, so passes_relevance_filter
    (imported fresh here, not monkeypatched) rejects it even though the
    original four scores are all high."""
    import suggestive_thinking as st

    messages = [{"subject": "hi", "from": "x@example.com"}]
    monkeypatch.setattr(src, "GOOGLE_API_PY", tmp_path / "google_api.py")
    (tmp_path / "google_api.py").touch()
    monkeypatch.setattr(src, "GOOGLE_API_PYTHON", tmp_path / "python")
    (tmp_path / "python").touch()
    monkeypatch.setattr(
        src.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=0, stdout=json.dumps(messages), stderr=""
        ),
    )
    [candidate] = src.gather_gmail_unread()
    assert candidate.outcome == 0.0
    surfaced, reason = st.passes_relevance_filter(candidate)
    assert surfaced is False
    assert reason.startswith("outcome=")


# ── 2026-10-23, round 2: upstream-proposals converted to proposal shape ──


def test_upstream_sha_ok_accepts_plain_hex_of_valid_length():
    assert src._upstream_sha_ok("a" * 40) is True
    assert src._upstream_sha_ok("abc1234") is True  # 7 chars, the git-short minimum


def test_upstream_sha_ok_rejects_too_short_non_hex_or_empty():
    assert src._upstream_sha_ok("abc123") is False  # 6 chars, one short
    assert src._upstream_sha_ok("g" * 40) is False  # not hex
    assert src._upstream_sha_ok("") is False
    assert src._upstream_sha_ok("../../etc/passwd") is False


def _write_plan(plans_dir, sha8, text):
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / f"{sha8}.md").write_text(text)


_CODE_PLAN = """Some narrative.

```python
# new_module.py
def hello():
    return "hi"
```
"""

_NO_CODE_PLAN = "Just a written-out idea, no code blocks here at all."

_UNSAFE_PLAN = """
```python
# master_ai.py
BROKEN = True
```
"""


def test_gather_upstream_proposals_fills_proposal_shape_when_plan_has_code(
    tmp_path, monkeypatch
):
    sha = "a" * 40
    log = tmp_path / "upstream-integration-log.jsonl"
    _write_log(
        log, [{"ts": datetime.now().isoformat(), "sha": sha, "status": "proposal"}]
    )
    plans_dir = tmp_path / "upstream-plans"
    _write_plan(plans_dir, sha[:8], _CODE_PLAN)
    monkeypatch.setattr(src, "UPSTREAM_LOG", log)
    monkeypatch.setattr(src, "LOG", tmp_path / "suggestive-thinking-log.jsonl")
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)

    [candidate] = src.gather_upstream_proposals()
    assert candidate.action_type == "upstream_implement"
    assert candidate.action_payload == {
        "sha": sha,
        "plan_path": str(plans_dir / f"{sha[:8]}.md"),
    }
    assert candidate.outcome == 0.75
    assert "implement upstream idea" in candidate.question.lower()
    assert "not merged" in candidate.question.lower()
    assert candidate.diff == "touches: new_module.py"


def test_gather_upstream_proposals_stays_informational_when_plan_has_no_code(
    tmp_path, monkeypatch
):
    sha = "b" * 40
    log = tmp_path / "upstream-integration-log.jsonl"
    _write_log(
        log, [{"ts": datetime.now().isoformat(), "sha": sha, "status": "proposal"}]
    )
    plans_dir = tmp_path / "upstream-plans"
    _write_plan(plans_dir, sha[:8], _NO_CODE_PLAN)
    monkeypatch.setattr(src, "UPSTREAM_LOG", log)
    monkeypatch.setattr(src, "LOG", tmp_path / "suggestive-thinking-log.jsonl")
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)

    [candidate] = src.gather_upstream_proposals()
    assert candidate.action_type == ""
    assert candidate.outcome == 0.0


def _init_repo_with_origin_master(path):
    _init_repo(path)
    subprocess.run(["git", "branch", "-M", "master"], cwd=path, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/master", "master"],
        cwd=path,
        check=True,
    )


def test_run_upstream_implement_applies_commits_and_never_pushes_or_merges(
    tmp_path, monkeypatch
):
    repo = tmp_path / "live-repo"
    _init_repo_with_origin_master(repo)
    plans_dir = tmp_path / "upstream-plans"
    sha = "c" * 40
    _write_plan(plans_dir, sha[:8], _CODE_PLAN)
    worktree_root = tmp_path / "upstream-impl"
    monkeypatch.setattr(src, "LIVE_REPO", repo)
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)
    monkeypatch.setattr(src, "UPSTREAM_IMPL_WORKTREE_ROOT", worktree_root)

    result = src._run_upstream_implement(
        {"payload": {"sha": sha, "plan_path": str(plans_dir / f"{sha[:8]}.md")}}
    )

    assert f"upstream/{sha[:8]}" in result
    assert "NOT pushed, NOT merged" in result
    worktree = worktree_root / sha[:8]
    assert (worktree / "new_module.py").read_text() == 'def hello():\n    return "hi"\n'
    log = subprocess.run(
        ["git", "-C", str(worktree), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sha[:8] in log
    # The original repo's own master branch never moved -- nothing pushed/merged.
    master_log = subprocess.run(
        ["git", "-C", str(repo), "log", "master", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Implement upstream idea" not in master_log


def test_run_upstream_implement_raises_on_bad_sha():
    import pytest

    with pytest.raises(ValueError, match="bad sha"):
        src._run_upstream_implement(
            {"payload": {"sha": "not-hex!", "plan_path": "/tmp/x.md"}}
        )


def test_run_upstream_implement_raises_on_missing_plan_path_key():
    import pytest

    with pytest.raises(ValueError, match="missing plan_path"):
        src._run_upstream_implement({"payload": {"sha": "a" * 40}})


def test_run_upstream_implement_raises_when_plan_outside_upstream_plans(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(src, "UPSTREAM_PLANS", tmp_path / "upstream-plans")
    outside = tmp_path / "elsewhere.md"
    outside.write_text(_CODE_PLAN)
    import pytest

    with pytest.raises(ValueError, match="outside"):
        src._run_upstream_implement(
            {"payload": {"sha": "a" * 40, "plan_path": str(outside)}}
        )


def test_run_upstream_implement_raises_on_missing_plan_file(tmp_path, monkeypatch):
    plans_dir = tmp_path / "upstream-plans"
    plans_dir.mkdir()
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)
    import pytest

    with pytest.raises(FileNotFoundError):
        src._run_upstream_implement(
            {"payload": {"sha": "a" * 40, "plan_path": str(plans_dir / "ffffffff.md")}}
        )


def test_run_upstream_implement_raises_when_plan_has_no_code_blocks(
    tmp_path, monkeypatch
):
    plans_dir = tmp_path / "upstream-plans"
    sha = "d" * 40
    _write_plan(plans_dir, sha[:8], _NO_CODE_PLAN)
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)
    import pytest

    with pytest.raises(ValueError, match="no applicable code blocks"):
        src._run_upstream_implement(
            {"payload": {"sha": sha, "plan_path": str(plans_dir / f"{sha[:8]}.md")}}
        )


def test_run_upstream_implement_raises_when_plan_touches_core_engine_file(
    tmp_path, monkeypatch
):
    plans_dir = tmp_path / "upstream-plans"
    sha = "e" * 40
    _write_plan(plans_dir, sha[:8], _UNSAFE_PLAN)
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)
    import pytest

    with pytest.raises(ValueError, match="disallowed path"):
        src._run_upstream_implement(
            {"payload": {"sha": sha, "plan_path": str(plans_dir / f"{sha[:8]}.md")}}
        )


def test_run_upstream_implement_raises_if_worktree_already_exists(
    tmp_path, monkeypatch
):
    repo = tmp_path / "live-repo"
    _init_repo_with_origin_master(repo)
    plans_dir = tmp_path / "upstream-plans"
    sha = "f" * 40
    _write_plan(plans_dir, sha[:8], _CODE_PLAN)
    worktree_root = tmp_path / "upstream-impl"
    (worktree_root / sha[:8]).mkdir(parents=True)
    monkeypatch.setattr(src, "LIVE_REPO", repo)
    monkeypatch.setattr(src, "UPSTREAM_PLANS", plans_dir)
    monkeypatch.setattr(src, "UPSTREAM_IMPL_WORKTREE_ROOT", worktree_root)
    import pytest

    with pytest.raises(FileExistsError, match="already exists"):
        src._run_upstream_implement(
            {"payload": {"sha": sha, "plan_path": str(plans_dir / f"{sha[:8]}.md")}}
        )
