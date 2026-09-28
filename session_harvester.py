"""Session harvester for Sensei skill auto-authoring.

Reads recent `.chat` transcripts from ~/.master_ai_chats/ and extracts
reusable, multi-turn workflows that are good candidates for skills.
Stdlib only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHAT_DIR = Path.home() / ".master_ai_chats"
SUMMARY_DIR = CHAT_DIR  # summaries live next to .chat files

# Heuristic signals that a session actually built / fixed something.
_ACTION_VERBS = re.compile(
    r"\b(created?|wrote?|patched?|fixed?|implemented?|added?|installed?|"
    r"configured?|verified?|deployed?|automated?|generated?|wired?|"
    r"built?|refactored?)\b",
    re.IGNORECASE,
)

# Tool directives used by Sensei/Master AI; sequences of these are strong
# indicators of a reproducible workflow.
_TOOL_RE = re.compile(
    r"\b(RUN|BROWSER|BROWSER_CONSOLE|SEARCH|READ|PATCH|WRITE|SCP|FETCH|"
    r"skill\s+create|skill\s+audit|skill\s+improve|mcp\s+list)\b",
    re.IGNORECASE,
)

# Messages that are pure conversational filler, not useful for a skill spec.
_NOISE_RE = re.compile(r"^\s*(hi|hey|hello|ok|okay|thanks|ty|np|brb|lol)\s*$", re.IGNORECASE)


@dataclass
class Candidate:
    name: str
    evidence: str  # one-line summary
    topic: str  # inferred topic
    transcript_path: Path
    message_count: int
    tool_commands: list[str] = field(default_factory=list)
    user_messages: list[str] = field(default_factory=list)


def _is_action_message(text: str) -> bool:
    return bool(_ACTION_VERBS.search(text) or _TOOL_RE.search(text))


def _extract_tool_commands(text: str) -> list[str]:
    """Pull RUN/BROWSER/SEARCH/etc command lines out of raw tool-result blocks."""
    out = []
    for m in _TOOL_RE.finditer(text):
        start = m.start()
        # grab from keyword to end of line or reasonable block
        end = text.find("\n", start)
        if end == -1:
            end = len(text)
        snippet = text[start:end].strip()
        if snippet:
            out.append(snippet)
    return out


def _strip_tool_blocks(text: str) -> str:
    """Return text with big tool-result blocks removed, leaving user intent."""
    # Remove lines that look like tool result headers and JSON dumps.
    lines = []
    skip = False
    for line in text.splitlines():
        if line.strip().startswith("[TOOL") or line.strip().startswith("[RUN RESULT]"):
            skip = True
            continue
        if skip and (line.strip() == "" or line.strip().startswith("Exit:") or line.strip().startswith("Output:")):
            continue
        if skip and not line.strip().startswith("["):
            skip = False
        if not skip:
            lines.append(line)
    return "\n".join(lines).strip()


def _topic_from_messages(messages: list[str]) -> str:
    """Naive topic extraction: first mention of a concrete noun phrase."""
    combined = " ".join(messages)[:2000]
    # look for "create a X", "build a X", "how do I X"
    m = re.search(r"(?:create|build|make|set up|install|fix|automate|wire)\s+(?:a|an|the|my)?\s+(.{3,60}?)(?:\.|\?|!|\n|for|with|using|to)", combined, re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()[:60]
    # fallback: first 3 meaningful words
    words = [w for w in re.findall(r"[A-Za-z0-9_]{4,}", combined) if w.lower() not in {"this", "that", "with", "from", "your", "have", "want", "need"}]
    return " ".join(words[:3]).lower()[:60]


def _name_from_topic(topic: str) -> str:
    """Convert a topic into a kebab-case skill name."""
    slug = topic.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug[:50].strip("-") or "untitled-skill"


def _read_chat(path: Path) -> list[dict[str, Any]]:
    """Parse a Sensei .chat file into turn dicts.

    Handles the two formats seen in the wild:
      - numbered: 1|[timestamp] You: ...
      - bare:     [timestamp] You: ...
    """
    if not path.exists():
        return []
    messages = []
    current = None
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.rstrip("\n")
        # Format: optional N| prefix, then [timestamp] Role: text
        m = re.match(r"(?:\d+\|)?\[([^\]]+)\]\s*(You|AI):\s*(.*)", line)
        if m:
            if current:
                messages.append(current)
            current = {"ts": m.group(1), "role": m.group(2).lower(), "text": m.group(3)}
        elif current is not None:
            current["text"] += "\n" + line
    if current:
        messages.append(current)
    return messages


def _read_summary(path: Path) -> dict[str, Any]:
    """Parse a .summary file if present; returns dict with bullets/next."""
    if not path.exists():
        return {}
    text = path.read_text(errors="replace")
    bullets = re.findall(r"•\s+(.+)", text)
    next_step = ""
    m = re.search(r"Next:\s*(.+)", text, re.IGNORECASE)
    if m:
        next_step = m.group(1).strip()
    return {"bullets": bullets, "next": next_step, "raw": text}


def _score_candidate(msgs: list[dict[str, Any]]) -> tuple[float, list[str], list[str]]:
    """Score a session for skill-worthiness.

    Returns (score 0-1, tool_commands, user_intents).
    Higher score = stronger candidate.
    """
    user_texts = [_strip_tool_blocks(m["text"]) for m in msgs if m.get("role") == "you"]
    user_texts = [t for t in user_texts if t and not _NOISE_RE.match(t)]
    ai_texts = [m["text"] for m in msgs if m.get("role") == "ai"]

    if len(user_texts) < 3:
        return 0.0, [], []

    action_count = sum(1 for t in user_texts if _is_action_message(t))
    tool_commands = []
    for t in ai_texts + user_texts:
        tool_commands.extend(_extract_tool_commands(t))

    # Score components
    action_ratio = action_count / len(user_texts)
    tool_depth = min(len(tool_commands) / 5.0, 1.0)
    length_factor = min(len(user_texts) / 8.0, 1.0)

    score = action_ratio * 0.4 + tool_depth * 0.4 + length_factor * 0.2
    return score, tool_commands, user_texts


def harvest_recent(limit: int = 20, min_score: float = 0.35) -> list[Candidate]:
    """Scan the most recent chat files and return candidate skill topics.

    Args:
        limit: number of recent .chat files to inspect.
        min_score: minimum candidacy score to return.
    """
    if not CHAT_DIR.is_dir():
        return []

    chats = sorted(CHAT_DIR.glob("*.chat"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for chat_path in chats:
        summary_path = chat_path.with_suffix(".summary")
        msgs = _read_chat(chat_path)
        if not msgs:
            continue
        score, tool_commands, user_texts = _score_candidate(msgs)
        if score < min_score:
            continue
        topic = _topic_from_messages(user_texts)
        name = _name_from_topic(topic)
        # de-dupe simple variants
        if any(c.name == name for c in out):
            continue
        evidence = f"{len(user_texts)} user turns, {len(tool_commands)} tool commands, score={score:.2f}"
        out.append(
            Candidate(
                name=name,
                evidence=evidence,
                topic=topic,
                transcript_path=chat_path,
                message_count=len(user_texts),
                tool_commands=tool_commands[:20],
                user_messages=user_texts,
            )
        )
    return out


def best_candidate() -> Candidate | None:
    """Return the single strongest candidate from recent sessions, or None."""
    candidates = harvest_recent(limit=50, min_score=0.45)
    if not candidates:
        return None
    # prefer transcripts with tool commands and more turns
    return max(candidates, key=lambda c: (len(c.tool_commands), c.message_count))


if __name__ == "__main__":
    for c in harvest_recent(limit=10, min_score=0.3):
        print(f"{c.name}: {c.topic} ({c.evidence})")
        print(f"  from {c.transcript_path}")
        print(f"  sample tool: {c.tool_commands[0] if c.tool_commands else 'none'}")
        print()
