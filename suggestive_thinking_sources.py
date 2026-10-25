"""Real GATHER and RECALL sources for the Suggestive Thinking loop.

Kept separate from suggestive_thinking.py because these touch real files and
git — the loop itself (run_loop, passes_relevance_filter) stays pure and
testable against injected stubs. This module is what a real caller wires in.
"""

from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# 2026-10-23 fix order, Hermes half (HANDOFF.md "work split with Hermes"):
# the GRANTS and MONEY/ORG sources. His business clocks, on the loop.
#
# Data lives OUTSIDE the repo in workspace state files -- the repo never
# hardcodes his business data, and both paths stay injectable parameters on
# each gather function so tests never touch real state.
#
# The GRANTS source deliberately re-surfaces DEADLINES on grants already
# listed to the operator. That is clock-watching on money clocks he cares
# about -- NOT the seen-grants ledger's "do not re-list as fresh" rule,
# which governs the weekly research batch, not deadline reminders.
# ---------------------------------------------------------------------------
GRANTS_STATE = (
    Path.home()
    / "workspace"
    / "goals"
    / "weekly-grant-applications"
    / "grant-deadlines.json"
)
MONEY_ORG_STATE = Path.home() / "workspace" / "goals" / "money-org-state.json"
GRANT_PACKET_DIR = GRANTS_STATE.parent / "packets"

# Attention budget (the spec's own hard rule): real clock pressure, small
# batches. Deadlines inside the window surface, worst clock first, so the
# cap rotates through the clock list instead of always showing the same
# three. Rolling grants have no clock and stay out of this source entirely
# -- the weekly research batch owns those.
GRANT_SURFACE_WINDOW_DAYS = 14
GRANT_CAP_PER_RUN = 3
# 2026-10-24 (his catch): cap=2 capped the Gumroad store audit OUT of the
# wake batch entirely — a real money item that never surfaced is the same
# class of bug as a suggestion that can't be executed. 3 money items now
# surface per run (attention budget still honored: they rotate).
MONEY_CAP_PER_RUN = 3
GRANT_TIME_CRITICAL_DAYS = 3  # inside 3 days the clock overrides the gate


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


def gather_upstream_proposals() -> list[Candidate]:
    """GATHER: unsurfaced proposal docs from the proposal-only
    perpetual-watcher cron — one candidate per "status": "proposal" line in
    the integration log that hasn't already produced a candidate before
    (see _already_gathered_shas; a 30-day window, not forever — the
    integrator's own 3-failed-attempt retry logic is what actually retires
    a dead idea from the queue)."""
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
        candidates.append(
            Candidate(
                source=f"upstream-proposal:{sha[:8]}",
                headline=f"upstream idea proposed, not yet reviewed: {sha[:8]}",
                detail=(
                    f"plan: {plan_path}"
                    if plan_path.exists()
                    else entry.get("detail", "")
                ),
                novelty=1.0,
                actionable=1.0,
                timely=0.8,
                interruption_worthy=0.7,
            )
        )
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


@approval_queue.register_handler("repo_test_check")
def _run_repo_test_check(entry: dict) -> str:
    """Executor for the 'stale repo' proposal's Yes: run whatever test
    runner the repo actually has and report pass/fail. Auto-detects
    pytest (conftest.py / pytest.ini / a tests/ dir) over npm test
    (package.json) since every active repo on this box is Python-first;
    reports plainly if neither is present rather than guessing a command.

    Scope note (2026-10-23): this runs the suite and reports red/green —
    it does NOT auto-fix failures yet. "Fix what's red" as a fully
    autonomous repair loop is a materially bigger feature (a coding
    subagent loop against the failures) and is called out as a separate
    next step in HANDOFF.md rather than being half-built here.
    """
    repo = Path(entry["payload"]["repo_path"])
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
    try:
        out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return f"test run failed to start: {e}"
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
        question = f"Want me to run {repo.name}'s test suite and fix what's red?"
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


def _load_state_items(path: Path) -> list[dict]:
    """Shared state-file loader for the two workspace-clock sources.
    Missing file, malformed JSON, or non-dict entries all degrade to an
    empty list -- a broken state file must never crash the whole GATHER
    cycle (same contract as _email_bridge_unread_count's None-on-failure)."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    items = data.get("items", []) if isinstance(data, dict) else data
    return [e for e in items if isinstance(e, dict)]


def _deadline_scores(days_left: int) -> tuple[float, bool]:
    """(timely, time_critical) for a deadline N days out. Inside
    GRANT_TIME_CRITICAL_DAYS the clock overrides the gate entirely -- the
    spec's time-critical bypass exists for exactly this: money/deadline-today."""
    if days_left <= GRANT_TIME_CRITICAL_DAYS:
        return 1.0, True
    return 0.75, False


def gather_grant_deadlines(index_path: Path | None = None) -> list[Candidate]:
    """GATHER — the GRANTS source (2026-10-23 fix order, Hermes half).

    Reads the machine-readable grant-deadlines.json under
    ~/workspace/goals/weekly-grant-applications/ (kept current by the
    weekly batch cron, seeded from the operator's verified seen-grants
    ledger) and turns approaching money clocks into proposals:

        "Grant X closes in N days — want me to assemble the packet?"

    Rules encoded here:
    - Only real deadlines surface: status in {"open", "pipeline"} and a
      parseable ISO deadline inside GRANT_SURFACE_WINDOW_DAYS. Rolling
      grants (no deadline) belong to the weekly research batch, not this
      source. "skip"/"closed" entries stay out — the ledger carries why.
    - On approval (action_type="grant_packet") the handler assembles the
      packet doc under GRANT_PACKET_DIR/<grant_id>/ — file writes only;
      NOTHING ever auto-submits. Submission is the operator's.
    - outcome=0.9: assembling a packet ahead of a real deadline is grant
      progress by definition. time_critical inside 3 days = money clock,
      gate bypass, per the spec's own override list.
    """
    index_path = GRANTS_STATE if index_path is None else Path(index_path)
    now = datetime.now()
    scored: list[tuple[int, Candidate]] = []
    for item in _load_state_items(index_path):
        if item.get("status") not in ("open", "pipeline"):
            continue
        try:
            deadline = datetime.fromisoformat(str(item.get("deadline")))
        except Exception:
            continue  # no parseable clock = not this source's job
        days_left = (deadline - now).days
        if not (0 <= days_left <= GRANT_SURFACE_WINDOW_DAYS):
            continue
        gid = (
            "".join(
                ch for ch in str(item.get("id") or "grant") if ch.isalnum() or ch == "-"
            )
            or "grant"
        )
        name = str(item.get("name") or gid)
        amount = str(item.get("amount") or "")
        url = str(item.get("url") or "")
        note = str(item.get("note") or "")
        timely, time_critical = _deadline_scores(days_left)
        detail = url or "no url listed"
        if note:
            detail += f" — {note}"
        scored.append(
            (
                days_left,
                Candidate(
                    source=f"grant-deadline:{gid}",
                    headline=(
                        f"{name} closes in {days_left} day{'s' if days_left != 1 else ''}"
                        f"{' — ' + amount if amount else ''} — clock it or let it pass?"
                    ),
                    detail=detail,
                    question=f"Want me to assemble the ready-to-submit packet for {name}?",
                    deliverable=(
                        f"an application packet for {name}"
                        f"{' (' + amount + ')' if amount else ''}"
                        " — cover sheet, checklist, and what you still need, on disk"
                    ),
                    action_type="grant_packet",
                    action_payload={
                        "grant_id": gid,
                        "name": name,
                        "amount": amount,
                        "deadline": deadline.strftime("%Y-%m-%d"),
                        "url": url,
                        "note": note,
                    },
                    novelty=1.0,
                    actionable=1.0,
                    timely=timely,
                    interruption_worthy=0.7,
                    outcome=0.9,
                    time_critical=time_critical,
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0])  # worst clock first
    return [c for _, c in scored[:GRANT_CAP_PER_RUN]]


def gather_money_org(state_path: Path | None = None) -> list[Candidate]:
    """GATHER — the MONEY/ORG source (2026-10-23 fix order, Hermes half).

    Reads money-org-state.json under ~/workspace/goals/ and surfaces the
    operator's open money/organization items as executable proposals:

        "Want me to finish/publish/submit X?"

    The honesty rule, same as the spec's own bar: a surfaced suggestion
    that cannot be acted on is a defect, and a FAKE check is worse than
    none. So only {"status": "active"} items with a real list-form
    check_cmd (argv, read-only) surface at all. A "seeded" item without a
    wired check stays silent until the wiring is real.

    On approval (action_type="money_org_check") the generic handler runs
    exactly that argv list and reports — read-only state check by design.
    """
    state_path = MONEY_ORG_STATE if state_path is None else Path(state_path)
    now = datetime.now()
    scored: list[tuple[tuple[int, float], Candidate]] = []
    for item in _load_state_items(state_path):
        if item.get("status") != "active":
            continue
        check_cmd = item.get("check_cmd")
        if not (
            isinstance(check_cmd, list)
            and check_cmd
            and all(isinstance(part, str) for part in check_cmd)
        ):
            continue  # defect guard: no real executable check, no surface
        try:
            outcome = min(1.0, max(0.0, float(item.get("outcome", 0.0))))
        except Exception:
            outcome = 0.0
        timely = 0.75
        time_critical = False
        sort_key = (999, -outcome)
        raw_deadline = item.get("deadline")
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(str(raw_deadline))
                days_left = (deadline - now).days
                timely, time_critical = _deadline_scores(days_left)
                if days_left < 0:
                    continue  # the event already passed; stop nudging
                sort_key = (days_left, -outcome)
            except Exception:
                pass
        iid = (
            "".join(
                ch for ch in str(item.get("id") or "item") if ch.isalnum() or ch == "-"
            )
            or "item"
        )
        scored.append(
            (
                sort_key,
                Candidate(
                    source=f"money-org:{iid}",
                    headline=str(item.get("question") or f"open money item: {iid}"),
                    detail=str(item.get("note") or ""),
                    question=str(item.get("question") or f"Want me to handle {iid}?"),
                    deliverable=str(
                        item.get("deliverable") or "a real state check, on the record"
                    ),
                    action_type="money_org_check",
                    action_payload={"item_id": iid, "check_cmd": check_cmd},
                    novelty=1.0,
                    actionable=1.0,
                    timely=timely,
                    interruption_worthy=0.65,
                    outcome=outcome,
                    time_critical=time_critical,
                ),
            )
        )
    scored.sort(key=lambda pair: pair[0])
    return [c for _, c in scored[:MONEY_CAP_PER_RUN]]


@approval_queue.register_handler("grant_packet")
def _run_grant_packet(entry: dict) -> str:
    """Executor for the GRANTS source's Yes: assemble the packet doc for
    one grant under GRANT_PACKET_DIR/<grant_id>/ — writes only; never
    submits anywhere. If the index entry ever carries ready-to-paste
    answers ('answers'), they go in verbatim, attributed to the index;
    otherwise the packet says so plainly instead of inventing content."""
    payload = entry.get("payload") or {}
    grant_id = (
        "".join(
            ch
            for ch in str(payload.get("grant_id") or "grant")
            if ch.isalnum() or ch == "-"
        )
        or "grant"
    )
    out_dir = GRANT_PACKET_DIR / grant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = out_dir / "packet.md"
    lines = [
        f"# Grant packet — {payload.get('name', grant_id)}",
        "",
        f"**Amount:** {payload.get('amount') or '—'}  ",
        f"**Deadline:** {payload.get('deadline') or '—'}  ",
        f"**URL:** {payload.get('url') or '—'}",
        "",
        "## Notes from the ledger",
        payload.get("note") or "—",
        "",
        "## Ready-to-paste answers",
    ]
    answers = payload.get("answers") or {}
    if isinstance(answers, dict) and answers:
        lines += [f"- **{k}:** {v}" for k, v in answers.items()]
    else:
        lines.append(
            "_(none recorded in the index yet — see the packet template in the seen-grants ledger)_"
        )
    lines += [
        "",
        "## Before submitting (his checklist, not mine)",
        "- confirm the form matches the ledger notes above"
        " (email — including any flag like the AOL/Gmail switch — fees, eligibility)",
        "- this packet never auto-submits: Lijah submits",
        "",
    ]
    text = "\n".join(lines)
    packet.write_text(text)
    return f"packet written: {packet} ({len(text)} chars)\n"


@approval_queue.register_handler("money_org_check")
def _run_money_org_check(entry: dict) -> str:
    """Executor for the MONEY/ORG source's Yes: run the item's own
    list-form read-only check exactly as stored in the state file and
    report the output. Refuses shell-string form on principle — the state
    file's format is argv, and argv is what argv gets."""
    payload = entry.get("payload") or {}
    cmd = payload.get("check_cmd")
    if not (
        isinstance(cmd, list) and cmd and all(isinstance(part, str) for part in cmd)
    ):
        return (
            "refused: check_cmd must be a non-empty list of argv strings "
            "(state-file format) — never a shell string"
        )
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return f"check failed to run: {e}"
    text = (out.stdout + out.stderr).strip()
    if len(text) > 1500:
        text = text[:1500] + " …[truncated]"
    command_head = " ".join(cmd[:3])
    return f"exit {out.returncode} — {command_head}…\n{text or '(no output)'}"


def gather_all() -> list[Candidate]:
    return (
        gather_upstream_proposals()
        + gather_stale_projects()
        + gather_gmail_unread()
        + gather_email_bridge_unread()
        + gather_sensei_chats()
        + gather_grant_deadlines()
        + gather_money_org()
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
        approval_queue.queue(
            entry_type=candidate.action_type,
            who="suggestive_thinking",
            what=candidate.deliverable or candidate.headline,
            where=candidate.source,
            why=candidate.detail or candidate.headline,
            how=candidate.question,
            payload=candidate.action_payload or {},
            trigger="suggestive-thinking wake cycle",
        )


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
