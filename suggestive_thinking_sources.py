"""Real GATHER and RECALL sources for the Suggestive Thinking loop.

Kept separate from suggestive_thinking.py because these touch real files and
git — the loop itself (run_loop, passes_relevance_filter) stays pure and
testable against injected stubs. This module is what a real caller wires in.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

import approval_queue
from suggestive_thinking import LOG, Candidate

# Deliberately NOT Path(__file__).resolve().parent: the scheduled daily
# job's copy runs from a pinned worktree (~/.master_ai_scheduled_jobs) so a
# branch switch in the live repo can't break the schedule -- but that means
# __file__'s own directory is NOT the live repo, and the real
# upstream-integration-log / upstream-plans / git history this module needs
# to read only exist in the live one. Found 2026-10-20: this cost a full
# day of zero real GATHER results (log file didn't exist at the worktree
# path at all, and the worktree's own .git is a file, not a directory, so
# the old REPO-as-master-ai-cli stale-check silently skipped every run).
LIVE_REPO = Path.home() / "master-ai-cli"
UPSTREAM_LOG = LIVE_REPO / "memory" / "upstream-integration-log.jsonl"
UPSTREAM_PLANS = LIVE_REPO / "memory" / "upstream-plans"

# 2026-10-23: where an approved upstream-implement proposal gets applied.
# A DEDICATED worktree per idea, not the shared live tree at LIVE_REPO --
# this project already got burned once by writing to a working tree another
# session could branch-switch underneath it (see suggestive_thinking_daily's
# own pinned-worktree fix, same root cause). Each sha gets its own isolated
# worktree so concurrent approvals never collide.
UPSTREAM_IMPL_WORKTREE_ROOT = Path.home() / ".master_ai_upstream_impl"

# Reused, not rebuilt (feedback_clone_dont_rebuild_from_sibling_agents): the
# Hermes google-workspace skill already owns OAuth token refresh and exposes
# a stable CLI/JSON contract over it. Calling it directly here means this
# module never touches credentials itself.
GOOGLE_API_PY = (
    Path.home()
    / ".hermes"
    / "skills"
    / "productivity"
    / "google-workspace"
    / "scripts"
    / "google_api.py"
)
GOOGLE_API_PYTHON = Path(
    "/home/elijah/.hermes/installs/97e06b4023a304c4/environments/"
    "4cd46783f536493a8b03a9429422ef87/venv/bin/python"
)
GMAIL_UNREAD_QUERY = "is:unread in:inbox -category:promotions -category:social"

# The operator's actual multi-address "sensei email bridge" (2026-10-20:
# "I've got multiple email addresses") -- ~/scripts/email_mcp/server.py,
# an IMAP/SMTP MCP server covering accounts beyond the one Gmail OAuth
# token above already reaches. Read its own credentials file directly with
# stdlib imaplib rather than importing server.py: that module does
# `from fastmcp import FastMCP` at top level, a dependency only the
# scheduler's own /usr/bin/python3 has installed -- not every interpreter
# that might run this script. The "gmail" account there is the SAME
# ebey317@gmail.com gather_gmail_unread() already watches via the Workspace
# API, so it's deliberately excluded here to avoid reporting one inbox twice.
EMAIL_BRIDGE_CREDS = Path.home() / ".config" / "email_mcp" / "credentials.json"
EMAIL_BRIDGE_PROVIDER_DEFAULTS = {
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": 993},
    "aol": {"imap_host": "imap.aol.com", "imap_port": 993},
}
EMAIL_BRIDGE_SKIP_ACCOUNTS = {"gmail"}

# Operator's rule (2026-10-19): suggestive thinking stays silent by default.
# Voice is opt-in, toggled by this touch-file's presence -- "vocalize
# suggestive thinking" -- not a setting buried in a config file.
VOCALIZE_FLAG = Path.home() / ".master_ai_vocalize_suggestive_thinking"

# NOTE: CLAUDE.md and feedback_attention_signal_tiers.md both reference
# ~/scripts/speak.sh as the voice channel -- that file does not exist on
# disk (verified 2026-10-19). The real, working mechanism is notify_voice.sh,
# which POSTs to the local TTS server on :5050.
NOTIFY_VOICE = Path.home() / "scripts" / "notify_voice.sh"

# Desktop popup -- the actual fix for "isn't doing any proactive thinking"
# (found 2026-10-20): the daily systemd job WAS surfacing real candidates
# (confirmed via the log -- a stale-project nudge and an upstream proposal
# both fired "surfaced": true) but delivery was a bare print() to stdout,
# which a systemd --user unit swallows into the void, plus voice was
# opt-in-off. A surfaced suggestion with zero visible delivery is
# indistinguishable from no suggestive thinking happening at all. Desktop
# notification now fires unconditionally (not gated on the voice toggle)
# so every surfaced candidate is actually seen; voice stays a separate,
# still-opt-in, louder channel on top of that.
NOTIFY_DESKTOP = Path.home() / "scripts" / "sensei-notify.sh"


# Operator's rule 2026-10-19 (finish-line scope lock) explicitly said widen
# this only when the lock is lifted. Operator's rule 2026-10-20 lifts it:
# "make sure it ... has access to ... all my repositories" -- so this is now
# a live discovery over every git repo directly under ~/ and ~/projects/,
# not a hardcoded 2-item list. Depth 1 only (no recursive filesystem walk,
# no ~/worktrees -- those are branches of repos already covered here, not
# separate projects, and double-counting them would just be noise).
def _discover_all_repos() -> list[Path]:
    roots = [Path.home(), Path.home() / "projects"]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            # Skip dotfile/tool dirs (.nvm, .cache, .config, ...) -- real
            # projects don't start with "." and a dotfile manager's own
            # git-cloned install (.nvm is literally a git clone) isn't
            # "one of my repositories" in the sense the operator means.
            if child.name.startswith("."):
                continue
            if (child / ".git").exists():
                found.append(child)
    return found


# A pure staleness score (days-stale, uncapped) clears every relevance-filter
# threshold identically for every repo once it's STALE_AFTER_DAYS+ old, so
# widening from 2 repos to 50+ would surface the entire backlog in one run
# -- "proactive" would become "a flood." Cap it: only the N most-stale repos
# surface per run; the rest wait for a future run (recall_suppress_recent_
# repeats' 3-day window means the ones that surface today won't repeat
# tomorrow, so the cap naturally rotates through the backlog over time).
MAX_STALE_SURFACED_PER_RUN = 3
STALE_AFTER_DAYS = 3


def _upstream_sha_ok(sha: str) -> bool:
    """Defense in depth: a sha is only ever used to name a branch
    (`upstream/<sha8>`) and a worktree directory -- reject anything that
    isn't plain hex so a malformed log/queue entry can never smuggle a
    path-traversal or shell-meaningful string into either."""
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", sha))


@approval_queue.register_handler("upstream_implement")
def _run_upstream_implement(entry: dict) -> str:
    """Executor for the upstream-proposal's Yes. Reuses
    scripts.upstream_integrator's existing plan -- it already fetched the
    patch, asked the planner model to reimplement the idea as real code
    blocks, and validated every touched path against CORE_ENGINE_FILES
    (master_ai.py, sensei_tui.py, hooks.py, router.py, typed_actions.py,
    sensei_reasoning_loop.py are never eligible, whole file untouched by
    the proposal). This handler is the only thing that was missing: it
    re-validates the plan (never trust it unchanged between generation and
    approval) and applies it -- to a NEW branch in a DEDICATED worktree,
    committed locally. It never pushes and never merges. That's the actual
    fix for the 2026-10-18 stranding incident: the old loop did this
    fully autonomously; this version only runs on explicit human approval,
    and even then stops one step short of touching origin or master.
    """
    payload = entry.get("payload", {})
    sha = payload.get("sha")
    plan_path = payload.get("plan_path")
    if not isinstance(sha, str) or not _upstream_sha_ok(sha):
        raise ValueError(f"upstream_implement payload has a bad sha: {sha!r}")
    if not isinstance(plan_path, str) or not plan_path:
        raise ValueError(f"upstream_implement payload missing plan_path: {payload!r}")

    plan_file = Path(plan_path)
    try:
        resolved_plan = plan_file.resolve()
        resolved_plans_dir = UPSTREAM_PLANS.resolve()
    except OSError as e:
        raise ValueError(f"cannot resolve plan_path {plan_path}: {e}") from e
    if resolved_plans_dir not in resolved_plan.parents:
        raise ValueError(f"plan_path {plan_file} is outside {UPSTREAM_PLANS}, refusing")
    if not resolved_plan.is_file():
        raise FileNotFoundError(f"plan file {plan_file} does not exist")

    # OCR finding 2026-10-23: importing a private helper from an
    # executable script couples this daemon to that module's path/import
    # surface. Deferred rather than fixed this pass -- `scripts/` is a
    # real package (`scripts/__init__.py` exists) and `upstream_integrator`
    # only imports stdlib at module level behind a `__main__` guard, so
    # the stated failure mode (import breaks the daemon) is already
    # narrow; moving `_parse_blocks`/`_path_allowed` to a shared
    # non-executable module is a real improvement but touches a third
    # file other automation also runs, not a same-night change.
    from scripts.upstream_integrator import _parse_blocks, _path_allowed

    blocks = _parse_blocks(resolved_plan.read_text())
    if not blocks:
        raise ValueError(f"no applicable code blocks found in {plan_file}")
    unsafe = [path for path, _ in blocks if not _path_allowed(path)]
    if unsafe:
        raise ValueError(
            f"plan touches disallowed path(s), refusing to apply: {unsafe}"
        )

    sha8 = sha[:8]
    branch = f"upstream/{sha8}"
    worktree = UPSTREAM_IMPL_WORKTREE_ROOT / sha8
    if worktree.exists():
        raise FileExistsError(
            f"worktree {worktree} already exists -- a prior attempt may be unfinished"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(LIVE_REPO),
            "worktree",
            "add",
            str(worktree),
            "-b",
            branch,
            "origin/master",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        worktree_real = worktree.resolve()
        for path, body in blocks:
            target = worktree / path
            # OCR finding 2026-10-23: _path_allowed only rejects literal
            # ".."/absolute strings -- if origin/master's checked-out tree
            # already has a symlink at an ancestor of `path` (e.g. a
            # symlinked `scripts/`), the resolved write target could land
            # outside the worktree entirely. Verify the resolved target
            # (parent need not exist yet, .resolve() still follows
            # existing ancestor symlinks) stays inside before writing.
            if not target.resolve().is_relative_to(worktree_real):
                raise ValueError(
                    f"block path {path!r} resolves outside the worktree, refusing to write"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        subprocess.run(
            ["git", "-C", str(worktree), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "commit",
                "-m",
                f"Implement upstream idea {sha8} (approved proposal)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        # OCR finding 2026-10-23: a failure after `worktree add` used to
        # leave the worktree+branch behind, so re-approving the same sha
        # would hit the FileExistsError guard above forever. Best-effort
        # cleanup (never masks the real error with a cleanup error) so a
        # transient failure doesn't permanently block retrying.
        subprocess.run(
            [
                "git",
                "-C",
                str(LIVE_REPO),
                "worktree",
                "remove",
                "--force",
                str(worktree),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(LIVE_REPO), "branch", "-D", branch],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raise
    return (
        f"applied to branch {branch} at {worktree} ({len(blocks)} file(s)) "
        "-- NOT pushed, NOT merged, ready for your review"
    )


def gather_upstream_proposals() -> list[Candidate]:
    """GATHER: unsurfaced proposal docs from the proposal-only
    perpetual-watcher cron — one candidate per "status": "proposal" line in
    the integration log that hasn't already produced a candidate before
    (see _already_gathered_shas; a 30-day window, not forever — the
    integrator's own 3-failed-attempt retry logic is what actually retires
    a dead idea from the queue).

    2026-10-23 fix order, round 2: converted to proposal shape per Lijah's
    explicit decision ("convert upstream proposals to the new shape") after
    the outcome gate silently stopped this source from surfacing at all --
    flagged independently by both my own review and Hermes's no-mistakes
    gate on a parallel branch. action_type="upstream_implement" only fires
    when the plan actually produced real code blocks (see the handler
    above); a plan-only/no-code proposal stays informational with
    outcome=0.0, since there's nothing for approval to execute yet.
    outcome=0.75 (not 0.8 like repo_test_check): a committed-but-unmerged
    branch is real progress but one deliberate step short of done.
    """
    if not UPSTREAM_LOG.exists():
        return []
    already = _already_gathered_shas()
    candidates: list[Candidate] = []
    for line in UPSTREAM_LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("status") != "proposal":
            continue
        sha = entry.get("sha", "")
        if not sha or sha[:8] in already:
            continue
        plan_path = UPSTREAM_PLANS / f"{sha[:8]}.md"
        blocks: list[tuple[str, str]] = []
        if plan_path.exists():
            try:
                from scripts.upstream_integrator import _parse_blocks

                blocks = _parse_blocks(plan_path.read_text())
            except Exception as e:
                # OCR finding 2026-10-23: this used to be a silent
                # `except Exception: has_code = False`, so a broken import
                # or a parser regression made upstream proposals vanish
                # with zero trace. Print (matches this file's existing
                # best-effort-but-visible convention, e.g. deliver_default
                # above) so a misconfigured environment is noticed instead
                # of just quietly looking like "no ideas right now."
                print(f"[SUGGEST] upstream plan parse failed for {sha[:8]}: {e}")
                blocks = []
        detail = f"plan: {plan_path}" if plan_path.exists() else entry.get("detail", "")
        candidate = Candidate(
            source=f"upstream-proposal:{sha[:8]}",
            headline=f"upstream idea proposed, not yet reviewed: {sha[:8]}",
            detail=detail,
            novelty=1.0,
            actionable=1.0,
            timely=0.8,
            interruption_worthy=0.7,
        )
        if blocks and _upstream_sha_ok(sha):
            candidate.question = (
                f"Want me to implement upstream idea {sha[:8]}? I'll apply "
                "the already-written plan on a new branch and commit it -- "
                "not merged automatically, yours to review first."
            )
            candidate.deliverable = (
                f"branch upstream/{sha[:8]} with the plan applied and "
                "committed, ready for your review"
            )
            candidate.action_type = "upstream_implement"
            candidate.action_payload = {"sha": sha, "plan_path": str(plan_path)}
            # OCR finding 2026-10-23: approval_queue's `diff` field exists
            # specifically so a human sees what's about to change before
            # approving -- this was being left at its "" default, so
            # approving was a blind yes to whatever the plan file
            # contained. List the touched paths so the review surface
            # actually shows something.
            candidate.diff = "touches: " + ", ".join(path for path, _ in blocks)
            candidate.outcome = 0.75
        candidates.append(candidate)
    return candidates


def gather_gmail_unread() -> list[Candidate]:
    """GATHER: real Gmail, via the Hermes google-workspace skill's existing
    OAuth-authenticated CLI -- reused, not re-wired (see GOOGLE_API_PY
    comment above). Scoped to inbox, excluding promotions/social, so
    newsletter noise doesn't dominate.

    One candidate for the whole unread count, not one per message: an
    inbox full of mail is one fact ("you have N unread"), not N facts. Raw
    snippet/body text never leaves this function or lands in the candidate/
    audit trail -- only the count and the single newest subject line do."""
    if not GOOGLE_API_PY.exists() or not GOOGLE_API_PYTHON.exists():
        return []
    try:
        out = subprocess.run(
            [
                str(GOOGLE_API_PYTHON),
                str(GOOGLE_API_PY),
                "gmail",
                "search",
                GMAIL_UNREAD_QUERY,
                "--max",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        messages = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    if not messages:
        return []
    count = len(messages)
    count_str = (
        f"{count}+" if count >= 10 else str(count)
    )  # --max 10 caps the real count
    newest = messages[0]
    subject = str(newest.get("subject") or "(no subject)")[:120]
    return [
        Candidate(
            # Count baked into the source key (unlike stale-project's fixed
            # per-repo key) -- unread mail is a moving count, not a static
            # fact, so recall_suppress_recent_repeats should only suppress
            # an exact repeat of the SAME count, not every future check
            # just because inbox-has-unread-mail was already reported once.
            source=f"gmail-unread:{count_str}",
            headline=f'{count_str} unread email(s) in your inbox — newest: "{subject}"',
            detail=f"from {newest.get('from', '')}",
            novelty=1.0,
            actionable=1.0,
            timely=0.75,
            interruption_worthy=0.6,
        )
    ]


def _email_bridge_unread_count(acct: dict) -> int | None:
    """One account, stdlib imaplib only. Returns None on any connection
    failure (bad creds, host down, ...) so gather_email_bridge_unread can
    skip that account without crashing the whole GATHER cycle -- a dead
    AOL login, say, must never block the Gmail/Outlook checks next to it."""
    import imaplib

    provider = acct.get("provider", "")
    defaults = EMAIL_BRIDGE_PROVIDER_DEFAULTS.get(provider, {})
    host = acct.get("imap_host") or defaults.get("imap_host")
    port = acct.get("imap_port") or defaults.get("imap_port")
    username = acct.get("username")
    password = acct.get("password")
    if not (
        isinstance(host, str)
        and isinstance(port, int)
        and isinstance(username, str)
        and isinstance(password, str)
    ):
        return None
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=15)
        try:
            conn.login(username, password)
            conn.select("INBOX", readonly=True)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return None
            ids = data[0].split() if data and data[0] else []
            return len(ids)
        finally:
            conn.logout()
    except Exception:
        return None


def gather_email_bridge_unread() -> list[Candidate]:
    """GATHER: the operator's actual multi-address bridge (2026-10-20: "I've
    got multiple email addresses") -- every configured account in
    ~/.config/email_mcp/credentials.json except "gmail" (already covered by
    gather_gmail_unread via the Workspace API; see EMAIL_BRIDGE_SKIP_ACCOUNTS).

    One candidate per account with nonzero unread -- these are genuinely
    separate identities (personal Outlook, AOL, the Fair Chance relay
    inbox), not one blended fact the way a single inbox's count is. Count
    only, via IMAP SEARCH UNSEEN -- never a FETCH, so no subject/snippet/
    body text is ever read by this function at all."""
    if not EMAIL_BRIDGE_CREDS.exists():
        return []
    try:
        creds = json.loads(EMAIL_BRIDGE_CREDS.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    candidates: list[Candidate] = []
    for name, acct in (creds.get("accounts") or {}).items():
        if name in EMAIL_BRIDGE_SKIP_ACCOUNTS:
            continue
        count = _email_bridge_unread_count(acct)
        if not count:
            continue
        label = acct.get("display_name") or name
        count_str = str(count)  # IMAP SEARCH returns the real count, no cap to flag
        candidates.append(
            Candidate(
                source=f"email-bridge:{name}:{count_str}",
                headline=f"{count_str} unread email(s) in {label} ({name})",
                detail="",
                novelty=1.0,
                actionable=1.0,
                timely=0.75,
                interruption_worthy=0.6,
            )
        )
    return candidates


SENSEI_CHATS_DIR = Path.home() / ".master_ai_chats"
SENSEI_CHATS_STALE_AFTER_DAYS = 2


def gather_sensei_chats() -> list[Candidate]:
    """GATHER: Sensei's own saved session transcripts (~/.master_ai_chats,
    confirmed by operator 2026-10-20 as the canonical live-thread location,
    not Claude Code or Hermes history).

    Deliberately NOT content analysis of what's "unresolved" inside a
    transcript -- free-form terminal output has no structure reliable
    enough to tell a finished conversation from a dropped one without real
    risk of a wrong, confusing nudge. Mirrors gather_stale_projects'
    honest, conservative shape instead: surface only that the newest saved
    chat is old enough that it's probably not still open in your head."""
    if not SENSEI_CHATS_DIR.is_dir():
        return []
    chats = sorted(
        (p for p in SENSEI_CHATS_DIR.glob("*.chat") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not chats:
        return []
    newest = chats[0]
    age_days = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).days
    if age_days < SENSEI_CHATS_STALE_AFTER_DAYS:
        return []
    return [
        Candidate(
            # Fixed key, not keyed by age_days (unlike gmail-unread's moving
            # count) -- this mirrors stale-project: the headline's day count
            # changes daily but it's the same underlying fact being
            # reconsidered, not a new one, so recall should suppress repeats.
            source="sensei-chat-stale",
            headline=(
                f"your newest saved Sensei chat ({newest.stem}) is {age_days} days old — "
                "anything in it still open?"
            ),
            detail=str(newest),
            novelty=1.0,
            actionable=0.7,
            timely=min(1.0, age_days / 7),
            interruption_worthy=0.6,
        )
    ]


def _repo_path_allowed(repo: Path, roots: list[Path] | None = None) -> bool:
    """OCR finding 2026-10-23: `repo_path` reaches this handler through a
    stored approval_queue payload, not a trusted in-process value -- a
    malformed or future-malicious payload could point `cwd` anywhere and
    get arbitrary `pytest`/`npm test` config executed from it. Require the
    resolved path to actually sit under one of _discover_all_repos()'s own
    roots (home or ~/projects, one level deep) before running anything.
    `roots` is injectable so tests can point this at a tmp_path tree
    instead of monkeypatching Path.home() for every caller.

    Second OCR pass, same day: the first version only checked the parent
    directory, which is strictly broader than what _discover_all_repos()
    itself would ever surface -- it accepted dotfile directories (e.g.
    ~/.ssh, even this module's own ~/.master_ai_upstream_impl) and never
    confirmed the target was actually a repo. Mirror both of
    _discover_all_repos()'s real criteria now: no dotfile name, and a
    `.git` directory must exist.
    """
    try:
        resolved = repo.resolve()
    except OSError:
        return False
    if resolved.name.startswith("."):
        return False
    if not (resolved / ".git").is_dir():
        return False
    for root in roots if roots is not None else (Path.home(), Path.home() / "projects"):
        try:
            if resolved.parent == root.resolve():
                return True
        except OSError:
            continue
    return False


@approval_queue.register_handler("repo_test_check")
def _run_repo_test_check(entry: dict) -> str:
    """Executor for the 'stale repo' proposal's Yes: run whatever test
    runner the repo actually has and report pass/fail. Auto-detects
    pytest (conftest.py / pytest.ini / a tests/ dir) over npm test
    (package.json) since every active repo on this box is Python-first.

    Scope note (2026-10-23): this runs the suite and reports red/green --
    it does NOT auto-fix failures yet. "Fix what's red" as a fully
    autonomous repair loop is a materially bigger feature (a coding
    subagent loop against the failures) and is called out as a separate
    next step in HANDOFF.md rather than being half-built here. The
    candidate's own `question` text says "report red/green", not "fix",
    to match.

    approval_queue's contract is "handlers should raise on failure" so
    approve() marks the entry FAILED -- per OCR review 2026-10-23, this
    used to return every outcome (including real failures) as a string,
    which marked a broken run RAN. Only the two legitimate *completed*
    outcomes (no runner found / a real GREEN-or-RED result) return a
    string now; a missing/invalid payload, a disallowed path, a missing
    repo directory, or the subprocess failing to start all raise instead.
    """
    repo_path = entry.get("payload", {}).get("repo_path")
    if not isinstance(repo_path, str) or not repo_path:
        raise ValueError(
            f"repo_test_check payload missing repo_path: {entry.get('payload')!r}"
        )
    repo = Path(repo_path)
    if not _repo_path_allowed(repo):
        raise ValueError(
            f"repo_path {repo} is outside the allowed repo roots, refusing to run"
        )
    if not repo.is_dir():
        raise FileNotFoundError(f"repo_path {repo} does not exist (moved or deleted?)")
    if (
        (repo / "conftest.py").exists()
        or (repo / "pytest.ini").exists()
        or (repo / "tests").is_dir()
    ):
        cmd = ["python3", "-m", "pytest", "-q"]
    elif (repo / "package.json").exists():
        cmd = ["npm", "test", "--silent"]
    else:
        return (
            f"no recognized test runner in {repo} (no pytest/tests/, no package.json)"
        )
    out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=600)
    tail = (out.stdout + out.stderr).strip().splitlines()[-15:]
    status = "GREEN" if out.returncode == 0 else "RED"
    return f"{status} (exit {out.returncode}) — {' / '.join(cmd)}\n" + "\n".join(tail)


def gather_stale_projects(
    repos: list[Path] | None = None, stale_after_days: int = STALE_AFTER_DAYS
) -> list[Candidate]:
    """GATHER: every discovered repo with no commit in stale_after_days+ days,
    capped to the MAX_STALE_SURFACED_PER_RUN most-stale (see the comment
    above MAX_STALE_SURFACED_PER_RUN for why the cap exists). `repos=None` means discover live via
    _discover_all_repos() — pass an explicit list (tests do) to bypass disk
    discovery entirely.

    2026-10-23 fix order: converted from a notice ("haven't touched X,
    pick it up or let it go?") into a work proposal with a real executable
    action — action_type="repo_test_check" runs the suite and reports
    red/green on approval, via the handler above. outcome=0.8: running
    down a real repo's test health is shipped-repo-work-adjacent progress,
    not pure information.
    """
    repos = _discover_all_repos() if repos is None else repos
    scored: list[tuple[int, Candidate]] = []
    now = datetime.now()
    for repo in repos:
        if not (repo / ".git").is_dir():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        last_commit = datetime.fromtimestamp(int(out.stdout.strip()))
        days_stale = (now - last_commit).days
        if days_stale < stale_after_days:
            continue
        question = f"Want me to run {repo.name}'s test suite and report what's red?"
        scored.append(
            (
                days_stale,
                Candidate(
                    source=f"stale-project:{repo.name}",
                    headline=(
                        f"haven't touched {repo.name} in {days_stale} days — "
                        "pick it back up or let it go?"
                    ),
                    detail=f"last commit {last_commit.isoformat()}",
                    question=question,
                    deliverable=f"a pass/fail report on {repo.name}'s test suite",
                    action_type="repo_test_check",
                    action_payload={"repo_path": str(repo)},
                    novelty=1.0,
                    actionable=1.0,
                    timely=min(1.0, days_stale / 7),
                    interruption_worthy=0.65,
                    outcome=0.8,
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:MAX_STALE_SURFACED_PER_RUN]]


def gather_all() -> list[Candidate]:
    return (
        gather_upstream_proposals()
        + gather_stale_projects()
        + gather_gmail_unread()
        + gather_email_bridge_unread()
        + gather_sensei_chats()
    )


def _recent_log_entries(within_days: int) -> list[dict]:
    if not LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(days=within_days)
    out = []
    for line in LOG.read_text().splitlines():
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
        except Exception:
            continue
        if ts >= cutoff:
            out.append(entry)
    return out


def _already_gathered_shas(within_days: int = 30) -> set[str]:
    seen = set()
    for entry in _recent_log_entries(within_days):
        source = entry.get("source", "")
        if source.startswith("upstream-proposal:"):
            seen.add(source.split(":", 1)[1])
    return seen


def recall_suppress_recent_repeats(
    candidates: Iterable[Candidate], within_days: int = 3
) -> list[Candidate]:
    """RECALL: per the spec's own guardrail — "a declined suggestion is not
    re-raised unless the facts change" — drop any candidate whose source was
    already logged (surfaced OR suppressed) within the recall window. Keyed
    on source alone, not source+headline: a stale-project headline's day
    count changes daily, but it's still the same underlying fact being
    reconsidered, not a new one."""
    recent_sources = {e.get("source") for e in _recent_log_entries(within_days)}
    return [c for c in candidates if c.source not in recent_sources]


def vocalization_enabled() -> bool:
    """Off by default. On only when VOCALIZE_FLAG exists -- a plain
    `touch`/`rm`, not a setting buried in config, so turning voice on or off
    is a literal one-line command from anywhere."""
    return VOCALIZE_FLAG.exists()


def set_vocalization(on: bool) -> None:
    if on:
        VOCALIZE_FLAG.touch()
    else:
        VOCALIZE_FLAG.unlink(missing_ok=True)


def deliver_default(candidate: Candidate) -> None:
    """Always: print to stdout (run_loop's own _log_decision already writes
    every verdict to the jsonl log regardless of this function) AND fire a
    desktop popup -- unconditional, not gated on vocalization_enabled().

    Found 2026-10-20: the daily systemd job WAS surfacing real candidates
    (confirmed in the log) but print() alone is invisible under a
    systemd --user unit, and voice was opt-in-off, so "surfaced": true in
    the log was indistinguishable from nothing happening at all. A
    surfaced-but-undelivered suggestion is a bug, not a quiet success.
    sensei-notify.sh exists specifically to reach a real desktop popup from
    a daemon with no inherited display/dbus env (see its own header). Voice
    stays a separate, louder, still-opt-in channel on top of this.

    2026-10-23 fix order: delivers `candidate.question` (the "Want me to
    <X>?" form), not the bare headline -- falls back to headline only for
    old-shape candidates that never set question. When action_type is set,
    this is also where the proposal becomes real: it queues an
    approval_queue.py entry whose approval runs the registered handler, so
    saying yes actually executes instead of just being acknowledged.
    """
    message = candidate.question or candidate.headline
    if candidate.deliverable:
        message = f"{message} ({candidate.deliverable})"
    print(f"[SUGGEST] {message}")
    if NOTIFY_DESKTOP.exists():
        try:
            subprocess.run(
                [str(NOTIFY_DESKTOP), "Master AI", message, "normal"],
                timeout=10,
            )
        except Exception:
            pass  # desktop popup is best-effort; never let it break the loop
    if vocalization_enabled() and NOTIFY_VOICE.exists():
        try:
            subprocess.run([str(NOTIFY_VOICE), message], timeout=10)
        except Exception:
            pass  # voice is a nice-to-have; never let it break the loop
    if candidate.action_type:
        try:
            # OCR findings 2026-10-23, both folded in here:
            # (a) no dedup -- a candidate whose recall-suppression window
            #     has lapsed (e.g. a stale repo still stale 3+ days later)
            #     would queue a second PENDING entry for the same thing
            #     every time it re-surfaces. Skip queueing if an identical
            #     PENDING entry (same type + source) already exists.
            # (b) queue()/render_md() raising used to propagate out of
            #     deliver_default and abort the whole wake cycle AFTER the
            #     popup already fired -- wrapped to match the best-effort
            #     handling the notification calls above already have.
            already_pending = any(
                e.get("status") == "PENDING"
                and e.get("type") == candidate.action_type
                and e.get("where") == candidate.source
                for e in approval_queue.list_pending()
            )
            if not already_pending:
                approval_queue.queue(
                    entry_type=candidate.action_type,
                    who="suggestive_thinking",
                    what=candidate.deliverable or candidate.headline,
                    where=candidate.source,
                    why=candidate.detail or candidate.headline,
                    how=candidate.question,
                    diff=candidate.diff,
                    payload=candidate.action_payload or {},
                    trigger="suggestive-thinking wake cycle",
                )
        except Exception as e:
            print(f"[SUGGEST] approval_queue.queue failed for {candidate.source}: {e}")


def main() -> int:
    """Manual/cron entrypoint: one real wake cycle using the real sources
    above, delivering through deliver_default (unconditional desktop popup
    via sensei-notify.sh, plus voice only when vocalization is enabled).

    The scheduled daily job runs from a pinned worktree at
    ~/.master_ai_scheduled_jobs (see the LIVE_REPO comment above), so a
    branch switch in the live repo can't break the schedule; this copy is
    also directly invokable.

    Usage:
        python3 suggestive_thinking_sources.py                 # run one cycle
        python3 suggestive_thinking_sources.py --vocalize on    # turn voice on
        python3 suggestive_thinking_sources.py --vocalize off   # turn voice off
    """
    import sys

    from suggestive_thinking import run_loop

    if "--vocalize" in sys.argv:
        idx = sys.argv.index("--vocalize")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if arg not in ("on", "off"):
            print("usage: --vocalize on|off")
            return 1
        set_vocalization(arg == "on")
        print(f"vocalization {'enabled' if arg == 'on' else 'disabled'}")
        return 0

    results = run_loop(
        gather=gather_all,
        recall=lambda cs: recall_suppress_recent_repeats(cs),
        deliver=deliver_default,
    )
    surfaced = sum(1 for r in results if r.surfaced)
    print(
        f"suggestive-thinking: {len(results)} candidate(s) considered, {surfaced} surfaced"
        f" (voice {'on' if vocalization_enabled() else 'off'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
