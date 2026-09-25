#!/usr/bin/env python3
# Sensei tool detector — enumerate this box's actual CLI capabilities.
# Read-only: shutil.which + `--version` probes. No sudo, no network.
# Writes ~/.sensei_tool_inventory.json so Sensei can stop guessing what she has.

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

INVENTORY_PATH = Path.home() / ".sensei_tool_inventory.json"
PROBE_TIMEOUT_S = 2.0

# Curated list. Keep under ~80; group by purpose so future edits land cleanly.
TOOLS: dict[str, list[str]] = {
    "vcs": ["git", "gh", "svn", "hg"],
    "editors": ["vim", "nvim", "nano", "emacs", "code"],
    "shells": ["bash", "zsh", "fish"],
    "languages": [
        "python3",
        "node",
        "npm",
        "npx",
        "pip",
        "ruby",
        "go",
        "rustc",
        "cargo",
        "java",
        "deno",
        "bun",
        "perl",
        "lua",
    ],
    "build": ["make", "cmake", "gcc", "g++", "pkg-config"],
    "network": [
        "curl",
        "wget",
        "ssh",
        "rsync",
        "nc",
        "dig",
        "ping",
        "mtr",
        "tailscale",
    ],
    "files_text": ["jq", "yq", "fd", "fzf", "rg", "tar", "gzip", "xz", "zip", "unzip"],
    "media": ["ffmpeg", "ffprobe", "yt-dlp"],
    "libreoffice": ["libreoffice", "soffice", "unopkg"],
    "db": ["sqlite3", "psql", "mysql", "redis-cli"],
    "ai": ["ollama"],
    "browsers": ["google-chrome", "firefox"],
    "desktop_x": [
        "wmctrl",
        "xdotool",
        "xclip",
        "xsel",
        "scrot",
        "gnome-screenshot",
        "gnome-terminal",
    ],
    "system": [
        "tmux",
        "screen",
        "htop",
        "btop",
        "lsof",
        "systemctl",
        "journalctl",
        "watch",
        "parallel",
    ],
    "crypto": ["openssl", "gpg", "sha256sum", "base64"],
    "extras": ["tree", "bat", "hyperfine", "ncdu"],
    "email_clients": [
        "thunderbird",
        "evolution",
        "mutt",
        "neomutt",
        "alpine",
        "sylpheed",
        "claws-mail",
        "geary",
    ],
}

# Tools whose --version writes to stderr or needs a different flag.
VERSION_FLAG_OVERRIDES: dict[str, list[str]] = {
    "gnome-terminal": ["--version"],
    "java": ["-version"],  # writes to stderr
    "convert": ["-version"],
    "openssl": ["version"],
    "soffice": ["--version"],
    "libreoffice": ["--version"],
}


def first_nonempty_line(s: str) -> str:
    for line in s.splitlines():
        line = line.strip()
        if line:
            return line[:240]
    return ""


def probe_version(path: str, name: str) -> str | None:
    args = VERSION_FLAG_OVERRIDES.get(name, ["--version"])
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None
    out = proc.stdout or ""
    err = proc.stderr or ""
    line = first_nonempty_line(out) or first_nonempty_line(err)
    return line or None


def detect(tools: Iterable[str]) -> list[dict]:
    rows: list[dict] = []
    for name in tools:
        path = shutil.which(name)
        if not path:
            rows.append({"name": name, "found": False})
            continue
        try:
            st = os.stat(path)
            mtime = int(st.st_mtime)
        except OSError:
            mtime = None
        rows.append(
            {
                "name": name,
                "found": True,
                "path": path,
                "version": probe_version(path, name),
                "mtime": mtime,
            }
        )
    return rows


def build_inventory() -> dict:
    started = time.time()
    sections: dict[str, list[dict]] = {}
    found_count = 0
    total = 0
    for group, names in TOOLS.items():
        rows = detect(names)
        sections[group] = rows
        found_count += sum(1 for r in rows if r["found"])
        total += len(rows)
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "generated_at": int(time.time()),
        "elapsed_ms": elapsed_ms,
        "total_probed": total,
        "total_found": found_count,
        "sections": sections,
    }


def main() -> int:
    inv = build_inventory()
    INVENTORY_PATH.write_text(json.dumps(inv, indent=2) + "\n")
    print(
        f"sensei_tool_detector: {inv['total_found']}/{inv['total_probed']} "
        f"tools present in {inv['elapsed_ms']}ms -> {INVENTORY_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
