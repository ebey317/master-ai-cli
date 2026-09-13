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
"""

import re
from pathlib import Path

PROPOSALS_DIR = Path.home() / ".master_ai_proposals"

# Matches "- [ ] Approve" / "- [x] Reject" / "- [X] Defer" lines inside a
# proposal's own "## Decision" checklist. Deliberately scoped to just these
# three labels -- the same file also has an "## Integration Plan" checklist
# with its own unrelated checkboxes ("Create skill:", "Add tests:", ...)
# that must never be touched by approve/reject.
_DECISION_RE = re.compile(r"^(- \[)([ xX])(\] (Approve|Reject|Defer))\s*$", re.MULTILINE)
_MODIFY_RE = re.compile(r"^- \[([ xX])\] Modify:\s*(.*)$", re.MULTILINE)

_HEADER_FIELDS = ("Source", "Commits", "Category", "Priority")


def _parse_header(text):
    fields = {}
    for key in _HEADER_FIELDS:
        m = re.search(rf"\*\*{key}\*\*:\s*(.+)", text)
        if m:
            fields[key.lower()] = m.group(1).strip()
    return fields


def _decision_state(text):
    """Return the checked decision label ('approve'/'reject'/'defer'/
    'modify'), or None if the proposal hasn't been decided yet."""
    for m in _DECISION_RE.finditer(text):
        if m.group(2).strip().lower() == "x":
            return m.group(4).lower()
    m = _MODIFY_RE.search(text)
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


def _find(proposal_id):
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return None
    exact = PROPOSALS_DIR / f"{proposal_id}.md"
    if exact.is_file():
        return exact
    # Allow a short/partial id (e.g. just "hermes_webui") when it uniquely
    # identifies one file -- typing the full date-prefixed stem every time
    # is real friction for something meant to be reviewed quickly.
    matches = [p for p in PROPOSALS_DIR.glob("*.md") if proposal_id in p.stem]
    return matches[0] if len(matches) == 1 else None


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


def _set_decision(proposal_id, decision):
    path = _find(proposal_id)
    if not path:
        return False, f"no proposal matching '{proposal_id}'"
    text = path.read_text()
    existing = _decision_state(text)
    if existing is not None:
        return False, f"{path.stem} already decided ({existing})"

    def _sub(m):
        label = m.group(4).lower()
        mark = "x" if label == decision else " "
        return f"{m.group(1)}{mark}{m.group(3)}"

    new_text, n = _DECISION_RE.subn(_sub, text)
    if n == 0:
        return False, f"{path.stem}: no Decision checklist found (malformed proposal)"
    path.write_text(new_text)
    return True, f"{path.stem} marked {decision}"


def approve(proposal_id):
    return _set_decision(proposal_id, "approve")


def reject(proposal_id):
    return _set_decision(proposal_id, "reject")
