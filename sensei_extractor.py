#!/usr/bin/env python3
"""Sensei's memory extractor — Claude-style structured auto-memory.

Given a conversation (list of {role, content}), ask a local model to pull
out items worth remembering across sessions and write them as typed
memory files in ~/scripts/memory/, alongside a MEMORY.md index line.

Design parity with Claude Code's memory layer:
  - Four memory types: feedback / project / user / reference
  - One file per memory, keyed by slug name (upsert-by-name)
  - MEMORY.md is a pure index, never a memory store
  - Frontmatter: name / description / type

Usage (library):
    from sensei_extractor import extract_memories, save_memories
    mems = extract_memories(history)   # list of dicts
    save_memories(mems)                # writes files + updates index

Usage (CLI, for manual one-shot or cron):
    # From a chat file (one JSON object per line, {"role","content"}):
    python3 sensei_extractor.py --chat ~/chats/2026-04-19.chat

    # Dry-run (show what WOULD be saved, don't write):
    python3 sensei_extractor.py --chat <path> --dry-run

    # From stdin (for piping from `sensei` command inside master_ai.py):
    cat conversation.json | python3 sensei_extractor.py --stdin
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

MEMORY_DIR = Path.home() / "scripts" / "memory"
INDEX_FILE = MEMORY_DIR / "MEMORY.md"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# qwen2.5:3b is fine for this task — it's extract-and-classify, not reasoning.
# Tunable via env if future-Elijah wants the 7b for better recall.
EXTRACTOR_MODEL = os.environ.get("SENSEI_EXTRACTOR_MODEL", "qwen2.5:3b")

_SYSTEM_PROMPT = """You are Sensei's memory extractor. Given a conversation, identify items worth remembering across future sessions.

Output STRICTLY this JSON — no prose, no markdown fences, no explanation:
{"memories": [
  {"type": "feedback|project|user|reference",
   "name": "short-slug-name",
   "description": "one-line description, specific enough to decide relevance later",
   "body": "the memory content as markdown"
  }
]}

TYPES:
- feedback: rules the user has given about how to approach work. Include a **Why:** line and **How to apply:** line.
- project: facts about ongoing projects, decisions, deadlines. Include **Why:** and **How to apply:**.
- user: facts about the user's role, preferences, responsibilities, knowledge.
- reference: pointers to external systems (Linear, Grafana, Slack channels).

SKIP (never save these):
- Ephemeral task state (in-progress work, current conversation details)
- Anything derivable from code (architecture, file paths, git history)
- Anything already documented in CLAUDE.md / PROJECTS.md / README files
- Debugging solutions — the fix is in the code, the commit has the context

RULES:
- One good memory beats five mediocre. If nothing qualifies, return {"memories": []}.
- name must be a kebab-case slug, 2-5 words.
- body must be real markdown, not JSON-escaped.
- Never include secrets, API keys, passwords.
"""


def _slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "memory"


def extract_memories(history, model=None, host=None, last_n=40):
    """Run the extractor against the last N turns. Returns a list of dicts.

    history: list of {"role": "user"|"assistant", "content": str}
    Returns: [{"type","name","description","body"}, ...] (possibly empty)
    Raises: URLError / JSONDecodeError on transport / model failure.
    """
    model = model or EXTRACTOR_MODEL
    host = host or OLLAMA_HOST
    recent = history[-last_n:] if len(history) > last_n else history
    convo_text = "\n\n".join(
        f"[{m.get('role', '?')}]\n{m.get('content', '')}" for m in recent
    )
    user_msg = (
        f"Conversation to extract memories from:\n\n---\n{convo_text}\n---\n\n"
        "Return the JSON now."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",
        "options": {"num_ctx": 8192, "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.loads(r.read().decode())
    content = (resp.get("message") or {}).get("content") or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return []
        parsed = json.loads(m.group(0))
    return parsed.get("memories", []) or []


def _render_preview(mem, memory_dir):
    """Build the who/what/where/why/how block + ± diff for one memory."""
    mtype = (mem.get("type") or "project").strip().lower()
    if mtype not in ("feedback", "project", "user", "reference"):
        mtype = "project"
    name = _slugify(mem.get("name") or mem.get("description") or "note")
    desc = (mem.get("description") or "").replace("\n", " ").strip()
    body = (mem.get("body") or "").strip()
    fname = f"{mtype}_{name}.md"
    fpath = memory_dir / fname
    is_new = not fpath.exists()
    new_content = (
        f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{body}\n"
    )
    old_content = fpath.read_text() if fpath.exists() else ""
    # Minimal ±-line diff — we don't need full unified format, just a
    # visual: new lines prefixed with +, removed (if any) with -. For a
    # brand-new file it's all +; for an upsert it shows the delta only.
    if is_new:
        diff = "\n".join("+ " + l for l in new_content.splitlines())
    else:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                fromfile=fname + " (current)",
                tofile=fname + " (proposed)",
                lineterm="",
            )
        )
    return {
        "name": name,
        "desc": desc,
        "type": mtype,
        "body": body,
        "fname": fname,
        "fpath": fpath,
        "is_new": is_new,
        "new_content": new_content,
        "diff": diff,
    }


def _confirm_write(preview):
    """confirm_run-style gate: show preview, ask, return (go, remember_choice).

    Mirrors `confirm_run()` in master_ai.py — TTY refusal, fail-closed,
    never auto-answer. Absent user is not consenting user.

    Returns:
      ('y', False)   — write this one
      ('a', True)    — write this one AND all remaining in batch
      ('n', False)   — skip this one
      ('q', True)    — skip all remaining (stop batch)
    """
    print()
    print("─" * 72)
    print("  ✎ Memory extractor wants to write a memory file")
    print("─" * 72)
    print("  Who:   sensei_extractor.py")
    print(f"  What:  {'NEW' if preview['is_new'] else 'UPDATE'} {preview['fname']}")
    print(f"  Where: ~/scripts/memory/{preview['fname']}")
    print(f"  Why:   {preview['desc']}")
    print(
        f"  How:   write {len(preview['new_content'])} chars + update MEMORY.md index"
    )
    print()
    print("  ─── Diff preview ──────────────────────────────────────────────")
    for line in preview["diff"].splitlines()[:50]:
        print(f"  {line}")
    if len(preview["diff"].splitlines()) > 50:
        print(f"  ... ({len(preview['diff'].splitlines()) - 50} more lines)")
    print("  ────────────────────────────────────────────────────────────────")
    print()
    if not sys.stdin.isatty():
        # Fail-closed: no TTY means no user to consent. Skip.
        print("  ⚠ no TTY — skipping (absent user ≠ consenting user)")
        return ("n", False)
    while True:
        try:
            ans = (
                input(
                    "  write this? [y]es / [a]ll-remaining / [n]o-skip / [q]uit-batch: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print("\n  (interrupted — skipping)")
            return ("n", False)
        if ans in ("y", "yes"):
            return ("y", False)
        if ans in ("a", "all"):
            return ("a", True)
        if ans in ("n", "no", "skip", ""):
            return ("n", False)
        if ans in ("q", "quit"):
            return ("q", True)


def save_memories(memories, memory_dir=None, confirm=True):
    """Write memories with optional per-file confirmation.

    confirm=True  — default. Show ± preview for each file, ask y/a/n/q.
                    Matches the safety model of confirm_run() in master_ai.py.
    confirm=False — write silently. Only use from code paths that have
                    already gated through their own approval layer.

    Upsert by slug name: re-extracting the same fact updates the file,
    doesn't duplicate. Returns list of (path, is_new) tuples for written
    files.
    """
    memory_dir = Path(memory_dir) if memory_dir else MEMORY_DIR
    memory_dir.mkdir(parents=True, exist_ok=True)
    index_file = memory_dir / "MEMORY.md"
    if not index_file.exists():
        index_file.write_text("# Sensei Memory Index\n\n")

    approve_all = False
    results = []
    for mem in memories:
        preview = _render_preview(mem, memory_dir)
        if not preview["body"]:
            continue

        if confirm and not approve_all:
            choice, remember = _confirm_write(preview)
            if choice == "q":
                print(f"  ⏹ stopped — {len(memories) - len(results)} memories skipped")
                break
            if choice == "n":
                continue
            if choice == "a":
                approve_all = True

        # Write the file + update the index.
        preview["fpath"].write_text(preview["new_content"])
        idx_text = index_file.read_text()
        pointer_line = f"- [{preview['name']}]({preview['fname']}) — {preview['desc']}"
        line_pattern = re.compile(
            r"^- \[[^\]]+\]\(" + re.escape(preview["fname"]) + r"\).*$", re.M
        )
        if line_pattern.search(idx_text):
            idx_text = line_pattern.sub(pointer_line, idx_text)
        else:
            idx_text = idx_text.rstrip() + f"\n{pointer_line}\n"
        index_file.write_text(idx_text)

        results.append((preview["fpath"], preview["is_new"]))
    return results


def _load_history_from_chat_file(path):
    """Chat files in ~/chats/ are JSON-lines: one {"role","content"} per line."""
    history = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "role" in obj and "content" in obj:
                    history.append(obj)
            except json.JSONDecodeError:
                continue
    return history


def main():
    ap = argparse.ArgumentParser(description="Sensei memory extractor")
    ap.add_argument("--chat", help="path to a chat file (JSON-lines)")
    ap.add_argument("--stdin", action="store_true", help="read history JSON from stdin")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="show extracted memories without writing (JSON dump)",
    )
    ap.add_argument(
        "--no-confirm",
        action="store_true",
        help="skip per-file confirm prompts (DANGEROUS — only "
        "from trusted code paths with their own gate)",
    )
    ap.add_argument("--model", default=None, help="ollama model (default qwen2.5:3b)")
    ap.add_argument(
        "--last-n", type=int, default=40, help="look at the last N turns (default 40)"
    )
    args = ap.parse_args()

    if args.chat:
        history = _load_history_from_chat_file(args.chat)
    elif args.stdin:
        history = json.load(sys.stdin)
    else:
        ap.error("provide --chat <path> or --stdin")

    if not history:
        print("(no history — nothing to extract)", file=sys.stderr)
        return 0

    print(
        f"✎ extracting from {len(history)} turns (last {args.last_n})…",
        file=sys.stderr,
        flush=True,
    )
    memories = extract_memories(history, model=args.model, last_n=args.last_n)
    if not memories:
        print("(nothing worth saving)", file=sys.stderr)
        return 0

    if args.dry_run:
        print(json.dumps({"memories": memories}, indent=2))
        return 0

    results = save_memories(memories, confirm=not args.no_confirm)
    for path, is_new in results:
        tag = "NEW" if is_new else "UPD"
        print(f"  [{tag}] {path.name}", file=sys.stderr)
    print(
        f"✓ saved {len(results)} memor{'y' if len(results) == 1 else 'ies'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
