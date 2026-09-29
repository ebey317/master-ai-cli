"""Tests for thread_factory.py's GitHub publish / repo-cap-replace logic.

These use a fake `run: RunFn` rather than a real `gh`/`git` process, so they
exercise the exact same branching code as a real run (create vs. replace,
the live re-verification, the diverged-HEAD abort) with zero network
dependency. This is the safety-critical section the design exists to get
right -- the live `biovega` finding (a real, non-managed repo in the
account) is what `test_publish_never_creates_past_cap_and_never_touches_an_unmanaged_repo`
guards against directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import thread_factory as tf


def _cmd(argv, returncode=0, stdout="", stderr=""):
    return tf.CommandResult(tuple(argv), returncode, stdout, stderr)


class FakeGitHub:
    """A scripted stand-in for `run_command`. Tracks every argv it was
    called with so a test can assert `gh repo create` never appears, and
    answers `gh api .../.thread-source.json` truthfully from a small
    in-memory table of which repo names are "managed".

    `flaky_markers` simulates a repo that genuinely was managed at listing
    time but loses its marker before the replacement loop gets to it (a
    real race the design must handle) -- it answers "managed" on its first
    marker check and "not managed" on every check after that."""

    def __init__(
        self,
        managed_repos: dict[str, str],
        repo_list: list[dict],
        flaky_markers: frozenset[str] = frozenset(),
    ):
        self.managed_repos = managed_repos  # {repo_name: head_sha}
        self.repo_list = repo_list
        self.flaky_markers = flaky_markers
        self._marker_checks: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.cloned_dirs: dict[str, Path] = {}

    def __call__(self, argv, *, timeout=0, cwd=None, stdin_text=None):
        self.calls.append(list(argv))
        cmd = argv[0]

        if cmd == "gh" and argv[1:3] == ["auth", "status"]:
            return _cmd(argv, 0)

        if cmd == "gh" and argv[1:3] == ["repo", "list"]:
            return _cmd(argv, 0, stdout=json.dumps(self.repo_list))

        if (
            cmd == "gh"
            and argv[1] == "api"
            and argv[2].endswith(f"/{tf.MARKER_FILENAME}")
        ):
            name = argv[2].split("/")[2]
            self._marker_checks[name] = self._marker_checks.get(name, 0) + 1
            managed_now = name in self.managed_repos
            if name in self.flaky_markers and self._marker_checks[name] > 1:
                managed_now = False  # lost its marker since the first check
            if managed_now:
                return _cmd(argv, 0, stdout=tf.MARKER_FILENAME)
            return _cmd(argv, 1, stderr="404 Not Found")

        if (
            cmd == "gh"
            and argv[1] == "api"
            and argv[2].endswith(f"/{tf.MANIFEST_FILENAME}")
        ):
            name = argv[2].split("/")[2]
            if name in self.managed_repos:
                return _cmd(argv, 0, stdout="512")
            return _cmd(argv, 1, stderr="404 Not Found")

        if cmd == "git" and argv[1] == "ls-remote":
            name = argv[2].rsplit("/", 1)[-1].removesuffix(".git")
            sha = self.managed_repos.get(name, "0" * 40)
            return _cmd(argv, 0, stdout=f"{sha}\tHEAD\n")

        if cmd == "git" and argv[1] == "clone":
            dest = Path(argv[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            name = argv[-2].rsplit("/", 1)[-1].removesuffix(".git")
            (dest / tf.MARKER_FILENAME).write_text(json.dumps({"thread_slug": name}))
            self.cloned_dirs[name] = dest
            return _cmd(argv, 0)

        if cmd == "git" and "rev-parse" in argv and "--git-dir" in argv:
            return _cmd(argv, 1, stderr="not a git repo")

        if cmd == "git" and "rev-parse" in argv and "HEAD" in argv:
            repo_dir = Path(cwd) if cwd else Path(argv[argv.index("-C") + 1])
            for name, sha in self.managed_repos.items():
                if str(self.cloned_dirs.get(name, "")) == str(repo_dir):
                    return _cmd(argv, 0, stdout=sha + "\n")
            return _cmd(argv, 0, stdout="f" * 40 + "\n")

        if cmd == "git" and "config" in argv:
            return _cmd(argv, 1)

        if cmd == "git" and "init" in argv:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            return _cmd(argv, 0)

        if cmd == "git" and "add" in argv:
            return _cmd(argv, 0)

        if cmd == "git" and "commit" in argv:
            return _cmd(argv, 0)

        if cmd == "git" and "push" in argv:
            return _cmd(argv, 0)

        if cmd == "gh" and argv[1:3] == ["repo", "create"]:
            return _cmd(argv, 0)

        raise AssertionError(f"FakeGitHub: unhandled command {argv!r}")


def _config(tmp_path: Path, cap: int) -> tf.ThreadFactoryConfig:
    return tf.ThreadFactoryConfig(
        sessions_dir=tmp_path / "sessions",
        threads_dir=tmp_path / "threads",
        catalog_path=tmp_path / "catalog.json",
        github_owner="tester",
        repo_visibility="private",
        live_repo_cap=cap,
        triage_model="m",
        distill_model="m",
        min_session_messages=1,
        harvest_min_similarity=0.8,
        dry_run=False,
    )


def _thread_dir(tmp_path: Path, slug: str) -> Path:
    d = tmp_path / "threads" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text(f"# {slug}\n\nreal content")
    return d


def test_is_thread_factory_repo_true_on_marker_present():
    fake = FakeGitHub(managed_repos={"managed-1": "a" * 40}, repo_list=[])
    assert tf.is_thread_factory_repo("tester", "managed-1", run=fake) is True


def test_is_thread_factory_repo_false_on_404():
    fake = FakeGitHub(managed_repos={}, repo_list=[])
    assert tf.is_thread_factory_repo("tester", "unmanaged-repo", run=fake) is False


def test_is_thread_factory_repo_false_on_ambiguous_error():
    def flaky_run(argv, **kw):
        if argv[0] == "gh" and argv[1] == "api":
            return _cmd(argv, -1, stderr="timed out after 45s")
        raise AssertionError("unexpected call")

    assert tf.is_thread_factory_repo("tester", "some-repo", run=flaky_run) is False


def test_publish_creates_new_repo_when_under_cap(tmp_path):
    fake = FakeGitHub(managed_repos={}, repo_list=[])
    catalog = tf.Catalog(tmp_path / "catalog.json")
    catalog.load()
    thread_dir = _thread_dir(tmp_path, "new-thread")
    config = _config(tmp_path, cap=20)

    result = tf.publish_thread(thread_dir, "new-thread", config, catalog, run=fake)

    assert result.action == "created"
    assert result.pushed_ok is True
    create_calls = [c for c in fake.calls if c[:3] == ["gh", "repo", "create"]]
    assert len(create_calls) == 1
    assert "tester/new-thread" in create_calls[0]


def test_publish_never_creates_past_cap_and_never_touches_an_unmanaged_repo(tmp_path):
    """The direct end-to-end guard for the real `biovega` finding: at cap,
    with one managed repo (replaceable) and one unmanaged repo that is
    OLDER (and would be picked by naive age-only sorting), the unmanaged
    repo must never be touched and `gh repo create` must never run."""
    fake = FakeGitHub(
        managed_repos={"managed-old": "a" * 40},
        repo_list=[
            {
                "name": "biovega",
                "url": "https://github.com/tester/biovega",
                "pushedAt": "2020-01-01T00:00:00Z",
            },
            {
                "name": "managed-old",
                "url": "https://github.com/tester/managed-old",
                "pushedAt": "2025-01-01T00:00:00Z",
            },
        ],
    )
    catalog = tf.Catalog(tmp_path / "catalog.json")
    catalog.load()
    catalog.upsert(
        tf.CatalogEntry(
            slug="prior-thread",
            session_id="s0",
            content_hash="h0",
            verdict="LIVE",
            github_repo_url="https://github.com/tester/managed-old",
            last_pushed_commit_sha="a" * 40,
            last_pushed_at="2025-01-01T00:00:00Z",
            last_processed_at="2025-01-01T00:00:00Z",
            pruned_locally=False,
        )
    )
    thread_dir = _thread_dir(tmp_path, "new-thread")
    config = _config(tmp_path, cap=1)

    result = tf.publish_thread(thread_dir, "new-thread", config, catalog, run=fake)

    assert result.action == "replaced_inactive"
    assert result.replaced_repo == "managed-old"
    create_calls = [c for c in fake.calls if c[:3] == ["gh", "repo", "create"]]
    assert create_calls == [], "gh repo create must never run once at cap"
    # A read-only ownership check on biovega is expected and safe -- it's how
    # the live, authoritative managed-count avoids the cross-machine
    # undercount bug. What must never happen is touching its CONTENT.
    biovega_mutations = [
        c
        for c in fake.calls
        if "biovega" in " ".join(c)
        and c[0] == "git"
        and c[1] in ("clone", "push", "commit")
    ]
    assert biovega_mutations == [], (
        "the unmanaged repo must never be cloned, committed to, or pushed"
    )


def test_verify_pushed_and_retrievable_catches_diverged_head():
    fake = FakeGitHub(managed_repos={"repo-x": "b" * 40}, repo_list=[])
    ok, detail = tf.verify_pushed_and_retrievable(
        "tester", "repo-x", "a" * 40, run=fake
    )
    assert ok is False
    assert "expected" in detail


def test_verify_pushed_and_retrievable_passes_on_matching_sha():
    fake = FakeGitHub(managed_repos={"repo-x": "a" * 40}, repo_list=[])
    ok, detail = tf.verify_pushed_and_retrievable(
        "tester", "repo-x", "a" * 40, run=fake
    )
    assert ok is True


def test_push_into_existing_repo_aborts_on_diverged_head(tmp_path):
    fake = FakeGitHub(managed_repos={"existing-repo": "c" * 40}, repo_list=[])
    catalog = tf.Catalog(tmp_path / "catalog.json")
    catalog.load()
    catalog.upsert(
        tf.CatalogEntry(
            slug="s",
            session_id="sid",
            content_hash="h",
            verdict="LIVE",
            github_repo_url=None,
            last_pushed_commit_sha=None,
            last_pushed_at=None,
            last_processed_at="2026-01-01T00:00:00Z",
            pruned_locally=False,
        )
    )
    thread_dir = _thread_dir(tmp_path, "s")
    config = _config(tmp_path, cap=20)

    ok, detail, sha, old_slug = tf._push_into_existing_repo(
        thread_dir,
        "s",
        "existing-repo",
        config,
        catalog,
        expected_sha="a" * 40,
        commit_message_prefix="Update",
        run=fake,
    )

    assert ok is False
    assert "ABORT" in detail
    push_calls = [c for c in fake.calls if "push" in c]
    assert push_calls == [], "must never push after a diverged-HEAD abort"


def test_publish_skips_past_a_target_that_lost_its_marker(tmp_path):
    """Both repos are genuinely managed at listing time (cap correctly reads
    as full), but the oldest one loses its marker in the window between that
    listing and the replacement loop's live re-check right before touching
    it -- a real race the design must survive by moving on to the
    next-oldest, never by falling through to create."""
    fake = FakeGitHub(
        managed_repos={"flaky-oldest": "e" * 40, "still-managed": "d" * 40},
        repo_list=[
            {
                "name": "flaky-oldest",
                "url": "https://github.com/tester/flaky-oldest",
                "pushedAt": "2024-01-01T00:00:00Z",
            },
            {
                "name": "still-managed",
                "url": "https://github.com/tester/still-managed",
                "pushedAt": "2025-01-01T00:00:00Z",
            },
        ],
        flaky_markers=frozenset({"flaky-oldest"}),
    )
    catalog = tf.Catalog(tmp_path / "catalog.json")
    catalog.load()
    for slug, name, sha in (
        ("old1", "flaky-oldest", "e" * 40),
        ("old2", "still-managed", "d" * 40),
    ):
        catalog.upsert(
            tf.CatalogEntry(
                slug=slug,
                session_id=slug,
                content_hash="h",
                verdict="LIVE",
                github_repo_url=f"https://github.com/tester/{name}",
                last_pushed_commit_sha=sha,
                last_pushed_at="2024-01-01T00:00:00Z",
                last_processed_at="2024-01-01T00:00:00Z",
                pruned_locally=False,
            )
        )
    thread_dir = _thread_dir(tmp_path, "new-thread")
    config = _config(tmp_path, cap=2)

    result = tf.publish_thread(thread_dir, "new-thread", config, catalog, run=fake)

    assert result.action == "replaced_inactive"
    assert result.replaced_repo == "still-managed"
    create_calls = [c for c in fake.calls if c[:3] == ["gh", "repo", "create"]]
    assert create_calls == [], (
        "a genuine race must never fall through to creating a new repo"
    )
    flaky_mutations = [
        c
        for c in fake.calls
        if "flaky-oldest" in " ".join(c)
        and c[0] == "git"
        and c[1] in ("clone", "push", "commit")
    ]
    assert flaky_mutations == [], (
        "the repo that lost its marker must never be touched, only checked"
    )
