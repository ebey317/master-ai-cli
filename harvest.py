"""
Master AI harvest layer.

Every cloud call's (prompt, model, response) gets appended to a local JSONL.
Next time a similar prompt arrives, the cache answers for free — no cloud hit.
Quality past answers also inject into local prompts as few-shot examples,
which is what gets qwen2.5:7b to actually emit EDIT:/CREATE:/RUN: directives
instead of describing changes in prose.

Storage: ~/.master_ai_harvest.jsonl (append-only, one JSON per line)
Similarity: token-level Jaccard. Cheap, no deps, no embedding model needed.
Upgrade path later: swap _jaccard for an nomic-embed-text cosine lookup.
"""

import json
import re
import time
from pathlib import Path

HARVEST_FILE = Path.home() / ".master_ai_harvest.jsonl"
PRIVATE_SKIP_FILE = Path.home() / ".master_ai_harvest_private_skips.jsonl"
MAX_ENTRIES_IN_MEMORY = 2000  # lookup only scans most recent N

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_PRIVATE_PATH_PATTERNS = (
    # 2026-09-08: narrowed from a blanket Desktop/Documents/Downloads/
    # Pictures/jobseeker path block. Desktop and Documents hold ordinary
    # everyday workspace files too (task lists, study plans, glossaries)
    # and blanket-blocking the whole folder dead-ended every cloud-first
    # task that so much as globbed through them, with no local fallback
    # to fall back to. Path-based blocking was also the wrong tool for
    # this anyway: it both over-blocked (a harmless .md on the Desktop)
    # and under-protected (a tax PDF saved anywhere else wasn't caught).
    # _PRIVATE_TERM_RE below already does real content-based detection
    # (resume/tax/ssn/credential/etc.) regardless of which folder the
    # content lives in -- keep the path fence only for folders that are
    # inherently about identity/sensitive-document storage rather than
    # everyday workspace files.
    re.compile(
        r"(?i)(?:^|[\s'\"`])(?:~|/home/[^/\s'\"`]+)/(?:Pictures|Downloads|jobseeker)(?:/|$|[\s'\"`])"
    ),
    re.compile(r"(?i)(?:^|/)\.(?:ssh|gnupg)(?:/|$)"),
    re.compile(r"(?i)(?:^|/)\.aws/(?:credentials|config)(?:$|[\s'\"`])"),
    re.compile(r"(?i)(?:^|/)\.master_ai_keys(?:$|[\s'\"`])"),
    re.compile(r"(?i)(?:^|/)\.netrc(?:$|[\s'\"`])"),
)
_PRIVATE_TERM_RE = re.compile(
    r"(?i)\b("
    r"resume|cover letter|job application|tax|w-?2|1099|irs|bank statement|"
    r"routing number|account number|social security|ssn|medical|doctor|patient|"
    r"prescription|"
    r"driver'?s license|passport"
    r")\b"
)
# 2026-09-12: dropped password|credential|api key|secret token|private key from
# the term list above -- those are generic security jargon that fires on any
# mention (filenames, code comments, casual conversation about auth), not just
# actual leaked secrets. _SECRET_VALUE_PATTERNS below already catches real
# secret VALUES by shape (AKIA/gh_/sk-/PEM) regardless of surrounding wording,
# which is the actual leak risk this gate exists to prevent. Elijah: privacy
# gate was "too harsh" -- narrowed to real PII categories + real secret shapes.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |DSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)
_IMAGE_PATH_RE = re.compile(
    r"(?i)(?:~|/home/[^/\s'\"`]+|/tmp)[^\s'\"`]*\.(?:png|jpe?g|webp|gif|heic|bmp)\b"
)
_REDACT_PATTERNS = (
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "[email]"),
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[phone]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
)


def _tokens(text):
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _privacy_reason(prompt="", response="", meta=None):
    text = f"{prompt or ''}\n{response or ''}"
    meta = meta or {}
    if meta.get("private") or meta.get("has_image"):
        return "private meta"
    for pat in _PRIVATE_PATH_PATTERNS:
        if pat.search(text):
            return "private path"
    if _IMAGE_PATH_RE.search(text):
        return "image path"
    if _PRIVATE_TERM_RE.search(text):
        return "private term"
    for pat in _SECRET_VALUE_PATTERNS:
        if pat.search(text):
            return "secret pattern"
    return ""


def is_private(prompt="", response="", meta=None):
    """True when text should not enter harvest, few-shot, or fine-tune data."""
    return bool(_privacy_reason(prompt, response, meta=meta))


def _redact_for_prompt(text):
    out = text or ""
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _record_private_skip(reason, model, task_type):
    entry = {
        "ts": int(time.time()),
        "reason": reason,
        "model": model,
        "task_type": task_type,
    }
    try:
        PRIVATE_SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRIVATE_SKIP_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


_entries = None
_last_mtime = 0


def _load():
    global _entries, _last_mtime
    if not HARVEST_FILE.exists():
        _entries = []
        _last_mtime = 0
        return
    try:
        mtime = HARVEST_FILE.stat().st_mtime
    except OSError:
        _entries = []
        return
    if _entries is not None and mtime == _last_mtime:
        return
    entries = []
    try:
        with open(HARVEST_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if _privacy_reason(
                        e.get("prompt", ""), e.get("response", ""), e.get("meta")
                    ):
                        continue
                    e["_tokens"] = _tokens(e.get("prompt", ""))
                    entries.append(e)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    if len(entries) > MAX_ENTRIES_IN_MEMORY:
        entries = entries[-MAX_ENTRIES_IN_MEMORY:]
    _entries = entries
    _last_mtime = mtime


def record(prompt, model, response, task_type="chat", meta=None):
    """Append one call to the harvest file. Called after every successful cloud response."""
    if not prompt or not response:
        return
    private_reason = _privacy_reason(prompt, response, meta)
    if private_reason:
        _record_private_skip(private_reason, model, task_type)
        return
    entry = {
        "ts": int(time.time()),
        "prompt": prompt,
        "model": model,
        "response": response,
        "task_type": task_type,
    }
    if meta:
        entry["meta"] = meta
    try:
        HARVEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HARVEST_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return
    global _last_mtime
    _last_mtime = 0  # invalidate in-memory cache


def lookup(prompt, min_similarity=0.80, max_age_days=None, task_type=None):
    """
    Check if a very similar prompt has been answered before.
    Returns (response, similarity, entry) on hit, (None, 0.0, None) on miss.
    Default threshold 0.80 is intentionally strict — only near-duplicates hit.
    """
    _load()
    if not _entries:
        return None, 0.0, None
    q_tokens = _tokens(prompt)
    if not q_tokens:
        return None, 0.0, None
    cutoff_ts = 0
    if max_age_days:
        cutoff_ts = int(time.time()) - max_age_days * 86400
    best = (None, 0.0, None)
    for e in _entries:
        if cutoff_ts and e.get("ts", 0) < cutoff_ts:
            continue
        if task_type and e.get("task_type") != task_type:
            continue
        sim = _jaccard(q_tokens, e.get("_tokens", set()))
        if sim > best[1]:
            best = (e.get("response"), sim, e)
    if best[1] >= min_similarity:
        return best
    return None, 0.0, None


def few_shot(prompt, max_examples=3, min_similarity=0.30, task_type=None):
    """
    Return top-N similar past (prompt, response) pairs as few-shot examples.
    Used to inject format-correct examples into the local model's system prompt.
    Lower threshold than lookup() because we want relevance, not duplicates.
    """
    _load()
    if not _entries:
        return []
    q_tokens = _tokens(prompt)
    if not q_tokens:
        return []
    scored = []
    for e in _entries:
        if task_type and e.get("task_type") != task_type:
            continue
        if _privacy_reason(e.get("prompt", ""), e.get("response", ""), e.get("meta")):
            continue
        sim = _jaccard(q_tokens, e.get("_tokens", set()))
        if sim >= min_similarity:
            scored.append((sim, e))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "prompt": e.get("prompt", ""),
            "response": e.get("response", ""),
            "similarity": round(sim, 3),
            "model": e.get("model", ""),
        }
        for sim, e in scored[:max_examples]
    ]


def format_few_shot(examples, max_prompt_chars=400, max_response_chars=800):
    """Format few-shot examples for injection into a system prompt."""
    if not examples:
        return ""
    lines = ["\n# Past examples of similar tasks (use these for FORMAT, not content):"]
    for i, ex in enumerate(examples, 1):
        p = _redact_for_prompt(ex["prompt"])[:max_prompt_chars]
        r = _redact_for_prompt(ex["response"])[:max_response_chars]
        lines.append(f"\n## Example {i} (similarity {ex['similarity']})")
        lines.append(f"User: {p}")
        lines.append(f"Assistant: {r}")
    return "\n".join(lines) + "\n"


def stats():
    """Harvest stats for the `harvest` Sensei command."""
    _load()
    total = len(_entries) if _entries else 0
    by_model = {}
    by_task = {}
    first_ts = None
    last_ts = None
    for e in _entries or []:
        by_model[e.get("model", "?")] = by_model.get(e.get("model", "?"), 0) + 1
        by_task[e.get("task_type", "?")] = by_task.get(e.get("task_type", "?"), 0) + 1
        ts = e.get("ts", 0)
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
    try:
        file_size = HARVEST_FILE.stat().st_size if HARVEST_FILE.exists() else 0
    except OSError:
        file_size = 0
    return {
        "total_entries": total,
        "file": str(HARVEST_FILE),
        "file_exists": HARVEST_FILE.exists(),
        "file_size_bytes": file_size,
        "by_model": by_model,
        "by_task": by_task,
        "first_entry_ts": first_ts,
        "last_entry_ts": last_ts,
    }


def format_stats():
    """Human-readable stats block for Sensei `harvest` command."""
    s = stats()
    lines = []
    lines.append("📦 Harvest layer")
    lines.append(f"   entries : {s['total_entries']}")
    lines.append(f"   file    : {s['file']} ({s['file_size_bytes']} bytes)")
    if s["first_entry_ts"]:
        first = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["first_entry_ts"]))
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_entry_ts"]))
        lines.append(f"   range   : {first} → {last}")
    if s["by_model"]:
        models = ", ".join(
            f"{m}={n}" for m, n in sorted(s["by_model"].items(), key=lambda x: -x[1])
        )
        lines.append(f"   models  : {models}")
    if s["by_task"]:
        tasks = ", ".join(
            f"{t}={n}" for t, n in sorted(s["by_task"].items(), key=lambda x: -x[1])
        )
        lines.append(f"   tasks   : {tasks}")
    return "\n".join(lines)


# CLI — for quick inspection outside Sensei.  Usage:
#   python3 harvest.py                     # stats
#   python3 harvest.py lookup "some query" # nearest match
#   python3 harvest.py few_shot "query"    # top-3 similar as examples
if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        print(format_stats())
    elif sys.argv[1] == "lookup" and len(sys.argv) > 2:
        q = " ".join(sys.argv[2:])
        r, sim, e = lookup(q, min_similarity=0.30)
        if r:
            print(f"[match sim={sim:.2f}  model={e.get('model')}]")
            print(r)
        else:
            print("(no match above threshold)")
    elif sys.argv[1] == "few_shot" and len(sys.argv) > 2:
        q = " ".join(sys.argv[2:])
        exs = few_shot(q, max_examples=5, min_similarity=0.20)
        if exs:
            print(format_few_shot(exs))
        else:
            print("(no similar entries)")
    else:
        print("usage: harvest.py [lookup|few_shot] <query>")
