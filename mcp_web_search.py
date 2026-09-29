#!/usr/bin/env python3
"""MCP server for web research — Google Search + Wikipedia.

Tools:
  web_search(query, num_results=5)     — Google via Serper.dev
  wikipedia_search(query, sentences=3) — Wikipedia summary (free, no key)

Uses SERPER_API_KEY from ~/.master_ai_keys or environment.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web_search")


def _load_key(name: str, env_fallback: str) -> str | None:
    """Read a key from ~/.master_ai_keys (JSON or key=value)."""
    keys_file = Path.home() / ".master_ai_keys"
    if not keys_file.exists():
        return os.environ.get(env_fallback)
    text = keys_file.read_text()
    # Try JSON first
    try:
        data = json.loads(text)
        return data.get(name) or data.get(name.lower())
    except json.JSONDecodeError:
        pass
    # Fallback: key=value lines
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(name):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(env_fallback)


_SERPER_KEY = _load_key("SERPER_API_KEY", "SERPER_API_KEY")
_FIRECRAWL_KEY = _load_key("FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY")


@mcp.tool()
def web_search(query: str, num_results: int = 5) -> str:
    """Search the web using Google (via Serper.dev).

    Args:
        query: The search query string.
        num_results: Number of results to return (1-10, default 5).

    Returns:
        A formatted string with search results (title, URL, snippet).
    """
    if not _SERPER_KEY:
        return "[ERROR] SERPER_API_KEY not found in ~/.master_ai_keys or environment."

    num_results = max(1, min(10, num_results))

    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": _SERPER_KEY,
        "Content-Type": "application/json",
    }
    body = json.dumps({"q": query, "num": num_results}).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return f"[ERROR] Serper HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

    # Format organic results
    organic = data.get("organic", [])
    if not organic:
        return f"No organic results for: {query}"

    lines = [f"Web search results for: '{query}'\n"]
    for i, r in enumerate(organic[:num_results], 1):
        title = r.get("title", "No title")
        link = r.get("link", "No URL")
        snippet = r.get("snippet", "No snippet")
        lines.append(f"{i}. {title}\n   URL: {link}\n   {snippet}\n")

    # Include answer box if present
    answer = data.get("answerBox")
    if answer:
        ans_text = answer.get("answer") or answer.get("snippet", "")
        if ans_text:
            lines.insert(1, f"📌 Answer: {ans_text}\n")

    return "\n".join(lines)


@mcp.tool()
def wikipedia_search(query: str, sentences: int = 3) -> str:
    """Search Wikipedia for a topic and return a summary.

    Free — no API key needed. Great for facts, history, science,
    biographies, definitions, and general knowledge.

    Args:
        query: Topic to search (e.g. "Artificial intelligence", "Moon landing").
        sentences: Number of summary sentences (1-10, default 3).

    Returns:
        A concise summary from Wikipedia, or a list of suggestions
        if the exact page doesn't exist.
    """
    sentences = max(1, min(10, sentences))

    # Try to get a summary directly
    encoded = urllib.parse.quote(query.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    headers = {"User-Agent": "MCP-Research-Agent/1.0 (elijah@local)"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Page not found — try search suggestions
            return _wikipedia_suggest(query)
        return f"[ERROR] Wikipedia HTTP {e.code}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

    title = data.get("title", query)
    extract = data.get("extract", "No summary available.")
    page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")

    lines = [f"📖 Wikipedia: {title}"]
    if page_url:
        lines.append(f"   {page_url}\n")
    lines.append(extract)
    return "\n".join(lines)


def _wikipedia_suggest(query: str) -> str:
    """Return search suggestions when exact page not found."""
    url = (
        f"https://en.wikipedia.org/w/api.php?"
        f"action=opensearch&search={urllib.parse.quote(query)}"
        f"&limit=5&namespace=0&format=json"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "MCP-Research-Agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return f"[ERROR] Wikipedia suggest failed: {e}"

    # data format: [query, [titles], [descriptions], [urls]]
    titles = data[1] if len(data) > 1 else []
    if not titles:
        return f"No Wikipedia page found for: '{query}'"

    lines = [f"No exact match for '{query}'. Did you mean:\n"]
    for t in titles[:5]:
        lines.append(f"  • {t}")
    return "\n".join(lines)


@mcp.tool()
def scrape_page(url: str, max_chars: int = 8000) -> str:
    """Scrape a full web page and return clean text/markdown.

    Use this when you need to READ the complete content of a page,
    not just search snippets. Great for job postings, articles,
    documentation, forms, and any page with a URL.

    Args:
        url: Full URL to scrape (e.g. "https://example.com/job-posting").
        max_chars: Max characters to return (500-20000, default 8000).

    Returns:
        Clean markdown/text of the page content.
    """
    if not _FIRECRAWL_KEY:
        return (
            "[ERROR] FIRECRAWL_API_KEY not found in ~/.master_ai_keys or environment."
        )

    max_chars = max(500, min(20000, max_chars))

    api_url = "https://api.firecrawl.dev/v1/scrape"
    headers = {
        "Authorization": f"Bearer {_FIRECRAWL_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(
        {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
    ).encode()

    req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return f"[ERROR] Firecrawl HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"

    if not data.get("success"):
        err = data.get("error", "Unknown error")
        return f"[ERROR] Firecrawl failed: {err}"

    result = data.get("data", {})
    markdown = result.get("markdown", "")
    title = result.get("metadata", {}).get("title", "")

    if not markdown:
        return f"[ERROR] No content extracted from {url}"

    # Truncate if needed
    if len(markdown) > max_chars:
        markdown = markdown[:max_chars].rstrip() + "\n\n[…content truncated…]"

    header = f"📄 {title}\n🔗 {url}\n" if title else f"📄 {url}\n"
    return header + "─" * 40 + "\n" + markdown


if __name__ == "__main__":
    mcp.run()
