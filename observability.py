"""Master AI observability rollup (P1.7).

Aggregates the two JSONL telemetry streams Sensei already writes:

  ~/.master_ai_router_metrics.jsonl  — route decisions, model calls,
                                        execution outcomes, harvest hits
                                        (commit 3b55fc0)
  ~/.master_ai_audit_typed.jsonl     — typed action audit records, one
                                        per RUN/RUNTERM/READ/CREATE/EDIT
                                        outcome (P0.4)

Returns a summary dict that callers (Sensei `stats` command, Pupil
`/metrics` endpoint) can render directly. Best-effort: missing files
yield zero counts, malformed lines are skipped.

Public API:
    summarize(limit=500) -> dict
    format_stats(summary, width=72) -> str       — terminal-friendly
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROUTER_METRICS_FILE = Path.home() / ".master_ai_router_metrics.jsonl"
AUDIT_TYPED_FILE = Path.home() / ".master_ai_audit_typed.jsonl"


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path or not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize(
    *,
    limit: int = 500,
    metrics_path: Path | None = None,
    audit_path: Path | None = None,
) -> dict:
    """Return a summary dict of the last ``limit`` events in each stream.

    Shape (all keys always present, counts may be 0):
        {
          'events_scanned':   {'router': N, 'audit': N},
          'by_route':         {route_name: count, ...},
          'by_model':         {model_name: count, ...},
          'route_decisions':  N,
          'model_calls':      N,
          'executions':       {'ok': N, 'fail': N},
          'blocked':          {'total': N, 'by_kind': {kind: count}},
          'audit_status':     {'completed': N, 'blocked': N, ...},
          'audit_by_kind':    {'RUN': N, 'EDIT': N, ...},
          'audit_by_risk':    {'safe': N, 'normal': N, 'high': N},
          'harvest':          {'hits': N, 'records': N},
          'hook_fires':       N,
          'fallbacks':        [{reason, ts}, ...]  # last 5
        }
    """
    metrics_path = metrics_path or ROUTER_METRICS_FILE
    audit_path = audit_path or AUDIT_TYPED_FILE

    router_events = _tail_jsonl(metrics_path, limit)
    audit_events = _tail_jsonl(audit_path, limit)

    by_route: Counter = Counter()
    by_model: Counter = Counter()
    route_decisions = 0
    model_calls = 0
    exec_ok = 0
    exec_fail = 0
    blocked_total = 0
    blocked_by_kind: Counter = Counter()
    harvest_hits = 0
    harvest_records = 0
    hook_fires = 0
    fallbacks: list[dict] = []

    for e in router_events:
        kind = e.get("kind", "")
        if kind == "route_decision":
            route_decisions += 1
            r = e.get("route") or ""
            if r:
                by_route[r] += 1
            m = e.get("model") or ""
            if m:
                by_model[m] += 1
            reason = e.get("reason") or ""
            if "fallback" in reason.lower() or "unavailable" in reason.lower():
                fallbacks.append({"reason": reason[:200], "ts": e.get("ts")})
        elif kind == "model_call":
            model_calls += 1
            m = e.get("model") or ""
            if m:
                by_model[m] += 1
        elif kind == "execution":
            if e.get("ok"):
                exec_ok += 1
            else:
                exec_fail += 1
        elif kind in ("harvest_lookup", "harvest_hit", "cached"):
            harvest_hits += 1
        elif kind == "harvest_record":
            harvest_records += 1
        elif kind in ("HOOK-BLOCK", "hook_block") or kind.startswith("hook_"):
            hook_fires += 1

    audit_status: Counter = Counter()
    audit_by_kind: Counter = Counter()
    audit_by_risk: Counter = Counter()
    for r in audit_events:
        st = r.get("status") or ""
        if st:
            audit_status[st] += 1
        k = r.get("kind") or ""
        if k:
            audit_by_kind[k] += 1
        risk = r.get("risk") or ""
        if risk:
            audit_by_risk[risk] += 1
        # Blocked audit records also flow into the blocked rollup.
        if st == "blocked":
            blocked_total += 1
            blocked_by_kind[k or "unknown"] += 1
        if r.get("audit_kind", "").upper().startswith("HOOK-BLOCK"):
            hook_fires += 1

    fallbacks = fallbacks[-5:]
    return {
        "events_scanned": {"router": len(router_events), "audit": len(audit_events)},
        "by_route": dict(by_route),
        "by_model": dict(by_model),
        "route_decisions": route_decisions,
        "model_calls": model_calls,
        "executions": {"ok": exec_ok, "fail": exec_fail},
        "blocked": {"total": blocked_total, "by_kind": dict(blocked_by_kind)},
        "audit_status": dict(audit_status),
        "audit_by_kind": dict(audit_by_kind),
        "audit_by_risk": dict(audit_by_risk),
        "harvest": {"hits": harvest_hits, "records": harvest_records},
        "hook_fires": hook_fires,
        "fallbacks": fallbacks,
    }


def format_stats(summary: dict, width: int = 72) -> str:
    """Render a terminal-friendly stats block. Plain text — caller adds
    color codes if it wants them. width is a target line length, not
    a hard cap.
    """
    if not isinstance(summary, dict):
        return "(no stats)"
    lines: list[str] = []
    es = summary.get("events_scanned", {})
    lines.append(
        f"Observability — scanned {es.get('router', 0)} router events, "
        f"{es.get('audit', 0)} typed audit records"
    )
    lines.append("")

    def _section(title: str, body: list[str]):
        lines.append(f"── {title} {'─' * max(2, width - len(title) - 4)}")
        for ln in body:
            lines.append(f"  {ln}")
        lines.append("")

    def _topn(d: dict, n: int = 6) -> list[str]:
        items = sorted(d.items(), key=lambda kv: -kv[1])[:n]
        if not items:
            return ["(none)"]
        return [f"{v:>5}  {k}" for k, v in items]

    _section("Routes", _topn(summary.get("by_route", {})))
    _section("Models", _topn(summary.get("by_model", {})))
    blocked = summary.get("blocked", {})
    _section(
        "Blocked actions",
        [f"total: {blocked.get('total', 0)}"] + _topn(blocked.get("by_kind", {})),
    )
    exec_ok = summary.get("executions", {}).get("ok", 0)
    exec_fail = summary.get("executions", {}).get("fail", 0)
    total_exec = exec_ok + exec_fail
    success_rate = (exec_ok * 100 // total_exec) if total_exec else 0
    _section(
        "Executions", [f"ok: {exec_ok}  fail: {exec_fail}  success: {success_rate}%"]
    )
    _section("Audit kinds", _topn(summary.get("audit_by_kind", {})))
    _section("Audit risk", _topn(summary.get("audit_by_risk", {})))
    _section(
        "Harvest",
        [
            f"hits: {summary.get('harvest', {}).get('hits', 0)}",
            f"records: {summary.get('harvest', {}).get('records', 0)}",
        ],
    )
    _section("Hook fires", [f"total: {summary.get('hook_fires', 0)}"])
    fbs = summary.get("fallbacks", [])
    if fbs:
        _section(
            "Recent fallbacks (last 5)",
            [(f.get("reason") or "")[: width - 6] for f in fbs],
        )
    return "\n".join(lines)
