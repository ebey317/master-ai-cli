#!/usr/bin/env python3
"""Scan local Thunderbird mail caches for work-related emails.

No server or credentials needed — reads the mbox files directly from
~/.thunderbird/<profile>/{ImapMail,Mail}/.../INBOX.
"""

import argparse
import email
import mailbox
import os
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_date(date_str: str) -> datetime | None:
    """Best-effort parse of an email Date header."""
    if not date_str:
        return None
    # Try a few common formats.
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    # Fallback: extract any date-ish tokens and try again.
    m = re.search(
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
        date_str,
    )
    if m:
        try:
            return datetime.strptime(m.group(0), "%d %b %Y")
        except ValueError:
            pass
    return None


def decode_header(hdr: str) -> str:
    """Decode MIME-encoded headers like =?UTF-8?B?...?=."""
    if not hdr:
        return ""
    parts = []
    for decoded, charset in email.header.decode_header(hdr):
        if isinstance(decoded, bytes):
            try:
                parts.append(decoded.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(decoded.decode("utf-8", errors="replace"))
        else:
            parts.append(decoded)
    return "".join(parts)


def find_inbox_files(thunderbird_dir: Path) -> Iterator[Path]:
    """Yield likely Inbox mbox paths under a Thunderbird profile."""
    if not thunderbird_dir.exists():
        return
    for sub in thunderbird_dir.iterdir():
        if not sub.is_dir():
            continue
        for base in ("ImapMail", "Mail"):
            base_dir = sub / base
            if not base_dir.exists():
                continue
            for root, _dirs, files in os.walk(base_dir):
                for name in files:
                    if name.lower() == "inbox":
                        yield Path(root) / name


WORK_KEYWORDS = re.compile(
    r"\b(job|jobs|hiring|career|position|application|applied|interview|"
    r"offer|recruiter|indeed|linkedin|ziprecruiter|technician|hvac|"
    r"installer|service|mechanical|maintenance|opportunity|resume|"
    r"staffing|employment|employer|work)\b",
    re.IGNORECASE,
)


def is_work_related(msg: email.message.EmailMessage) -> bool:
    """Return True if the email looks work-related."""
    text = ""
    for hdr in ("Subject", "From", "To"):
        text += decode_header(msg.get(hdr, "")) + " "
    # Also include a snippet of the body.
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text += payload.decode("utf-8", errors="replace")[:500]
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text += payload.decode("utf-8", errors="replace")[:500]
    except Exception:
        pass
    return bool(WORK_KEYWORDS.search(text))


def main():
    parser = argparse.ArgumentParser(
        description="Scan Thunderbird local mail for work-related emails"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="Only show emails from the last N days"
    )
    parser.add_argument(
        "--thunderbird-dir",
        default=str(Path.home() / ".thunderbird"),
        help="Path to Thunderbird directory",
    )
    parser.add_argument(
        "--max-body", type=int, default=400, help="Max body chars to display"
    )
    parser.add_argument(
        "--all", action="store_true", help="Show all emails, not just work-related"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Show sender/subject summary only"
    )
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    thunderbird_dir = Path(args.thunderbird_dir)
    found = []

    for inbox_path in find_inbox_files(thunderbird_dir):
        try:
            mbox = mailbox.mbox(str(inbox_path))
        except Exception as e:
            print(f"[skip] {inbox_path}: {e}")
            continue
        for msg in mbox:
            date = parse_date(msg.get("Date", ""))
            if date is None:
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date < cutoff:
                continue
            if not args.all and not is_work_related(msg):
                continue
            body = ""
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="replace")[
                                    : args.max_body
                                ]
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")[
                            : args.max_body
                        ]
            except Exception:
                pass
            found.append(
                {
                    "folder": str(inbox_path.relative_to(thunderbird_dir)),
                    "date": date,
                    "from": decode_header(msg.get("From", "")),
                    "subject": decode_header(msg.get("Subject", "")),
                    "body": body,
                }
            )

    found.sort(key=lambda x: x["date"], reverse=True)

    label = "email" if args.all else "work-related email"
    print(f"Found {len(found)} {label}(s) in the last {args.days} days\n")

    if args.summary:
        for item in found:
            date_str = item["date"].strftime("%Y-%m-%d %H:%M")
            folder = item["folder"].split("/")[0]  # profile
            acct = (
                item["folder"].split("/")[2]
                if len(item["folder"].split("/")) > 2
                else "?"
            )
            print(
                f"{date_str} | {acct:20} | {item['from'][:40]:40} | {item['subject'][:60]}"
            )
    else:
        for item in found:
            print(f"--- {item['folder']} ---")
            print(f"Date:    {item['date'].isoformat()}")
            print(f"From:    {item['from']}")
            print(f"Subject: {item['subject']}")
            if item["body"]:
                print(f"Body:    {item['body'][: args.max_body].strip()}")
            print()


if __name__ == "__main__":
    main()
