"""
Storage waste analytics for the scan report.

Pure read-only summaries over the ItemRecord + FindingRecord lists the
engine already produces. No mutations. Surfaces the four things the
customer-facing report asks for:

  * biggest files on disk (sorted by size, top-N)
  * oldest files (sorted by modified-time, top-N)
  * reclaim potential from duplicates (keep largest, remove the rest)
  * total bytes/items by category/sensitivity

Used by reports.write_summary to fill out the Storage Waste Report
section, by the GUI to render the metrics, and by the
`sensei-clean status` command to show the last-scan headline number.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone

from .schemas import FindingRecord, ItemRecord


def reclaimable_bytes(
    items: Iterable[ItemRecord], findings: Iterable[FindingRecord]
) -> int:
    """How many bytes would be reclaimed if every exact_duplicate
    finding's extra copies were moved to quarantine. Keeps the largest
    copy of each cluster (since 'largest' rarely differs for true
    duplicates, this is mostly a tie-breaker)."""
    size_by_id = {it.item_id: (it.size_bytes or 0) for it in items}
    total = 0
    for f in findings:
        if f.finding_type != "exact_duplicate":
            continue
        sizes = sorted((size_by_id.get(i, 0) for i in f.item_ids), reverse=True)
        total += sum(sizes[1:])
    return total


def biggest_files(items: Iterable[ItemRecord], n: int = 20) -> list[ItemRecord]:
    """Top-N items by size_bytes, largest first. Skips zero-byte files
    so we don't fill the report with empty entries."""
    nonempty = [it for it in items if (it.size_bytes or 0) > 0]
    nonempty.sort(key=lambda it: it.size_bytes or 0, reverse=True)
    return nonempty[:n]


def _modified_ts(item: ItemRecord) -> float | None:
    raw = (item.timestamps or {}).get("modified")
    if not raw:
        return None
    try:
        # ISO 8601 with Z suffix is what LocalFSAdapter writes.
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def oldest_files(items: Iterable[ItemRecord], n: int = 20) -> list[ItemRecord]:
    """Top-N items by modified time (oldest first). Items with no
    timestamp are excluded — the goal is to surface things the user
    forgot, not to penalize records with missing metadata."""
    with_ts = [(it, _modified_ts(it)) for it in items]
    with_ts = [(it, ts) for it, ts in with_ts if ts is not None]
    with_ts.sort(key=lambda pair: pair[1])
    return [it for it, _ in with_ts[:n]]


def stale_age_days(item: ItemRecord, now_ts: float | None = None) -> int | None:
    """Age in days since modified. None when no timestamp."""
    ts = _modified_ts(item)
    if ts is None:
        return None
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    return max(0, int((now - ts) / 86400))


def by_category(items: Iterable[ItemRecord]) -> dict[str, tuple[int, int]]:
    """Returns {category_guess: (count, total_bytes)} sorted by total
    bytes descending. Used in the waste report's category breakdown."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for it in items:
        agg[it.category_guess][0] += 1
        agg[it.category_guess][1] += it.size_bytes or 0
    return dict(
        sorted(
            ((k, (v[0], v[1])) for k, v in agg.items()),
            key=lambda kv: kv[1][1],
            reverse=True,
        )
    )


def human_bytes(n: int) -> str:
    """1234 -> "1.2 KB". Used in the report formatters."""
    if n is None or n < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{f:.1f} {units[-1]}"


def summary(
    items: list[ItemRecord],
    findings: list[FindingRecord],
    biggest_n: int = 10,
    oldest_n: int = 10,
) -> dict:
    """Single-call rollup used by reports + status + GUI."""
    total_bytes = sum((it.size_bytes or 0) for it in items)
    reclaim = reclaimable_bytes(items, findings)
    return {
        "total_items": len(items),
        "total_bytes": total_bytes,
        "total_bytes_human": human_bytes(total_bytes),
        "reclaim_bytes": reclaim,
        "reclaim_bytes_human": human_bytes(reclaim),
        "duplicate_clusters": sum(
            1 for f in findings if f.finding_type == "exact_duplicate"
        ),
        "by_category": [
            {"category": cat, "count": c, "bytes": b, "bytes_human": human_bytes(b)}
            for cat, (c, b) in by_category(items).items()
        ],
        "biggest": [
            {
                "path": it.identity.get("path", ""),
                "name": it.display_name,
                "bytes": it.size_bytes or 0,
                "bytes_human": human_bytes(it.size_bytes or 0),
                "category": it.category_guess,
            }
            for it in biggest_files(items, biggest_n)
        ],
        "oldest": [
            {
                "path": it.identity.get("path", ""),
                "name": it.display_name,
                "modified": (it.timestamps or {}).get("modified"),
                "age_days": stale_age_days(it),
                "bytes": it.size_bytes or 0,
                "bytes_human": human_bytes(it.size_bytes or 0),
            }
            for it in oldest_files(items, oldest_n)
        ],
    }
