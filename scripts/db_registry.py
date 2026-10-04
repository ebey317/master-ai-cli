"""
Process-shared SQLite connection registry for Sensei.

Provides a single writer connection per database path per process with refcounting,
and lightweight read-only connections that don't acquire write locks or start
background threads. Mirrors the upstream hermes_state_registry pattern.
"""
from __future__ import annotations

import atexit
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .db_schema import ensure_schema  # assumes a schema module exists


class _ConnectionHandle:
    """Wrapper around a sqlite3 connection with refcounting and cleanup."""

    def __init__(self, path: Path, read_only: bool):
        self.path = path
        self.read_only = read_only
        self._refcount = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"Handle for {self.path} already closed")
            if self._conn is None:
                self._conn = self._create_connection()
            self._refcount += 1
            return self._conn

    def release(self) -> bool:
        """Release a reference. Returns True if handle should be destroyed."""
        with self._lock:
            self._refcount -= 1
            if self._refcount <= 0 and self._conn is not None:
                self._close_connection()
                return True
            return False

    def _create_connection(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode={'ro' if self.read_only else 'rwc'}"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        if not self.read_only:
            ensure_schema(conn)
        return conn

    def _close_connection(self) -> None:
        if self._conn is not None:
            if not self.read_only:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
            self._conn.close()
            self._conn = None
        self._closed = True

    def __del__(self):
        # Safety net; explicit release() is preferred
        if self._conn is not None and not self._closed:
            self._close_connection()


class _Registry:
    """Process-global registry of connection handles keyed by (path, read_only)."""

    def __init__(self):
        self._handles: dict[tuple[Path, bool], _ConnectionHandle] = {}
        self._lock = threading.Lock()
        self._finalized = False
        atexit.register(self._shutdown)

    def acquire(self, path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        """Get a connection from the shared handle, creating if needed."""
        if self._finalized:
            raise RuntimeError("Registry shut down")
        key = (path.resolve(), read_only)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                handle = _ConnectionHandle(path, read_only)
                self._handles[key] = handle
            return handle.acquire()

    def release(self, path: Path, *, read_only: bool = False) -> None:
        """Release a connection back to the handle."""
        key = (path.resolve(), read_only)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                return
            if handle.release():
                del self._handles[key]

    def _shutdown(self) -> None:
        """Close all handles at process exit."""
        self._finalized = True
        with self._lock:
            for handle in self._handles.values():
                handle._close_connection()
            self._handles.clear()


# Global registry instance
_registry = _Registry()


def acquire(db_path: Optional[Path | str] = None, *, read_only: bool = False) -> sqlite3.Connection:
    """
    Acquire a shared database connection for the given path.

    Args:
        db_path: Path to the SQLite database. If None, uses default Sensei state DB.
        read_only: If True, returns a read-only connection (no write lock, no WAL checkpoint thread).

    Returns:
        A sqlite3.Connection from the process-shared handle.

    Usage:
        # Writer (mutating operations)
        with acquire() as conn:
            conn.execute("INSERT INTO sessions ...")
            conn.commit()

        # Reader (queries, exports, listings)
        with acquire(read_only=True) as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
    """
    from .config import get_state_db_path  # assumes config helper exists

    path = Path(db_path) if db_path else get_state_db_path()
    return _registry.acquire(path, read_only=read_only)


def release(db_path: Optional[Path | str] = None, *, read_only: bool = False) -> None:
    """Release a connection acquired via acquire()."""
    from .config import get_state_db_path

    path = Path(db_path) if db_path else get_state_db_path()
    _registry.release(path, read_only=read_only)


@contextmanager
def session(db_path: Optional[Path | str] = None, *, read_only: bool = False):
    """
    Context manager for a database session.

    Automatically releases the connection on exit. For writers, commits on
    success or rolls back on exception.
    """
    from .config import get_state_db_path

    path = Path(db_path) if db_path else get_state_db_path()
    conn = _registry.acquire(path, read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        if not read_only:
            conn.rollback()
        raise
    finally:
        _registry.release(path, read_only=read_only)


# Backwards-compatible alias for existing code
class SessionDB:
    """
    Legacy-style wrapper for gradual migration.

    Usage:
        with SessionDB() as db:
            db.execute(...)
    """

    def __init__(self, db_path: Optional[Path | str] = None, *, read_only: bool = True):
        self._path = db_path
        self._read_only = read_only
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        self._conn = acquire(self._path, read_only=self._read_only)
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn is not None:
            if exc_type is None and not self._read_only:
                self._conn.commit()
            elif not self._read_only:
                self._conn.rollback()
            release(self._path, read_only=self._read_only)
            self._conn = None
