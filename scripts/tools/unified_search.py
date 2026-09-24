"""
Unified tool search for Sensei.

The upstream idea: connector tools (Gmail, Linear, Notion, ...) were being
appended only into leftover slots after local BM25 ranking, so they were
effectively invisible. The portable fix is to fold remote/connector entries
into the same catalog and ranking pass as local script tools.

This module is a helper that core search logic can call. It intentionally
does not touch network details; callers supply connector entries already
resolved from whatever registry or gateway Sensei uses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# Same admission rule the upstream used: a query term must appear in at least
# one token of the search text for an entry to be considered. We keep the
# tokenizer simple and deterministic so local script tests are stable.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _rare_token_score(query_terms: Sequence[str], text_terms: Sequence[str]) -> float:
    """BM25-ish score used for ranking.

    The upstream insight is that connector hits and local hits should compete
    on the same score. We use a lightweight rare-token-biased frequency score.
    """
    if not query_terms:
        return 0.0

    text_set = set(text_terms)
    matches = sum(1 for t in query_terms if t in text_set)
    if matches == 0:
        return 0.0

    # Reward rare query terms appearing in the text.
    idf_sum = 0.0
    for t in query_terms:
        if t in text_set:
            idf = math.log(1 + len(text_terms) / (text_terms.count(t) + 1))
            idf_sum += idf

    # Normalize by query length so long queries do not dominate.
    return idf_sum / len(query_terms)


@dataclass(frozen=True)
class ToolEntry:
    """One searchable item.

    `name` is the fully-qualified tool name the model sees.
    `search_text` is what we rank against (description plus slug words).
    `source` marks local vs connector for debugging; ranking ignores it.
    `payload` is opaque metadata the caller can attach.
    """

    name: str
    search_text: str
    source: str = "local"
    payload: dict | None = None


def search(
    queries: Sequence[str],
    local_entries: Sequence[ToolEntry],
    connector_entries: Sequence[ToolEntry],
    limit: int = 5,
    max_queries_per_call: int = 7,
) -> dict[str, list[ToolEntry]]:
    """Rank local and connector tools together for each query.

    Args:
        queries: one or more search strings.
        local_entries: tools discovered from scripts/ and built-ins.
        connector_entries: remote/plugin tools resolved by the caller.
        limit: results returned per query (total, not per source).
        max_queries_per_call: hard cap to protect any downstream gateway.

    Returns:
        Mapping from query string to ordered result entries.
    """
    if len(queries) > max_queries_per_call:
        raise TooManyQueriesError(
            f"tool_search received {len(queries)} queries; "
            f"at most {max_queries_per_call} are allowed per call."
        )

    catalog = list(local_entries) + list(connector_entries)
    results: dict[str, list[ToolEntry]] = {}

    for query in queries:
        query_terms = _tokens(query)
        scored: list[tuple[float, ToolEntry]] = []

        for entry in catalog:
            text_terms = _tokens(entry.search_text)
            # Admission gate: every query term must appear somewhere.
            if not all(t in text_terms for t in query_terms):
                continue
            score = _rare_token_score(query_terms, text_terms)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        results[query] = [entry for _, entry in scored[:limit]]

    return results


class TooManyQueriesError(ValueError):
    """Raised when a single call requests more queries than the remote leg allows."""
