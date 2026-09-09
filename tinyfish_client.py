"""TinyFish REST client for Sensei CLI (stdlib only).

Mirrors the TinyFish Hermes plugin's REST client shape but uses only
stdlib urllib so Sensei has no new pip dependencies. Reads the API key
from TINYFISH_API_KEY env, then MCP_TINYFISH_API_KEY env, then the
Hermes .env file at ~/.hermes/.env (written by `tinyfish connect hermes`).

Exposes the free/search tools Sensei actually needs:
  - search(query, ...)
  - fetch_content(urls, ...)
  - wallet()
  - create_browser_session(url=..., timeout_seconds=...)
  - close_browser_session(session_id)

2026-09-07: added so Sensei CLI can route web_search() through TinyFish
when a key is present, matching Hermes' `search_backend: tinyfish` setup.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
BROWSER_URL = "https://api.browser.tinyfish.ai"
WALLET_URL = "https://agent.tinyfish.ai/v1/wallet"


def _load_key() -> Optional[str]:
    """Resolve the TinyFish API key from env or Hermes .env."""
    for key in (os.environ.get("TINYFISH_API_KEY"), os.environ.get("MCP_TINYFISH_API_KEY")):
        if key and key.strip():
            return key.strip()
    env_path = Path.home() / ".hermes" / ".env"
    try:
        if env_path.exists():
            text = env_path.read_text()
            for line in text.splitlines():
                if line.startswith("MCP_TINYFISH_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
    except Exception:
        pass
    return None


_API_KEY: Optional[str] = _load_key()


def has_key() -> bool:
    global _API_KEY
    _API_KEY = _load_key()
    return _API_KEY is not None


def api_key() -> Optional[str]:
    global _API_KEY
    _API_KEY = _load_key()
    return _API_KEY


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise RuntimeError("TinyFish API key not found")
    return {"X-API-Key": key, "Accept": "application/json"}


def _json_headers() -> dict[str, str]:
    h = _headers()
    h["Content-Type"] = "application/json"
    return h


def _get_json(url: str, headers: dict[str, str], timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: float = 30.0) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _delete(url: str, headers: dict[str, str], timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": 404, "ok": False}
        raise


def search(
    query: str,
    *,
    location: Optional[str] = None,
    language: Optional[str] = None,
    recency_minutes: Optional[int] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    domain_type: Optional[str] = None,
    page: Optional[int] = None,
    purpose: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run a TinyFish Search API query. Returns the raw JSON dict."""
    params: dict[str, Any] = {"query": query}
    for name, value in (
        ("location", location),
        ("language", language),
        ("recency_minutes", recency_minutes),
        ("after_date", after_date),
        ("before_date", before_date),
        ("domain_type", domain_type),
        ("page", page),
        ("purpose", purpose),
    ):
        if value is not None and value != "":
            params[name] = value

    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{SEARCH_URL}?{qs}"
    return _get_json(url, _headers(), timeout=timeout)


def fetch_content(
    urls: list[str],
    *,
    output_format: str = "markdown",
    links: Optional[bool] = None,
    image_links: Optional[bool] = None,
    ttl: Optional[int] = None,
    per_url_timeout_ms: Optional[int] = None,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Run TinyFish Fetch for one or more URLs. Returns raw JSON dict."""
    body: dict[str, Any] = {"urls": list(urls), "format": output_format}
    for name, value in (
        ("links", links),
        ("image_links", image_links),
        ("ttl", ttl),
        ("per_url_timeout_ms", per_url_timeout_ms),
    ):
        if value is not None:
            body[name] = value
    return _post_json(FETCH_URL, body, _json_headers(), timeout=timeout)


def wallet(timeout: float = 30.0) -> dict[str, Any]:
    """Return TinyFish wallet/balance info."""
    return _get_json(WALLET_URL, _headers(), timeout=timeout)


def create_browser_session(
    url: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Create a TinyFish remote browser session."""
    body: dict[str, Any] = {}
    if url:
        body["url"] = url
    if timeout_seconds is not None:
        body["timeout_seconds"] = timeout_seconds
    return _post_json(BROWSER_URL, body, _json_headers(), timeout=timeout)


def close_browser_session(session_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """Close a TinyFish browser session."""
    return _delete(f"{BROWSER_URL}/{session_id}", _headers(), timeout=timeout)


def format_search(result: dict[str, Any], max_results: int = 5) -> str:
    """Format a TinyFish search result into the string shape web_search() uses."""
    results = result.get("results") or result.get("data", {}).get("results") or []
    if not results:
        return ""
    lines = ["[TinyFish Search]"]
    for i, r in enumerate(results[:max_results], 1):
        title = r.get("title") or "(no title)"
        url = r.get("url") or ""
        snippet = r.get("snippet") or r.get("description") or ""
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


def format_fetch(result: dict[str, Any]) -> str:
    """Format a TinyFish fetch result into a plain-text block."""
    results = result.get("results") or result.get("data", {}).get("results") or []
    if not results:
        return ""
    lines = ["[TinyFish Fetch]"]
    for r in results:
        url = r.get("url") or ""
        title = r.get("title") or ""
        content = r.get("content") or r.get("text") or ""
        lines.append(f"URL: {url}\nTitle: {title}\n{content[:4000]}")
    return "\n\n".join(lines)


# urllib.parse.quote import needed for _post_json? no, but for qs yes
import urllib.parse  # noqa: E402
