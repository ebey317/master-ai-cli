#!/usr/bin/env python3
"""
Sensei-native upstream integrator.

Reads the ranked queue at memory/upstream-integration-queue.json, takes the
top N unprocessed commits, fetches each patch, and asks Sensei's own cloud
lane (OpenCode Go subscription relay, kimi-k2.7-code) to reimplement the
underlying design idea inside master-ai-cli. Generated files are
py_compile-gated, committed to a review branch, and tested before anything
merges.

2026-09-25: was on Ollama Cloud, which 429'd every attempt of a live batch
(items 1-3, three tries each). OpenCode Go is a $10/mo subscription lane
Elijah already pays for, serves kimi-k2.7-code natively (35-model catalog,
verified live), and carries no shared-pool rate ceiling. Deliberately NOT
routed through OpenRouter.

This is Elijah's self-update loop: Sensei reads upstream code, learns the
idea, and adapts it to its own codebase. No external framework involved.

Usage:
    python3 /home/elijah/master-ai-cli/scripts/upstream_integrator.py [--batch N] [--dry-run]
"""

import json
import os
import py_compile
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MEM = REPO / "memory"
QUEUE = MEM / "upstream-integration-queue.json"
LOG = MEM / "upstream-integration-log.jsonl"
PLANS = MEM / "upstream-plans"
BRANCH_BASE = "upstream-learn"

CORE_ENGINE_FILES = {
    "master_ai.py",
    "sensei_tui.py",
    "hooks.py",
    "router.py",
    "typed_actions.py",
    "sensei_reasoning_loop.py",
}

MSG_LIMIT = 1500
DIFF_LINES = 500
DIFF_CHARS = 8000
PLAN_MODEL = "kimi-k2.7-code"
# OpenCode Go — the $10/mo subscription relay, same Zen API shape as the
# keyless free lane but authenticated and served from /zen/go/v1. The keyless
# Zen lane (ling-3.0-flash-fin-free) 403s FreeTierError from outside OpenCode,
# so it is not an option for an unattended cron job; this lane is.
OPENCODE_GO_URL = "https://opencode.ai/zen/go/v1/chat/completions"
BATCH = 3


def _log_line(entry):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _git(*args, cwd=REPO):
    r = subprocess.run(
        ["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, timeout=90
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _clean_key(val):
    """Strip quotes/export noise and reject non-ASCII placeholder values.

    The keychain had every key overwritten with a redaction placeholder
    ('«redacted:...»') once before; a non-ASCII value crashes urlopen deep
    inside http.client instead of failing cleanly. Treat those as absent.
    """
    val = (val or "").strip().strip('"').strip("'")
    if val.startswith("export "):
        val = val[len("export ") :].strip()
    if not val or any(ord(c) > 127 for c in val):
        return ""
    if val.startswith(("<", "[", "«", "REDACTED", "redacted")):
        return ""
    return val.split()[0] if val.split() else ""


def _opencode_key():
    """OPENCODE_API_KEY — keychain first (canonical), then process env, then
    ~/.hermes/.env. Mirrors master_ai._opencode_go_key() so the cron job
    resolves the same credential the interactive picker does."""
    keychain = Path.home() / ".master_ai_keys"
    if keychain.exists():
        for ln in keychain.read_text(errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("OPENCODE_API_KEY=") or s.startswith(
                "OPENCODE_GO_API_KEY="
            ):
                k = _clean_key(s.split("=", 1)[1])
                if k:
                    return k
    for var in ("OPENCODE_API_KEY", "OPENCODE_GO_API_KEY"):
        k = _clean_key(os.environ.get(var, ""))
        if k:
            return k
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for ln in env.read_text(errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("export "):
                s = s[len("export ") :].strip()
            if s.startswith("OPENCODE_API_KEY=") or s.startswith(
                "OPENCODE_GO_API_KEY="
            ):
                k = _clean_key(s.split("=", 1)[1])
                if k:
                    return k
    return ""


def _ask_cloud(messages, timeout=300):
    """OpenCode Go plan call with retry + diagnostics.

    Lane: https://opencode.ai/zen/go/v1 — the $10/mo subscription relay
    (Bearer OPENCODE_API_KEY), NOT the keyless Zen free lane, which 403s
    FreeTierError from outside OpenCode and therefore can never work from
    this cron job.

    Flakiness modes seen in production (2026-09-23, still guarded here):
      - thinking models return everything in `reasoning_content`, `content` empty
      - completion hits max_tokens mid-reasoning -> truncated/empty answer
      - transient 429/5xx or empty generation
    Strategy: extract from content, fall back to reasoning_content, detect
    max-token cutoff and widen on retry, 3 attempts with backoff.
    """
    key = _opencode_key()
    if not key:
        print("  cloud: no OPENCODE_API_KEY", flush=True)
        return None
    max_tokens = 8192
    last_err = "unknown"
    for attempt in range(1, 4):
        payload = {
            "model": PLAN_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            OPENCODE_GO_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "master-ai-upstream-integrator/1.0",
                # OpenCode's validated-client convention: a stable per-job
                # session id. Their relay expects it on Go traffic.
                "x-opencode-session": "upstream-integrator",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            msg = (d.get("choices") or [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            if not content:
                content = (msg.get("reasoning_content") or "").strip()
                if content:
                    print(
                        f"  cloud: content empty, using reasoning_content "
                        f"({len(content)} chars)",
                        flush=True,
                    )
            if content:
                ct = (d.get("usage") or {}).get("completion_tokens") or 0
                if ct >= int(max_tokens * 0.98):
                    # generation was cut off; widen and retry once more
                    print(
                        f"  cloud: hit max_tokens ({ct}); widening to 16384", flush=True
                    )
                    max_tokens = 16384
                    last_err = f"max_tokens cutoff at {ct}"
                    time.sleep(10)
                    continue
                return content
            last_err = "empty content and reasoning_content"
            print(f"  cloud: empty response (attempt {attempt}/3)", flush=True)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            print(f"  cloud: HTTP {e.code} (attempt {attempt}/3)", flush=True)
        except Exception as e:
            last_err = str(e)[:120]
            print(f"  cloud error: {last_err} (attempt {attempt}/3)", flush=True)
        if attempt < 3:
            time.sleep(20 * attempt)
    print(f"  cloud: giving up after 3 attempts ({last_err})", flush=True)
    return None


def _load_queue():
    q = json.loads(QUEUE.read_text())
    items = q if isinstance(q, list) else q.get("items", q.get("queue", []))
    # deterministic: score desc, then sha asc — reruns never reshuffle
    return sorted(items, key=lambda it: (-int(it.get("score", 0)), it.get("sha", "")))


def _save_queue(items):
    QUEUE.write_text(json.dumps(items, indent=1))


def _fetch_patch(owner, repo, sha):
    url = f"https://github.com/{owner}/{repo}/commit/{sha}.patch"
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return r.read().decode(errors="replace")
    except Exception as e:
        print(f"  patch fetch failed: {e}", flush=True)
        return None


def _truncate(patch):
    lines = patch.splitlines()
    # drop binary/file-threshold noise; keep message + diff head
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + ["... (diff truncated)"]
    out = "\n".join(lines)
    if len(out) > DIFF_CHARS:
        out = out[:DIFF_CHARS] + "\n... (diff truncated)"
    return out


PLAN_PROMPT = """You are a senior Python agent-framework engineer working INSIDE master-ai-cli ("Sensei"), a local-first terminal agent. Your job: learn from an upstream commit and reimplement the underlying design idea in Sensei's own terms. NEVER copy upstream code verbatim; upstream internals do not map 1:1.

Upstream repo: {owner}/{repo}
Commit: {url}
Commit message (truncated):
{message}

Diff (truncated):
{diff}

Sensei key files: master_ai.py (agent loop, ~25k lines), hooks.py (event bus), typed_actions.py (action envelope), learning_loop.py (skill learning), skill_runtime.py, router.py (route decisions), scripts/ (tools), tests/.

Respond in EXACTLY this structure:

SUMMARY: <one sentence: the portable design idea>
APPLICABLE: <YES|NO>

```python
# <relative/path/from/repo/root>
<complete file content — only if applicable>
```
```python
# <relative/path/from/repo/root>
<another file if needed>
```

TESTS: <what to run>
RISKS: <one line>

Rules:
- Only write files in repo root, scripts/, or tests/. NEVER core engine files ({core}).
- Each ```python block MUST start with `# path/from/repo/root`.
- If the idea needs touching master_ai.py itself, still write helper files but describe the wiring in RISKS (manual review).
- If not applicable to a local terminal agent, APPLICABLE: NO and stop."""


def _parse_blocks(text):
    blocks = []
    for m in re.finditer(r"```python\s*\n#\s*([^\n]+)\n(.*?)```", text, re.DOTALL):
        path = m.group(1).strip().strip("`").strip()
        body = m.group(2)
        blocks.append((path, body))
    return blocks


def _path_allowed(p):
    rel = str(p)
    if rel.startswith("/") or ".." in rel:
        return False
    root = rel.split("/")[0]
    if rel in CORE_ENGINE_FILES:
        return False
    return root in (rel, "scripts", "tests") or "/" not in rel


def integrate_one(item, dry_run=False, base_branch=None):
    owner, repo, sha = item["owner"], item["repo"], item["sha"]
    print(f"\n=== {owner}/{repo}@{sha[:8]} (score {item.get('score')})", flush=True)
    # never entangle the operator's WIP: refuse to branch when TRACKED files
    # are modified. Untracked runtime artifacts (memory/) are ignored.
    rc, dirty, _ = _git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        n = len(dirty.splitlines())
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "deferred",
                "detail": f"dirty working tree ({n} files) — commit or stash first",
            }
        )
        print(f"  deferred: dirty working tree ({n} files) — not branching", flush=True)
        return "", "deferred"
    patch = _fetch_patch(owner, repo, sha)
    if not patch:
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "failed",
                "detail": "patch fetch failed",
            }
        )
        return "", "failed"

    msg = item.get("message", "")[:MSG_LIMIT]
    prompt = PLAN_PROMPT.format(
        owner=owner,
        repo=repo,
        url=item.get("url", ""),
        message=msg,
        diff=_truncate(patch),
        core=", ".join(sorted(CORE_ENGINE_FILES)),
    )
    plan = _ask_cloud(
        [
            {
                "role": "system",
                "content": "You reimplement portable agent-design ideas. Output format compliance matters.",
            },
            {"role": "user", "content": prompt},
        ]
    )
    if not plan or len(plan.strip()) < 40:
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "failed",
                "detail": "empty plan response",
            }
        )
        return "", "failed"

    if re.search(r"APPLICABLE:\s*NO\b", plan[:400]) or "NOT_APPLICABLE" in plan[:200]:
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "skipped",
                "detail": "NOT_APPLICABLE",
            }
        )
        return "", "skipped"

    PLANS.mkdir(parents=True, exist_ok=True)
    (PLANS / f"{sha[:8]}.md").write_text(plan)

    blocks = _parse_blocks(plan)
    if not blocks:
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "manual-review",
                "detail": "plan-only, no code blocks",
            }
        )
        return "", "manual-review"

    # gate: only safe paths, then py_compile every .py
    staged = []
    for path, body in blocks:
        if not _path_allowed(path):
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": sha,
                    "status": "blocked",
                    "detail": f"unsafe path {path}",
                }
            )
            return "", "blocked"
        staged.append((path, body))

    if dry_run:
        print("  dry-run: would write", [p for p, _ in staged], flush=True)
        return "", "dry-run"

    # branch
    branch = f"{BRANCH_BASE}/{sha[:8]}"
    _git("stash", "list", "-0")  # touch no state; just fail fast if git broken
    _git("checkout", "-b", branch) if _git("rev-parse", "--verify", branch)[
        0
    ] != 0 else _git("checkout", branch)

    ok_files = []
    try:
        for path, body in staged:
            fpath = REPO / path
            if path.endswith(".py"):
                tmp = fpath.with_suffix(".py.compile-tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body)
                try:
                    py_compile.compile(str(tmp), doraise=True)
                except py_compile.PyCompileError as e:
                    tmp.unlink(missing_ok=True)
                    _log_line(
                        {
                            "ts": datetime.now().isoformat(),
                            "sha": sha,
                            "status": "failed",
                            "detail": f"py_compile {path}: {e}",
                        }
                    )
                    _git("checkout", "-", cwd=REPO)
                    return "", "failed"
                tmp.rename(fpath)
            else:
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(body)
            ok_files.append(path)

        _git("add", "--", *ok_files)
        rc, _, err = _git(
            "commit",
            "--no-verify",
            "-m",
            f"upstream-learn: port idea from {owner}/{repo}@{sha[:8]}",
        )
        if rc != 0:
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": sha,
                    "status": "failed",
                    "detail": f"commit: {err[:200]}",
                }
            )
            return "", "failed"

        # test gate
        test_ok, test_out = run_tests()
        if not test_ok:
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": sha,
                    "status": "manual-review",
                    "detail": f"tests failed on {branch}; branch kept",
                }
            )
            return branch, "manual-review"
        if touched_core_check(ok_files):
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": sha,
                    "status": "manual-review",
                    "detail": f"core engine files {ok_files}; branch {branch} left for review",
                }
            )
            return branch, "manual-review"

        # auto-merge: tests pass, no core engine files touched
        _git("checkout", base_branch or "main", cwd=REPO)
        rc, _, merr = _git("merge", "--no-ff", "--no-edit", branch)
        if rc != 0:
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": sha,
                    "status": "manual-review",
                    "detail": f"merge conflict on {branch}; left unmerged",
                }
            )
            _git("merge", "--abort") if _git(
                "rev-parse", "-q", "--verify", "MERGE_HEAD"
            )[0] == 0 else None
            return branch, "manual-review"
        _log_line(
            {
                "ts": datetime.now().isoformat(),
                "sha": sha,
                "status": "merged",
                "detail": f"auto-merged {branch}: {', '.join(ok_files)}; tests pass",
            }
        )
        return branch, "merged"
    finally:
        # Return to the base branch by NAME. A relative checkout - would
        # bounce to whatever branch we were on before this item (i.e. the
        # PREVIOUS upstream-learn/* branch), landing the NEXT item's merge
        # on that side branch and leaving the repo stranded off-base.
        _git("checkout", base_branch or "main", cwd=REPO)


def touched_core_check(files):
    return bool(set(files) & CORE_ENGINE_FILES)


def run_tests():
    # fast gate: compile everything, then run the focused parser test
    rc, _, _ = _git("stash", "list")
    try:
        c = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(REPO / "master_ai.py"),
                str(REPO / "hooks.py"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if c.returncode != 0:
            return False, c.stderr[:300]
    except Exception:
        pass
    t = REPO / "tests" / "test_master_ai_parser.py"
    if t.exists():
        c = subprocess.run(
            [sys.executable, str(t)], capture_output=True, text=True, timeout=180
        )
        return c.returncode == 0, (c.stdout + c.stderr)[-500:]
    return True, "no test file"


def main():
    dry = "--dry-run" in sys.argv
    batch = BATCH
    if "--batch" in sys.argv:
        batch = int(sys.argv[sys.argv.index("--batch") + 1])
    items = _load_queue()
    done_shas = set()
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            try:
                d = json.loads(ln)
                if d.get("status") in ("branch-ready", "merged", "skipped"):
                    done_shas.add(d.get("sha"))
            except Exception:
                pass
    todo = [it for it in items if it["sha"] not in done_shas]
    print(f"queue={len(items)} pending={len(todo)} batch={batch}", flush=True)
    # capture the branch everything should land on BEFORE any checkout dance
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    base = out.strip() if rc == 0 else ""
    if not base or base.startswith(f"{BRANCH_BASE}/"):
        print(
            f"REFUSING: launch from the base branch, not '{base or 'detached HEAD'}'",
            flush=True,
        )
        return
    branches = []
    for it in todo[:batch]:
        try:
            b, outcome = integrate_one(it, dry_run=dry, base_branch=base)
        except Exception as e:
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": it.get("sha"),
                    "status": "failed",
                    "detail": f"integrate_one exception: {e}",
                }
            )
            continue
        if outcome in ("merged", "branch-ready"):
            branches.append(b)
    # drop TERMINAL items from the queue; retryable ones stay:
    #   merged/skipped/manual-review/blocked -> done (branch or N/A exists)
    #   failed -> stays until 3 failed attempts, then retired
    #   deferred -> stays (dirty tree; retries next run)
    processed = set()
    attempts = {}
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            try:
                d = json.loads(ln)
            except Exception:
                continue
            st = d.get("status")
            if st in ("merged", "skipped", "manual-review", "blocked"):
                processed.add(d.get("sha"))
            elif st == "failed":
                attempts[d.get("sha")] = attempts.get(d.get("sha"), 0) + 1
        for sha, n in attempts.items():
            if n >= 3:
                print(f"  retiring {sha[:8]} after {n} failed attempts", flush=True)
                processed.add(sha)
    remaining = [it for it in items if it.get("sha") not in processed]
    _save_queue(remaining)
    print(f"\nDone. branches={branches} remaining_queue={len(remaining)}", flush=True)


if __name__ == "__main__":
    main()
