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


def test_gather_all_combines_all_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "UPSTREAM_LOG", tmp_path / "nope.jsonl")
    monkeypatch.setattr(
        src, "gather_stale_projects", lambda: [Candidate(source="s", headline="h")]
    )
    monkeypatch.setattr(src, "gather_gmail_unread", lambda: [])
    monkeypatch.setattr(src, "gather_email_bridge_unread", lambda: [])
    monkeypatch.setattr(src, "gather_sensei_chats", lambda: [])
    monkeypatch.setattr(
        src, "gather_grant_deadlines", lambda: [Candidate(source="g", headline="gh")]
    )
    monkeypatch.setattr(
        src, "gather_money_org", lambda: [Candidate(source="m", headline="mh")]
    )
    results = src.gather_all()
    assert len(results) == 3
    assert {r.source for r in results} == {"s", "g", "m"}


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
        == "Want me to run stale-repo's test suite and fix what's red?"
    )
    assert "pass/fail report" in candidate.deliverable
    assert candidate.action_type == "repo_test_check"
    assert candidate.action_payload == {"repo_path": str(stale)}
    assert candidate.outcome == 0.8


def test_repo_test_check_handler_reports_green_for_passing_pytest_suite(tmp_path):
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / "test_toy.py").write_text("def test_ok():\n    assert True\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert result.startswith("GREEN (exit 0)")


def test_repo_test_check_handler_reports_red_for_failing_pytest_suite(tmp_path):
    repo = tmp_path / "toy-repo"
    repo.mkdir()
    (repo / "test_toy.py").write_text("def test_fail():\n    assert False\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert result.startswith("RED")


def test_repo_test_check_handler_reports_no_runner_honestly(tmp_path):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    result = src._run_repo_test_check({"payload": {"repo_path": str(repo)}})
    assert "no recognized test runner" in result


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


# ---------------------------------------------------------------------------
# 2026-10-23 fix order, Hermes half: GRANTS + MONEY/ORG sources.
# State files live under tmp_path -- never the real workspace state.
# ---------------------------------------------------------------------------


def test_grant_deadlines_surface_within_window(tmp_path):
    index = tmp_path / "grant-deadlines.json"
    _write_grants(
        index,
        [
            {
                "id": "galaxy-grant",
                "name": "Galaxy Grant",
                "amount": "$2,500",
                "deadline": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
                "url": "https://x.example",
                "status": "open",
                "note": "n",
            }
        ],
    )
    candidates = src.gather_grant_deadlines(index)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "grant-deadline:galaxy-grant"
    assert c.action_type == "grant_packet"
    assert (
        c.question == "Want me to assemble the ready-to-submit packet for Galaxy Grant?"
    )
    assert "application packet" in c.deliverable
    assert c.outcome == 0.9
    assert not c.time_critical
    import suggestive_thinking as st

    surfaced, _ = st.passes_relevance_filter(c)
    assert surfaced


def _write_grants(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}))


def test_grant_deadline_inside_3_days_is_time_critical(tmp_path):
    index = tmp_path / "grant-deadlines.json"
    _write_grants(
        index,
        [
            {
                "id": "d-prize",
                "name": "D-Prize",
                "amount": "$20k",
                "deadline": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
                "url": "https://x.example",
                "status": "open",
                "note": "",
            }
        ],
    )
    c = src.gather_grant_deadlines(index)[0]
    assert c.time_critical is True


def test_grant_deadline_past_or_outside_window_dropped(tmp_path):
    index = tmp_path / "grant-deadlines.json"
    _write_grants(
        index,
        [
            {
                "id": "old",
                "deadline": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                "status": "open",
            },
            {
                "id": "far",
                "deadline": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
                "status": "open",
            },
            {"id": "baddate", "deadline": "not-a-date", "status": "open"},
        ],
    )
    assert src.gather_grant_deadlines(index) == []


def test_grant_deadline_only_open_statuses(tmp_path):
    index = tmp_path / "grant-deadlines.json"
    soon = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")
    _write_grants(
        index,
        [
            {"id": "skipped", "deadline": soon, "status": "skip"},
            {"id": "closedg", "deadline": soon, "status": "closed"},
            {"id": "pipe", "deadline": soon, "status": "pipeline"},
        ],
    )
    candidates = src.gather_grant_deadlines(index)
    assert [c.source for c in candidates] == ["grant-deadline:pipe"]


def test_grant_deadlines_capped_to_three_worst_clock_first(tmp_path):
    index = tmp_path / "grant-deadlines.json"
    # deadlines 9..5 days out: the three CLOSEST clocks are g4(5), g3(6), g2(7)
    items = [
        {
            "id": f"g{i}",
            "name": f"G{i}",
            "deadline": (datetime.now() + timedelta(days=9 - i)).strftime("%Y-%m-%d"),
            "status": "open",
        }
        for i in range(5)
    ]
    _write_grants(index, items)
    cands = src.gather_grant_deadlines(index)
    assert len(cands) == src.GRANT_CAP_PER_RUN
    assert [c.source for c in cands] == [
        "grant-deadline:g4",
        "grant-deadline:g3",
        "grant-deadline:g2",
    ]


def test_grant_missing_or_malformed_index_returns_empty(tmp_path):
    assert src.gather_grant_deadlines(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert src.gather_grant_deadlines(bad) == []


def test_money_org_only_active_items_with_real_checks_surface(tmp_path):
    state = tmp_path / "money-org-state.json"
    state.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "a",
                        "status": "active",
                        "question": "Want me to A?",
                        "deliverable": "D",
                        "outcome": 0.85,
                        "check_cmd": ["echo", "ok"],
                    },
                    {
                        "id": "b",
                        "status": "seeded",
                        "question": "Want me to B?",
                        "deliverable": "D",
                    },
                    {
                        "id": "c",
                        "status": "active",
                        "question": "Want me to C?",
                        "deliverable": "D",
                        "outcome": 0.8,
                        "check_cmd": "echo shell-string",
                    },
                ]
            }
        )
    )
    candidates = src.gather_money_org(state)
    assert [c.source for c in candidates] == ["money-org:a"]
    assert candidates[0].action_type == "money_org_check"
    assert candidates[0].action_payload == {"item_id": "a", "check_cmd": ["echo", "ok"]}
    import suggestive_thinking as st

    surfaced, _ = st.passes_relevance_filter(candidates[0])
    assert surfaced


def test_money_org_deadline_item_is_timecritical(tmp_path):
    state = tmp_path / "money-org-state.json"
    state.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "air",
                        "status": "active",
                        "question": "Want me to prep?",
                        "deliverable": "D",
                        "outcome": 0.8,
                        "deadline": (datetime.now() + timedelta(days=2)).strftime(
                            "%Y-%m-%d"
                        ),
                        "check_cmd": ["echo", "prep"],
                    },
                ]
            }
        )
    )
    c = src.gather_money_org(state)[0]
    assert c.time_critical is True


def test_money_org_capped_to_three(tmp_path):
    state = tmp_path / "money-org-state.json"
    state.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": f"m{i}",
                        "status": "active",
                        "question": "Q{i}",
                        "deliverable": "D",
                        "outcome": 0.8,
                        "check_cmd": ["echo", str(i)],
                    }
                    for i in range(4)
                ]
            }
        )
    )
    assert len(src.gather_money_org(state)) == src.MONEY_CAP_PER_RUN


def test_money_org_missing_file_returns_empty(tmp_path):
    assert src.gather_money_org(tmp_path / "nope.json") == []


def test_grant_packet_handler_writes_sanitized_packet(tmp_path):
    import suggestive_thinking_sources as live_src

    original = live_src.GRANT_PACKET_DIR
    live_src.GRANT_PACKET_DIR = tmp_path / "packets"
    try:
        entry = {
            "payload": {
                "grant_id": "galaxy-grant",
                "name": "Galaxy Grant",
                "amount": "$2,500",
                "deadline": "2026-10-31",
                "url": "https://x.example",
                "note": "use Gmail",
            }
        }
        result = live_src._run_grant_packet(entry)
        packet = tmp_path / "packets" / "galaxy-grant" / "packet.md"
        assert packet.exists()
        assert "Galaxy Grant" in packet.read_text()
        assert "never auto-submits" in packet.read_text()
        assert "galaxy-grant" in result

        # path traversal cannot escape the packets dir
        evil = {
            "payload": {
                "grant_id": "../evil",
                "name": "E",
                "amount": "",
                "deadline": "",
                "url": "",
                "note": "",
            }
        }
        live_src._run_grant_packet(evil)
        assert (tmp_path / "packets" / "evil" / "packet.md").exists()
        assert not (tmp_path / "evil").exists()
    finally:
        live_src.GRANT_PACKET_DIR = original


def test_grant_packet_handler_includes_answers_when_recorded(tmp_path):
    import suggestive_thinking_sources as live_src

    original = live_src.GRANT_PACKET_DIR
    live_src.GRANT_PACKET_DIR = tmp_path / "packets"
    try:
        entry = {
            "payload": {
                "grant_id": "g",
                "name": "G",
                "amount": "",
                "deadline": "",
                "url": "",
                "note": "",
                "answers": {"email": "ebey317@gmail.com"},
            }
        }
        live_src._run_grant_packet(entry)
        text = (tmp_path / "packets" / "g" / "packet.md").read_text()
        assert "ebey317@gmail.com" in text
        assert "_(none recorded" not in text
    finally:
        live_src.GRANT_PACKET_DIR = original


def test_money_org_check_handler_runs_list_and_refuses_string(tmp_path):
    ok_entry = {"payload": {"item_id": "a", "check_cmd": ["echo", "state-ok"]}}
    result = src._run_money_org_check(ok_entry)
    assert "exit 0" in result and "state-ok" in result

    bad_entry = {"payload": {"item_id": "b", "check_cmd": "echo shell"}}
    assert "refused" in src._run_money_org_check(bad_entry)
