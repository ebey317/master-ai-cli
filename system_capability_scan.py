#!/usr/bin/env python3
"""System capability self-discovery for CLAF.

Detects the operating system, catalogs commonly available terminal commands,
and can refresh its knowledge via web search when asked. The resulting JSON
lives at ~/.claf/system_capabilities.json and is read by CLAF prompts so the
model always knows which native commands are available.
"""

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

CAP_FILE = Path.home() / ".claf" / "system_capabilities.json"
CAP_FILE.parent.mkdir(parents=True, exist_ok=True)


def run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def discover() -> dict:
    os_name = platform.system()
    os_release = platform.release()
    os_version = platform.version()
    machine = platform.machine()

    caps = {
        "os": os_name,
        "os_release": os_release,
        "os_version": os_version,
        "machine": machine,
        "shell": os.environ.get("SHELL", "unknown"),
        "desktop": {
            "xdg_current_desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop_session": os.environ.get("DESKTOP_SESSION", "unknown"),
            "display_server": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        },
        "terminal_emulators": [],
        "package_managers": [],
        "common_commands": {},
    }

    # Commands to probe for existence and version
    probes = {
        "Linux": [
            "bash", "sh", "zsh",
            "which", "whereis", "find", "locate", "grep", "awk", "sed",
            "ls", "cat", "cp", "mv", "rm", "mkdir", "chmod", "chown",
            "ps", "top", "htop", "kill", "pgrep",
            "git", "python3", "python", "node", "npm",
            "apt", "apt-get", "dpkg", "snap", "flatpak", "yum", "dnf", "pacman",
            "curl", "wget", "ssh", "scp", "rsync",
            "xdg-open", "notify-send",
        ],
        "Darwin": [
            "bash", "sh", "zsh",
            "which", "whereis", "find", "mdfind", "grep", "awk", "sed",
            "ls", "cat", "cp", "mv", "rm", "mkdir", "chmod", "chown",
            "ps", "top", "htop", "kill", "pgrep",
            "git", "python3", "python", "node", "npm",
            "brew", "port",
            "curl", "wget", "ssh", "scp", "rsync",
            "open", "osascript",
        ],
        "Windows": [
            "powershell", "cmd", "where", "findstr", "dir", "tasklist", "taskkill",
            "git", "python", "node", "npm",
            "winget", "choco", "scoop",
        ],
    }

    for cmd in probes.get(os_name, probes["Linux"]):
        path = shutil.which(cmd)
        if path:
            caps["common_commands"][cmd] = {"path": path}

    # Package managers detected
    pkg_managers = {
        "Linux": ["apt", "apt-get", "dpkg", "snap", "flatpak", "yum", "dnf", "pacman", "zypper"],
        "Darwin": ["brew", "port"],
        "Windows": ["winget", "choco", "scoop"],
    }
    caps["package_managers"] = [
        pm for pm in pkg_managers.get(os_name, [])
        if shutil.which(pm)
    ]

    # Terminal emulators / desktops (Linux-only quick scan)
    if os_name == "Linux":
        for term in ["gnome-terminal", "konsole", "xfce4-terminal", "lxterminal", "alacritty", "kitty", "xterm"]:
            if shutil.which(term):
                caps["terminal_emulators"].append(term)
        # Desktop-environment helpers
        for de_cmd in ["xdg-open", "xdg-mime", "xdg-settings", "notify-send",
                       "gnome-control-center", "cinnamon-settings", "xfce4-settings-manager",
                       "plasma-systemsettings5", "mate-control-center"]:
            if shutil.which(de_cmd):
                caps["common_commands"][de_cmd] = {"path": shutil.which(de_cmd)}

    # Distro info
    if os_name == "Linux":
        if Path("/etc/os-release").exists():
            info = {}
            for line in Path("/etc/os-release").read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k] = v.strip('"')
            caps["distro"] = info

    return caps


def save(caps: dict):
    CAP_FILE.write_text(json.dumps(caps, indent=2))
    print(f"Capabilities saved to {CAP_FILE}")


def load() -> dict:
    if CAP_FILE.exists():
        return json.loads(CAP_FILE.read_text())
    return discover()


def format_prompt_block(caps: dict) -> str:
    """Return a compact system-prompt block describing native capabilities."""
    os_name = caps["os"]
    de = caps.get("desktop", {})
    de_name = de.get("xdg_current_desktop", "unknown")
    session_type = de.get("xdg_session_type", "unknown")
    lines = [
        f"PLATFORM: {os_name} {caps.get('distro', {}).get('PRETTY_NAME', caps['os_release'])} ({caps['machine']}).",
        f"DESKTOP ENVIRONMENT: {de_name} on {session_type}.",
        "Use native terminal commands for this OS. Preferred local-search patterns:",
    ]

    if os_name == "Linux":
        lines.append("- Find executable: `which <name>` or `whereis <name>`")
        lines.append("- Find files: `find /usr -name '<name>*'` or `locate <name>`")
        lines.append("- List packages: `dpkg -L <package>` / `rpm -ql <package>` / `pacman -Ql <package>`")
        lines.append("- Search installed: `apt list --installed | grep <name>`")
    elif os_name == "Darwin":
        lines.append("- Find executable: `which <name>` or `whereis <name>`")
        lines.append("- Find files: `mdfind 'kMDItemFSName == \"*<name>*\"'` or `find / -name '<name>*'`")
        lines.append("- List apps: `ls /Applications | grep <name>`")
        lines.append("- Homebrew: `brew list | grep <name>`")
    elif os_name == "Windows":
        lines.append("- Find executable: `where <name>` or `Get-Command <name>`")
        lines.append("- Find files: `dir /s /b *<name>*`")
        lines.append("- Packages: `winget list | findstr <name>`")

    if caps.get("package_managers"):
        lines.append(f"Available package managers: {', '.join(caps['package_managers'])}")

    if caps.get("terminal_emulators"):
        lines.append(f"Available terminal emulators: {', '.join(caps['terminal_emulators'])}")

    lines.append("Use `xdg-open` (Linux), `open` (macOS), or `start` (Windows) to open files/URLs in the default desktop app.")
    lines.append("If a needed command is missing, use Bash to install it via the native package manager.")
    lines.append("Do not open `file://` URLs in the browser unless explicitly asked.")
    return "\n".join(lines)


if __name__ == "__main__":
    caps = discover()
    save(caps)
    print("\n--- Prompt block ---\n")
    print(format_prompt_block(caps))
