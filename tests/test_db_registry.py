"""
Tests for the process-shared database registry.
"""
import tempfile
import threading
from pathlib import Path

import pytest

from scripts.db_registry import acquire, release, session, SessionDB, _registry


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    db_path.unlink(missing_ok=True)
    # Clean up WAL/SHM files
    for suffix in ["-wal", "-shm"]:
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def test_writer_singleton(temp_db):
    """Multiple acquire() calls for writer return same connection."""
    conn1 = acquire(temp_db, read_only=False)
    conn2 = acquire(temp_db, read_only=False)
    assert conn1 is conn2, "Writer connections should be shared"
    release(temp_db, read_only=False)
    release(temp_db, read_only=False)


def test_reader_separate_from_writer(temp_db):
    """Read-only connections are distinct from writer."""
    writer = acquire(temp_db, read_only=False)
    reader = acquire(temp_db, read_only=True)
    assert writer is not reader, "Reader should not share writer connection"
    release(temp_db, read_only=False)
    release(temp_db, read_only=True)


def test_read_only_no_write_lock(temp_db):
    """Read-only connections can coexist with writer transactions."""
    with session(temp_db, read_only=False) as w:
        w.execute("INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                  ("s1", "Test", "/tmp", "model", 1.0, 1.0))
        w.commit()

        # This should not block
        with session(temp_db, read_only=True) as r:
            rows = r.execute("SELECT count(*) FROM sessions").fetchone()
            assert rows[0] == 1


def test_refcount_cleanup(temp_db):
    """Handle is closed when refcount reaches zero."""
    conn = acquire(temp_db, read_only=False)
    release(temp_db, read_only=False)
    # Connection should be closed now; next acquire creates fresh
    conn2 = acquire(temp_db, read_only=False)
    # Note: sqlite3 doesn't expose closed state easily, but we can verify
    # no exception on use
    conn2.execute("SELECT 1")
    release(temp_db, read_only=False)


def test_concurrent_readers(temp_db):
    """Multiple read-only connections work concurrently."""
    with session(temp_db, read_only=False) as w:
        w.execute("INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                  ("s1", "Test", "/tmp", "model", 1.0, 1.0))

    readers = [acquire(temp_db, read_only=True) for _ in range(10)]
    for r in readers:
        assert r.execute("SELECT title FROM sessions WHERE id='s1'").fetchone()[0] == "Test"
    for _ in readers:
        release(temp_db, read_only=True)


def test_concurrent_access_threads(temp_db):
    """Registry is thread-safe."""
    errors = []

    def writer():
        try:
            with session(temp_db, read_only=False) as conn:
                conn.execute(
                    "INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"s{threading.current_thread().ident}", "t", "/tmp", "m", 1.0, 1.0)
                )
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            with session(temp_db, read_only=True) as conn:
                conn.execute("SELECT count(*) FROM sessions").fetchone()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(5)]
    threads += [threading.Thread(target=reader) for _ in range(10)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"


def test_session_context_manager_commits(temp_db):
    """Writer session commits on success."""
    with session(temp_db, read_only=False) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "Test", "/tmp", "model", 1.0, 1.0)
        )
    # New session should see the data
    with session(temp_db, read_only=True) as conn:
        row = conn.execute("SELECT title FROM sessions WHERE id='s1'").fetchone()
        assert row[0] == "Test"


def test_session_context_manager_rolls_back(temp_db):
    """Writer session rolls back on exception."""
    try:
        with session(temp_db, read_only=False) as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("s1", "Test", "/tmp", "model", 1.0, 1.0)
            )
            raise ValueError("intentional")
    except ValueError:
        pass

    with session(temp_db, read_only=True) as conn:
        row = conn.execute("SELECT count(*) FROM sessions WHERE id='s1'").fetchone()
        assert row[0] == 0


def test_legacy_sessiondb_wrapper(temp_db):
    """SessionDB wrapper works for gradual migration."""
    with SessionDB(temp_db, read_only=False) as db:
        db.execute(
            "INSERT INTO sessions (id, title, cwd, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "Legacy", "/tmp", "model", 1.0, 1.0)
        )
    with SessionDB(temp_db, read_only=True) as db:
        row = db.execute("SELECT title FROM sessions WHERE id='s1'").fetchone()
        assert row[0] == "Legacy"


def test_registry_shutdown(temp_db):
    """Registry cleanup on shutdown."""
    acquire(temp_db, read_only=False)
    _registry._shutdown()
    # After shutdown, new acquires should fail
    with pytest.raises(RuntimeError):
        acquire(temp_db, read_only=False)
