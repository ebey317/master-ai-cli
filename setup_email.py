#!/usr/bin/env python3
"""
setup_email.py — one-shot interactive setup for Master AI's SEND_EMAIL.

Run this in any terminal:

    python3 ~/scripts/setup_email.py

It walks you through configuring Gmail, AOL, and Outlook accounts. For each
provider you choose to configure it:
  1. Opens the app-password page in your default browser
  2. Asks for your full email address (e.g., you@example.com)
  3. Asks for the 16-character app password (hidden — your password never
     prints to screen, never lands in shell history, never enters Claude's chat)
  4. Atomically writes both into ~/.master_ai_keys (JSON, chmod 600)
  5. Sends a small test email to yourself; reports OK/FAIL

If you weren't here and I weren't here, this is how you'd do it. Self-contained,
idempotent, safe to re-run.
"""
import json
import os
import sys
import getpass
import subprocess
from pathlib import Path

KEYS_FILE = Path.home() / ".master_ai_keys"

PROVIDERS = {
    "gmail": {
        "label": "Gmail",
        "password_url": "https://myaccount.google.com/apppasswords",
        "key_password": "gmail_app_password",
        "key_sender": "gmail_sender",
        "default_sender": "you@example.com",
        "host": "smtp.gmail.com", "port": 465, "ssl": True,
        "instructions": (
            "Sign in as your Gmail account → app-name box → type \"Master AI\" → click\n"
            "  Create → Google shows a 16-character string (with spaces). Copy it."
        ),
    },
    "aol": {
        "label": "AOL",
        "password_url": "https://login.aol.com/account/security",
        "key_password": "aol_app_password",
        "key_sender": "aol_sender",
        "default_sender": "",
        "host": "smtp.aol.com", "port": 465, "ssl": True,
        "instructions": (
            "Sign in to AOL → Account Security → \"Generate app password\" or\n"
            "  \"3rd-party app passwords\" → name it \"Master AI\" → copy the 16-char string."
        ),
    },
    "outlook": {
        "label": "Outlook / Hotmail / Live",
        "password_url": "https://account.live.com/proofs/AppPassword",
        "key_password": "outlook_app_password",
        "key_sender": "outlook_sender",
        "default_sender": "",
        "host": "smtp-mail.outlook.com", "port": 587, "ssl": False,
        "instructions": (
            "Sign in to your Outlook/Hotmail/Live account → Security → \"Create a new app\n"
            "  password\" → copy the 16-char string. Requires two-step verification enabled."
        ),
    },
}

BANNER = "\033[1;32m"   # bright green
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def banner(text):
    bar = "═" * (len(text) + 4)
    print(f"\n{BANNER}╔{bar}╗{RESET}")
    print(f"{BANNER}║  {BOLD}{text}{RESET}{BANNER}  ║{RESET}")
    print(f"{BANNER}╚{bar}╝{RESET}\n")


def step(text):
    print(f"{BOLD}→ {text}{RESET}")


def warn(text):
    print(f"{YELLOW}! {text}{RESET}")


def fail(text):
    print(f"{RED}✗ {text}{RESET}")


def ok(text):
    print(f"{BANNER}✓ {text}{RESET}")


def read_keys():
    if not KEYS_FILE.exists():
        return {}
    try:
        return json.loads(KEYS_FILE.read_text())
    except Exception as e:
        warn(f"~/.master_ai_keys exists but is unreadable as JSON: {e}")
        warn("Aborting to avoid overwriting. Fix the file and re-run.")
        sys.exit(2)


def write_keys(keys):
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(KEYS_FILE)


def yes_no(prompt, default_no=True):
    suffix = " [y/N] " if default_no else " [Y/n] "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return not default_no
    return raw in ("y", "yes")


def open_url(url):
    """Best-effort browser open via xdg-open. Non-fatal if it fails."""
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def configure_provider(name, cfg, keys):
    """Walk the user through configuring one provider. Returns True if configured (or skipped successfully), False if aborted."""
    existing_pw = keys.get(cfg["key_password"])
    existing_sender = keys.get(cfg["key_sender"]) or cfg["default_sender"]
    banner(f"{cfg['label']} setup")
    if existing_pw:
        warn(f"{cfg['label']} is already configured (sender={existing_sender or '?'}).")
        if not yes_no(f"Re-enter {cfg['label']} credential?"):
            print(f"{DIM}  keeping existing {cfg['label']} credential.{RESET}")
            return True
    if not yes_no(f"Configure {cfg['label']} now?", default_no=False):
        print(f"{DIM}  skipping {cfg['label']}.{RESET}")
        return True
    step(f"Opening {cfg['label']} app-password page in your browser …")
    if not open_url(cfg["password_url"]):
        warn(f"Couldn't auto-open. Visit manually: {cfg['password_url']}")
    print()
    print(f"{DIM}  {cfg['instructions']}{RESET}")
    print()
    sender_prompt = f"Your {cfg['label']} email address"
    if cfg["default_sender"]:
        sender_prompt += f" [{cfg['default_sender']}]"
    sender_prompt += ": "
    sender = input(sender_prompt).strip() or cfg["default_sender"]
    if not sender or "@" not in sender:
        fail("Need a valid email address. Skipping this provider.")
        return False
    pw = getpass.getpass(f"Paste {cfg['label']} app password (input hidden): ").strip()
    pw = pw.replace(" ", "")  # Strip spaces; Google et al. show "xxxx xxxx xxxx xxxx"
    if not pw:
        fail("No password entered. Skipping.")
        return False
    if len(pw) < 8:
        warn(f"That looks short ({len(pw)} chars). App passwords are usually 16 chars.")
        if not yes_no("Save anyway?", default_no=True):
            return False
    keys[cfg["key_password"]] = pw
    keys[cfg["key_sender"]] = sender
    write_keys(keys)
    ok(f"{cfg['label']} credentials saved to ~/.master_ai_keys (chmod 600).")
    return True


def test_send(name, cfg, keys):
    """Send a small test email to the configured sender (self-send)."""
    sender = keys.get(cfg["key_sender"]) or cfg["default_sender"]
    if not sender or not keys.get(cfg["key_password"]):
        print(f"{DIM}  {cfg['label']}: not configured, skipping test send.{RESET}")
        return None
    print(f"{DIM}  Sending {cfg['label']} test email → {sender} …{RESET}")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from master_ai import send_email_via_smtp
    except Exception as e:
        fail(f"Couldn't import send_email_via_smtp: {e}")
        return False
    result = send_email_via_smtp(
        sender,
        f"Master AI {cfg['label']} setup test",
        f"This is the configuration test from setup_email.py. If you see this in your inbox, {cfg['label']} send is working end-to-end.\n\n— Master AI",
        sender=sender,
    )
    if result.get("ok"):
        ok(f"{cfg['label']} test send OK — check your inbox at {sender}")
        return True
    else:
        fail(f"{cfg['label']} test send FAILED: {result.get('error')}")
        print(f"{DIM}  Common fixes: re-check the app password (no extra spaces), confirm 2-factor is enabled, confirm the sender address matches the account that generated the password.{RESET}")
        return False


def main():
    banner("Master AI — Email Setup")
    print("This will configure Gmail, AOL, and Outlook so Sensei can send mail.")
    print("Your password is typed into a hidden prompt — it never echoes to the")
    print("screen, never enters shell history, never enters Claude's chat.\n")
    if KEYS_FILE.exists():
        print(f"{DIM}Existing keys file: {KEYS_FILE} ({KEYS_FILE.stat().st_size} bytes){RESET}")
    else:
        print(f"{DIM}No keys file yet — will be created at {KEYS_FILE} with chmod 600.{RESET}")
    print()
    keys = read_keys()
    for name, cfg in PROVIDERS.items():
        configure_provider(name, cfg, keys)
    banner("Test sends")
    any_tested = False
    for name, cfg in PROVIDERS.items():
        if keys.get(cfg["key_password"]):
            test_send(name, cfg, keys)
            any_tested = True
    if not any_tested:
        warn("No provider was configured — nothing to test.")
    banner("Done")
    print(f"You can now ask Sensei to send email. The model emits {BOLD}SEND_EMAIL:{RESET} directives;")
    print(f"the dispatcher uses the provider that matches the {BOLD}from={RESET} address (or default = Gmail).")
    print()
    print(f"Re-run {BOLD}python3 ~/scripts/setup_email.py{RESET} anytime to update or add a provider.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Existing keys file is unchanged unless a save already happened above.")
        sys.exit(130)
