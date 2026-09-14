#!/usr/bin/env python3
"""Master AI — Approval Queue.

The safety layer between AI agents and the filesystem. Any agent that
wants to modify the system (write a file, run a shell command, edit a
config, pull a model) appends an entry here INSTEAD of executing. Elijah
reviews the queue and approves or rejects; approved items dispatch to
type-specific handlers.

This exists because one of the scary properties of AI is that it can
maneuver around authentication, knows when you're in and out of files,
potentially knows your credentials. The antidote is: nothing mutates the
system without passing through this queue.

## The auto-mode boundary

Auto mode = "run anything except things that need master approval."
Queue an action when it MUTATES protected state. Don't queue when it
only reads or returns inference.

Queue (needs master approval):
- File writes anywhere under ~/scripts/, ~/.master_ai_*, ~/.config/
- Shell commands that change system state (package install, systemctl,
  git push/reset, chmod, mkdir outside /tmp)
- Model pulls, systemd drop-ins, key writes
- Anything that would burn Elijah's quota or time silently

Don't queue (just run):
- Reading files, running greps, /api/tags lookups
- Web search, Firecrawl fetch, Ollama inference calls (no persistence)
- Displaying data, rendering summaries
- Writes under /tmp or ephemeral caches

Rule of thumb: if an agent vanished mid-action, could a later Elijah
tell what happened from git diff + filesystem? If yes, queue it.

## Data model

Canonical store: `~/scripts/.pending_actions.jsonl`  (one entry per line)
Human view:      `~/scripts/pending_actions.md`      (regenerated on change)
Audit trail:     `~/.master_ai_audit.log`            (append-only, tamper-visible)

Every entry has Elijah's who/what/where/why/when/how shape plus:
- id      (timestamp-based, unique)
- status  (PENDING | RAN | REJECTED | FAILED | APPROVED_NO_HANDLER)
- type    (memory_write | shell_command | file_patch | model_pull | ...)
- diff    (±-line preview the user reviews)
- payload (type-specific data the handler consumes)

## CLI

    python3 approval_queue.py pending            # list pending entries
    python3 approval_queue.py diff <id>          # show full ± preview
    python3 approval_queue.py approve <id|all>   # run handler, mark RAN
    python3 approval_queue.py reject <id>        # drop the entry
    python3 approval_queue.py render             # regenerate .md view

## Library (for consumer agents)

    from approval_queue import queue, register_handler

    @register_handler('memory_write')
    def _run_memory_write(entry):
        for path, content in entry['payload']['files']:
            Path(path).write_text(content)

    queue(
        entry_type='memory_write',
        who='sensei_extractor.py',
        what='save 2 new memories',
        where='~/scripts/memory/feedback_*.md',
        why='Elijah stated new rule at 19:42',
        how='[+] write 2 files, [~] append 2 lines to MEMORY.md',
        diff='(± preview string)',
        payload={'files': [(path, content), ...]},
    )
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

SCRIPTS_DIR = Path.home() / "scripts"
JSONL_FILE = SCRIPTS_DIR / ".pending_actions.jsonl"
MD_FILE = SCRIPTS_DIR / "pending_actions.md"
AUDIT_LOG = Path.home() / ".master_ai_audit.log"

_HANDLERS: Dict[str, Callable] = {}


def register_handler(entry_type: str):
    """Decorator: register a function as the executor for an entry type.

    Handlers are looked up at approve() time. They receive the full entry
    dict and should raise on failure. Return value is str-truncated into
    entry['result'] for the audit trail.
    """
    def deco(fn):
        _HANDLERS[entry_type] = fn
        return fn
    return deco


def _audit(event: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{ts} | APPROVAL_QUEUE | {event}\n")
    except Exception:
        pass


def _next_id(existing_ids):
    base = datetime.now().strftime("%Y%m%d%H%M%S")
    if base not in existing_ids:
        return base
    # second-collision fallback: add a -NN suffix
    for suffix in range(1, 100):
        candidate = f"{base}-{suffix:02d}"
        if candidate not in existing_ids:
            return candidate
    return base + "-xx"  # shouldn't happen in practice


def _read_all() -> List[dict]:
    if not JSONL_FILE.exists():
        return []
    entries = []
    with open(JSONL_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_all(entries: List[dict]):
    JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish: write to temp then rename
    tmp = JSONL_FILE.with_suffix(JSONL_FILE.suffix + ".tmp")
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    tmp.replace(JSONL_FILE)


def queue(entry_type: str, who: str, what: str, where: str, why: str,
          when: str = "next Elijah review", how: str = "",
          diff: str = "", payload: Optional[dict] = None,
          trigger: str = "") -> str:
    """Append a new PENDING entry. Returns the entry ID.

    `trigger` is the originating user ask — a short quote or summary of
    the conversation turn that caused this action. Populating it lets
    Elijah skim the review file as Q→A pairs instead of decontextualized
    actions. Pass the most recent user message (truncated if long).
    """
    existing = {e["id"] for e in _read_all()}
    entry_id = _next_id(existing)
    entry = {
        "id": entry_id,
        "ts": time.time(),
        "status": "PENDING",
        "type": entry_type,
        "who": who, "what": what, "where": where,
        "why": why, "when": when, "how": how,
        "diff": diff,
        "trigger": trigger,
        "payload": payload or {},
    }
    JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JSONL_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    _audit(f"QUEUE id={entry_id} type={entry_type} who={who} what={what!r}")
    render_md()
    return entry_id


def list_all(status: Optional[str] = None) -> List[dict]:
    entries = _read_all()
    if status:
        return [e for e in entries if e["status"] == status]
    return entries


def list_pending() -> List[dict]:
    return list_all(status="PENDING")


def get(entry_id: str) -> Optional[dict]:
    for e in _read_all():
        if e["id"] == entry_id:
            return e
    return None


def _update(entry_id: str, changes: dict):
    entries = _read_all()
    for e in entries:
        if e["id"] == entry_id:
            e.update(changes)
    _write_all(entries)
    render_md()


def approve(entry_id: str):
    """Run the handler for entry_id. Returns (ok, message)."""
    entry = get(entry_id)
    if not entry:
        return False, f"no entry {entry_id}"
    if entry["status"] != "PENDING":
        return False, f"entry {entry_id} is {entry['status']}, not PENDING"
    handler = _HANDLERS.get(entry["type"])
    if not handler:
        _update(entry_id, {"status": "APPROVED_NO_HANDLER"})
        _audit(f"APPROVE_NO_HANDLER id={entry_id} type={entry['type']}")
        return False, (f"no handler registered for type '{entry['type']}' — "
                       f"marked APPROVED_NO_HANDLER. Import the consumer module first.")
    try:
        result = handler(entry)
        _update(entry_id, {"status": "RAN",
                           "result": str(result)[:500] if result is not None else ""})
        _audit(f"APPROVE_RAN id={entry_id} type={entry['type']}")
        return True, f"ran: {result}"
    except Exception as e:
        _update(entry_id, {"status": "FAILED", "error": str(e)[:500]})
        _audit(f"APPROVE_FAILED id={entry_id} type={entry['type']} err={e}")
        return False, f"handler raised: {e}"


def reject(entry_id: str):
    entry = get(entry_id)
    if not entry:
        return False, f"no entry {entry_id}"
    if entry["status"] != "PENDING":
        return False, f"entry {entry_id} is {entry['status']}, not PENDING"
    _update(entry_id, {"status": "REJECTED"})
    _audit(f"REJECT id={entry_id}")
    return True, f"rejected {entry_id}"


def render_md():
    """Regenerate `pending_actions.md` from JSONL. Humans read this."""
    entries = _read_all()
    pending = [e for e in entries if e["status"] == "PENDING"]
    done = [e for e in entries if e["status"] != "PENDING"]

    def block(e, is_pending):
        ts = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        status_tag = "⏳ PENDING" if is_pending else {
            "RAN": "✅ RAN", "REJECTED": "🚫 REJECTED",
            "FAILED": "❌ FAILED", "APPROVED_NO_HANDLER": "⚠ NO HANDLER",
        }.get(e["status"], e["status"])
        diff_txt = e.get("diff") or ""
        diff_block = f"\n**Diff preview:**\n```diff\n{diff_txt.rstrip()}\n```\n" if diff_txt else ""
        result = e.get("result") or e.get("error") or ""
        result_block = f"\n**Result:** `{result[:200]}`\n" if result else ""
        trigger = (e.get("trigger") or "").strip()
        trigger_block = f"\n> **You asked:** {trigger}\n" if trigger else ""
        return (
            f"### [{e['id']}] {e['what']}\n"
            f"{trigger_block}\n"
            f"- **Status:** {status_tag}\n"
            f"- **Who:**   {e['who']}\n"
            f"- **What:**  {e['what']}\n"
            f"- **Where:** {e['where']}\n"
            f"- **Why:**   {e['why']}\n"
            f"- **When:**  {e['when']} · queued {ts}\n"
            f"- **How:**   {e['how']}\n"
            f"{diff_block}{result_block}\n"
            f"---\n"
        )

    lines = [
        "# Master AI — Pending Actions Queue",
        "",
        "Every AI agent that wants to change the system queues here first.",
        "Elijah reviews and approves before anything runs.",
        "",
        "**Sensei commands:**  `pending` · `diff <id>` · `approve <id|all>` · `reject <id>`",
        "**CLI:** `python3 ~/scripts/approval_queue.py <cmd> [id]`",
        "",
        "---",
        "",
    ]
    if pending:
        lines.append(f"## ⏳ Pending ({len(pending)})\n")
        for e in pending:
            lines.append(block(e, True))
    else:
        lines.append("## ⏳ Pending\n\n*(queue empty)*\n\n---\n")
    if done:
        lines.append(f"\n## History ({len(done)} — most recent first, capped at 20)\n")
        for e in sorted(done, key=lambda x: -x["ts"])[:20]:
            lines.append(block(e, False))
    MD_FILE.write_text("\n".join(lines))


# ── Handler discovery ─────────────────────────────────────────────────
# At CLI startup we import the known consumer modules so their
# @register_handler decorators fire. Hardcoded for now — add a line per
# new consumer. When the list gets long, switch to plugin discovery.
def _load_handlers():
    _sys_path_add = str(SCRIPTS_DIR)
    if _sys_path_add not in sys.path:
        sys.path.insert(0, _sys_path_add)
    # Each import is optional — missing consumers don't block the CLI.
    for mod_name in ("sensei_extractor", "master_ai"):
        try:
            __import__(mod_name)
        except Exception as e:
            print(f"  (warning: couldn't load handlers from {mod_name}: {e})",
                  file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────
def _cli():
    if len(sys.argv) < 2:
        print("usage: approval_queue.py {pending|diff|approve|reject|render|history} [id|all]")
        return 1
    cmd = sys.argv[1].lower()

    if cmd in ("pending", "list", "ls"):
        pending = list_pending()
        if not pending:
            print("(queue empty — nothing to review)")
            return 0
        print(f"{len(pending)} pending:")
        for e in pending:
            print(f"  [{e['id']}] {e['who']:30s} → {e['what']}")
        print()
        print(f"  diff <id>     — full preview")
        print(f"  approve <id>  — run it  ·  approve all — run every pending")
        print(f"  reject <id>   — drop it")
        return 0

    if cmd == "history":
        for e in sorted(list_all(), key=lambda x: -x["ts"])[:30]:
            ts = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
            print(f"  {ts} [{e['id']}] {e['status']:10s} {e['who']:25s} → {e['what']}")
        return 0

    if cmd == "diff":
        if len(sys.argv) < 3:
            print("usage: diff <id>"); return 1
        e = get(sys.argv[2])
        if not e:
            print(f"no entry {sys.argv[2]}"); return 1
        print(f"=== [{e['id']}] {e['what']} ===")
        print(f"Status: {e['status']}")
        print(f"Who:    {e['who']}")
        print(f"Why:    {e['why']}")
        print(f"Where:  {e['where']}")
        print(f"How:    {e['how']}")
        print()
        print(e.get("diff") or "(no diff attached)")
        return 0

    if cmd == "approve":
        if len(sys.argv) < 3:
            print("usage: approve <id|all>"); return 1
        _load_handlers()
        if sys.argv[2] == "all":
            pending = list_pending()
            if not pending:
                print("(nothing pending)"); return 0
            ok_count = 0
            for e in pending:
                ok, msg = approve(e["id"])
                marker = "✓" if ok else "✗"
                print(f"  {marker} [{e['id']}] {msg}")
                if ok: ok_count += 1
            print(f"\n{ok_count}/{len(pending)} approved")
            return 0
        ok, msg = approve(sys.argv[2])
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1

    if cmd == "reject":
        if len(sys.argv) < 3:
            print("usage: reject <id>"); return 1
        ok, msg = reject(sys.argv[2])
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1

    if cmd == "render":
        render_md()
        print(f"rendered {MD_FILE}")
        return 0

    print(f"unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    # Alias this running __main__ module into sys.modules["approval_queue"]
    # BEFORE _load_handlers() imports master_ai. Otherwise `import
    # approval_queue` inside master_ai.py creates a second, separate module
    # object, and every @register_handler decorator it fires registers into
    # that copy's _HANDLERS dict — invisible to the approve() below, which
    # reads the __main__ copy's _HANDLERS. This makes both names point at
    # the same module object so registrations land in one place.
    sys.modules.setdefault("approval_queue", sys.modules[__name__])
    sys.exit(_cli())
