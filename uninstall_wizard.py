"""uninstall_wizard.py — guided uninstall for Master AI.

Supports:
  python3 uninstall_wizard.py        # interactive CLI menu
  master-ai --uninstall              # invoked from master_ai.py main()

Can optionally use GitHub Models as a temporary assistant to walk the user
through the uninstall, then disconnect. The token is only used during the
uninstall session.

Removal levels:
  1) pip package + config keys        (keeps Ollama/models)
  2) full user data + entry points     (keeps Ollama/models)
  3) total wipe                        (optional Ollama + models)
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_MODELS_DEFAULT_MODEL = "gpt-4o"

C = {
    "bold": "\033[1m",
    "green": "\033[1;32m",
    "cyan": "\033[1;36m",
    "yellow": "\033[33m",
    "red": "\033[91m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

SPLASH = f"""{C['red']}
    ██╗   ██╗███╗   ██╗██╗███╗   ██╗███████╗████████╗ █████╗ ██╗     ██╗
    ██║   ██║████╗  ██║██║████╗  ██║██╔════╝╚══██╔══╝██╔══██╗██║     ██║
    ██║   ██║██╔██╗ ██║██║██╔██╗ ██║███████╗   ██║   ███████║██║     ██║
    ██║   ██║██║╚██╗██║██║██║╚██╗██║╚════██║   ██║   ██╔══██║██║     ██║
    ███████╗██║ ╚████║██║██║ ╚████║███████║   ██║   ██║  ██║███████╗███████╗
    ╚══════╝╚═╝  ╚═══╝╚═╝╚═╝  ╚═══╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚══════╝
{C['reset']}
{C['bold']}  Master AI — Uninstall Wizard{C['reset']}
"""

SYSTEM_PROMPT = """You are the Master AI uninstall assistant, running temporarily through GitHub Models.
Help the user choose an uninstall level and confirm their choice.

Rules:
- Be concise. One short paragraph per turn.
- Explain the three levels:
  1) Remove pip package + API keys/config (keeps Ollama/models)
  2) Remove all user data + entry points (keeps Ollama/models)
  3) Total wipe including Ollama and downloaded models
- Do not execute anything. Only guide.
- When the user is ready, tell them to type the level number (1, 2, or 3) or 'cancel'.
- Never claim to be connected after uninstall ends.
"""

HOME_FILES = [
    Path.home() / ".master_ai_keys",
    Path.home() / ".master_ai_memory",
    Path.home() / ".master_ai_approved",
    Path.home() / ".master_ai_settings",
    Path.home() / ".master_ai_permissions_done",
    Path.home() / ".master_ai_setup_done",
    Path.home() / ".master_ai_install.log",
    Path.home() / ".master_ai_email_log.jsonl",
    Path.home() / ".master_ai_approved_components",
]

LOCAL_BIN_SCRIPTS = [
    Path.home() / ".local" / "bin" / "master-ai",
    Path.home() / ".local" / "bin" / "sensei",
]

SYSTEMD_USER_SERVICES = [
    "master-ai-ui.service",
    "master-ai-tts.service",
    "master-ai-prewarm.service",
    "master-ai-deep-clean.service",
    "master-ai-deep-clean.timer",
]


def _print(text: str = "") -> None:
    print(text)


def _yes_no(prompt: str, default_no: bool = True) -> bool:
    suffix = " [y/N] " if default_no else " [Y/n] "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return not default_no
    return raw.startswith("y")


def _github_models_chat(messages: list[dict], token: str) -> str | None:
    payload = {
        "model": GITHUB_MODELS_DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GITHUB_MODELS_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        _print(f"{C['red']}✗ GitHub Models HTTP {e.code}: {body}{C['reset']}")
        return None
    except Exception as e:
        _print(f"{C['red']}✗ GitHub Models error: {e}{C['reset']}")
        return None


def _remove_pip_package() -> bool:
    _print(f"{C['yellow']}  → Removing pip package master-ai-cli...{C['reset']}")
    try:
        r = os.system("pip3 uninstall -y master-ai-cli 2>/dev/null")
        return r == 0
    except Exception:
        return False


def _remove_home_files() -> list[str]:
    removed = []
    for p in HOME_FILES:
        if p.is_file():
            try:
                p.unlink()
                removed.append(str(p))
            except Exception as e:
                _print(f"{C['red']}  ✗ could not remove {p}: {e}{C['reset']}")
        elif p.is_dir():
            try:
                shutil.rmtree(p)
                removed.append(str(p))
            except Exception as e:
                _print(f"{C['red']}  ✗ could not remove {p}: {e}{C['reset']}")
    return removed


def _remove_entry_points() -> list[str]:
    removed = []
    for p in LOCAL_BIN_SCRIPTS:
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
                removed.append(str(p))
            except Exception as e:
                _print(f"{C['red']}  ✗ could not remove {p}: {e}{C['reset']}")
    return removed


def _remove_systemd_services() -> None:
    for svc in SYSTEMD_USER_SERVICES:
        os.system(f"systemctl --user stop {svc} 2>/dev/null")
        os.system(f"systemctl --user disable {svc} 2>/dev/null")
    ud = Path.home() / ".config" / "systemd" / "user"
    for svc in SYSTEMD_USER_SERVICES:
        p = ud / svc
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    os.system("systemctl --user daemon-reload 2>/dev/null")


def _remove_scripts_dir() -> None:
    scripts = Path.home() / "scripts"
    if not scripts.exists():
        return
    _print(f"{C['yellow']}  → Remove ~/scripts/ used by install.sh?{C['reset']}")
    if _yes_no("Delete the entire ~/scripts directory", default_no=True):
        try:
            shutil.rmtree(scripts)
            _print(f"{C['green']}  ✓ Removed ~/scripts/{C['reset']}")
        except Exception as e:
            _print(f"{C['red']}  ✗ could not remove ~/scripts/: {e}{C['reset']}")


def _remove_ollama() -> None:
    if not _yes_no("Stop/disable Ollama systemd service", default_no=True):
        return
    os.system("sudo systemctl stop ollama 2>/dev/null")
    os.system("sudo systemctl disable ollama 2>/dev/null")
    if shutil.which("ollama"):
        _print(f"{C['yellow']}  → Removing downloaded Ollama models...{C['reset']}")
        os.system("ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | xargs -r ollama rm 2>/dev/null")
    if _yes_no("Uninstall Ollama binary and data (/usr/local/bin/ollama, /usr/share/ollama)", default_no=True):
        os.system("sudo rm -f /usr/local/bin/ollama")
        os.system("sudo rm -rf /usr/share/ollama")
        os.system("sudo rm -f /etc/systemd/system/ollama.service")
        os.system("sudo rm -rf /etc/systemd/system/ollama.service.d")
        os.system("sudo systemctl daemon-reload 2>/dev/null")
        _print(f"{C['green']}  ✓ Ollama uninstalled{C['reset']}")


def level_1_pip_and_keys() -> None:
    _print(f"\n{C['bold']}Level 1: pip package + API keys/config{C['reset']}")
    _print(f"{C['dim']}Keeps Ollama, models, and ~/scripts/ intact.{C['reset']}\n")
    if not _yes_no("Proceed", default_no=True):
        _print(f"{C['dim']}Cancelled.{C['reset']}")
        return
    _remove_pip_package()
    removed = _remove_home_files()
    for r in removed:
        _print(f"{C['green']}  ✓ Removed: {r}{C['reset']}")
    _remove_systemd_services()
    _print(f"\n{C['green']}✓ Level 1 complete. Run `pip install -e .` to reinstall.{C['reset']}")


def level_2_full_user_data() -> None:
    _print(f"\n{C['bold']}Level 2: full user data + entry points{C['reset']}")
    _print(f"{C['dim']}Removes pip package, keys, memory, approved, entry points, and systemd services. Keeps Ollama.{C['reset']}\n")
    if not _yes_no("Proceed", default_no=True):
        _print(f"{C['dim']}Cancelled.{C['reset']}")
        return
    _remove_pip_package()
    removed = _remove_home_files()
    for r in removed:
        _print(f"{C['green']}  ✓ Removed: {r}{C['reset']}")
    ep = _remove_entry_points()
    for r in ep:
        _print(f"{C['green']}  ✓ Removed: {r}{C['reset']}")
    _remove_systemd_services()
    _remove_scripts_dir()
    _print(f"\n{C['green']}✓ Level 2 complete. Ollama and models remain.{C['reset']}")


def level_3_total_wipe() -> None:
    _print(f"\n{C['red']}{C['bold']}Level 3: TOTAL WIPE{C['reset']}")
    _print(f"{C['red']}This removes everything including Ollama and downloaded models.{C['reset']}")
    _print(f"{C['red']}This cannot be undone.{C['reset']}\n")
    if not _yes_no("Proceed", default_no=True):
        _print(f"{C['dim']}Cancelled.{C['reset']}")
        return
    level_2_full_user_data()
    _remove_ollama()
    _print(f"\n{C['green']}✓ Total wipe complete.{C['reset']}")


def _interactive_github_uninstall(token: str) -> None:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    _print(f"{C['bold']}  Temporary GitHub Models uninstall assistant.{C['reset']}")
    _print(f"{C['dim']}  Type a question, or type 1 / 2 / 3 to choose a level, or 'cancel'.{C['reset']}\n")

    intro = (
        "I can help you uninstall Master AI. There are three levels:\n"
        "  1) pip package + keys/config (keeps Ollama)\n"
        "  2) all user data + entry points (keeps Ollama)\n"
        "  3) total wipe including Ollama and models\n\n"
        "Type the number you want, ask a question, or type 'cancel'."
    )
    _print(f"{C['green']}Assistant:{C['reset']} {intro}\n")

    while True:
        try:
            user_text = input(f"{C['bold']}You:{C['reset']} ").strip()
        except (EOFError, KeyboardInterrupt):
            _print(f"\n{C['yellow']}Cancelled.{C['reset']}")
            return
        lower = user_text.lower()
        if lower in ("cancel", "quit", "exit", "x"):
            _print(f"{C['yellow']}Cancelled.{C['reset']}")
            return
        if lower in ("1", "one"):
            level_1_pip_and_keys()
            break
        if lower in ("2", "two"):
            level_2_full_user_data()
            break
        if lower in ("3", "three"):
            level_3_total_wipe()
            break
        messages.append({"role": "user", "content": user_text})
        reply = _github_models_chat(messages, token)
        if reply is None:
            _print(f"{C['yellow']}GitHub Models not answering. Switching to manual menu.{C['reset']}")
            return
        messages.append({"role": "assistant", "content": reply})
        _print(f"\n{C['green']}Assistant:{C['reset']} {reply}\n")

    _print(f"\n{C['cyan']}🔌 GitHub AI disconnected. Uninstall complete.{C['reset']}")


def menu() -> None:
    _print(f"\n{C['bold']}Choose an uninstall level:{C['reset']}\n")
    _print(f"  {C['yellow']}1){C['reset']} Remove pip package + API keys/config (keeps Ollama)")
    _print(f"  {C['yellow']}2){C['reset']} Remove all user data + entry points (keeps Ollama)")
    _print(f"  {C['yellow']}3){C['reset']} TOTAL WIPE — Ollama + models + everything")
    _print(f"  {C['dim']}x) Cancel{C['reset']}\n")
    try:
        choice = input(f"{C['bold']}Choice [1/2/3/x]: {C['reset']}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "x"
    if choice == "1":
        level_1_pip_and_keys()
    elif choice == "2":
        level_2_full_user_data()
    elif choice == "3":
        level_3_total_wipe()
    else:
        _print(f"{C['dim']}Cancelled.{C['reset']}")


def run_uninstall(use_github: bool = False) -> None:
    os.system("clear")
    _print(SPLASH)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if use_github:
        if not token:
            token = getpass.getpass("Paste GitHub token for temporary uninstall assistant (hidden): ").strip()
        if token:
            _interactive_github_uninstall(token)
            return
        _print(f"{C['yellow']}No token provided. Switching to manual menu.{C['reset']}")
    menu()


def main() -> None:
    use_github = "--github" in sys.argv
    run_uninstall(use_github=use_github)


if __name__ == "__main__":
    main()
