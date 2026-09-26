"""
Memoized .env file tokenizer with strong freshness guarantees.

Every call opens the file to get a fresh fstat fingerprint (mtime_ns, size, inode, device),
re-checks it after reading, and only caches on match. Open/read failures return {} without
caching and evict any warm entry. A generation counter under the lock prevents a slow reader
from repopulating an entry that a concurrent writer invalidated.
"""
import codecs
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Tuple

# Per-path LRU cache: path_str -> (fingerprint, secrets_dict, generation)
# fingerprint = (mtime_ns, size, inode, device)
_ENV_FILE_CACHE: "OrderedDict[str, Tuple[tuple, Dict[str, str], int]]" = OrderedDict()
_ENV_FILE_CACHE_LOCK = threading.Lock()
_ENV_FILE_CACHE_MAX = 64
_ENV_FILE_CACHE_GENERATION = 0  # incremented on every invalidation


def _fd_fingerprint(fileno: int) -> tuple:
    """Return (mtime_ns, size, inode, device) from an open file descriptor."""
    st = os.fstat(fileno)
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)


def invalidate_env_file_cache(env_path: Optional[Path] = None) -> None:
    """
    Drop one path from the load_env_file() memo, or all of them.
    Increments the generation counter so in-flight readers won't repopulate stale data.
    """
    global _ENV_FILE_CACHE_GENERATION
    with _ENV_FILE_CACHE_LOCK:
        if env_path is None:
            _ENV_FILE_CACHE.clear()
        else:
            _ENV_FILE_CACHE.pop(str(env_path), None)
        _ENV_FILE_CACHE_GENERATION += 1


def _decode_env_bytes(raw: bytes) -> str:
    """BOM stripped; invalid UTF-8 falls back to latin-1 exactly like dotenv does."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _parse_env_text(text: str) -> Dict[str, str]:
    """Tokenize already-read .env text. export prefix, # comments, quote escapes."""
    secrets: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching quotes
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        # Unescape common sequences
        value = value.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
        secrets[key] = value
    return secrets


def load_env_file(env_path: Path) -> Dict[str, str]:
    """
    THE .env tokenizer: every reader parses through here so no two boundaries
    disagree on which keys/values a file defines. Dict only — never touches os.environ.
    Memoized per file change with strong freshness guarantees.
    """
    path_str = str(env_path)

    # Fast path: check cache under lock, but we must still open file to verify freshness
    with _ENV_FILE_CACHE_LOCK:
        cached = _ENV_FILE_CACHE.get(path_str)
        if cached is not None:
            cached_fingerprint, cached_secrets, cached_generation = cached
        else:
            cached_fingerprint = cached_secrets = cached_generation = None

    # Always open the file to get a fresh fingerprint (NFS close-to-open, symlink pinning)
    try:
        with open(env_path, "rb") as f:
            fileno = f.fileno()
            pre_read_fingerprint = _fd_fingerprint(fileno)
            raw = f.read()
            post_read_fingerprint = _fd_fingerprint(fileno)

            # Fingerprint changed during read → file mutated mid-read; don't cache
            if pre_read_fingerprint != post_read_fingerprint:
                return _parse_env_text(_decode_env_bytes(raw))

            # Check if cached entry matches current fingerprint and generation
            if cached_fingerprint == pre_read_fingerprint and cached_generation == _ENV_FILE_CACHE_GENERATION:
                with _ENV_FILE_CACHE_LOCK:
                    # Re-verify under lock (generation could have changed)
                    entry = _ENV_FILE_CACHE.get(path_str)
                    if entry is not None:
                        fp, secrets, gen = entry
                        if fp == pre_read_fingerprint and gen == _ENV_FILE_CACHE_GENERATION:
                            _ENV_FILE_CACHE.move_to_end(path_str)  # LRU touch
                            return secrets

            # Cache miss or stale: parse fresh
            text = _decode_env_bytes(raw)
            secrets = _parse_env_text(text)

            # Store under lock with current generation
            with _ENV_FILE_CACHE_LOCK:
                # Double-check: another thread may have populated while we parsed
                entry = _ENV_FILE_CACHE.get(path_str)
                if entry is not None:
                    fp, existing_secrets, gen = entry
                    if fp == pre_read_fingerprint and gen == _ENV_FILE_CACHE_GENERATION:
                        _ENV_FILE_CACHE.move_to_end(path_str)
                        return existing_secrets

                # Evict LRU if at capacity
                if len(_ENV_FILE_CACHE) >= _ENV_FILE_CACHE_MAX:
                    _ENV_FILE_CACHE.popitem(last=False)

                _ENV_FILE_CACHE[path_str] = (pre_read_fingerprint, secrets, _ENV_FILE_CACHE_GENERATION)
                return secrets

    except OSError:
        # Open/read failed: evict any warm entry, return empty, DO NOT cache failure
        with _ENV_FILE_CACHE_LOCK:
            _ENV_FILE_CACHE.pop(path_str, None)
        return {}
