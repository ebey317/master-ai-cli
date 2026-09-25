#!/usr/bin/env python3
# Sensei memory index — SQLite FTS5 over Sensei's text stores.
# Incremental: each source carries a last_offset cursor so re-runs are cheap.
# Usage:
#   python3 sensei_memory_index.py build               # incremental ingest
#   python3 sensei_memory_index.py search "query…"     # ranked snippet hits
#   python3 sensei_memory_index.py stats               # row counts per source

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".sensei_memory_index.sqlite"

# (path, kind) — kind="jsonl" appends line-by-line; kind="text" re-chunks on change.
SOURCES: list[tuple[Path, str]] = [
    (Path.home() / ".master_ai_audit_typed.jsonl", "jsonl"),
    (Path.home() / ".master_ai_harvest.jsonl", "jsonl"),
    (Path.home() / ".master_ai_memory", "text"),
    (Path.home() / ".master_ai_about_elijah", "text"),
    (Path.home() / ".master_ai_active_task", "text"),
    (Path.home() / ".master_ai_approved", "text"),
    (Path.home() / ".master_ai_audit.log", "text"),
]

TEXT_CHUNK_LINES = 40
SEARCH_LIMIT_DEFAULT = 20


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
            id        INTEGER PRIMARY KEY,
            source    TEXT NOT NULL,
            line_no   INTEGER,
            ts        INTEGER,
            body      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS docs_source_idx ON docs(source);

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            body,
            content='docs',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
            INSERT INTO docs_fts(rowid, body) VALUES (new.id, new.body);
        END;
        CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, body) VALUES('delete', old.id, old.body);
        END;
        CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, body) VALUES('delete', old.id, old.body);
            INSERT INTO docs_fts(rowid, body) VALUES (new.id, new.body);
        END;

        CREATE TABLE IF NOT EXISTS sources (
            path             TEXT PRIMARY KEY,
            kind             TEXT NOT NULL,
            last_offset      INTEGER DEFAULT 0,
            last_size        INTEGER DEFAULT 0,
            last_indexed_at  INTEGER
        );
        """
    )
    conn.commit()


def extract_ts(line: str) -> int | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    for key in ("ts", "timestamp", "time", "t"):
        v = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def ingest_jsonl(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    row = conn.execute(
        "SELECT last_offset FROM sources WHERE path=?", (str(path),)
    ).fetchone()
    last_offset = row[0] if row else 0
    size = path.stat().st_size
    if last_offset > size:
        last_offset = 0  # file rotated/truncated; re-index from start
    if last_offset == size:
        return 0

    added = 0
    with path.open("rb") as fh:
        fh.seek(last_offset)
        # Approximate line_no — count lines processed in THIS run, not absolute.
        line_no_in_batch = 0
        for raw in fh:
            line_no_in_batch += 1
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                continue
            if not line.strip():
                continue
            conn.execute(
                "INSERT INTO docs(source, line_no, ts, body) VALUES (?, ?, ?, ?)",
                (str(path), line_no_in_batch, extract_ts(line), line),
            )
            added += 1
        new_offset = fh.tell()

    conn.execute(
        """
        INSERT INTO sources(path, kind, last_offset, last_size, last_indexed_at)
        VALUES (?, 'jsonl', ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            last_offset=excluded.last_offset,
            last_size=excluded.last_size,
            last_indexed_at=excluded.last_indexed_at
        """,
        (str(path), new_offset, size, int(time.time())),
    )
    conn.commit()
    return added


def ingest_text(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    size = path.stat().st_size
    row = conn.execute(
        "SELECT last_size FROM sources WHERE path=?", (str(path),)
    ).fetchone()
    last_size = row[0] if row else -1
    if last_size == size:
        return 0

    # Text files: re-chunk on any change. Small; rebuilding is fine.
    conn.execute("DELETE FROM docs WHERE source=?", (str(path),))
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    added = 0
    for i in range(0, len(lines), TEXT_CHUNK_LINES):
        chunk = "\n".join(lines[i : i + TEXT_CHUNK_LINES]).strip()
        if not chunk:
            continue
        conn.execute(
            "INSERT INTO docs(source, line_no, ts, body) VALUES (?, ?, NULL, ?)",
            (str(path), i + 1, chunk),
        )
        added += 1

    conn.execute(
        """
        INSERT INTO sources(path, kind, last_offset, last_size, last_indexed_at)
        VALUES (?, 'text', 0, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            last_size=excluded.last_size,
            last_indexed_at=excluded.last_indexed_at
        """,
        (str(path), size, int(time.time())),
    )
    conn.commit()
    return added


def cmd_build() -> int:
    started = time.time()
    with connect() as conn:
        init_schema(conn)
        totals: list[tuple[str, int]] = []
        for path, kind in SOURCES:
            if kind == "jsonl":
                n = ingest_jsonl(conn, path)
            else:
                n = ingest_text(conn, path)
            totals.append((str(path), n))
        row_count = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    elapsed = time.time() - started
    print(f"sensei_memory_index: built in {elapsed:.2f}s  total_rows={row_count}")
    for path_str, n in totals:
        marker = "+" if n else "."
        print(f"  {marker} {n:>6}  {path_str}")
    return 0


def cmd_search(query: str, limit: int = SEARCH_LIMIT_DEFAULT) -> int:
    with connect() as conn:
        init_schema(conn)
        try:
            rows = conn.execute(
                """
                SELECT docs.source,
                       docs.line_no,
                       snippet(docs_fts, 0, '«', '»', '…', 12)
                FROM docs_fts
                JOIN docs ON docs.id = docs_fts.rowid
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"FTS5 error: {e}", file=sys.stderr)
            return 2
    if not rows:
        print("(no hits)")
        return 1
    for source, line_no, snip in rows:
        short = Path(source).name
        print(f"{short}:{line_no}  {snip}")
    return 0


def cmd_stats() -> int:
    with connect() as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n
            FROM docs
            GROUP BY source
            ORDER BY n DESC
            """
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"total rows: {total}")
    for source, n in rows:
        print(f"  {n:>8}  {Path(source).name}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help", "help"}:
        print(__doc__ or "see source", file=sys.stderr)
        print("commands: build | search <query> | stats", file=sys.stderr)
        return 64
    cmd = argv[1]
    if cmd == "build":
        return cmd_build()
    if cmd == "search":
        if len(argv) < 3:
            print("search needs a query string", file=sys.stderr)
            return 64
        return cmd_search(" ".join(argv[2:]))
    if cmd == "stats":
        return cmd_stats()
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
