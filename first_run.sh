#!/bin/bash
# first_run.sh — Sensei's first-run experience.
# Runs after install.sh. Goal: show immediate value safely.
#
# 1. Runs the cleanup-first-run skill (disk cleanup, no destructive deletes).
# 2. Offers to set up Google Workspace so Sensei can organize Gmail/Drive/Calendar.
# 3. Leaves the user at the Sensei prompt.

set -u

TARGET="$HOME/scripts"
SENSEI="bash $TARGET/launch_master_ai.sh"

: "${BC:=$(tput bold 2>/dev/null; tput setaf 4 2>/dev/null)}"
: "${BG:=$(tput bold 2>/dev/null; tput setaf 2 2>/dev/null)}"
: "${BY:=$(tput bold 2>/dev/null; tput setaf 3 2>/dev/null)}"
: "${BR:=$(tput bold 2>/dev/null; tput setaf 1 2>/dev/null)}"
: "${BW:=$(tput bold 2>/dev/null; tput setaf 0 2>/dev/null)}"
: "${D:=$(tput setaf 8 2>/dev/null)}"
: "${C:=$(tput setaf 6 2>/dev/null)}"
: "${X:=$(tput sgr0 2>/dev/null)}"

echo ""
echo -e "  ${BC}╔══════════════════════════════════════════════════════╗${X}"
echo -e "  ${BC}║${X}  ${BG}🥷  SENSEI FIRST RUN${X}                               ${BC}║${X}"
echo -e "  ${BC}╚══════════════════════════════════════════════════════╝${X}"
echo ""
echo -e "  ${BW}Sensei will do two things now:${X}"
echo -e "    ${BG}1.${X} Run a safe cleanup of unnecessary files"
echo -e "    ${BG}2.${X} Optionally connect Google Workspace for organization"
echo ""

# ── 1. Install cleanup-first-run skill if not present ──────────
SKILL_DIR="$HOME/.master_ai_skills/cleanup-first-run"
if [ ! -d "$SKILL_DIR" ]; then
    echo -e "  ${C}  Installing cleanup-first-run skill...${X}"
    mkdir -p "$SKILL_DIR"
    cat > "$SKILL_DIR/SKILL.md" <<'EOF'
---
name: cleanup-first-run
description: "Safely clean up unnecessary files on first install."
version: 1.0.0
author: sensei
---

# Cleanup First Run

Scans common clutter directories and reports what can be removed.
No files are deleted without explicit confirmation.
EOF
    cat > "$SKILL_DIR/recipe.py" <<'EOF'
#!/usr/bin/env python3
"""cleanup-first-run skill — safe disk cleanup for new Sensei users."""

import json
import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from skill_runtime import Step, END, ABORT, INTERRUPT, SkillState
from sandbox import run_sandboxed

# Directories we scan. Downloads/Trash/Temp are common clutter.
SCAN_DIRS = [
    Path.home() / "Downloads",
    Path.home() / ".local" / "share" / "Trash" / "files",
    Path("/tmp"),
]

# File patterns considered safe to flag for cleanup
CLUTTER_PATTERNS = [
    "*.log",
    "*.tmp",
    "*.cache",
    "*.part",
    "*.crdownload",
]


def _scan(state, params):
    """Scan for old/large clutter files."""
    days = int(params.get("days", 30))
    cutoff = datetime.now() - timedelta(days=days)
    candidates = []
    total_bytes = 0
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for pattern in CLUTTER_PATTERNS:
            for f in d.rglob(pattern):
                try:
                    if not f.is_file():
                        continue
                    stat = f.stat()
                    mtime = datetime.fromtimestamp(stat.st_mtime)
                    if mtime < cutoff:
                        size = stat.st_size
                        candidates.append({
                            "path": str(f),
                            "size": size,
                            "mtime": mtime.isoformat(),
                        })
                        total_bytes += size
                except (OSError, PermissionError):
                    continue
    candidates.sort(key=lambda x: x["size"], reverse=True)
    state.data["candidates"] = candidates[:100]
    state.data["total_bytes"] = total_bytes
    return {"next": "report"}


def _report(state, params):
    """Show what was found and ask for confirmation."""
    candidates = state.data.get("candidates", [])
    total = state.data.get("total_bytes", 0)
    if not candidates:
        state.data["_reason"] = "No old clutter files found — your system is already tidy."
        return {"next": END}
    mb = total / (1024 * 1024)
    lines = [f"Found {len(candidates)} files using ~{mb:.1f} MB:"]
    for c in candidates[:20]:
        lines.append(f"  {c['path']} ({c['size'] / 1024 / 1024:.1f} MB)")
    if len(candidates) > 20:
        lines.append(f"  ... and {len(candidates) - 20} more")
    state.data["_reason"] = "\n".join(lines)
    # INTERRUPT lets the operator confirm before we delete anything.
    return {"next": INTERRUPT, "state_update": {"_pending_step": "delete", "_interrupt_reason": "confirm cleanup"}}


def _delete(state, params):
    """Delete only the confirmed files."""
    candidates = state.data.get("candidates", [])
    deleted = 0
    freed = 0
    errors = []
    for c in candidates:
        p = Path(c["path"])
        try:
            p.unlink()
            deleted += 1
            freed += c["size"]
        except Exception as e:
            errors.append(f"{p}: {e}")
    state.data["deleted"] = deleted
    state.data["freed_bytes"] = freed
    state.data["errors"] = errors
    state.data["_reason"] = f"Deleted {deleted} files, freed {freed / 1024 / 1024:.1f} MB."
    return {"next": END}


STEPS = [
    Step(name="scan", fn=_scan),
    Step(name="report", fn=_report),
    Step(name="delete", fn=_delete),
]

ENTRYPOINT = "scan"


def CHECK_PRECONDITIONS():
    pass
EOF
    chmod +x "$SKILL_DIR/recipe.py"
    echo -e "  ${BG}✓ cleanup-first-run skill installed${X}"
fi

# ── 2. Run the cleanup skill via Sensei ──────────────────────
echo ""
echo -e "  ${BC}━━━ Running safe cleanup scan ━━━${X}"
echo ""

# Use a one-shot Sensei command to run the skill
python3 - "$TARGET" <<'PY'
import json, os, sys, importlib.util
from pathlib import Path
target = sys.argv[1]
sys.path.insert(0, target)
sys.path.insert(0, str(Path(target).parent))
import skill_runtime as sr
try:
    state = sr.run_skill("cleanup-first-run", {"days": 30})
    data = state.data
    candidates = data.get("candidates", [])
    total = data.get("total_bytes", 0)
    if not candidates:
        print("  ✅ No old clutter files found — your system is already tidy.")
    else:
        print(f"  Found {len(candidates)} files using ~{total / 1024 / 1024:.1f} MB")
        for c in candidates[:10]:
            print(f"    {c['path']} ({c['size'] / 1024 / 1024:.1f} MB)")
        if len(candidates) > 10:
            print(f"    ... and {len(candidates) - 10} more")
    # Persist interrupt so the user can resume to delete if they want
    sr.save_state(state)
    print(f"\n  Session ID: {state.session_id}")
    print(f"  To confirm deletion, run: sensei cleanup-first-run {state.session_id}")
except Exception as e:
    print(f"  ⚠ Cleanup scan failed: {e}")
PY

# ── 3. Offer Google Workspace setup ──────────────────────────
echo ""
echo -e "  ${BC}━━━ Google Workspace (optional) ━━━${X}"
echo -e "  ${BW}Connect Gmail, Drive, Calendar so Sensei can organize your cloud stuff.${X}"
echo ""
read -rp "  Set up Google Workspace now? (y/N) " gw
if [[ "$gw" =~ ^[yY]$ ]]; then
    echo -e "  ${C}  Starting Google Workspace setup...${X}"
    # The google-workspace skill has scripts/setup.py for OAuth
    GSETUP="python3 $HOME/.hermes/skills/productivity/google-workspace/scripts/setup.py"
    if [ -f "$HOME/.hermes/skills/productivity/google-workspace/scripts/setup.py" ]; then
        bash -c "$GSETUP --check"
        echo -e "  ${BY}Follow the setup steps above, then ask Sensei to use Google Workspace.${X}"
    else
        echo -e "  ${BY}⚠ Google Workspace skill not found in ~/.hermes/skills/.${X}"
        echo -e "  ${D}  Install it later with: skill install hermes productivity/google-workspace${X}"
    fi
else
    echo -e "  ${D}  Skipped. Add Google Workspace anytime by asking Sensei.${X}"
fi

# ── 4. Drop into Sensei prompt or print next command ───────────
echo ""
echo -e "  ${BG}✓ First run complete.${X}"

# If we're in a real interactive terminal, launch the Sensei TUI.
# In CI/headless, just print the command.
if [ -t 0 ] && [ -t 1 ] && [ -z "${CI:-}" ] && [ "${MASTER_AI_NONINTERACTIVE:-0}" != "1" ]; then
    echo -e "  ${BW}Starting Sensei...${X}"
    echo ""
    exec $SENSEI
else
    echo -e "  ${BW}To start Sensei, run:${X} ${BC}sensei${X}"
    echo -e "  ${D}(headless/CI mode detected — not launching TUI)${X}"
fi
