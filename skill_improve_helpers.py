"""REPL helpers for the skill marketplace + learning-loop commands.

2026-09-01 — Hermes's half of the split (task file:
hermes_task_skill_marketplace_learning_loop.md). All presentation logic
for the `skill browse/install/audit/improve` command family lives here;
master_ai.py keeps only a thin dispatch block, mirroring how `mcp` sits
on sensei_mcp_client.py. Claude's modules (skill_marketplace.py,
learning_loop.py) own state and analysis; nothing here duplicates that.

The improve-loop safety rule is structural: `_build_fix` only ever
returns a (filepath, find_text, replace_text, rationale) tuple for the
caller to push through master_ai.confirm_edit(). No write path exists in
this module — if a future patch adds one, the typed-EDIT gate is being
bypassed and the 3.3 sandbox-bypass lesson is being repeated.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import skill_marketplace as sm
import learning_loop as ll


def browse(source: str = None) -> str:
    """`skill browse [source]` — list skills available in a source,
    flagging which are already adapted under ~/.master_ai_skills/."""
    try:
        rows = sm.browse_source(source or None)
    except (KeyError, NotADirectoryError, ValueError) as e:
        return f"browse failed: {e}"
    if not rows:
        return "no skills found in this source"
    lines = [f"skills in source ({len(rows)}):"]
    for r in rows:
        flag = "[adapted]" if r["adapted"] else "[staging candidate]"
        lines.append(f"  {r['name']:<28} {flag:<18} {r['description'][:76]}")
    return "\n".join(lines)


def audit(name: str) -> str:
    """`skill audit <name>` — re-scan an already-adapted skill's files for
    sandbox-bypass patterns; read-only, no state change."""
    if not name:
        return "usage: skill audit <name>"
    res = sm.audit_adapted_skill(name)
    lines = [f"audit {name}: {'PASS' if res.passed else 'FAIL'} "
             f"({res.scanned_files} file(s) scanned)"]
    for r in res.reasons:
        lines.append(f"  ✗ {r}")
    for w in res.warnings[:8]:
        lines.append(f"  ⚠ {w}")
    if res.passed and not res.warnings:
        lines.append("  no sandbox-bypass patterns found")
    return "\n".join(lines)


def install(source: str, skill_id: str) -> str:
    """`skill install <source> <id>` — audit first; refuse broken skills
    outright (same UX as mcp enable refusing a broken server). On pass,
    stage raw files + say plainly that STEPS adaptation is still needed."""
    if not source or not skill_id:
        return ("usage: skill install <source> <skill-id>\n"
                "  (skill ids come from `skill browse` — e.g. research/web-search-ddgr)")
    result = sm.install_skill(source, skill_id)
    a = result["audit"]
    if not result["staged"]:
        lines = ["REFUSED — audit failed, nothing copied:"]
        for r in a["reasons"]:
            lines.append(f"  ✗ {r}")
        return "\n".join(lines)
    lines = [f"staged {skill_id} → {result['path']}",
             f"  audit: PASS ({a['scanned_files']} file(s) scanned)"]
    for w in a["warnings"][:5]:
        lines.append(f"  ⚠ {w}")
    lines.append(result["note"])
    return "\n".join(lines)


# ─── skill improve — the learning loop entry point ──────────────────

def _session_abort_messages(name: str) -> list:
    """Pull the final abort `message` from each aborted session's
    state.data (the runtime stores the gate message there)."""
    from skill_runtime import SKILLS_ROOT
    d = SKILLS_ROOT / name / "sessions"
    msgs = []
    if not d.is_dir():
        return msgs
    for f in sorted(d.glob("*.json")):
        try:
            s = json.loads(f.read_text())
        except Exception:
            continue
        if s.get("aborted"):
            msg = (s.get("data") or {}).get("message") or ""
            if msg:
                msgs.append(msg)
    return msgs


def _build_fix(report, name: str):
    """Narrow, rule-based v1 fix builder.

    The one mechanical fix we draft: a skill whose sessions abort
    repeatedly (>=2) at a Phase-gate step gets its gate's ABORT message
    sharpened to point at the SKILL.md 'Parameters' section explicitly.
    Evidence-driven: picks the most frequent abort message from the real
    session records, not a canned guess.

    Returns (filepath, find_text, replace_text, rationale) or None
    (None = honest 'no mechanical fix identifiable' outcome).
    """
    # 1) Is there a dominant abort pattern at all?
    if report.aborted_count < 2:
        return None
    if not report.top_error_messages:
        return None
    top_msg, top_n = report.top_error_messages[0]
    if top_n < 2:
        return None

    # 2) Does the top abort message look like a Phase-gate message from
    #    one of OUR recipes? (gate messages carry the pattern
    #    '<PHASE n> GATE: missing required params [...]')
    if "GATE: missing required params" not in top_msg:
        return None

    # 3) Which recipe.py string produced it? Find the exact source line.
    recipe = Path.home() / ".master_ai_skills" / name / "recipe.py"
    if not recipe.exists():
        return None
    text = recipe.read_text(errors="replace")
    # the gate builds its message with an f-string fragment; find the
    # line containing 'GATE:' and 'missing required params'
    needle = "GATE: missing required params"
    idx = text.find(needle)
    if idx < 0:
        return None

    # locate the full f-string fragment containing the needle
    lines = text[:idx].count("\n")
    all_lines = text.split("\n")
    target_line = all_lines[lines]

    old_fragment = target_line
    new_fragment = target_line.replace(
        "missing required params",
        "missing required params (see the SKILL.md Parameters section: "
        f"~/.master_ai_skills/{name}/SKILL.md)",
        1,
    )
    if old_fragment == new_fragment:
        return None  # already sharpened; nothing mechanical left

    rationale = (
        f"{top_n} of {report.aborted_count} aborts die at this gate with the "
        f"same message; callers keep omitting required params. Sharpening "
        f"the gate message to name the SKILL.md Parameters section makes "
        f"the failure self-documenting."
    )
    return (str(recipe), old_fragment, new_fragment, rationale)


def improve(name: str):
    """`skill improve <name>` — analyze, report, and (only if a mechanical
    fix is identifiable) return a fix proposal dict for the REPL to route
    through confirm_edit(). Never writes anything itself. Returns
    (report_text, fix_dict_or_None)."""
    if not name:
        return "usage: skill improve <name>", None
    try:
        report = ll.analyze_skill(name)
    except Exception as e:
        return f"analysis failed: {e}", None

    out = [report.format_text()]

    fix = None
    try:
        fix = _build_fix(report, name)
    except Exception as e:
        out.append(f"  (fix analysis error: {e})")

    if fix is None:
        out.append(
            "\nno mechanical fix identifiable from the run history — "
            "report above is the deliverable (that is a valid outcome, "
            "not a failure)."
        )
        return "\n".join(out), None

    filepath, find_text, replace_text, rationale = fix
    out.append("")
    out.append(f"MECHANICAL FIX IDENTIFIED — {rationale}")
    out.append("proposed diff (goes through the typed EDIT confirm gate — "
               "nothing is written without your approval):")
    for line in difflib.unified_diff(
            find_text.splitlines(), replace_text.splitlines(),
            fromfile=f"{filepath} (current)", tofile="proposed", lineterm=""):
        out.append(f"  {line}")
    return "\n".join(out), {
        "filepath": filepath,
        "find_text": find_text,
        "replace_text": replace_text,
        "skill": name,
    }