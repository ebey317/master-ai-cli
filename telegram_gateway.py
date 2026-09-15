#!/usr/bin/env python3
"""Telegram gateway daemon for Master AI CLI.

Built on python-telegram-bot's Application/CommandHandler framework instead
of a hand-rolled getUpdates polling loop -- 2026-09-15 migration. That
library owns polling retry/backoff and command-name dispatch, which used to
be bespoke code here (a manual offset-tracked getUpdates loop plus an
if/elif string-matching `_handle_command`). The actual Sensei-facing logic
(model state, the REPL bridge, the free-text task path) is unchanged.

Credentials read from ~/.master_ai_keys:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=123456789      # default; optional comma-separated list

Run standalone:
    python3 telegram_gateway.py

Or as a systemd user service via telegram-gateway.service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import sensei_repl_bridge

LOG = logging.getLogger(__name__)
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
    """Free-text path: spawns headless_runner.py fresh per message (no
    persisted conversation state to reset -- see the /new handler's note)."""
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


_COMMAND_HELP = (
    "Commands:\n"
    "/help - show this list\n"
    "/status - gateway uptime + current model\n"
    "/model - show the model currently answering you\n"
    "/model <name> - switch models (e.g. /model glm-5.3-flash)\n"
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
# Deliberately excludes "clear"/"kick"/"x" (and handles "new" itself via a
# dedicated command below rather than piping the text through) -- these
# restart or exit the underlying engine process, which the bridge's pipes
# and prompt-detection logic aren't built to survive mid-command.
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


def _close_repl() -> None:
    global _repl
    with _repl_lock:
        if _repl is not None:
            try:
                _repl.close()
            except Exception:
                LOG.exception("error closing sensei repl during /new")
            _repl = None


def _is_allowed(update: Update, allowed_ids: list[str]) -> bool:
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if allowed_ids and chat_id not in allowed_ids:
        LOG.warning("Ignoring message from unallowed chat %s", chat_id)
        return False
    return True


def _log_incoming(update: Update) -> None:
    chat_id = str(update.effective_chat.id) if update.effective_chat else "?"
    user = update.effective_user
    text = update.message.text if update.message else ""
    LOG.info(
        "Processing message from %s (%s %s): %r",
        chat_id,
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
        (text or "")[:80],
    )


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
    # python-telegram-bot's own libraries are chatty at INFO; keep this
    # gateway's own logger at INFO but quiet the library internals down to
    # WARNING so the journal stays readable.
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    LOG.info(
        "Telegram gateway starting (python-telegram-bot); allowed chats: %s",
        allowed_ids,
    )
    _write_pid()

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        await update.message.reply_text(_COMMAND_HELP)

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        uptime_s = int(time.time() - _START_TIME)
        hours, rem = divmod(uptime_s, 3600)
        minutes, seconds = divmod(rem, 60)
        await update.message.reply_text(
            f"Gateway uptime: {hours}h {minutes}m {seconds}s\n"
            f"Current model: {_get_current_model()}"
        )

    async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        arg = " ".join(context.args).strip() if context.args else ""
        if not arg:
            await update.message.reply_text(f"Current model: {_get_current_model()}")
            return
        _set_current_model(arg)
        await update.message.reply_text(f"Model switched to: {arg}")

    async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        # Deliberately NOT sending Sensei's own "new"/"clear" text into the
        # bridge -- that command restarts the engine process from the
        # INSIDE (execvp), which the bridge's pipes and prompt-detection
        # logic aren't built to survive. Controlling the subprocess's
        # lifecycle ourselves is simpler and predictable: the next bridged
        # command lazily spins up a fresh one. Free-text chat already
        # starts a brand-new, history-less process per message.
        await asyncio.to_thread(_close_repl)
        await update.message.reply_text(
            "New session started. Sensei's REPL bridge will spin up fresh on your next command."
        )

    async def generic_command_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Catches any /command not claimed by an explicit handler above --
        checked against the Sensei REPL bridge allowlist."""
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        text = update.message.text or ""
        parts = text.strip().split(None, 1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        bridged = (cmd + (f" {arg}" if arg else "")).strip()
        target = _sensei_command_target(bridged)
        if target is not None:
            reply = await asyncio.to_thread(_run_sensei_repl_command, target)
        else:
            reply = f"Unknown command: /{cmd}\n\n{_COMMAND_HELP}"
        await update.message.reply_text(reply)

    async def free_text_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _is_allowed(update, allowed_ids):
            return
        _log_incoming(update)
        text = update.message.text or ""
        reply = await asyncio.to_thread(_run_sensei_task, text)
        await update.message.reply_text(reply)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOG.error(
            "Unhandled error processing update %r", update, exc_info=context.error
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["help", "start"], help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    # Registered after the named handlers above -- PTB tries handlers in
    # registration order within a group and stops at the first match, so
    # this only fires for commands none of those already claimed.
    app.add_handler(MessageHandler(filters.COMMAND, generic_command_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
    app.add_error_handler(on_error)

    try:
        # run_polling() owns retry/backoff on network hiccups (read
        # timeouts, connection resets) internally -- the bespoke getUpdates
        # loop this replaced only logged those and hoped for the best.
        app.run_polling(drop_pending_updates=False, close_loop=False)
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
