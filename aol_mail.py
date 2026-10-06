#!/usr/bin/env python3
"""
aol_mail.py — AOL IMAP reader for MCP stack
Usage:
  python3 aol_mail.py                        # unread inbox (last 30 days)
  python3 aol_mail.py --search "jobs"        # keyword search
  python3 aol_mail.py --folder INBOX --all   # all inbox
  python3 aol_mail.py --unread               # unread only
  python3 aol_mail.py --days 60              # look back 60 days
  python3 aol_mail.py --id <UID>             # fetch full message by UID
"""

import argparse
import email
import imaplib
import os
import ssl
import subprocess
from datetime import datetime, timedelta
from email.header import decode_header

IMAP_HOST = "imap.aol.com"
IMAP_PORT = 993
CREDS_FILE = os.path.expanduser("~/.aol_imap.gpg")
PASSPHRASE_CMD = f"echo '{os.uname().nodename}-aol-imap-key'"


def get_credentials():
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
    user, pwd = result.split(":", 1)
    return user, pwd


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


def connect():
    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    user, pwd = get_credentials()
    mail.login(user, pwd)
    return mail


def search_inbox(args):
    mail = connect()
    folder = getattr(args, "folder", "INBOX")
    mail.select(folder, readonly=True)

    since_date = (datetime.now() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    criteria = [f"SINCE {since_date}"]

    if args.unread:
        criteria.append("UNSEEN")

    if args.search:
        criteria.append(f'TEXT "{args.search}"')

    query = " ".join(criteria) if len(criteria) > 1 else criteria[0]
    typ, data = mail.search(None, query)

    if typ != "OK" or not data[0]:
        print("No messages found.")
        mail.logout()
        return

    uids = data[0].split()
    # Most recent first, cap at 50
    uids = uids[::-1][:50]

    print(f"Found {len(uids)} message(s) matching criteria:\n")
    print(f"{'UID':<8} {'DATE':<22} {'FROM':<35} {'SUBJECT'}")
    print("-" * 110)

    for uid in uids:
        typ, msg_data = mail.fetch(
            uid, "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])"
        )
        if typ != "OK":
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = decode_str(msg.get("Subject", "(no subject)"))[:60]
        sender = decode_str(msg.get("From", ""))[:33]
        date = msg.get("Date", "")[:20]
        print(f"{uid.decode():<8} {date:<22} {sender:<35} {subject}")

    mail.logout()


def fetch_message(args):
    mail = connect()
    mail.select("INBOX", readonly=True)
    typ, msg_data = mail.fetch(str(args.id).encode(), "(RFC822)")
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
    # Trim excessive whitespace
    lines = [l.rstrip() for l in body.splitlines()]
    print("\n".join(lines[:200]))
    mail.logout()


def main():
    parser = argparse.ArgumentParser(description="AOL IMAP reader")
    parser.add_argument("--search", "-s", help="Keyword search in body/subject")
    parser.add_argument("--unread", "-u", action="store_true", help="Unread only")
    parser.add_argument(
        "--all", "-a", action="store_true", help="All messages (ignore unread filter)"
    )
    parser.add_argument(
        "--days", "-d", type=int, default=30, help="Look back N days (default 30)"
    )
    parser.add_argument(
        "--folder", "-f", default="INBOX", help="Folder/mailbox (default INBOX)"
    )
    parser.add_argument("--id", type=int, help="Fetch full message by UID")
    args = parser.parse_args()

    if args.id:
        fetch_message(args)
    else:
        search_inbox(args)


if __name__ == "__main__":
    main()
