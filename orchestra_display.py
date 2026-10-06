#!/usr/bin/env python3
"""
orchestra_display.py — Real-time Orchestra Router Monitor
Shows live which model is talking, which tier routed it, and running cost.
Runs standalone. Reads from /tmp/orchestra_audit.log (populated by orchestra_audit.py).
"""

import json
import os
import time
from collections import defaultdict
from datetime import date, datetime

AUDIT_LOG = os.environ.get("ORCHESTRA_AUDIT_LOG", "/tmp/orchestra_audit.log")
REFRESH_MS = int(os.environ.get("ORCHESTRA_REFRESH", "500"))

# ANSI
R = "\033[0m"
B = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GRN = "\033[92m"
YEL = "\033[93m"
BLU = "\033[94m"
MAG = "\033[95m"
CYN = "\033[96m"
WHT = "\033[97m"

TIER_COLORS = {
    0: GRN,
    1: CYN,
    2: BLU,
    3: WHT,
    6: MAG,
    7: YEL,
    8: DIM,
    9: RED,
}

PROVIDER_COLORS = {
    "local-ollama": GRN,
    "ollama-cloud-coder": CYN,
    "groq": BLU,
    "cerebras": WHT,
    "fireworks": MAG,
    "openrouter": YEL,
    "gemini": DIM,
    "anthropic": RED,
}

MODEL_COLORS = {
    "qwen2.5:3b": GRN,
    "qwen2.5:7b": GRN,
    "fast-agent:latest": GRN,
    "qwen3-coder:480b-cloud": CYN,
    "llama-3.3-70b-versatile": BLU,
    "qwen-3-235b-a22b-instruct-2507": WHT,
    "accounts/fireworks/models/deepseek-v4-pro": MAG,
    "anthropic/claude-sonnet-4.6": YEL,
    "claude-haiku-4-5-20251001": RED,
    "gemini-2.5-flash": DIM,
}


def clear():
    os.system("clear" if os.name != "nt" else "cls")


def load_entries():
    entries = []
    if not os.path.exists(AUDIT_LOG):
        return entries
    try:
        with open(AUDIT_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def fmt_cost(c):
    if c == 0:
        return f"{GRN}$0.0000{R}"
    if c < 0.001:
        return f"{DIM}${c:.6f}{R}"
    if c < 0.01:
        return f"{YEL}${c:.4f}{R}"
    return f"{RED}${c:.4f}{R}"


def render_live(entries):
    today = date.today().isoformat()
    today_entries = [e for e in entries if e.get("timestamp", "").startswith(today)]

    # Session totals
    total = len(today_entries)
    local = sum(1 for e in today_entries if e.get("tier", -1) == 0)
    cloud = total - local
    anthropic_hits = sum(
        1
        for e in today_entries
        if "anthropic" in e.get("provider", "").lower()
        or "openrouter" in e.get("provider", "").lower()
    )
    total_cost = sum(e.get("cost_usd", 0) for e in today_entries)

    # Provider breakdown
    prov_cost = defaultdict(float)
    prov_calls = defaultdict(int)
    for e in today_entries:
        p = e.get("provider", "unknown")
        prov_cost[p] += e.get("cost_usd", 0)
        prov_calls[p] += 1

    # Model breakdown
    model_calls = defaultdict(int)
    model_cost = defaultdict(float)
    for e in today_entries:
        m = e.get("model", "unknown")
        model_calls[m] += 1
        model_cost[m] += e.get("cost_usd", 0)

    lines = []
    lines.append(
        f"{B}{CYN}╔══════════════════════════════════════════════════════════════════╗{R}"
    )
    lines.append(
        f"{B}{CYN}║           ORCHESTRA LIVE MONITOR  {datetime.now().strftime('%H:%M:%S')}              ║{R}"
    )
    lines.append(
        f"{B}{CYN}╚══════════════════════════════════════════════════════════════════╝{R}"
    )
    lines.append("")

    # Top bar: session stats
    lines.append(
        f"  {B}Session Today{R}  │  Calls: {WHT}{total}{R}  │  Local: {GRN}{local}{R}  │  Cloud: {YEL}{cloud}{R}  │  Anthropic: {RED}{anthropic_hits}{R}  │  Cost: {fmt_cost(total_cost)}"
    )
    lines.append("")

    # Provider bar chart (ASCII)
    lines.append(f"  {B}Provider Activity{R}")
    max_calls = max(prov_calls.values()) if prov_calls else 1
    for prov in sorted(prov_calls.keys(), key=lambda x: prov_cost[x], reverse=True):
        calls = prov_calls[prov]
        cost = prov_cost[prov]
        bar_len = int(20 * calls / max_calls)
        bar = "█" * bar_len
        color = PROVIDER_COLORS.get(prov, WHT)
        lines.append(
            f"    {color}{prov:22s}{R} {bar:<20s} {calls:4d} calls  {fmt_cost(cost)}"
        )
    lines.append("")

    # Model bar chart
    lines.append(f"  {B}Model Activity{R}")
    max_mcalls = max(model_calls.values()) if model_calls else 1
    for model in sorted(model_calls.keys(), key=lambda x: model_cost[x], reverse=True):
        calls = model_calls[model]
        cost = model_cost[model]
        bar_len = int(20 * calls / max_mcalls)
        bar = "█" * bar_len
        color = MODEL_COLORS.get(model, WHT)
        short_model = model.split("/")[-1][:28]
        lines.append(
            f"    {color}{short_model:28s}{R} {bar:<20s} {calls:4d} calls  {fmt_cost(cost)}"
        )
    lines.append("")

    # Live feed (last 8 decisions)
    lines.append(f"  {B}Live Router Decisions (last 8){R}")
    for e in today_entries[-8:]:
        ts = e.get("timestamp", "?")[11:19]
        tier = e.get("tier", -1)
        model = e.get("model", "?")
        prov = e.get("provider", "?")
        task = e.get("task_type", "?")
        cost = e.get("cost_usd", 0)
        reason = e.get("hard_reason", "")
        mode = e.get("mode", "")

        t_color = TIER_COLORS.get(tier, WHT)
        m_color = MODEL_COLORS.get(model, WHT)
        p_color = PROVIDER_COLORS.get(prov, WHT)

        tier_tag = f"{t_color}[T{tier}]{R}"
        model_short = model.split("/")[-1][:20]

        reason_tag = f" {DIM}→ {reason}{R}" if reason else ""

        lines.append(
            f"    {DIM}{ts}{R} {tier_tag} {p_color}{prov[:12]:12s}{R} {m_color}{model_short:20s}{R} {task:12s} {fmt_cost(cost)}{reason_tag}"
        )

    lines.append("")

    # Alert zone
    if anthropic_hits > 5:
        lines.append(
            f"  {B}{RED}⚠ ANTHROPIC LEAK DETECTED: {anthropic_hits} hits today{R}"
        )
        lines.append(
            f"  {DIM}   Check _select_mode() or _is_hard_task() — local should catch these{R}"
        )
    elif cloud > local and total > 20:
        lines.append(
            f"  {B}{YEL}⚠ Cloud-heavy session: {cloud} cloud vs {local} local{R}"
        )
        lines.append(
            f"  {DIM}   Your local models may be failing or routing is too aggressive{R}"
        )
    else:
        lines.append(
            f"  {B}{GRN}✓ Healthy split: {local} local, {cloud} cloud, ${total_cost:.4f} spent{R}"
        )

    lines.append("")
    lines.append(
        f"  {DIM}Refresh: {REFRESH_MS}ms  │  Log: {AUDIT_LOG}  │  Ctrl+C to exit{R}"
    )

    return "\n".join(lines)


def main():
    print(f"{DIM}[orchestra_display] Starting monitor...{R}")
    print(f"{DIM}[orchestra_display] Reading from: {AUDIT_LOG}{R}")
    print(f"{DIM}[orchestra_display] Refresh: {REFRESH_MS}ms{R}")
    print(
        f"{DIM}[orchestra_display] Run 'python orchestra_audit.py' first to generate log{R}"
    )
    time.sleep(1)

    try:
        while True:
            entries = load_entries()
            clear()
            print(render_live(entries))
            time.sleep(REFRESH_MS / 1000.0)
    except KeyboardInterrupt:
        print(f"\n{DIM}Monitor stopped.{R}")


if __name__ == "__main__":
    main()
