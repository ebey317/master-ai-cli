#!/usr/bin/env python3
"""
outlook_mail.py — Outlook/Office365 IMAP reader via OAuth2 (Thunderbird tokens)
Credentials: ~/.outlook_imap.gpg (encrypted refresh token)

Usage:
  python3 outlook_mail.py                        # unread inbox, last 30 days
  python3 outlook_mail.py --search "job"         # keyword search
  python3 outlook_mail.py --all --days 60        # all mail, 60 days
  python3 outlook_mail.py --unread               # unread only
  python3 outlook_mail.py --id <UID>             # fetch full message by UID
"""

import argparse
import base64
import email
import imaplib
import json
import os
import ssl
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header

IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
CREDS_FILE = os.path.expanduser("~/.outlook_imap.gpg")
PASSPHRASE_CMD = f"echo '{os.uname().nodename}-outlook-imap-key'"
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def get_stored_creds():
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
            "grant_type": "refresh_token",
            "scope": IMAP_SCOPE,
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
    return result["access_token"], result.get("refresh_token", refresh_token)


def connect():
    user, refresh_token = get_stored_creds()
    access_token, new_refresh = refresh_access_token(refresh_token)

    # Save new refresh token if rotated
    if new_refresh != refresh_token:
        save_refresh_token(user, new_refresh)

    auth_bytes = base64.b64encode(
        f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()
    )

    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)

    # Send AUTHENTICATE XOAUTH2 inline (avoids imaplib challenge/response bug)
    tag = mail._new_tag().decode()
    cmd = f"{tag} AUTHENTICATE XOAUTH2 {auth_bytes.decode()}\r\n"
    mail.send(cmd.encode())
    resp = mail._get_response()
    if b"OK" not in resp:
        raise Exception(f"Auth failed: {resp}")
    mail.state = "AUTH"
    return mail, user


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


def search_inbox(args):
    mail, user = connect()
    print(f"Connected: {user}\n")

    folder = getattr(args, "folder", "INBOX")
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

    uids = data[0].split()[::-1][:50]  # newest first, cap 50
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
    mail, _ = connect()
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


def main():
    parser = argparse.ArgumentParser(description="Outlook IMAP reader (OAuth2)")
    parser.add_argument("--search", "-s", help="Keyword search")
    parser.add_argument("--unread", "-u", action="store_true", help="Unread only")
    parser.add_argument("--all", "-a", action="store_true", help="All messages")
    parser.add_argument("--days", "-d", type=int, default=30, help="Look back N days")
    parser.add_argument(
        "--folder", "-f", default="INBOX", help="Folder (default INBOX)"
    )
    parser.add_argument("--id", type=int, help="Fetch full message by UID")
    args = parser.parse_args()

    if args.id:
        fetch_message(args)
    else:
        search_inbox(args)


if __name__ == "__main__":
    main()
