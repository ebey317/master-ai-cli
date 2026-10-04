"""
Schema initialization for Sensei state database.
Separated to allow registry to call it without circular imports.
"""
import sqlite3

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT,
    model TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    tool_results_json TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    description TEXT,
    code TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id UNINDEXED,
    title,
    content,
    tokenize='porter'
);

CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(session_id, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    DELETE FROM sessions_fts WHERE session_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    UPDATE sessions_fts SET title = new.title WHERE session_id = old.id;
END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Initialize schema if needed. Idempotent."""
    conn.executescript(DDL)
    # Schema version tracking could be added here
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO _schema_version VALUES (?)", (SCHEMA_VERSION,)
    )
    conn.commit()
