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
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger(__name__)
UPDATE_INTERVAL_SECONDS = 2
TOKEN_PATH = Path.home() / ".master_ai_keys"
LOG_FILE = Path.home() / ".master_ai_telegram_gateway.log"
PID_FILE = Path.home() / ".master_ai_telegram_gateway.pid"
MASTER_AI_DIR = Path(__file__).resolve().parent


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


def _run_sensei_task(task_text: str, max_turns: int = 5) -> str:
    env = os.environ.copy()
    env["SENSEI_TUI"] = "0"
    model = os.environ.get("TELEGRAM_SENSEI_MODEL", "glm-5.3-flash")
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
