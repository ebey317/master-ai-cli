#!/usr/bin/env python3
"""Hybrid retrieval over the memory index: keyword + vector, fused.

sensei_memory_index.py already ingests the memory stores into SQLite FTS5.
It has never been built and nothing calls it, so retrieval today is
token-overlap only (harvest.py's Jaccard). This module adds the missing
half: embeddings, and a search that combines both signals.

Why both, measured rather than assumed:

    0.414  "router port" ~ "CLAF listen address to 8791"   (no shared tokens)
    0.591  "router port" ~ "update the proxy listen port"  (shares "port")
    0.28-0.29  unrelated noise floor

The pure paraphrase scores LOWER than the one sharing a single word, so
vector search alone ranks a worse match above a better one. Keyword search
alone cannot see the paraphrase at all. Fusing them is not a refinement;
it is the difference between working and not.

Fusion is Reciprocal Rank Fusion, not a weighted score sum. FTS5's rank and
cosine live on unrelated scales, so any weight chosen today would be wrong
after the next model or index change. RRF only needs each list to be
correctly ordered, which both already are.

Degradation is total and safe: no model, keyword only. No index, nothing.
Neither raises into the agent loop.
"""

from __future__ import annotations

import sqlite3
import sys

import embeddings
import sensei_memory_index as _idx

RRF_K = 60.0
SEARCH_LIMIT_DEFAULT = 5


def init_schema(conn: sqlite3.Connection) -> None:
    """Add the embedding column to the existing index.

    Idempotent: the column is added only when absent, so this is safe to run
    against an index built before embeddings existed.
    """
    _idx.init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
    if "emb" not in columns:
        conn.execute("ALTER TABLE docs ADD COLUMN emb BLOB")
        conn.commit()


def _fts_hits(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    """Keyword hits as (rowid, rank), best first."""
    try:
        rows = conn.execute(
            """
            SELECT docs.id, rank
            FROM docs_fts
            JOIN docs ON docs.id = docs_fts.rowid
            WHERE docs_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(rowid), float(rank)) for rowid, rank in rows]


def _vector_hits(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    """Vector hits as (rowid, cosine), best first. Empty if no model."""
    query_vec = embeddings.embed(query)
    if query_vec is None:
        return []
    try:
        rows = conn.execute("SELECT id, emb FROM docs WHERE emb IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return []
    scored = []
    for rowid, blob in rows:
        if not blob:
            continue
        try:
            vec = embeddings._unpack(blob)
        except Exception:
            continue
        score = embeddings.cosine(query_vec, vec)
        if score > 0.0:
            scored.append((int(rowid), score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def _fuse(
    fts: list[tuple[int, float]], vec: list[tuple[int, float]], limit: int
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over the two ranked lists.

    A document present in only one list still scores, so a pure keyword hit
    with no embedding and a pure semantic hit with no keyword match both
    survive. That is the point of fusing rather than intersecting.
    """
    scores: dict[int, float] = {}
    for position, (rowid, _score) in enumerate(fts):
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + position + 1)
    for position, (rowid, _score) in enumerate(vec):
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + position + 1)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def search(query: str, limit: int = SEARCH_LIMIT_DEFAULT) -> list[dict]:
    """Hybrid search. Returns [] when there is nothing to search.

    Never raises. A retrieval failure must cost the agent its memory, not
    its reply.
    """
    if not query or not query.strip():
        return []
    try:
        with _idx.connect() as conn:
            init_schema(conn)
            fts = _fts_hits(conn, query, limit * 4)
            vec = _vector_hits(conn, query, limit * 4)
            fused = _fuse(fts, vec, limit)
            if not fused:
                return []
            placeholders = ",".join("?" for _ in fused)
            bodies = {
                int(rowid): body
                for rowid, body in conn.execute(
                    f"SELECT id, body FROM docs WHERE id IN ({placeholders})",
                    [rowid for rowid, _ in fused],
                ).fetchall()
            }
            sources = {
                int(rowid): source
                for rowid, source in conn.execute(
                    f"SELECT id, source FROM docs WHERE id IN ({placeholders})",
                    [rowid for rowid, _ in fused],
                ).fetchall()
            }
            out = []
            for rowid, score in fused:
                body = bodies.get(rowid, "")
                if not body:
                    continue
                out.append(
                    {
                        "id": rowid,
                        "source": sources.get(rowid, ""),
                        "score": round(score, 6),
                        "body": body[:1200],
                    }
                )
            return out
    except Exception:
        return []


def build(batch: int = 16, verbose: bool = True) -> dict:
    """Ingest the memory stores, then embed whatever is new.

    Text ingestion is incremental (each source carries a cursor), so
    re-running is cheap. Embedding is not: it is the expensive half, and it
    is skipped for anything already stored.
    """
    _idx.cmd_build()
    with _idx.connect() as conn:
        init_schema(conn)
        pending = conn.execute(
            "SELECT id, body FROM docs WHERE emb IS NULL AND body != ''"
        ).fetchall()
        total = len(pending)
        if verbose:
            print(f"  {total} chunks to embed")
        done = 0
        for start in range(0, total, batch):
            chunk = pending[start : start + batch]
            bodies = [body for _id, body in chunk]
            vectors = embeddings.embed_many(bodies, batch=batch)
            for (_id, _body), vector in zip(chunk, vectors):
                if vector is None:
                    continue
                conn.execute(
                    "UPDATE docs SET emb = ? WHERE id = ?",
                    (embeddings._pack(vector), _id),
                )
            conn.commit()
            done += len(chunk)
            if verbose:
                print(f"  embedded {done}/{total}")
        return {"chunks": total, "embedded": done}


def stats() -> dict:
    try:
        with _idx.connect() as conn:
            init_schema(conn)
            total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            embedded = conn.execute(
                "SELECT COUNT(*) FROM docs WHERE emb IS NOT NULL"
            ).fetchone()[0]
            sources = dict(
                conn.execute(
                    "SELECT source, COUNT(*) FROM docs GROUP BY source"
                ).fetchall()
            )
            return {
                "chunks": total,
                "embedded": embedded,
                "coverage": round(embedded / total, 3) if total else 0.0,
                "sources": sources,
                "model": embeddings.MODEL,
                "model_available": embeddings.available(),
            }
    except Exception:
        return {"chunks": 0, "embedded": 0, "coverage": 0.0, "sources": {}}


def format_results(results: list[dict], max_body: int = 220) -> str:
    if not results:
        return "(no relevant memory found)"
    lines = []
    for item in results:
        body = " ".join(item["body"].split())
        if len(body) > max_body:
            body = body[:max_body] + "…"
        lines.append(f"[{item['score']:.3f}] {item['source']}\n    {body}")
    return "\n".join(lines)


def cmd_search(query: str, limit: int = SEARCH_LIMIT_DEFAULT) -> int:
    results = search(query, limit)
    if not results:
        print("(no hits)")
        return 1
    print(format_results(results))
    return 0


def cmd_build(batch: int = 16) -> int:
    result = build(batch=batch)
    print(f"indexed {result['embedded']}/{result['chunks']} chunks")
    return 0


def cmd_stats() -> int:
    info = stats()
    print(
        f"chunks: {info['chunks']}  embedded: {info['embedded']}  coverage: {info['coverage']:.0%}"
    )
    print(f"model: {info['model']}  available: {info['model_available']}")
    for source, count in sorted(info["sources"].items()):
        print(f"  {count:6}  {source}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: retrieval.py {search <query>|build|stats}")
        raise SystemExit(2)
    command = sys.argv[1]
    if command == "search":
        raise SystemExit(cmd_search(" ".join(sys.argv[2:])))
    if command == "build":
        raise SystemExit(cmd_build())
    if command == "stats":
        raise SystemExit(cmd_stats())
    print(f"unknown command: {command}")
    raise SystemExit(2)
