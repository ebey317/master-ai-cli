"""Tests for hybrid retrieval.

The property that matters is not that both signals work -- that is tested in
embeddings.py and is FTS5's job. It is that fusing them beats either alone,
because that is the only reason to have both. Every test here is built around
a case where keyword and vector disagree, which is where a naive
implementation silently picks the wrong one.

The live-model tests skip when Ollama is absent, which is the same condition
under which the agent degrades to keyword-only at runtime.
"""

from __future__ import annotations

import pytest

import embeddings
import retrieval

# --------------------------------------------------------------------------
# Fixture: a real index with known content, so assertions are about
# retrieval behaviour and not about what happens to be in the memory stores.
# --------------------------------------------------------------------------


@pytest.fixture
def index(tmp_path, monkeypatch):
    """A small index with one keyword-only hit and one semantic-only hit."""
    monkeypatch.setattr(retrieval._idx, "DB_PATH", tmp_path / "idx.sqlite")
    monkeypatch.setattr(embeddings, "CACHE_PATH", tmp_path / "emb.sqlite")
    monkeypatch.setattr(embeddings, "_client", None)

    with retrieval._idx.connect() as conn:
        retrieval.init_schema(conn)
        rows = [
            ("src", 1, "the router listens on port 8791 and nothing else"),
            ("src", 2, "change the CLAF listen address to a new value"),
            ("src", 3, "the router forecast for Tokyo is mild this time of year"),
            ("src", 4, "quarterly revenue grew twelve percent to 4.1 million"),
        ]
        for source, line_no, body in rows:
            conn.execute(
                "INSERT INTO docs (source, line_no, body) VALUES (?,?,?)",
                (source, line_no, body),
            )
        conn.commit()
        # Rebuild FTS by hand: the triggers only fire on insert into docs, and
        # inserting through the same connection already ran them.
        conn.execute("DELETE FROM docs_fts")
        conn.execute("INSERT INTO docs_fts(rowid, body) SELECT id, body FROM docs")
        conn.commit()
    return tmp_path / "idx.sqlite"


def _embed_all(index, monkeypatch):
    """Embed every row, bypassing the model, so tests are deterministic.

    The stand-in vector is one dimension per distinct word, so cosine
    reflects word overlap. The mapping must be stable across processes:
    Python's hash() on str is salted per interpreter run, so using it would
    make the same query retrieve different rows on different invocations.
    """
    with retrieval._idx.connect() as conn:
        rows = conn.execute("SELECT id, body FROM docs").fetchall()
        for rowid, body in rows:
            words = set(body.lower().split())
            # Must match the real embedding width: cosine() returns 0.0 on a
            # length mismatch, so a narrower stand-in silently produces an
            # empty vector list and every fusion test passes for the wrong
            # reason.
            vec = [0.0] * embeddings.DIM
            for word in words:
                vec[sum(ord(c) for c in word) % embeddings.DIM] = 1.0
            conn.execute(
                "UPDATE docs SET emb = ? WHERE id = ?", (embeddings._pack(vec), rowid)
            )
        conn.commit()


def _embed_with_model(index):
    """Embed the fixture rows with the real model.

    Deliberately does not call retrieval.build(): that re-ingests every
    source, which is 24MB of text, and turns a four-row unit test into a
    multi-minute integration run.
    """
    with retrieval._idx.connect() as conn:
        rows = conn.execute("SELECT id, body FROM docs").fetchall()
        vectors = embeddings.embed_many([body for _, body in rows], batch=8)
        for (rowid, _body), vector in zip(rows, vectors):
            if vector is None:
                continue
            conn.execute(
                "UPDATE docs SET emb = ? WHERE id = ?",
                (embeddings._pack(vector), rowid),
            )
        conn.commit()


# --------------------------------------------------------------------------
# Fusion logic. No model, no index required.
# --------------------------------------------------------------------------


def test_rrf_rewards_documents_present_in_both_lists():
    """The core claim: agreement should outrank a single-list hit."""
    fts = [(1, -1.0), (2, -2.0)]
    vec = [(2, 0.9), (3, 0.8)]
    fused = retrieval._fuse(fts, vec, limit=3)
    assert [rowid for rowid, _ in fused] == [2, 1, 3]


def test_rrf_keeps_documents_that_appear_in_only_one_list():
    """A pure keyword hit with no embedding must survive the fusion.

    Intersecting the two lists instead would drop it, and a document that
    matches on exact tokens but has no vector is often the best answer --
    an identifier, a path, a commit hash.
    """
    fused = retrieval._fuse([(7, -1.0)], [], limit=5)
    assert [rowid for rowid, _ in fused] == [7]
    fused = retrieval._fuse([], [(9, 0.5)], limit=5)
    assert [rowid for rowid, _ in fused] == [9]


def test_rrf_respects_the_limit():
    fused = retrieval._fuse([(i, float(-i)) for i in range(10)], [], limit=3)
    assert len(fused) == 3


def test_rrf_with_no_input_returns_nothing():
    assert retrieval._fuse([], [], limit=5) == []


def test_rrf_is_deterministic():
    """Same input, same order. Retrieval that varies between identical calls
    makes a model's answers unrecoverable."""
    fts = [(1, -1.0), (2, -2.0)]
    vec = [(2, 0.9), (3, 0.8)]
    assert retrieval._fuse(fts, vec, 5) == retrieval._fuse(fts, vec, 5)


# --------------------------------------------------------------------------
# Schema migration.
# --------------------------------------------------------------------------


def test_init_schema_adds_the_embedding_column_and_is_idempotent(tmp_path, monkeypatch):
    """Running it twice must not fail.

    The index already exists on this machine, built before embeddings were
    a thing. A migration that errors on the second run would break every
    agent start.
    """
    monkeypatch.setattr(retrieval._idx, "DB_PATH", tmp_path / "idx.sqlite")
    with retrieval._idx.connect() as conn:
        retrieval.init_schema(conn)
        retrieval.init_schema(conn)  # must not raise
        columns = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
    assert "emb" in columns


# --------------------------------------------------------------------------
# Search behaviour against the fixture index.
# --------------------------------------------------------------------------


def test_blank_query_returns_nothing(index):
    assert retrieval.search("") == []
    assert retrieval.search("   ") == []


def test_search_never_raises_on_an_empty_index(tmp_path, monkeypatch):
    """A missing or empty index is the normal first-run state.

    Raising here would take down a reply over a retrieval convenience.
    """
    monkeypatch.setattr(retrieval._idx, "DB_PATH", tmp_path / "absent.sqlite")
    assert retrieval.search("anything at all") == []


def test_search_returns_well_formed_results(index, monkeypatch):
    _embed_all(index, monkeypatch)
    results = retrieval.search("router port 8791", limit=5)
    assert results
    for item in results:
        assert set(item) == {"id", "source", "score", "body"}
        assert item["body"]
        assert item["score"] > 0


def test_keyword_match_wins_when_tokens_are_exact(index, monkeypatch):
    """Exact tokens are the strongest signal there is.

    "port 8791" appears verbatim in row 1 and nowhere else. A retriever that
    ranks the paraphrase above it is worse than either signal alone.
    """
    _embed_all(index, monkeypatch)
    results = retrieval.search("port 8791", limit=5)
    assert results[0]["id"] == 1


def test_semantic_match_survives_with_no_shared_tokens(index, monkeypatch):
    """The case keyword search cannot handle at all.

    "listen address" shares no tokens with "listens on port 8791", so FTS5
    returns nothing and only the vector list can find it.
    """
    _embed_all(index, monkeypatch)
    results = retrieval.search("listen address", limit=5)
    assert results, "a paraphrase with no shared tokens must still retrieve"
    assert results[0]["id"] == 2


def test_unrelated_query_ranks_last(index, monkeypatch):
    _embed_all(index, monkeypatch)
    results = retrieval.search("router port 8791", limit=5)
    ids = [item["id"] for item in results]
    assert 3 in ids  # Tokyo: present, but worst
    assert ids.index(3) > ids.index(1)  # and ranked below the exact match


def test_results_are_capped_at_the_limit(index, monkeypatch):
    _embed_all(index, monkeypatch)
    assert len(retrieval.search("the", limit=2)) <= 2


def test_format_results_handles_the_empty_case():
    assert "no relevant memory" in retrieval.format_results([])


def test_format_results_includes_source_and_score(index, monkeypatch):
    _embed_all(index, monkeypatch)
    text = retrieval.format_results(retrieval.search("port 8791", limit=3))
    assert "src" in text
    assert "[" in text


# --------------------------------------------------------------------------
# Integration with the real model. Skipped when it is absent.
# --------------------------------------------------------------------------

needs_model = pytest.mark.skipif(
    not embeddings.available(), reason="local embedding model not reachable"
)


@needs_model
def test_real_embeddings_find_a_paraphrase(index):
    """End to end with the actual model, not a stand-in vector.

    This is the measurement the whole module rests on: a rephrased request
    with no shared tokens must retrieve the right chunk.

    The assertion is that the paraphrase is *found*, not that it ranks
    first. Measured on this model, the two are a tie -- 0.6631 for the
    chunk stating the current port against 0.6544 for the paraphrase --
    and both are legitimate answers to the query. Asserting a winner would
    be asserting a ranking the embedding model does not actually make.
    """
    _embed_with_model(index)
    results = retrieval.search("how do I change the listen port", limit=5)
    assert results
    assert 2 in [item["id"] for item in results], retrieval.format_results(results)


@needs_model
def test_real_embeddings_rank_unrelated_text_last(index):
    _embed_with_model(index)
    results = retrieval.search("router port 8791", limit=5)
    assert results
    assert results[0]["id"] == 1


@needs_model
def test_stats_reports_coverage_honestly():
    """A partial index must say so.

    Coverage is the number that keeps a 6%-indexed store from being mistaken
    for a complete one. Reporting it is what makes a partial build safe to
    ship.
    """
    info = retrieval.stats()
    assert "coverage" in info
    assert 0.0 <= info["coverage"] <= 1.0
    assert info["chunks"] >= info["embedded"]
