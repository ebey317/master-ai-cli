#!/usr/bin/env python3
"""
ValueMark / Ops Number — inbound SMS/Telegram handler.

Receives messages from customers and crews, parses them into jobs/updates,
and stores them in a local SQLite database. Works with Hermes' native SMS
gateway or Telegram. When Twilio is configured, this becomes the SMS backend.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".valumark" / "ops_number.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_number TEXT UNIQUE NOT NULL,
            customer_phone TEXT,
            customer_name TEXT,
            description TEXT,
            address TEXT,
            status TEXT DEFAULT 'open',
            accounting_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            sender_role TEXT CHECK(sender_role IN ('customer', 'crew', 'office', 'ai')),
            phone TEXT,
            body TEXT,
            media_urls TEXT,
            received_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            source TEXT,
            body TEXT,
            qualified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_phone ON jobs(customer_phone);
        CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(job_id);
        """
    )
    conn.commit()
    conn.close()


def _extract_job_number(text: str) -> str | None:
    m = re.search(r"\b(VM|JOB|#)?\s*(\d{4,6})\b", text, re.IGNORECASE)
    if m:
        return m.group(2)
    return None


def _classify_sender(text: str) -> str:
    t = text.lower()
    crew_markers = [
        "done",
        "finished",
        "receipt",
        "photo",
        "on site",
        "heading",
        "completed",
        "parts",
    ]
    if any(m in t for m in crew_markers):
        return "crew"
    customer_markers = [
        "book",
        "schedule",
        "quote",
        "estimate",
        "need",
        "broken",
        "leak",
        "no heat",
        "ac",
    ]
    if any(m in t for m in customer_markers):
        return "customer"
    return "unknown"


def _create_job(phone: str, body: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ts = _now()
    job_number = f"VM{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')}"
    # Very basic description extraction: first sentence or first 80 chars.
    description = body.strip().split("\n")[0][:120]
    cur.execute(
        "INSERT INTO jobs (job_number, customer_phone, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_number, phone, description, "open", ts, ts),
    )
    job_id = cur.lastrowid
    cur.execute(
        "INSERT INTO messages (job_id, sender_role, phone, body, received_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, "customer", phone, body, ts),
    )
    conn.commit()
    conn.close()
    return job_number


def _add_message_to_job(job_number: str, phone: str, body: str, role: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM jobs WHERE job_number = ?", (job_number,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    job_id = row[0]
    ts = _now()
    cur.execute(
        "INSERT INTO messages (job_id, sender_role, phone, body, received_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, role, phone, body, ts),
    )
    cur.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts, job_id))
    conn.commit()
    conn.close()
    return True


def handle_inbound(
    phone: str, body: str, source: str = "sms", media_urls: list[str] | None = None
) -> str:
    """Main entry point for an inbound message. Returns the reply text."""
    init_db()
    role = _classify_sender(body)
    media_json = json.dumps(media_urls or [])

    job_number = _extract_job_number(body)
    if job_number:
        ok = _add_message_to_job(job_number, phone, body, role)
        if ok:
            return f"Thanks — added to job {job_number}. We'll update you shortly."
        # If job number referenced but not found, treat as new lead.

    if role == "customer":
        new_job = _create_job(phone, body)
        return (
            f"Thanks for reaching out. Your job number is {new_job}. "
            "A crew will be assigned and you'll get updates here."
        )
    if role == "crew":
        # Standalone crew update without job number — create or attach to latest open job from this phone.
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, job_number FROM jobs WHERE customer_phone = ? OR EXISTS (SELECT 1 FROM messages WHERE messages.job_id = jobs.id AND messages.phone = ?) ORDER BY updated_at DESC LIMIT 1",
            (phone, phone),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            _add_message_to_job(row[1], phone, body, "crew")
            return f"Update recorded on job {row[1]}."
        return "Got it — but I don't see a job number. Reply with the job number or call the office."
    # Save as lead, ask for clarification.
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ts = _now()
    cur.execute(
        "INSERT INTO leads (phone, source, body, created_at) VALUES (?, ?, ?, ?)",
        (phone, source, body, ts),
    )
    conn.commit()
    conn.close()
    return "Thanks — are you a customer booking work, or a crew sending a field update?"


def get_open_jobs(limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM jobs WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_job_summary(job_number: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE job_number = ?", (job_number,))
    job = cur.fetchone()
    if not job:
        conn.close()
        return None
    cur.execute(
        "SELECT * FROM messages WHERE job_id = ? ORDER BY received_at", (job["id"],)
    )
    messages = [dict(r) for r in cur.fetchall()]
    conn.close()
    result = dict(job)
    result["messages"] = messages
    return result


if __name__ == "__main__":
    # Simple CLI smoke test.
    import sys

    if len(sys.argv) < 3:
        print("Usage: ops_number_handler.py <phone> <message>")
        sys.exit(1)
    print(handle_inbound(sys.argv[1], sys.argv[2]))
