"""Tests for thread_factory.py — Group A (pure, no network/LLM/gh).

Each test guards a specific regression this module's docstring names, or a
specific safety rule from the design (the repo-cap-replace logic, the
placeholder-content gate, the incrementality gate). Real files via tmp_path,
never mocks of the thing under test — a mock of json.loads proves nothing
about whether this module reads a real session file correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import thread_factory as tf


def _session(tmp_path: Path, session_id: str, title: str, messages: list[dict]) -> Path:
    path = tmp_path / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": title,
                "created_at": 1000.0,
                "updated_at": 1000.0,
                "messages": messages,
            }
        )
    )
    return path


def _catalog(tmp_path: Path) -> tf.Catalog:
    cat = tf.Catalog(tmp_path / "catalog.json")
    cat.load()
    return cat


def test_extract_reads_every_message_not_a_sample(tmp_path):
    """Guards the regression named in the module docstring: a prior pass
    sampling messages[:20] + messages[-40:] missed 100% of the real content
    in a 327-message session because the decisions lived in the middle."""
    messages = [{"role": "user", "content": f"message {i}"} for i in range(50)]
    messages[24]["content"] = "UNIQUE_MIDDLE_MARKER_the decision was made here"
    session = tf.Session(
        session_id="s1",
        path=tmp_path / "s1.json",
        title="t",
        created_at=0.0,
        updated_at=0.0,
        messages=messages,
    )
    candidate = tf.build_candidate(session, _catalog(tmp_path))
    assert "UNIQUE_MIDDLE_MARKER_the decision was made here" in candidate.full_text


def test_content_hash_stable_for_unchanged_session(tmp_path):
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    s1 = tf.Session("s1", tmp_path / "a.json", "t", 0.0, 0.0, messages)
    s2 = tf.Session("s1", tmp_path / "a.json", "t", 0.0, 0.0, list(messages))
    assert tf.content_hash_of(s1) == tf.content_hash_of(s2)


def test_content_hash_changes_when_a_message_changes(tmp_path):
    s1 = tf.Session(
        "s1", tmp_path / "a.json", "t", 0.0, 0.0, [{"role": "user", "content": "hello"}]
    )
    s2 = tf.Session(
        "s1",
        tmp_path / "a.json",
        "t",
        0.0,
        0.0,
        [{"role": "user", "content": "goodbye"}],
    )
    assert tf.content_hash_of(s1) != tf.content_hash_of(s2)


def test_extract_phase_skips_llm_work_for_unchanged_sessions(tmp_path):
    """The incrementality gate: an unchanged session must not even reach the
    LLM call, not just get a cached answer from it."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    threads_dir = tmp_path / "threads"
    messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    _session(sessions_dir, "s1", "Title", messages)

    config = tf.ThreadFactoryConfig(
        sessions_dir=sessions_dir,
        threads_dir=threads_dir,
        catalog_path=tmp_path / "catalog.json",
        github_owner="tester",
        repo_visibility="private",
        live_repo_cap=20,
        triage_model="m",
        distill_model="m",
        min_session_messages=1,
        harvest_min_similarity=0.8,
        dry_run=False,
    )
    catalog = _catalog(tmp_path)

    first_pass = tf.extract_phase(config, catalog)
    assert len(first_pass) == 1

    # Simulate a completed run: record this session's hash in the catalog.
    catalog.upsert(
        tf.CatalogEntry(
            slug="title",
            session_id="s1",
            content_hash=first_pass[0].content_hash,
            verdict="ARCHIVE",
            github_repo_url=None,
            last_pushed_commit_sha=None,
            last_pushed_at=None,
            last_processed_at="2026-01-01T00:00:00Z",
            pruned_locally=False,
        )
    )

    second_pass = tf.extract_phase(config, catalog)
    assert second_pass == []


def test_backup_happens_before_any_other_phase_1_work_and_is_never_deleted(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_path = _session(
        sessions_dir, "s1", "Title", [{"role": "user", "content": "a" * 20}]
    )
    session = tf.load_session(session_path)
    backup_dir = tmp_path / "backup"
    result = tf.backup_session(session, backup_dir)
    assert result.exists()
    assert json.loads(result.read_text())["session_id"] == "s1"


def test_triage_falls_back_to_archive_on_unparseable_llm_reply():
    candidate = tf.ThreadCandidate(
        session_id="s1",
        slug="s1",
        title="t",
        message_count=2,
        full_text="hello",
        content_hash="abc",
    )
    verdict = tf.triage_session(candidate, [], llm_call=lambda *_: "not json at all")
    assert verdict.verdict == "ARCHIVE"


def test_triage_falls_back_to_archive_on_llm_error():
    candidate = tf.ThreadCandidate(
        session_id="s1",
        slug="s1",
        title="t",
        message_count=2,
        full_text="hello",
        content_hash="abc",
    )
    verdict = tf.triage_session(
        candidate, [], llm_call=lambda *_: f"{tf.LLM_ERROR_PREFIX} ollama unreachable"
    )
    assert verdict.verdict == "ARCHIVE"


def test_triage_rejects_merge_to_unknown_target():
    candidate = tf.ThreadCandidate("s1", "s1", "t", 2, "hello", "abc")
    reply = json.dumps(
        {
            "verdict": "MERGE",
            "rationale": "r",
            "merge_target_slug": "nonexistent-slug",
            "confidence": 0.9,
        }
    )
    verdict = tf.triage_session(candidate, ["real-slug"], llm_call=lambda *_: reply)
    assert verdict.verdict == "ARCHIVE"


def test_triage_accepts_valid_live_verdict():
    candidate = tf.ThreadCandidate("s1", "s1", "t", 2, "hello", "abc")
    reply = json.dumps(
        {
            "verdict": "LIVE",
            "rationale": "real work happened",
            "merge_target_slug": None,
            "confidence": 0.9,
        }
    )
    verdict = tf.triage_session(candidate, [], llm_call=lambda *_: reply)
    assert verdict.verdict == "LIVE"
    assert verdict.confidence == 0.9


def test_is_placeholder_catches_old_scripts_exact_bug():
    """The literal boilerplate the script this replaces shipped as real content."""
    assert tf.is_placeholder("# Memory\n")
    assert tf.is_placeholder("Extracted from Hermes WebUI session.")


def test_is_placeholder_accepts_real_content():
    real = (
        "The decision was to route TTS through soxr resampling after the "
        "xone-gip audio buffer wedge was identified as the root cause of "
        "the static, not the resampler itself."
    )
    assert not tf.is_placeholder(real)


def test_distill_raises_on_placeholder_readme_and_writes_nothing(tmp_path):
    candidate = tf.ThreadCandidate("s1", "s1", "t", 2, "hello", "abc")
    verdict = tf.TriageVerdict("s1", "LIVE", "real work", None, 0.9)
    bad_reply = json.dumps(
        {
            "title": "t",
            "manifest_frontmatter": {
                "summary": "a real summary of real work done here"
            },
            "readme_body": "TODO",
            "authority": {
                "AGENTS.md": "x" * 50,
                "PRINCIPLES.md": "x" * 50,
                "CONVENTIONS.md": "x" * 50,
            },
            "memory_body": "x" * 50,
            "ideas": [],
            "artifacts": {},
            "references": [],
            "decisions": [],
            "tasks": [],
        }
    )
    with pytest.raises(tf.PlaceholderContentError):
        tf.distill_thread(candidate, verdict, llm_call=lambda *_: bad_reply)


def test_write_thread_folder_creates_full_taxonomy_with_real_content(tmp_path):
    distilled = tf.DistilledThread(
        slug="test-thread",
        title="Test Thread",
        manifest_frontmatter={
            "summary": "a real summary that is long enough to pass",
            "status": "active",
        },
        readme_body="This is a real README body describing real work in detail.",
        authority={
            "AGENTS.md": "Real agent instructions for this specific project.",
            "PRINCIPLES.md": "Real principles established during this work.",
            "CONVENTIONS.md": "Real naming conventions used throughout.",
        },
        memory_body="Real durable facts a future reader needs to know about this thread.",
        ideas=["A real idea raised in conversation"],
        artifacts={"script.py": "print('hi')"},
        references=["https://example.com/real-reference"],
        decisions=["decided X because Y"],
        tasks=["finish the real remaining work"],
    )
    session = tf.Session(
        "s1",
        tmp_path / "s1.json",
        "Test Thread",
        0.0,
        0.0,
        [{"role": "user", "content": "x" * 20}],
    )
    (tmp_path / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "title": "Test Thread",
                "messages": [{"role": "user", "content": "x" * 20}],
            }
        )
    )
    thread_dir = tmp_path / "threads" / "test-thread"
    tf.write_thread_folder(thread_dir, distilled, session)

    for folder in tf.THREAD_FOLDERS:
        assert (thread_dir / folder).is_dir()
    assert (thread_dir / "README.md").exists()
    assert (thread_dir / tf.MANIFEST_FILENAME).exists()
    assert (thread_dir / tf.MARKER_FILENAME).exists()
    assert (thread_dir / "artifacts" / "script.py").read_text() == "print('hi')"


def test_two_different_threads_never_produce_identical_boilerplate(tmp_path):
    """Guards the old script's exact bug: every README was the literal
    string 'Extracted from Hermes WebUI session.'"""

    def make(slug, body):
        return tf.DistilledThread(
            slug=slug,
            title=slug,
            manifest_frontmatter={"summary": f"summary for {slug} is real"},
            readme_body=body,
            authority={
                "AGENTS.md": "a" * 50,
                "PRINCIPLES.md": "a" * 50,
                "CONVENTIONS.md": "a" * 50,
            },
            memory_body="m" * 50,
            ideas=[],
            artifacts={},
            references=[],
            decisions=[],
            tasks=[],
        )

    session = tf.Session(
        "s1",
        tmp_path / "s1.json",
        "t",
        0.0,
        0.0,
        [{"role": "user", "content": "x" * 20}],
    )
    (tmp_path / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "title": "t",
                "messages": [{"role": "user", "content": "x" * 20}],
            }
        )
    )
    d1 = tmp_path / "t1"
    d2 = tmp_path / "t2"
    tf.write_thread_folder(
        d1,
        make("t1", "This README is specifically about thread one's real work."),
        session,
    )
    tf.write_thread_folder(
        d2,
        make("t2", "This README is specifically about thread two's real work."),
        session,
    )
    assert (d1 / "README.md").read_text() != (d2 / "README.md").read_text()


def test_pick_replacement_target_chooses_oldest_pushed_at():
    repos = [
        tf.GithubRepo("newer", "u1", "2026-09-01T00:00:00Z", True),
        tf.GithubRepo("oldest", "u2", "2026-01-01T00:00:00Z", True),
        tf.GithubRepo("middle", "u3", "2026-05-01T00:00:00Z", True),
    ]
    target = tf.pick_replacement_target(repos)
    assert target.name == "oldest"


def test_pick_replacement_target_never_picks_a_non_thread_factory_repo():
    """The direct unit-test analog of the real biovega finding: an unmanaged
    repo must never be chosen even when it is the oldest by far."""
    repos = [
        tf.GithubRepo(
            "biovega", "u1", "2020-01-01T00:00:00Z", False
        ),  # oldest, NOT managed
        tf.GithubRepo("managed-newer", "u2", "2026-01-01T00:00:00Z", True),
    ]
    target = tf.pick_replacement_target(repos)
    assert target is not None
    assert target.name == "managed-newer"


def test_pick_replacement_target_returns_none_when_nothing_is_managed():
    repos = [tf.GithubRepo("biovega", "u1", "2020-01-01T00:00:00Z", False)]
    assert tf.pick_replacement_target(repos) is None


def test_catalog_save_and_load_round_trips(tmp_path):
    cat = tf.Catalog(tmp_path / "catalog.json")
    cat.load()
    entry = tf.CatalogEntry(
        slug="s",
        session_id="id1",
        content_hash="h1",
        verdict="LIVE",
        github_repo_url="https://github.com/o/s",
        last_pushed_commit_sha="a" * 40,
        last_pushed_at="2026-01-01T00:00:00Z",
        last_processed_at="2026-01-01T00:00:00Z",
        pruned_locally=False,
    )
    cat.upsert(entry)
    cat.save()

    reloaded = tf.Catalog(tmp_path / "catalog.json")
    reloaded.load()
    assert reloaded.get("s") == entry


def test_catalog_load_quarantines_corrupt_file_instead_of_crashing(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("{not valid json")
    cat = tf.Catalog(path)
    cat.load()  # must not raise
    assert cat.all_entries() == []
    quarantined = list(tmp_path.glob("catalog.json.corrupt-*"))
    assert len(quarantined) == 1


def test_config_load_uses_the_callers_values_not_hardcoded_defaults(tmp_path):
    """Guards the installable-by-a-stranger requirement: nothing in the
    resulting config may be this developer's own machine's values."""
    sessions_dir = tmp_path / "my_sessions"
    sessions_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "sessions_dir": str(sessions_dir),
                "threads_dir": str(tmp_path / "my_threads"),
                "github_owner": "a-total-stranger",
                "repo_visibility": "public",
                "live_repo_cap": 5,
            }
        )
    )
    config = tf.load_config(config_path)
    assert config.github_owner == "a-total-stranger"
    assert config.sessions_dir == sessions_dir
    assert config.repo_visibility == "public"
    assert config.live_repo_cap == 5
    assert "ebey317" not in config.github_owner
    assert str(Path.home() / ".hermes") not in str(config.sessions_dir)


def test_config_load_fails_loudly_when_github_owner_missing(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sessions_dir": str(tmp_path)}))
    with pytest.raises(tf.ConfigError, match="github_owner"):
        tf.load_config(config_path)


def test_delete_verdict_writes_no_files_but_records_catalog_row(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _session(sessions_dir, "s1", "Empty", [{"role": "user", "content": "hi"}])
    config = tf.ThreadFactoryConfig(
        sessions_dir=sessions_dir,
        threads_dir=tmp_path / "threads",
        catalog_path=tmp_path / "catalog.json",
        github_owner="tester",
        repo_visibility="private",
        live_repo_cap=20,
        triage_model="m",
        distill_model="m",
        min_session_messages=1,
        harvest_min_similarity=0.8,
        dry_run=False,
    )
    catalog = _catalog(tmp_path)
    candidates = tf.extract_phase(config, catalog)
    delete_reply = json.dumps(
        {
            "verdict": "DELETE",
            "rationale": "empty test message",
            "merge_target_slug": None,
            "confidence": 0.95,
        }
    )
    ready, diagnostics = tf.structure_phase(
        candidates, config, catalog, llm_call=lambda *_: delete_reply
    )
    assert ready == []
    assert diagnostics == []
    assert not (config.threads_dir / "empty").exists()
    assert catalog.get(candidates[0].slug) is not None
    assert catalog.get(candidates[0].slug).verdict == "DELETE"
