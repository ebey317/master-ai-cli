"""Review gate for perpetual-watcher proposals -- ~/.master_ai_proposals/*.md.

perpetual-watcher's SKILL.md has always specified `review_gate: sensei`, but
nothing ever implemented that gate: proposals just accumulated as plain
markdown files with an unchecked Decision checklist, with no way to see or
act on them from inside a live Sensei session. This mirrors approval_queue.py's
pending/approve/reject shape (same verbs, same feel) for a different kind of
pending decision -- not a queued action waiting for a live terminal, but a
generated integration proposal waiting for a human read-and-decide.

The proposal file itself is the only state -- no separate database. A
proposal is "pending" as long as none of its Decision checkboxes are checked;
approving/rejecting checks the corresponding box in place. This keeps
`~/.master_ai_proposals/*.md` as the single source of truth Elijah can also
just open and edit by hand if he'd rather do that than use these commands.

2026-09-13: three real issues found by CodeRabbit review on the first version
of this file, all fixed here:
  1. Decision matching searched the whole file, not just the "## Decision"
     section -- a checkbox-shaped line anywhere else in the proposal could
     make a pending proposal look decided, or get rewritten by approve/reject.
  2. Path traversal: proposal_id came straight from user REPL input into
     PROPOSALS_DIR / f"{proposal_id}.md" with no containment check. Since
     Path("/a/b") / "/absolute/x" silently discards the left side and
     evaluates to just "/absolute/x", a proposal_id like "/tmp/evil" (or a
     "../../x" traversal) could read/rewrite an arbitrary file outside
     PROPOSALS_DIR entirely.
  3. TOCTOU race: two sessions could both read an undecided proposal before
     either wrote it, and the second write would silently clobber the
     first decision with no conflict signal.
"""

import fcntl
import os
import re
import tempfile
from pathlib import Path

PROPOSALS_DIR = Path.home() / ".master_ai_proposals"

_DECISION_HEADING_RE = re.compile(r"^## Decision\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##[^#]", re.MULTILINE)

# Matches "- [ ] Approve" / "- [x] Reject" / "- [X] Defer" lines -- but only
# ever run against the isolated "## Decision" section (see _decision_span),
# never the full file, so an unrelated checkbox elsewhere (e.g. the
# "## Integration Plan" section's "Create skill:", "Add tests:", ...) can
# never be mistaken for a decision or get rewritten by approve/reject.
_DECISION_RE = re.compile(r"^(- \[)([ xX])(\] (Approve|Reject|Defer))\s*$", re.MULTILINE)
_MODIFY_RE = re.compile(r"^- \[([ xX])\] Modify:\s*(.*)$", re.MULTILINE)

_HEADER_FIELDS = ("Source", "Commits", "Category", "Priority")


def _decision_span(text):
    """Return (start, end) offsets of the "## Decision" section's body
    (heading line excluded, next "##" heading or EOF as the boundary), or
    None if the proposal has no such section at all."""
    m = _DECISION_HEADING_RE.search(text)
    if not m:
        return None
    start = m.end()
    nxt = _NEXT_HEADING_RE.search(text, start)
    end = nxt.start() if nxt else len(text)
    return start, end


def _parse_header(text):
    fields = {}
    for key in _HEADER_FIELDS:
        m = re.search(rf"\*\*{key}\*\*:\s*(.+)", text)
        if m:
            fields[key.lower()] = m.group(1).strip()
    return fields


def _decision_state(text):
    """Return the checked decision label ('approve'/'reject'/'defer'/
    'modify'), or None if the proposal hasn't been decided yet. Only looks
    inside the "## Decision" section -- see module docstring, issue 1."""
    span = _decision_span(text)
    if span is None:
        return None
    section = text[span[0] : span[1]]
    for m in _DECISION_RE.finditer(section):
        if m.group(2).strip().lower() == "x":
            return m.group(4).lower()
    m = _MODIFY_RE.search(section)
    if m and m.group(1).strip().lower() == "x":
        return "modify"
    return None


def list_pending():
    if not PROPOSALS_DIR.is_dir():
        return []
    out = []
    for path in sorted(PROPOSALS_DIR.glob("*.md")):
        text = path.read_text()
        if _decision_state(text) is not None:
            continue
        fields = _parse_header(text)
        out.append(
            {
                "id": path.stem,
                "path": str(path),
                "source": fields.get("source", "?"),
                "priority": fields.get("priority", "?"),
                "category": fields.get("category", "?"),
            }
        )
    return out


def _safe_child(path):
    """Resolve `path` and confirm it's a real (non-symlink) file that lives
    inside PROPOSALS_DIR -- the actual containment check, not just string
    matching. is_file() alone doesn't enforce this: a symlink pointing
    outside PROPOSALS_DIR, or a proposal_id that produced an absolute path
    escaping it entirely, both pass is_file() but must not be treated as a
    real proposal."""
    try:
        resolved = path.resolve()
        base = PROPOSALS_DIR.resolve()
    except OSError:
        return None
    if base not in resolved.parents:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def _find(proposal_id):
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return None
    # f"{proposal_id}.md" can itself be absolute (e.g. proposal_id =
    # "/tmp/evil"), and Path(base) / "/absolute/x" silently discards `base`
    # per pathlib's own join semantics -- so the containment check in
    # _safe_child (not just this join) is what actually prevents escape.
    exact = _safe_child(PROPOSALS_DIR / f"{proposal_id}.md")
    if exact:
        return exact
    # Allow a short/partial id (e.g. just "hermes_webui") when it uniquely
    # identifies one file -- typing the full date-prefixed stem every time
    # is real friction for something meant to be reviewed quickly. Globbed
    # from PROPOSALS_DIR itself, but still containment-checked in case a
    # symlink was ever placed inside the directory.
    candidates = [
        c
        for c in (
            _safe_child(p) for p in PROPOSALS_DIR.glob("*.md") if proposal_id in p.stem
        )
        if c
    ]
    return candidates[0] if len(candidates) == 1 else None


def get(proposal_id):
    path = _find(proposal_id)
    if not path:
        return None
    text = path.read_text()
    fields = _parse_header(text)
    return {
        "id": path.stem,
        "path": str(path),
        "text": text,
        "decision": _decision_state(text),
        **fields,
    }


def _atomic_write(path, text):
    """Write `text` to `path` via a same-directory temp file + os.replace,
    so a process interruption mid-write can never leave a truncated
    proposal -- the rename is the only atomic step, and it only happens
    once the full content is safely on disk."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _set_decision(proposal_id, decision):
    path = _find(proposal_id)
    if not path:
        return False, f"no proposal matching '{proposal_id}'"

    # Inter-process lock for the whole read-check-write sequence -- without
    # this, two Sensei sessions can both read the same undecided proposal
    # before either writes, and the second write silently clobbers the
    # first decision with no conflict signal. Locking the proposal file
    # itself (not a separate lock file) means the lock's lifetime is tied
    # to this one call, released automatically when the `with` block exits
    # or the process dies, never left stale.
    with open(path, "r+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        text = lock_f.read()
        existing = _decision_state(text)
        if existing is not None:
            return False, f"{path.stem} already decided ({existing})"

        span = _decision_span(text)
        if span is None:
            return False, f"{path.stem}: no Decision section found (malformed proposal)"
        start, end = span
        section = text[start:end]

        def _sub(m):
            label = m.group(4).lower()
            mark = "x" if label == decision else " "
            return f"{m.group(1)}{mark}{m.group(3)}"

        new_section, n = _DECISION_RE.subn(_sub, section)
        if n == 0:
            return False, f"{path.stem}: no Decision checklist found (malformed proposal)"
        new_text = text[:start] + new_section + text[end:]
        _atomic_write(path, new_text)
        return True, f"{path.stem} marked {decision}"


def approve(proposal_id):
    return _set_decision(proposal_id, "approve")


def reject(proposal_id):
    return _set_decision(proposal_id, "reject")
