#!/usr/bin/env python3
# Sensei reflector — aggregate recent audit events into a digest.
# Pure analysis: counts, groupings, time-of-day. No LLM call.
# Reads from the FTS index (sensei_memory_index.sqlite) populated by
# sensei_memory_index.py — run that first if the index is empty.
#
# Usage:
#   python3 sensei_reflect.py              # reflect on last 2000 events
#   python3 sensei_reflect.py --tail 5000
#   python3 sensei_reflect.py --show       # print the most recent digest

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".sensei_memory_index.sqlite"
DIGEST_PATH = Path.home() / ".sensei_reflections.jsonl"
AUDIT_SOURCE = str(Path.home() / ".master_ai_audit_typed.jsonl")
TOP_N = 10


def parse_ts(value) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value[:26], fmt)
            except ValueError:
                continue
    return None


def fetch_recent_audit(limit: int) -> list[dict]:
    if not DB_PATH.exists():
        sys.exit("no FTS index yet — run `python3 sensei_memory_index.py build` first")
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT body FROM docs
            WHERE source = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (AUDIT_SOURCE, limit),
        ).fetchall()
    finally:
        conn.close()
    events: list[dict] = []
    for (body,) in rows:
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def extract_error_signature(ev: dict) -> str | None:
    fs = ev.get("final_state")
    if not isinstance(fs, dict):
        return None
    for key in ("error", "err", "message", "reason"):
        v = fs.get(key)
        if isinstance(v, str) and v:
            return v[:160]
    if fs.get("ok") is False:
        return "ok=false (no message)"
    return None


def build_digest(events: list[dict]) -> dict:
    sources: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    results: Counter[str] = Counter()
    source_kind: Counter[str] = Counter()
    hour_hist: Counter[int] = Counter()
    error_sigs: Counter[str] = Counter()

    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for ev in events:
        ts_raw = ev.get("ts")
        ts = parse_ts(ts_raw)
        if ts:
            hour_hist[ts.hour] += 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        src = ev.get("source") or "?"
        kind = ev.get("kind") or "?"
        sources[src] += 1
        kinds[kind] += 1
        source_kind[f"{src}/{kind}"] += 1
        v = ev.get("verdict")
        if v:
            verdicts[v] += 1
        r = ev.get("result")
        if r:
            results[r] += 1
        if r == "error" or ev.get("verdict") == "reject":
            sig = extract_error_signature(ev)
            if sig:
                error_sigs[sig] += 1

    return {
        "generated_at": int(time.time()),
        "window_event_count": len(events),
        "window_start": first_ts.isoformat() if first_ts else None,
        "window_end": last_ts.isoformat() if last_ts else None,
        "top_sources": sources.most_common(TOP_N),
        "top_kinds": kinds.most_common(TOP_N),
        "top_source_kind": source_kind.most_common(TOP_N),
        "verdicts": dict(verdicts),
        "results": dict(results),
        "hour_histogram": dict(sorted(hour_hist.items())),
        "top_errors": error_sigs.most_common(TOP_N),
    }


def print_digest(d: dict) -> None:
    print(
        f"--- sensei reflection {datetime.fromtimestamp(d['generated_at']).isoformat()} ---"
    )
    print(
        f"events in window: {d['window_event_count']}  "
        f"({d['window_start']} → {d['window_end']})"
    )
    print(f"verdicts: {d['verdicts']}   results: {d['results']}")
    print("top source/kind:")
    for k, n in d["top_source_kind"]:
        print(f"  {n:>5}  {k}")
    if d["top_errors"]:
        print("top error signatures:")
        for sig, n in d["top_errors"]:
            print(f"  {n:>4}  {sig}")
    else:
        print("top error signatures: (none)")
    print("hour histogram (0-23):")
    for hr, n in d["hour_histogram"].items():
        bar = "█" * min(40, n)
        print(f"  {hr:02d}  {n:>5}  {bar}")


def append_digest(d: dict) -> None:
    with DIGEST_PATH.open("a") as fh:
        fh.write(json.dumps(d) + "\n")


def cmd_show() -> int:
    if not DIGEST_PATH.exists():
        print("no reflections yet")
        return 1
    last = None
    with DIGEST_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        print("no reflections yet")
        return 1
    print_digest(json.loads(last))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Sensei audit-log reflector")
    ap.add_argument(
        "--tail", type=int, default=2000, help="how many recent events to reflect on"
    )
    ap.add_argument(
        "--show", action="store_true", help="print the most recent stored digest"
    )
    args = ap.parse_args(argv[1:])

    if args.show:
        return cmd_show()

    events = fetch_recent_audit(args.tail)
    if not events:
        print("no audit events in index", file=sys.stderr)
        return 1
    digest = build_digest(events)
    append_digest(digest)
    print_digest(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
