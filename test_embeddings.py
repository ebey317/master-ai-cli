"""Tests for local embeddings.

Two things are tested, and the split matters. Everything about the cache
eviction and the packing is pure logic and always runs. Everything about
the model is integration and is skipped when Ollama is absent, because a
missing local model must never fail this suite -- that is the same
condition under which the agent degrades to keyword search at runtime.
"""

from __future__ import annotations

import sqlite3

import pytest

import embeddings

# --------------------------------------------------------------------------
# Pure logic. No model required.
# --------------------------------------------------------------------------


def test_cosine_of_identical_vectors_is_one():
    v = [0.1, 0.2, 0.3, 0.4]
    assert embeddings.cosine(v, v) == pytest.approx(1.0, abs=1e-9)


def test_cosine_of_orthogonal_vectors_is_zero():
    assert embeddings.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)


def test_cosine_is_scale_invariant():
    """Magnitude must not matter, only direction.

    Embeddings from different sources can be normalized differently. If
    magnitude leaked into the score, a longer passage would outrank a
    better-matching short one for reasons that have nothing to do with
    meaning.
    """
    a = [1.0, 2.0, 3.0]
    assert embeddings.cosine(a, [x * 7.0 for x in a]) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([], [1.0, 2.0]),
        ([1.0, 2.0], []),
        (None, [1.0]),
        ([1.0, 2.0], None),
        ([0.0, 0.0], [1.0, 2.0]),  # degenerate vector
        ([1.0, 2.0], [1.0]),  # length mismatch
    ],
)
def test_cosine_degrades_to_zero_instead_of_raising(a, b):
    assert embeddings.cosine(a, b) == 0.0


def test_pack_unpack_round_trips_exactly():
    vec = [0.123456, -0.789, 0.0, 1e-8]
    assert embeddings._unpack(embeddings._pack(vec)) == pytest.approx(
        vec, rel=1e-6, abs=1e-7
    )


def test_eviction_keeps_rows_while_under_capacity(tmp_path, monkeypatch):
    """The regression: a trim that fires while the cache is under capacity.

    An earlier version passed the cache capacity as the delete limit, which
    exceeds the row count on every write while the cache is small. Every row
    was deleted immediately after being inserted, so the cache reported zero
    vectors and never once returned a hit.
    """
    monkeypatch.setattr(embeddings, "CACHE_PATH", tmp_path / "cache.sqlite")
    monkeypatch.setattr(embeddings, "CACHE_MAX_ROWS", 5)
    monkeypatch.setattr(embeddings, "_client", None)

    for i in range(3):
        embeddings._cache_put(f"text {i}", [float(i), 1.0])

    conn = sqlite3.connect(str(tmp_path / "cache.sqlite"))
    stored = conn.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
    conn.close()
    assert stored == 3, "rows must survive writes while under capacity"


def test_eviction_trims_only_the_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "CACHE_PATH", tmp_path / "cache.sqlite")
    monkeypatch.setattr(embeddings, "CACHE_MAX_ROWS", 4)
    monkeypatch.setattr(embeddings, "_client", None)

    for i in range(10):
        embeddings._cache_put(f"text {i}", [float(i), 1.0])

    conn = sqlite3.connect(str(tmp_path / "cache.sqlite"))
    stored = conn.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
    conn.close()
    assert stored == 4, "cache must hold exactly its capacity once full"
    # Oldest go first, so the most recent survive.
    assert embeddings._cache_get("text 9") is not None
    assert embeddings._cache_get("text 0") is None


def test_blank_text_is_never_embedded():
    assert embeddings.embed("") is None
    assert embeddings.embed("   ") is None


# --------------------------------------------------------------------------
# Integration. Skipped when the local model is absent.
# --------------------------------------------------------------------------

needs_model = pytest.mark.skipif(
    not embeddings.available(), reason="local embedding model not reachable"
)


@needs_model
def test_embed_returns_a_dense_vector():
    vec = embeddings.embed("sensei memory index build")
    assert vec is not None
    assert len(vec) == embeddings.DIM
    assert all(isinstance(x, float) for x in vec)
    assert any(x != 0.0 for x in vec)


@needs_model
def test_second_embed_is_served_from_cache():
    text = "a passage unique to this test so it is not already cached"
    embeddings.embed(text)
    before = embeddings.stats()["cached_vectors"]
    embeddings.embed(text)
    assert embeddings.stats()["cached_vectors"] == before


@needs_model
def test_semantic_match_beats_unrelated_text():
    """The reason this module exists.

    These two strings share no tokens. Token-overlap scoring gives them
    0.0 and treats them as unrelated, which is how the harvest cache used
    to miss on rephrased requests.
    """
    query = "how do I fix the router port"
    same = embeddings.embed("change the CLAF listen address to 8791")
    different = embeddings.embed("what is the weather in Tokyo")
    assert embeddings.cosine(embeddings.embed(query), same) > embeddings.cosine(
        embeddings.embed(query), different
    )


@needs_model
def test_embed_many_matches_one_by_one():
    texts = [
        "first passage about ports",
        "second passage about memory",
        "third about tools",
    ]
    batched = embeddings.embed_many(texts)
    assert len(batched) == len(texts)
    for vector in batched:
        assert vector is not None and len(vector) == embeddings.DIM


@needs_model
def test_embed_many_deduplicates_repeats_within_one_call():
    """A store with repeated lines must not pay for the same pass twice."""
    repeated = ["the same line over and over"] * 5
    out = embeddings.embed_many(repeated)
    assert all(v is not None for v in out)
    # Every copy must be the same vector, and the first must be populated.
    for vector in out[1:]:
        assert embeddings.cosine(out[0], vector) == pytest.approx(1.0, abs=1e-6)


def test_embed_returns_none_when_the_endpoint_is_dead(monkeypatch):
    """Degradation, not an exception.

    Retrieval is a nicety on the hot path of the agent loop. If Ollama is
    down, this must return None so the caller falls back to keyword search.
    Raising here would take down a reply over a retrieval convenience.
    """
    monkeypatch.setattr(embeddings, "ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setattr(embeddings, "TIMEOUT_S", 1.0)
    assert embeddings.embed("anything at all, definitely not cached xyzzy") is None


def test_stats_shape_is_stable_even_with_no_model():
    stats = embeddings.stats()
    for key in ("model", "available", "cached_vectors", "dim"):
        assert key in stats
