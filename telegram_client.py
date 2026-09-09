"""telegram_client.py — Sensei CLI Telegram connector

Minimal, no-dependencies (stdlib) wrapper around Telegram Bot API for
one-way outbound messages. Inbound polling is deliberately out of scope;
Sensei will call this via SEND_TELEGRAM: <chat_id> <message> directives.
"""

import json
import urllib.request
import urllib.error
from pathlib import Path


def _get_token():
    """Read TELEGRAM_BOT_TOKEN from ~/.master_ai_keys (plain KEY=VALUE)."""
    keyfile = Path.home() / ".master_ai_keys"
    try:
        text = keyfile.read_text()
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "TELEGRAM_BOT_TOKEN":
            return v.strip()
    return None


def _get_default_chat_id():
    """Read TELEGRAM_CHAT_ID from ~/.master_ai_keys (plain KEY=VALUE)."""
    keyfile = Path.home() / ".master_ai_keys"
    try:
        text = keyfile.read_text()
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "TELEGRAM_CHAT_ID":
            return v.strip()
    return None


def send_message(chat_id, text, token=None, silent=False):
    """Send a plain-text message. Returns {"ok": bool, "message_id": int|None, "error": str|None}."""
    chat_id = chat_id or _get_default_chat_id()
    token = token or _get_token()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not found in ~/.master_ai_keys", "message_id": None}
    if not chat_id:
        return {"ok": False, "error": "chat_id is empty and no TELEGRAM_CHAT_ID default set", "message_id": None}
    if not text:
        return {"ok": False, "error": "message text is empty", "message_id": None}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if silent:
        payload["disable_notification"] = True
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            return {"ok": True, "message_id": body["result"].get("message_id"), "error": None}
        return {"ok": False, "error": body.get("description", "unknown Telegram error"), "message_id": None}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("description", str(e))
        except Exception:
            detail = str(e)
        return {"ok": False, "error": detail, "message_id": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "message_id": None}


def get_updates(token=None, limit=10):
    """Pull recent updates to discover chat_id(s). Returns list of {chat_id, username, text}."""
    token = token or _get_token()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not found"}
    url = f"https://api.telegram.org/bot{token}/getUpdates?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            return {"ok": False, "error": body.get("description", "unknown error")}
        out = []
        for upd in body.get("result", []):
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            from_user = msg.get("from", {})
            out.append({
                "chat_id": chat.get("id"),
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
                "text": msg.get("text", ""),
            })
        return {"ok": True, "updates": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        chat_id = sys.argv[2]
        text = " ".join(sys.argv[3:])
        print(json.dumps(send_message(chat_id, text), indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "updates":
        print(json.dumps(get_updates(), indent=2))
    else:
        print("usage: python3 telegram_client.py send <chat_id> <message>")
        print("       python3 telegram_client.py updates")
