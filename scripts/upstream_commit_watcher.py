#!/usr/bin/env python3
"""
Upstream commit watcher for Sensei / master-ai-cli.

Polls selected upstream repositories every two weeks, fetches commits since
the last check, and produces a dated digest of commits that look applicable to
Sensei's own architecture.  The digest is written to
~/master-ai-cli/memory/upstream-digest/ for human review.

Tracked upstreams (default):
    anthropics/claude-code
    NousResearch/hermes-agent
    MoonshotAI/kimi-code

The script is intentionally read-only: it fetches commit metadata and does NOT
apply any changes to Sensei automatically.  A later `/learn` or manual review
step can turn digest entries into real changes.

Usage:
    python3 /home/elijah/master-ai-cli/scripts/upstream_commit_watcher.py
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPOS: list[tuple[str, str]] = [
    # (owner, repo)
    ("anthropics", "claude-code"),
    ("NousResearch", "hermes-agent"),
    ("MoonshotAI", "kimi-code"),
]

# Topics we care about when scoring a commit's applicability to Sensei.
# 2026-09-25: this list was pure backend/agent-loop vocabulary -- nothing
# here could ever match a front-end, UX, performance, security, testing,
# or docs improvement, no matter how good it was. Elijah: "it needs to add
# those to the keywords to make my framework... better. improvements
# overall, not just in the code. all around." Added two groups below,
# each real and scoped -- not generic buzzwords like "fix"/"update" (those
# are already the action-verb half of _score_commit()'s bonus regex, and
# adding them as bare topic keywords would match nearly every commit ever
# written, defeating the whole point of a relevance filter).
SENSEI_KEYWORDS = {
    "tool",
    "dispatch",
    "sandbox",
    "skill",
    "mcp",
    "router",
    "agent",
    "loop",
    "terminal",
    "tui",
    "safety",
    "approval",
    "privacy",
    "reasoning",
    "subagent",
    "audit",
    "capability",
    "filesystem",
    "parse",
    "directive",
    "prompt",
    "context",
    "memory",
    "cron",
    "schedule",
    "extension",
    "bridge",
    "headless",
    "observability",
    "self-improve",
    "selfimprove",
    "self improve",
    "learn",
    "learning",
    "reflection",
    "feedback",
    "evolve",
    "continuation",
    "continue",
    "turn continuation",
    "multi-turn",
    # ── front-end / UX / design ──
    "ui",
    "ux",
    "frontend",
    "front-end",
    "design",
    "theme",
    "color scheme",
    "accessibility",
    "responsive",
    "styling",
    "layout",
    "usability",
    "onboarding",
    "dashboard",
    # ── back-end / broader engineering quality ──
    "performance",
    "latency",
    "caching",
    "database",
    "logging",
    "monitoring",
    "security",
    "vulnerability",
    "hardening",
    "encryption",
    "authentication",
    "testing",
    "test coverage",
    "documentation",
    "deployment",
    "optimization",
}

# Minimum score for a commit to appear in the digest.
RELEVANCE_THRESHOLD = 4

# Max entries per repo in the digest (keeps reading manageable).
MAX_ENTRIES_PER_REPO = 50

# Paths
REPO_ROOT = Path.home() / "master-ai-cli"
MEMORY_DIR = REPO_ROOT / "memory"
DIGEST_DIR = MEMORY_DIR / "upstream-digest"
STATE_FILE = MEMORY_DIR / "upstream-watcher-state.json"

# Lookback window when no previous state exists (e.g. first run).
DEFAULT_LOOKBACK_DAYS = 14

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _github_api(owner: str, repo: str, path: str) -> dict | list:
    """Call the GitHub REST API for a repo, returning parsed JSON."""
    url = f"https://api.github.com/repos/{owner}/{repo}{path}"
    cmd = [
        "gh",
        "api",
        url,
        "--method",
        "GET",
        "--paginate",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(
            f"[ERROR] gh API failed for {owner}/{repo}{path}: {exc.stderr}\n"
        )
        return {}
    except FileNotFoundError:
        sys.stderr.write(
            "[ERROR] 'gh' CLI not found. Install GitHub CLI and authenticate.\n"
        )
        return {}

    try:
        data = json.loads(result.stdout)
        if isinstance(data, list):
            return data
        return data
    except json.JSONDecodeError:
        combined: list[dict] = []
        decoder = json.JSONDecoder()
        text = result.stdout.strip()
        while text:
            text = text.lstrip()
            try:
                obj, idx = decoder.raw_decode(text)
                text = text[idx:]
                if isinstance(obj, list):
                    combined.extend(obj)
                else:
                    combined.append(obj)
            except json.JSONDecodeError:
                break
        return combined


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"repos": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _score_commit(commit: dict) -> int:
    """Score how relevant a commit message is to Sensei's architecture."""
    msg = commit.get("commit", {}).get("message", "").lower()
    score = 0
    for keyword in SENSEI_KEYWORDS:
        if keyword in msg:
            score += 1
            if re.search(
                rf"\b(add|fix|implement|refactor|wire|enforce|validate|harden|sandbox|improve|learn)\s+\S*\s*{keyword}\b|\b{keyword}\s+(fix|support|improvement|refactor|rewrite|harden)\b",
                msg,
            ):
                score += 2
    return score


def _trim_message(msg: str, max_lines: int = 12) -> str:
    lines = msg.splitlines()
    if len(lines) <= max_lines:
        return msg
    return "\n".join(lines[:max_lines]) + "\n[...message truncated...]"


def _fetch_commits(owner: str, repo: str, since: datetime) -> list[dict]:
    """Fetch commits on default branch since `since` using GitHub API."""
    since_iso = _iso(since)
    commits = _github_api(owner, repo, f"/commits?since={since_iso}&per_page=100")
    if not isinstance(commits, list):
        return []
    return commits


def _write_digest(
    run_date: datetime,
    findings: dict[tuple[str, str], list[dict]],
) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    date_str = run_date.strftime("%Y-%m-%d")
    path = DIGEST_DIR / f"upstream-digest-{date_str}.md"

    lines = [
        f"# Upstream Commit Digest — {date_str}",
        "",
        f"Generated: {_iso(run_date)}",
        "Watched upstreams: `anthropics/claude-code`, `NousResearch/hermes-agent`, `MoonshotAI/kimi-code`.",
        "",
        "This digest lists commits from upstream agent frameworks that may contain ideas, fixes, or patterns applicable to Sensei / master-ai-cli.  It does **not** apply changes automatically; review entries and run Sensei's `/learn` workflow or manual integration for anything worth adopting.",
        "",
        "---",
        "",
    ]

    total = 0
    for (owner, repo), commits in findings.items():
        repo_slug = f"{owner}/{repo}"
        lines.append(f"## {repo_slug}")
        lines.append("")
        if not commits:
            lines.append("No new commits since last check.")
            lines.append("")
            continue

        relevant = [c for c in commits if _score_commit(c) >= RELEVANCE_THRESHOLD]
        total += len(relevant)

        if not relevant:
            lines.append(
                f"{len(commits)} new commit(s); none scored above the relevance threshold."
            )
            lines.append("")
            continue

        relevant.sort(
            key=lambda c: (
                -_score_commit(c),
                c.get("commit", {}).get("committer", {}).get("date", ""),
            ),
            reverse=False,
        )
        shown = relevant[:MAX_ENTRIES_PER_REPO]
        extra_count = len(relevant) - len(shown)

        lines.append(
            f"{len(relevant)} relevant of {len(commits)} new commit(s):"
            + (f" (showing top {len(shown)})" if extra_count else "")
        )
        lines.append("")

        for c in shown:
            sha = c.get("sha", "")[:7]
            html_url = c.get("html_url", "")
            commit = c.get("commit", {})
            msg = commit.get("message", "")
            author = commit.get("author", {}).get("name", "unknown")
            date = commit.get("committer", {}).get("date", "")
            score = _score_commit(c)
            lines.append(f"### [{sha}]({html_url}) — score {score}")
            lines.append(f"- **Author:** {author}  ")
            lines.append(f"- **Date:** {date}  ")
            lines.append("- **Message:**")
            lines.append("```text")
            lines.append(_trim_message(msg))
            lines.append("```")
            lines.append("")

        if extra_count:
            lines.append(
                f"_{extra_count} more relevant commit(s) omitted; lower the threshold or inspect via `gh api`._"
            )
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"**Total relevant commits this run:** {total}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    run_date = _now()
    state = _load_state()
    state.setdefault("repos", {})

    findings: dict[tuple[str, str], list[dict]] = {}

    for owner, repo in REPOS:
        repo_key = f"{owner}/{repo}"
        last_check_str = state["repos"].get(repo_key, {}).get("last_check")
        if last_check_str:
            since = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))
        else:
            since = run_date - timedelta(days=DEFAULT_LOOKBACK_DAYS)

        commits = _fetch_commits(owner, repo, since)
        findings[(owner, repo)] = commits

        state["repos"][repo_key] = {
            "last_check": _iso(run_date),
            "last_commit_count": len(commits),
        }

    digest_path = _write_digest(run_date, findings)
    _save_state(state)

    print(f"Upstream commit digest written to: {digest_path}")
    for (owner, repo), commits in findings.items():
        relevant = [c for c in commits if _score_commit(c) >= RELEVANCE_THRESHOLD]
        print(
            f"  {owner}/{repo}: {len(relevant)} relevant / {len(commits)} new commits"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
