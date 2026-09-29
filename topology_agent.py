#!/usr/bin/env python3
"""
topology_agent.py — Live system briefing for Claude Code session injection.
Reads actual system state every run. Never stale.

Usage:
  python3 ~/scripts/topology_agent.py          → markdown briefing to stdout
  python3 ~/scripts/topology_agent.py --json   → JSON to stdout
  python3 ~/scripts/topology_agent.py --short  → one-liner summary only
"""

import json
import pathlib
import subprocess
import sys
from datetime import datetime

HOME = pathlib.Path.home()
SCRIPTS = HOME / "scripts"
CONTEXT_DIR = HOME / "Desktop" / "AI_CONTEXT"
ODS_FILE = HOME / "Desktop" / "MasterAI_Office.ods"
MEMORY_FILE = HOME / ".master_ai_memory"
MEMORY_INDEX = HOME / ".claude/projects/-home-elijah/memory/MEMORY.md"


def run(cmd, timeout=5):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception:
        return ""


def check_services():
    """Check which key services are alive."""
    services = {}

    # Process-based checks
    procs = run(
        "pgrep -a -f 'ollama|master_ai|pupil|piper|jellyfin|soffice|mpv' 2>/dev/null"
    )
    services["ollama"] = "ollama" in procs
    services["master_ai"] = "master_ai.py" in procs
    services["jellyfin"] = "jellyfin" in procs
    services["mpv"] = "mpv" in procs
    services["tts"] = "piper" in procs

    # Port-based checks (faster than curl)
    ports = run(
        "ss -tuln 2>/dev/null | awk '{print $5}' | grep -oE ':[0-9]+$' | tr -d ':'"
    )
    open_ports = set(ports.splitlines())
    services["pupil_ui"] = "8080" in open_ports
    services["ollama_api"] = "11434" in open_ports
    services["tts_api"] = "5050" in open_ports
    services["sunkissed"] = "5173" in open_ports

    return services


def recent_changes(hours=24):
    """Files changed in ~/scripts in last N hours."""
    out = run(
        f"find {SCRIPTS} -maxdepth 1 -name '*.py' -o -name '*.sh' | xargs ls -t 2>/dev/null | head -5"
    )
    return [p.replace(str(HOME) + "/", "~/") for p in out.splitlines() if p][:5]


def read_memory():
    """Read ~/.master_ai_memory first 20 lines."""
    try:
        lines = MEMORY_FILE.read_text().splitlines()
        return [l for l in lines if l.strip() and not l.startswith("#")][:8]
    except Exception:
        return []


def read_portfolio():
    """Read business portfolio from ODS via LibreOffice CSV conversion."""
    try:
        run(
            f"libreoffice --headless --convert-to csv '{ODS_FILE}' --outdir /tmp 2>/dev/null",
            timeout=15,
        )
        csv = pathlib.Path("/tmp/MasterAI_Office.csv")
        if csv.exists():
            rows = []
            for line in csv.read_text().splitlines()[1:]:  # skip header
                parts = line.split(",")
                if len(parts) >= 3 and parts[1].strip():
                    rows.append(
                        {
                            "num": parts[0].strip(),
                            "name": parts[1].strip(),
                            "status": parts[2].strip(),
                        }
                    )
            return rows
    except Exception:
        pass
    return []


def latest_context():
    """Get newest context snapshot summary."""
    try:
        files = sorted(
            CONTEXT_DIR.glob("context_*.txt"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if files:
            lines = files[0].read_text().splitlines()
            # grab ACTIVE TASK and RECENTLY CHANGED
            task = next(
                (
                    l
                    for l in lines
                    if "ACTIVE TASK" in l
                    or (
                        lines.index(l) > 0
                        and "ACTIVE TASK" in lines[lines.index(l) - 1]
                    )
                ),
                "",
            )
            return {"file": files[0].name, "task": task.strip() or "none recorded"}
    except Exception:
        pass
    return {"file": "none", "task": "none"}


def active_task_from_context():
    """Extract active task line from context file."""
    try:
        files = sorted(
            CONTEXT_DIR.glob("context_*.txt"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if files:
            text = files[0].read_text()
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "[ACTIVE TASK]" in line and i + 1 < len(lines):
                    return lines[i + 1].strip()
    except Exception:
        pass
    return "none"


def build_briefing():
    svc = check_services()
    portfolio = read_portfolio()
    mem_lines = read_memory()
    changes = recent_changes()
    active_task = active_task_from_context()
    ctx = latest_context()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Service status string
    def dot(ok):
        return "✓" if ok else "✗"

    svc_str = (
        f"Ollama:{dot(svc['ollama_api'])} "
        f"Pupil:{dot(svc['pupil_ui'])} "
        f"TTS:{dot(svc['tts_api'])} "
        f"Jellyfin:{dot(svc['jellyfin'])} "
        f"MasterAI:{dot(svc['master_ai'])} "
        f"Sunkissed:{dot(svc['sunkissed'])} "
        f"MPV:{dot(svc['mpv'])}"
    )

    # Portfolio summary
    live = [b["name"] for b in portfolio if b["status"] in ("LIVE", "ACTIVE")]
    build = [b["name"] for b in portfolio if b["status"] == "IN BUILD"]
    new_biz = [b["name"] for b in portfolio if b["status"] == "NEW"]

    lines = [
        f"# TOPOLOGY BRIEFING — {now}",
        "**Machine:** Madam-Mary (Ubuntu) | Elijah | voice-only | no mouse/keyboard",
        f"**Services:** {svc_str}",
        f"**Active Task:** {active_task}",
        "",
        f"**Portfolio LIVE/ACTIVE:** {', '.join(live) or 'none'}",
        f"**IN BUILD:** {', '.join(build) or 'none'}",
        f"**NEW:** {', '.join(new_biz) or 'none'}",
        "",
        f"**Recent script changes:** {', '.join(changes) or 'none'}",
        "",
        "**Key paths:**",
        "  ~/scripts/ = project root | ~/projects/ = all businesses",
        "  ~/.iptv/channels.m3u = IPTV list | MPV or Hypnotix to play",
        "  localhost:8080/pupil.html = Pupil UI | 100.101.249.96:8080 = remote (Tailscale)",
        "  ~/.master_ai_keys = API keys | ~/.master_ai_memory = persistent facts",
        "  ~/Desktop/AI_CONTEXT/context_latest.zip = session snapshot (every 5min)",
        "",
        "**Models:** master-ai:latest (qwen2.5:7b+Sensei) | qwen2.5:3b (idle) | llava (vision)",
        "**Cloud:** Groq | DeepSeek-R1 | Gemini 2.0 Flash | OpenRouter (keys loaded)",
        "",
        "**Recovery:** refresh → kick → ~/scripts/master_ai_refresh.sh → pkill -KILL -f master_ai.py",
        "",
        "**⛔ BROWSER NON-NEGOTIABLES (enforced every session):**",
        "  #1 MCP TABS ONLY — No bare Chrome tabs. No google-chrome <url>. sensei tab_create ONLY.",
        "  #2 NO EXTENSION DEPENDENCY — Full parity via Playwright/CDP if extension breaks.",
        "  #3 SENSEI IS SOLE BROWSER PATH — mcp__claude-in-chrome__* tools DO NOT connect. Dead.",
        "  #4 CLICK METHOD — read_full → CSS selector → one click → screenshot. Batch/ref = silent fail.",
        "  #5 AUTHENTICATED ACTIONS VISIBLE — narrate before every sensei/Drive/Gmail/Canva call.",
        "  #6 OPEN TAB = sensei tab_create to google.com. Only method. No alternatives.",
    ]

    return "\n".join(lines)


def build_short():
    svc = check_services()
    active_task = active_task_from_context()

    def dot(ok):
        return "✓" if ok else "✗"

    return (
        f"[TOPOLOGY] Madam-Mary | "
        f"Ollama:{dot(svc['ollama_api'])} Pupil:{dot(svc['pupil_ui'])} Jellyfin:{dot(svc['jellyfin'])} MasterAI:{dot(svc['master_ai'])} | "
        f"Task: {active_task}"
    )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--json":
        svc = check_services()
        portfolio = read_portfolio()
        print(
            json.dumps(
                {
                    "services": svc,
                    "portfolio": portfolio,
                    "active_task": active_task_from_context(),
                    "timestamp": datetime.now().isoformat(),
                },
                indent=2,
            )
        )
    elif mode == "--short":
        print(build_short())
    else:
        print(build_briefing())
