"""Semantic browser find subagent.

Input:
    task: query string such as "apply button" or "send resume"
    context: {"ax_tree": ...} where ax_tree is the compact Chrome
             accessibility snapshot emitted by service_worker.js.

Output:
    {"matches": [{"ref", "name", "role", "selector", "confidence"}], ...}

This is deliberately deterministic. The Anthropic version can spend a nested
LLM call on the full tree; Sensei's local-first path needs a reliable offline
fallback. The scoring below behaves like semantic search for common browser
controls by expanding intent terms, role priors, and action synonyms.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

name = "find"
description = "Find browser elements semantically inside a compact AX tree"

_MAX_CANDIDATES = 800
_MAX_MATCHES = 20

_SYNONYMS = {
    "apply": {"apply", "application", "submit", "send", "resume", "cv", "candidate"},
    "submit": {"submit", "send", "apply", "continue", "finish", "complete"},
    "send": {"send", "submit", "apply", "share"},
    "resume": {"resume", "résumé", "cv", "curriculum", "vitae", "file", "upload"},
    "upload": {"upload", "attach", "choose", "file", "resume", "cv", "browse"},
    "login": {"login", "log", "sign", "signin", "account", "continue"},
    "sign": {"sign", "login", "register", "join", "account"},
    "search": {"search", "find", "lookup", "query"},
    "next": {"next", "continue", "forward", "proceed"},
    "back": {"back", "previous", "return"},
    "delete": {"delete", "remove", "trash", "discard"},
    "cancel": {"cancel", "close", "dismiss", "stop"},
    "save": {"save", "store", "keep", "update"},
}

_ROLE_HINTS = {
    "button": {"button"},
    "link": {"link"},
    "field": {"textbox", "searchbox", "combobox", "spinbutton", "slider"},
    "input": {"textbox", "searchbox", "combobox", "spinbutton", "slider"},
    "text": {"textbox", "searchbox"},
    "checkbox": {"checkbox", "switch"},
    "radio": {"radio"},
    "tab": {"tab", "treeitem"},
    "row": {"row", "gridcell", "listitem", "treeitem"},
    "folder": {"row", "treeitem", "listitem", "link"},
}


def _norm(text: Any) -> str:
    raw = str(text or "").lower()
    raw = "".join(
        ch
        for ch in unicodedata.normalize("NFD", raw)
        if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", raw).strip()


def _terms(text: Any) -> list[str]:
    return [t for t in _norm(text).split() if len(t) >= 2]


def _expanded_terms(query: str) -> set[str]:
    terms = set(_terms(query))
    expanded = set(terms)
    for term in terms:
        expanded.update(_SYNONYMS.get(term, set()))
    return {_norm(t) for t in expanded if _norm(t)}


def _role_targets(query: str) -> set[str]:
    roles = set()
    for term in _terms(query):
        roles.update(_ROLE_HINTS.get(term, set()))
    return roles


def _iter_nodes(ax_tree: Any):
    if not isinstance(ax_tree, dict):
        return
    buckets = [
        "buttons",
        "links",
        "inputs",
        "dialogs",
        "file_folder_rows",
        "lists",
        "headings",
        "landmarks",
        "rows",
    ]
    seen = set()
    count = 0
    for bucket in buckets:
        values = ax_tree.get(bucket)
        if not isinstance(values, list):
            continue
        for node in values:
            if not isinstance(node, dict):
                continue
            ref = str(node.get("ref") or "")
            key = (
                ref
                or f"{bucket}:{node.get('role')}:{node.get('name')}:{node.get('selector')}"
            )
            if key in seen:
                continue
            seen.add(key)
            count += 1
            if count > _MAX_CANDIDATES:
                return
            yield node


def _score_node(
    node: dict, query: str, expanded: set[str], role_targets: set[str]
) -> tuple[int, list[str]]:
    role = _norm(node.get("role"))
    name_text = _norm(node.get("name"))
    value_text = _norm(node.get("value"))
    selector_text = _norm(node.get("selector"))
    haystack = " ".join(x for x in (name_text, value_text, selector_text, role) if x)
    hay_terms = set(haystack.split())
    q_terms = set(_terms(query))

    score = 0
    reasons: list[str] = []
    if role_targets and role in role_targets:
        score += 28
        reasons.append(f"role:{role}")
    if name_text == _norm(query):
        score += 70
        reasons.append("exact_name")
    if _norm(query) and _norm(query) in name_text:
        score += 52
        reasons.append("query_in_name")
    overlap = expanded & hay_terms
    if overlap:
        score += min(48, 12 * len(overlap))
        reasons.append("terms:" + ",".join(sorted(overlap)[:5]))
    direct_overlap = q_terms & hay_terms
    if direct_overlap:
        score += min(36, 18 * len(direct_overlap))
        reasons.append("direct:" + ",".join(sorted(direct_overlap)[:5]))
    if (
        role in {"button", "link"}
        and {"apply", "submit", "send", "continue"} & expanded
    ):
        score += 18
        reasons.append("actionable")
    if (
        role in {"textbox", "searchbox", "combobox"}
        and {"field", "input", "type", "enter", "search"} & expanded
    ):
        score += 18
        reasons.append("input_role")
    if node.get("state", {}).get("disabled"):
        score -= 30
        reasons.append("disabled")
    if not node.get("ref") and not node.get("selector"):
        score -= 12
        reasons.append("no_handle")
    return score, reasons


def run(task, context=None):
    query = str(task or "").strip()
    if not query:
        return {"error": "find: missing query", "matches": []}
    ctx = context if isinstance(context, dict) else {}
    ax_tree = ctx.get("ax_tree") or ctx.get("tree") or {}
    expanded = _expanded_terms(query)
    role_targets = _role_targets(query)
    scored = []
    for node in _iter_nodes(ax_tree) or []:
        score, reasons = _score_node(node, query, expanded, role_targets)
        if score <= 0:
            continue
        confidence = max(0.05, min(0.99, score / 120.0))
        scored.append(
            {
                "ref": str(node.get("ref") or ""),
                "name": str(node.get("name") or "")[:240],
                "role": str(node.get("role") or "")[:80],
                "selector": str(node.get("selector") or "")[:300],
                "confidence": round(confidence, 3),
                "score": score,
                "reasons": reasons[:6],
            }
        )
    scored.sort(key=lambda item: (-item["score"], -item["confidence"], item["name"]))
    matches = [
        {k: v for k, v in item.items() if k not in {"score", "reasons"}}
        for item in scored[:_MAX_MATCHES]
    ]
    return {
        "matches": matches,
        "query": query,
        "count": len(matches),
        "summary": f"{len(matches)} semantic match(es) for {query!r}",
    }
