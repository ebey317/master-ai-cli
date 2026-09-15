#!/usr/bin/env python3
"""Telegram gateway daemon for Master AI CLI.

Polls Telegram getUpdates and feeds each incoming message into the
headless Sensei runner. Replies are sent back to the same chat.

Credentials read from ~/.master_ai_keys:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=123456789      # default; optional comma-separated list

Run standalone:
    python3 telegram_gateway.py

Or as a systemd user service via telegram-gateway.service.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import sensei_repl_bridge

LOG = logging.getLogger(__name__)
UPDATE_INTERVAL_SECONDS = 2
TOKEN_PATH = Path.home() / ".master_ai_keys"
LOG_FILE = Path.home() / ".master_ai_telegram_gateway.log"
PID_FILE = Path.home() / ".master_ai_telegram_gateway.pid"
MODEL_STATE_FILE = Path.home() / ".master_ai_telegram_model"
MASTER_AI_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = "glm-5.3-flash"
_START_TIME = time.time()


def _read_key(name: str) -> str | None:
    try:
        text = TOKEN_PATH.read_text(encoding="utf-8")
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip()
    return None


def _load_allowed_chat_ids() -> list[str]:
    raw = _read_key("TELEGRAM_CHAT_ID") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _telegram_api(method: str, token: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8") if params else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("description", str(e))
        except Exception:
            detail = str(e)
        return {"ok": False, "description": detail}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def _get_updates(
    token: str, offset: int, limit: int = 10
) -> tuple[bool, list[dict], int]:
    body = _telegram_api("getUpdates", token, offset=offset, limit=limit)
    if not body.get("ok"):
        LOG.error("getUpdates failed: %s", body.get("description"))
        return False, [], offset
    updates = body.get("result", []) or []
    new_offset = offset
    for upd in updates:
        upd_id = upd.get("update_id")
        if isinstance(upd_id, int) and upd_id >= new_offset:
            new_offset = upd_id + 1
    return True, updates, new_offset


def _send_reply(token: str, chat_id: str, text: str) -> bool:
    if not text:
        return True
    # Telegram max message length is 4096; chunk if needed
    max_len = 4000
    chunks = []
    while len(text) > max_len:
        idx = text.rfind("\n", 0, max_len)
        if idx == -1:
            idx = max_len
        chunks.append(text[:idx])
        text = text[idx:].lstrip()
    chunks.append(text)
    for chunk in chunks:
        body = _telegram_api(
            "sendMessage", token, chat_id=chat_id, text=chunk, parse_mode="HTML"
        )
        if not body.get("ok"):
            # If HTML parse fails, retry as plain text
            if "can't parse" in str(body.get("description", "")).lower():
                body = _telegram_api("sendMessage", token, chat_id=chat_id, text=chunk)
            if not body.get("ok"):
                LOG.error("sendMessage failed: %s", body.get("description"))
                return False
    return True


def _get_current_model() -> str:
    """Model state file (set via /model <name>) wins; falls back to the
    TELEGRAM_SENSEI_MODEL env var, then DEFAULT_MODEL."""
    try:
        stored = MODEL_STATE_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    except Exception:
        pass
    return os.environ.get("TELEGRAM_SENSEI_MODEL", DEFAULT_MODEL)


def _set_current_model(name: str) -> None:
    MODEL_STATE_FILE.write_text(name.strip() + "\n", encoding="utf-8")


def _run_sensei_task(task_text: str, max_turns: int = 5) -> str:
    env = os.environ.copy()
    env["SENSEI_TUI"] = "0"
    model = _get_current_model()
    cmd = [
        sys.executable,
        str(MASTER_AI_DIR / "headless_runner.py"),
        "--headless",
        "--task",
        task_text,
        "--max-turns",
        str(max_turns),
        "--model",
        model,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(MASTER_AI_DIR),
            env=env,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            LOG.error("headless_runner exited %d: %s", result.returncode, err)
            return f"Sensei error (exit {result.returncode}):\n{err or out}"[:4000]
        return out[:8000]
    except subprocess.TimeoutExpired:
        LOG.error("headless_runner timed out")
        return "Sensei timed out after 5 minutes."
    except Exception as e:
        LOG.exception("headless_runner failed")
        return f"Sensei failed: {e}"[:4000]


def _make_reply(text: str) -> str:
    # Escape HTML special chars so Telegram doesn't choke on parse_mode=HTML
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Slash commands ──────────────────────────────────────────────────
# 2026-09-15: before this, every message -- including /status and /model --
# was forwarded as literal chat text to headless_runner, which has no
# concept of a bot command. The model just tried (and usually failed) to
# answer "/status" as a question. These are real Telegram bot commands,
# handled here, never reaching the LLM.

_COMMAND_HELP = (
    "Commands:\n"
    "/help - show this list\n"
    "/status - gateway uptime + current model\n"
    "/model - show the model currently answering you\n"
    "/model &lt;name&gt; - switch models (e.g. /model glm-5.3-flash)\n"
    "/new - fresh Sensei REPL session (doctor/sessions/tasks/etc. state resets)\n"
    "Plus any Sensei REPL command (doctor, sessions list, memory, tasks, "
    "git, save session, ...) works directly, e.g. /doctor or /sessions list.\n"
    "Anything else is sent to Sensei as a normal task."
)

# ── Sensei REPL bridge ───────────────────────────────────────────────
# 2026-09-15: most of Sensei's own commands (doctor, sessions list, memory,
# tasks, git, ...) only exist as `if lo == "...":` handlers deeply closed
# over master_ai.py's interactive main() loop -- not importable as
# functions, and not reachable through headless_runner.py's model-driven
# RUN/READ path. sensei_repl_bridge.py drives the real classic REPL
# (SENSEI_TUI=0) as a persistent subprocess instead, so these commands work
# exactly as they do in the terminal, with zero changes to master_ai.py.
#
# Deliberately excludes "new"/"clear"/"kick"/"x" -- these restart or exit
# the underlying engine process, which would kill this bridge's subprocess
# out from under it. Handling that gracefully (detect the exit, respawn,
# don't lose a message in flight) is real work saved for a follow-up
# rather than rushed in here.
_SENSEI_EXACT_COMMANDS = {
    "doctor",
    "standards",
    "memory",
    "sessions list",
    "load summary",
    "load session",
    "tasks",
    "task clear",
    "git",
    "git log",
    "git diff",
    "save session",
    "help",
    "tips",
    "keys",
    "approved",
    "clear approved",
    "clear history",
    "clear cache",
    "model stats",
    "model auto",
}
_SENSEI_PREFIX_COMMANDS = (
    "sessions resume ",
    "task add ",
    "task done ",
    "task ",
    "git commit ",
    "model ",
    "remember:",
    "forget:",
)

_repl_lock = threading.Lock()
_repl: sensei_repl_bridge.SenseiRepl | None = None


def _sensei_command_target(text: str) -> str | None:
    """The exact Sensei REPL command to send for a bridgeable request, or
    None if `text` isn't one of the allowlisted safe/non-interactive
    commands."""
    stripped = text.strip()
    lo = stripped.lower()
    if lo in _SENSEI_EXACT_COMMANDS:
        return stripped
    for prefix in _SENSEI_PREFIX_COMMANDS:
        if lo.startswith(prefix):
            return stripped
    return None


def _get_repl() -> sensei_repl_bridge.SenseiRepl:
    global _repl
    with _repl_lock:
        if _repl is None or _repl.proc.poll() is not None:
            LOG.info("Starting Sensei REPL bridge subprocess")
            _repl = sensei_repl_bridge.SenseiRepl()
        return _repl


def _run_sensei_repl_command(command: str) -> str:
    try:
        repl = _get_repl()
        return repl.send(command, timeout=45)
    except Exception as e:
        LOG.exception("sensei_repl_bridge command failed: %r", command)
        return f"Sensei command failed: {e}"


def _handle_command(text: str) -> str | None:
    """Return a reply for a recognized /command, or None to fall through
    to the normal Sensei task path."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        return _COMMAND_HELP
    if cmd == "/status":
        uptime_s = int(time.time() - _START_TIME)
        hours, rem = divmod(uptime_s, 3600)
        minutes, seconds = divmod(rem, 60)
        return (
            f"Gateway uptime: {hours}h {minutes}m {seconds}s\n"
            f"Current model: {_get_current_model()}"
        )
    if cmd == "/model":
        if not arg:
            return f"Current model: {_get_current_model()}"
        _set_current_model(arg)
        return f"Model switched to: {arg}"
    if cmd == "/new":
        # Deliberately NOT sending Sensei's own "new"/"clear" text into the
        # bridge -- that command restarts the engine process from the
        # INSIDE (execvp), which the bridge's stdin/stdout pipes and
        # prompt-detection logic aren't built to survive. Controlling the
        # subprocess's lifecycle ourselves (close + drop the reference) is
        # simpler and predictable: the next bridged command lazily spins up
        # a fresh one via _get_repl(). Free-text chat (headless_runner.py)
        # already starts a brand-new, history-less process per message, so
        # there's nothing to reset there.
        global _repl
        with _repl_lock:
            if _repl is not None:
                try:
                    _repl.close()
                except Exception:
                    LOG.exception("error closing sensei repl during /new")
                _repl = None
        return "New session started. Sensei's REPL bridge will spin up fresh on your next command."

    # Anything else recognized as a genuine Sensei REPL command (doctor,
    # sessions list, memory, tasks, git, ...) gets driven through the real
    # REPL instead of being reported unknown.
    bridged = (cmd[1:] + (f" {arg}" if arg else "")).strip()
    target = _sensei_command_target(bridged)
    if target is not None:
        return _run_sensei_repl_command(target)

    return f"Unknown command: {cmd}\n\n{_COMMAND_HELP}"


def _process_message(token: str, allowed_ids: list[str], msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    if allowed_ids and chat_id not in allowed_ids:
        LOG.warning("Ignoring message from unallowed chat %s", chat_id)
        return
    text = msg.get("text", "")
    if not text:
        return
    from_user = msg.get("from", {})
    LOG.info(
        "Processing message from %s (%s %s): %r",
        chat_id,
        from_user.get("first_name"),
        from_user.get("last_name"),
        text[:80],
    )
    command_reply = _handle_command(text)
    if command_reply is not None:
        _send_reply(token, chat_id, command_reply)
        return
    reply = _run_sensei_task(text)
    reply = _make_reply(reply)
    _send_reply(token, chat_id, reply)


def _write_pid():
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    token = _read_key("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not found in ~/.master_ai_keys", file=sys.stderr)
        sys.exit(1)
    allowed_ids = _load_allowed_chat_ids()
    if not allowed_ids:
        print("TELEGRAM_CHAT_ID not found in ~/.master_ai_keys", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )
    LOG.info("Telegram gateway starting; allowed chats: %s", allowed_ids)
    _write_pid()

    offset = 0
    try:
        while True:
            ok, updates, offset = _get_updates(token, offset)
            if ok:
                for upd in updates:
                    msg = upd.get("message") or upd.get("edited_message")
                    if msg:
                        _process_message(token, allowed_ids, msg)
            time.sleep(UPDATE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        LOG.info("Stopping on interrupt")
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
