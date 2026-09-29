#!/usr/bin/env python3
"""
gmail_mail.py — Gmail IMAP reader + SMTP sender via OAuth2 (Thunderbird tokens)
Credentials: ~/.gmail_imap.gpg (encrypted: email:refresh_token)

Bootstrap (one-time): python3 ~/scripts/gmail_init.py

Usage — READ:
  python3 gmail_mail.py                        # unread inbox, last 30 days
  python3 gmail_mail.py --search "job"         # keyword search
  python3 gmail_mail.py --all --days 60        # all mail, 60 days
  python3 gmail_mail.py --unread               # unread only
  python3 gmail_mail.py --id <UID>             # fetch full message by UID
  python3 gmail_mail.py --folder "[Gmail]/Sent Mail"

Usage — SEND:
  python3 gmail_mail.py --send --to "addr@example.com" --subject "Hello" --body "Message"
  python3 gmail_mail.py --send --to "addr@example.com" --subject "Hi" --body-file /tmp/body.txt
"""

import argparse
import base64
import email
import email.mime.multipart
import email.mime.text
import imaplib
import json
import os
import smtplib
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header

# ── Config ────────────────────────────────────────────────────────────────────
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
CREDS_FILE = os.path.expanduser("~/.gmail_imap.gpg")
PASSPHRASE_CMD = f"echo '{os.uname().nodename}-gmail-imap-key'"

# Thunderbird's registered Google OAuth2 client (public — in Thunderbird open source)
CLIENT_ID = "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com"
CLIENT_SECRET = "kSmqreRr0qwBWJgbf5Y-PjSU"
TOKEN_ENDPOINT = "https://www.googleapis.com/oauth2/v3/token"


# ── Credential helpers ────────────────────────────────────────────────────────
def get_stored_creds():
    if not os.path.exists(CREDS_FILE):
        print("ERROR: ~/.gmail_imap.gpg not found.", file=sys.stderr)
        print("Run first: python3 ~/scripts/gmail_init.py", file=sys.stderr)
        sys.exit(1)
    passphrase = subprocess.check_output(PASSPHRASE_CMD, shell=True).decode().strip()
    result = (
        subprocess.check_output(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--decrypt",
                "--passphrase",
                passphrase,
                CREDS_FILE,
            ],
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )
    user, token = result.split(":", 1)
    return user, token


def save_refresh_token(user, new_refresh_token):
    passphrase = subprocess.check_output(PASSPHRASE_CMD, shell=True).decode().strip()
    data = f"{user}:{new_refresh_token}"
    proc = subprocess.Popen(
        [
            "gpg",
            "--batch",
            "--yes",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--passphrase",
            passphrase,
            "-o",
            CREDS_FILE,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc.communicate(data.encode())
    os.chmod(CREDS_FILE, 0o600)


def refresh_access_token(refresh_token):
    data = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise RuntimeError(f"Token refresh failed: {result}")
    return result["access_token"], result.get("refresh_token", refresh_token)


def get_xoauth2_string(user, access_token):
    return base64.b64encode(
        f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()
    ).decode()


# ── IMAP connect ──────────────────────────────────────────────────────────────
def connect():
    user, refresh_token = get_stored_creds()
    access_token, new_refresh = refresh_access_token(refresh_token)
    if new_refresh != refresh_token:
        save_refresh_token(user, new_refresh)

    auth_b64 = get_xoauth2_string(user, access_token)
    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)

    # AUTHENTICATE XOAUTH2 inline — read until tagged response
    # (Gmail sends an untagged * CAPABILITY banner before the tagged OK)
    tag = mail._new_tag().decode()
    tag_bytes = tag.encode()
    mail.send(f"{tag} AUTHENTICATE XOAUTH2 {auth_b64}\r\n".encode())
    while True:
        resp = mail._get_response()
        if resp.startswith(b"*") or resp.startswith(b"+"):
            continue  # untagged / continuation — keep reading
        # Tagged response — should start with our tag
        if b"OK" in resp:
            break
        raise Exception(f"IMAP auth failed: {resp}")
    mail.state = "AUTH"
    return mail, user, access_token


# ── Decode helpers ────────────────────────────────────────────────────────────
def decode_str(s):
    if s is None:
        return ""
    parts = decode_header(s)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    return part.get_payload(decode=True).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


# ── IMAP read ─────────────────────────────────────────────────────────────────
def search_inbox(args):
    mail, user, _ = connect()
    print(f"Connected: {user}\n")

    folder = args.folder
    # Gmail folders need quoting when they contain spaces
    mail.select(f'"{folder}"', readonly=True)

    since_date = (datetime.now() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    criteria = [f"SINCE {since_date}"]
    if args.unread:
        criteria.append("UNSEEN")
    if args.search:
        criteria.append(f'TEXT "{args.search}"')

    query = "(" + " ".join(criteria) + ")" if len(criteria) > 1 else criteria[0]
    typ, data = mail.uid("SEARCH", None, query)

    if typ != "OK" or not data[0]:
        print("No messages found.")
        mail.logout()
        return

    uids = data[0].split()[::-1][:50]
    print(f"Found {len(uids)} message(s):\n")
    print(f"{'UID':<10} {'DATE':<22} {'FROM':<35} {'SUBJECT'}")
    print("-" * 115)

    for uid in uids:
        typ, msg_data = mail.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])"
        )
        if typ != "OK":
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = decode_str(msg.get("Subject", "(no subject)"))[:60]
        sender = decode_str(msg.get("From", ""))[:33]
        date = msg.get("Date", "")[:20]
        print(f"{uid.decode():<10} {date:<22} {sender:<35} {subject}")

    mail.logout()


def fetch_message(args):
    mail, _, _ = connect()
    mail.select("INBOX", readonly=True)
    typ, msg_data = mail.uid("FETCH", str(args.id).encode(), "(RFC822)")
    if typ != "OK":
        print(f"Could not fetch UID {args.id}")
        mail.logout()
        return
    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)
    print(f"From:    {decode_str(msg.get('From', ''))}")
    print(f"Date:    {msg.get('Date', '')}")
    print(f"Subject: {decode_str(msg.get('Subject', ''))}")
    print(f"To:      {decode_str(msg.get('To', ''))}")
    print("-" * 80)
    body = get_body(msg)
    lines = [l.rstrip() for l in body.splitlines()]
    print("\n".join(lines[:200]))
    mail.logout()


# ── SMTP send ─────────────────────────────────────────────────────────────────
def send_message(args):
    user, refresh_token = get_stored_creds()
    access_token, new_refresh = refresh_access_token(refresh_token)
    if new_refresh != refresh_token:
        save_refresh_token(user, new_refresh)

    if not args.to:
        print("ERROR: --to is required for --send", file=sys.stderr)
        sys.exit(1)
    if not args.subject:
        print("ERROR: --subject is required for --send", file=sys.stderr)
        sys.exit(1)

    # Build body
    body = args.body or ""
    if args.body_file:
        with open(args.body_file) as f:
            body = f.read()

    # Compose
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["From"] = user
    msg["To"] = args.to
    msg["Subject"] = args.subject
    if args.reply_to:
        msg["In-Reply-To"] = args.reply_to
        msg["References"] = args.reply_to

    auth_b64 = get_xoauth2_string(user, access_token)
    ctx = ssl.create_default_context()

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ctx)
        smtp.ehlo()
        # XOAUTH2 auth
        code, resp = smtp.docmd("AUTH", f"XOAUTH2 {auth_b64}")
        if code != 235:
            raise RuntimeError(f"SMTP AUTH failed ({code}): {resp}")
        smtp.sendmail(user, [args.to], msg.as_bytes())

    print(f"Sent to {args.to}: {args.subject}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Gmail IMAP reader + SMTP sender (OAuth2)"
    )

    # Read flags
    parser.add_argument("--search", "-s", help="Keyword search in body/subject")
    parser.add_argument("--unread", "-u", action="store_true", help="Unread only")
    parser.add_argument("--all", "-a", action="store_true", help="All messages")
    parser.add_argument("--days", "-d", type=int, default=30, help="Look back N days")
    parser.add_argument(
        "--folder", "-f", default="INBOX", help="Folder (default INBOX)"
    )
    parser.add_argument("--id", type=int, help="Fetch full message by UID")

    # Send flags
    parser.add_argument("--send", action="store_true", help="Send a message")
    parser.add_argument("--to", help="Recipient address")
    parser.add_argument("--subject", help="Subject line")
    parser.add_argument("--body", help="Message body (inline)")
    parser.add_argument("--body-file", help="Message body from file")
    parser.add_argument("--reply-to", help="Message-ID to reply to")

    args = parser.parse_args()

    if args.send:
        send_message(args)
    elif args.id:
        fetch_message(args)
    else:
        search_inbox(args)


if __name__ == "__main__":
    main()
