#!/usr/bin/env python3
"""Local text embeddings, with no cloud and no API key.

Retrieval in this agent used to be token-overlap only. harvest.py scored
near-duplicate calls with Jaccard similarity, which is exact token overlap:
"how do I fix the router port" and "change the CLAF listen address to 8791"
share no tokens, so they score 0.0 and the cache never fires, even though
they are the same request. That is the gap this closes.

The model is nomic-embed-text, served by the local Ollama daemon. That
choice is the whole point: embeddings are the one piece of retrieval that
usually pushes a system to a hosted API, and this is a local-first agent
whose entire premise is that nothing has to leave the machine. A 768-dim
embedding of a short passage is a few kilobytes, so nothing meaningful ever
crossed the wire even in the version that used a cloud provider.

Three properties matter more than raw quality here:

  Caching. Every call is a round trip and a model forward pass. Retrieval
  runs on the hot path of the agent loop, so repeats are common and free to
  skip. The cache is keyed on the exact text, so a hit is exact, not
  approximate.

  Honest degradation. If Ollama is down, this must not raise into the agent
  loop and take a reply down with it. embed() returns None and the caller
  falls back to keyword search. A degraded agent is recoverable; a crashed
  one is not.

  Batching. Indexing a store means hundreds of passages, and one request per
  passage turns a two-second build into a two-minute one.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = os.environ.get("MASTER_AI_EMBED_MODEL", "nomic-embed-text:v1.5")
ENDPOINT = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DIM = 768
TIMEOUT_S = 60.0

CACHE_PATH = Path.home() / ".master_ai_embed_cache.sqlite"
CACHE_MAX_ROWS = 20_000

_client = None


def available() -> bool:
    """True when the embedding model can be reached right now."""
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/api/tags", timeout=3) as response:
            payload = json.loads(response.read())
        return any(
            m.get("name", "").startswith(MODEL.split(":")[0])
            for m in payload.get("models", [])
        )
    except Exception:
        return False


def _connect() -> sqlite3.Connection | None:
    """Open (or create) the cache. A cache failure is never fatal."""
    global _client
    if _client is not None:
        return _client
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(CACHE_PATH), timeout=5.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS emb (text TEXT PRIMARY KEY, vec BLOB, ts INTEGER)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS emb_ts_idx ON emb(ts)")
        conn.commit()
        _client = conn
    except Exception:
        _client = None
    return _client


def _pack(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    import struct

    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cache_get(text: str) -> list[float] | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT vec FROM emb WHERE text = ?", (text,)).fetchone()
        return _unpack(row[0]) if row and row[0] else None
    except Exception:
        return None


def _cache_put(text: str, vec: list[float]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO emb (text, vec, ts) VALUES (?,?,?)",
            (text, _pack(vec), int(time.time())),
        )
        # Bounded so a long-lived agent cannot grow the cache without limit.
        # Trim by the actual overflow, oldest first. Passing the cache
        # capacity here instead would delete every row on every write,
        # because the limit always exceeds the row count while the cache is
        # under it -- which is exactly when trimming should do nothing.
        total = conn.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
        overflow = max(0, total - CACHE_MAX_ROWS)
        if overflow:
            conn.execute(
                "DELETE FROM emb WHERE text IN (SELECT text FROM emb ORDER BY ts ASC LIMIT ?)",
                (overflow,),
            )
        conn.commit()

    except Exception:
        pass


def _post(texts: list[str]) -> list[list[float]] | None:
    """Embed one or many passages.

    Uses /api/embed with an "input" array. The older /api/embeddings
    endpoint takes a single "prompt" and answers 400 for a list, so batching
    through it yields nothing at all instead of raising -- which is how a
    broken batch path stays invisible right up until you notice every index
    build produced zero vectors.
    """
    body = json.dumps({"model": MODEL, "input": texts}).encode()
    request = urllib.request.Request(
        f"{ENDPOINT}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        payload = json.loads(response.read())
    vectors = payload.get("embeddings")
    if not vectors:
        return None
    return [[float(x) for x in v] for v in vectors if v]


def embed(text: str) -> list[float] | None:
    """One embedding, or None if the model is unreachable.

    None is a normal return, not an error. Retrieval degrades to keyword
    search, which is worse but correct; raising here would take down the
    agent loop over a retrieval nicety.
    """
    if not text or not text.strip():
        return None
    cached = _cache_get(text)
    if cached is not None:
        return cached
    try:
        result = _post([text])
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    except Exception:
        return None
    if not result or not result[0]:
        return None
    vector = [float(x) for x in result[0]]
    _cache_put(text, vector)
    return vector


def embed_many(texts: list[str], batch: int = 16) -> list[list[float] | None]:
    """Embed a list, using the cache and batching the misses.

    Indexing a store is hundreds of passages. One request per passage turns
    a two-second build into a two-minute one, so misses go out in batches.
    Cache hits never touch the network.

    Duplicates are collapsed to one forward pass, and the result is written
    back to every position that shared the text -- indexing a store whose
    lines repeat must not return None for the copies.
    """
    out: list[list[float] | None] = [None] * len(texts)
    # text -> every index that wants it
    groups: dict[str, list[int]] = {}
    for index, text in enumerate(texts):
        cached = _cache_get(text)
        if cached is not None:
            out[index] = cached
        elif text and text.strip():
            groups.setdefault(text, []).append(index)

    pending = list(groups)
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        try:
            result = _post(chunk)
        except Exception:
            continue
        if not result:
            continue
        for position, text in enumerate(chunk):
            if position >= len(result) or not result[position]:
                continue
            vector = result[position]
            _cache_put(text, vector)
            for index in groups[text]:
                out[index] = vector
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. 0.0 if either vector is missing or degenerate.

    Note the scale when tuning thresholds: paraphrases of the same intent
    typically land around 0.40-0.50, and unrelated text around 0.0-0.20.
    A threshold carried over from token-overlap scoring (0.80 and up) will
    reject every genuine match.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def stats() -> dict:
    conn = _connect()
    rows = conn.execute("SELECT COUNT(*) FROM emb").fetchone()[0] if conn else 0
    return {
        "model": MODEL,
        "endpoint": ENDPOINT,
        "available": available(),
        "cached_vectors": rows,
        "dim": DIM,
    }
