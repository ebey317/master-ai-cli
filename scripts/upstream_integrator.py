#!/usr/bin/env python3
"""
Sensei-native upstream integrator — PROPOSAL-ONLY (2026-10-18).

Reads the ranked queue at memory/upstream-integration-queue.json, takes the
top N unprocessed commits, fetches each patch, and asks Sensei's own cloud
lane (OpenRouter free tier, nemotron-3-ultra-550b) to reimplement the
underlying design idea inside master-ai-cli. Writes a proposal doc under
memory/upstream-plans/<sha>.md and logs it. **Never touches git, never
writes a file into the live repo, never branches, never commits, never
merges.** Prior behavior (auto-branch, py_compile-gate, auto-merge into
main when tests passed) is retired as of 2026-10-18 — it left the repo
stranded off master at least once and committed unreviewed code into a
live working tree. Operator's own call: "flip it to proposal-only," so this
script's only output is a markdown proposal for a human (or a reviewed
no-mistakes/CodeRabbit pass) to act on later. This is also the mechanism
behind the "Muse-style" passive check-in/reminder behavior the operator
wants from master_ai — surface, don't act.

2026-09-25: moved off the $10/mo OpenCode Go plan (kimi-k2.7-code) onto
OpenRouter's free tier — Elijah: "let's put it to a free model that's
capable of doing the same thing as kimi." nvidia/nemotron-3-ultra-550b-a55b
:free is the pick: this exact slug is already this codebase's proven
planner-tier model (see PLAN_DEBATE_PLANNER_B, master_ai.py), so it's not
a downgrade guess. Only batch=3/night, so OpenRouter's shared free-tier
rate ceiling (the reason OpenCode was chosen last time, for the much
higher-volume Ollama Cloud path) isn't the same risk here. Hard-gated to
":free"-suffixed models only, per the 2026-08-27 operator rule enforced at
master_ai.py's own OpenRouter chokepoint: no silent paid fallback.

This is Elijah's self-update loop: Sensei reads upstream code, learns the
idea, and adapts it to its own codebase. No external framework involved.

Usage:
    python3 /home/elijah/master-ai-cli/scripts/upstream_integrator.py [--batch N]
"""

import json
import os
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

CORE_ENGINE_FILES = {
    "master_ai.py",
    "sensei_tui.py",
    "hooks.py",
    "router.py",
    "typed_actions.py",
    "sensei_reasoning_loop.py",
}

MSG_LIMIT = 1500
DIFF_LINES = 200
DIFF_CHARS = 4000
# Must end in ":free" -- see the free-only gate in _ask_cloud().
PLAN_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
OPENCODEGO_URL = "http://127.0.0.1:8793/v1/chat/completions"
OPENCODEGO_MODEL = "glm-5.3-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
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


def _openrouter_key():
    """OPENROUTER_API_KEY — keychain first (canonical: ~/.master_ai_keys is
    JSON with an "openrouter" field, same file master_ai.py's KEYS dict
    loads), then legacy KV-line form, then process env, then ~/.hermes/.env.
    Mirrors master_ai.load_keys()/KEYS["openrouter"] so the cron job
    resolves the same credential the interactive picker does."""
    keychain = Path.home() / ".master_ai_keys"
    if keychain.exists():
        text = keychain.read_text(errors="replace")
        try:
            k = _clean_key(json.loads(text).get("openrouter", ""))
            if k:
                return k
        except Exception:
            for ln in text.splitlines():
                s = ln.strip()
                if s.startswith("OPENROUTER_API_KEY="):
                    k = _clean_key(s.split("=", 1)[1])
                    if k:
                        return k
    k = _clean_key(os.environ.get("OPENROUTER_API_KEY", ""))
    if k:
        return k
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for ln in env.read_text(errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("export "):
                s = s[len("export ") :].strip()
            if s.startswith("OPENROUTER_API_KEY="):
                k = _clean_key(s.split("=", 1)[1])
                if k:
                    return k
    return ""


def _opencodego_key():
    """OpenCode Go key from the same keychain the relay uses.

    The relay expects a line: 'opencode go <key>' in:
    ~/Desktop/Projects/keychain/master_ai_keys
    Fall back to ~/.master_ai_keys and ~/.hermes/.env for OPENCODE_API_KEY.
    """
    primary = Path.home() / "Desktop" / "Projects" / "keychain" / "master_ai_keys"
    if primary.exists():
        for ln in primary.read_text(errors="replace").splitlines():
            parts = ln.strip().split()
            if len(parts) >= 3 and parts[0] == "opencode" and parts[1] == "go":
                k = _clean_key(parts[2])
                if k:
                    return k
    keychain = Path.home() / ".master_ai_keys"
    if keychain.exists():
        text = keychain.read_text(errors="replace")
        try:
            k = _clean_key(json.loads(text).get("opencode_go", ""))
            if k:
                return k
        except Exception:
            for ln in text.splitlines():
                s = ln.strip()
                if not s.startswith("#") and s.startswith("OPENCODE_API_KEY="):
                    k = _clean_key(s.split("=", 1)[1])
                    if k:
                        return k
    k = _clean_key(os.environ.get("OPENCODE_API_KEY", ""))
    if k:
        return k
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for ln in env.read_text(errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("export "):
                s = s[len("export ") :].strip()
            if not s.startswith("#") and s.startswith("OPENCODE_API_KEY="):
                k = _clean_key(s.split("=", 1)[1])
                if k:
                    return k
    return ""


def _ask_cloud(messages, timeout=300):
    """Plan call via OpenCode Go (paid/cheap lane) with OpenRouter free fallback.

    Tries OpenCode Go first using the master keychain OPENCODE_API_KEY;
    falls back to OpenRouter free tier if Go is unavailable or fails.
    """
    key = _opencodego_key()
    url = OPENCODEGO_URL
    model = OPENCODEGO_MODEL
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "x-opencode-session": "master-ai-upstream-integrator",
    }
    if not key:
        print(
            "  cloud: no OPENCODE_API_KEY; falling back to OpenRouter free", flush=True
        )
        url = OPENROUTER_URL
        model = PLAN_MODEL
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_openrouter_key()}",
            "HTTP-Referer": "http://localhost",
            "X-Title": "master-ai-upstream-integrator",
            "User-Agent": "master-ai-upstream-integrator/1.0",
        }
    max_tokens = 4096
    last_err = "unknown"
    for attempt in range(1, 4):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
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
    # Fallback to OpenRouter free if Go failed
    if url != OPENROUTER_URL and _openrouter_key():
        print("  cloud: OpenCode Go failed, trying OpenRouter free", flush=True)
        return _ask_cloud_openrouter(messages, timeout)
    print(f"  cloud: giving up after 3 attempts ({last_err})", flush=True)
    return None


def _ask_cloud_openrouter(messages, timeout=300):
    """OpenRouter free-tier fallback plan call with retry + diagnostics."""
    if not PLAN_MODEL.endswith(":free"):
        print(f"  cloud: refusing non-free model '{PLAN_MODEL}'", flush=True)
        return None
    key = _openrouter_key()
    if not key:
        print("  cloud: no OPENROUTER_API_KEY", flush=True)
        return None
    max_tokens = 4096
    last_err = "unknown"
    for attempt in range(1, 4):
        payload = {
            "model": PLAN_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "http://localhost",
                "X-Title": "master-ai-upstream-integrator",
                "User-Agent": "master-ai-upstream-integrator/1.0",
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


def integrate_one(item):
    owner, repo, sha = item["owner"], item["repo"], item["sha"]
    print(f"\n=== {owner}/{repo}@{sha[:8]} (score {item.get('score')})", flush=True)
    # Proposal-only: this never touches git or the working tree, so there is
    # no "dirty tree" case to defer on anymore — it's safe to run alongside
    # a live editing session.
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

    # Proposal-only: sanity-check which paths WOULD be touched, but never
    # write, branch, commit, or merge anything. The plan doc written above
    # (PLANS / f"{sha[:8]}.md") is the actual deliverable — this just records
    # a structured summary of it in the log.
    staged, unsafe = [], []
    for path, _body in blocks:
        (staged if _path_allowed(path) else unsafe).append(path)

    detail = f"proposal: {PLANS / f'{sha[:8]}.md'}; would touch {staged}"
    if unsafe:
        detail += f"; UNSAFE paths excluded from proposal: {unsafe}"
    _log_line(
        {
            "ts": datetime.now().isoformat(),
            "sha": sha,
            "status": "proposal",
            "detail": detail,
        }
    )
    print(
        f"  proposal only — would touch {staged} (full plan: {PLANS / f'{sha[:8]}.md'})",
        flush=True,
    )
    return "", "proposal"


def main():
    batch = BATCH
    if "--batch" in sys.argv:
        batch = int(sys.argv[sys.argv.index("--batch") + 1])
    items = _load_queue()
    done_shas = set()
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            try:
                d = json.loads(ln)
                if d.get("status") in ("proposal", "skipped"):
                    done_shas.add(d.get("sha"))
            except Exception:
                pass
    todo = [it for it in items if it["sha"] not in done_shas]
    print(f"queue={len(items)} pending={len(todo)} batch={batch}", flush=True)
    # Proposal-only: no git state to capture or guard, nothing branches.
    for it in todo[:batch]:
        try:
            integrate_one(it)
        except Exception as e:
            _log_line(
                {
                    "ts": datetime.now().isoformat(),
                    "sha": it.get("sha"),
                    "status": "failed",
                    "detail": f"integrate_one exception: {e}",
                }
            )
    # drop TERMINAL items from the queue; retryable ones stay:
    #   proposal/skipped/manual-review/blocked -> done (proposal exists or N/A)
    #   failed -> stays until 3 failed attempts, then retired
    processed = set()
    attempts = {}
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            try:
                d = json.loads(ln)
            except Exception:
                continue
            st = d.get("status")
            if st in ("proposal", "skipped", "manual-review", "blocked"):
                processed.add(d.get("sha"))
            elif st == "failed":
                attempts[d.get("sha")] = attempts.get(d.get("sha"), 0) + 1
        for sha, n in attempts.items():
            if n >= 3:
                print(f"  retiring {sha[:8]} after {n} failed attempts", flush=True)
                processed.add(sha)
    remaining = [it for it in items if it.get("sha") not in processed]
    _save_queue(remaining)
    print(f"\nDone. remaining_queue={len(remaining)}", flush=True)


if __name__ == "__main__":
    main()
