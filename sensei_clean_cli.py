#!/usr/bin/env python3
"""
Sensei Clean — review-first local cleanup.

Three customer commands:

  sensei-clean scan   [--roots ...] [--sha256] [--run-dir DIR]
  sensei-clean apply  RUN_DIR [--yes] [--approve-monitored]
  sensei-clean undo   RUN_DIR

Default behavior is SCAN — nothing on disk gets moved. Run `apply`
explicitly to enact the queued actions. Run `undo` to reverse what was
applied.

Privacy: items whose path or content matches the harvest privacy policy
(Pictures / Documents / Downloads / Desktop / jobseeker / .ssh / .gnupg /
.aws/credentials / .master_ai_keys / .netrc, plus the standard private
terms and secret patterns) get sensitivity bumped and their actions are
routed to the 'monitored' lane with approval_required=True. Apply only
acts on those when --approve-monitored is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from sensei_clean import status as _status
from sensei_clean import waste as _waste
from sensei_clean.adapters.local_fs import LocalFSAdapter
from sensei_clean.apply import apply_actions, load_undo_records, undo_actions
from sensei_clean.connectors import detect_sources
from sensei_clean.engine import scan_run
from sensei_clean.policy import MONITORED_SENSITIVITIES
from sensei_clean.schemas import (
    ActionRecord,
    CapabilityReport,
    FindingRecord,
    ItemRecord,
)

# Source of truth for privacy: the harvest module if available, else a
# minimal fallback that flags the same path roots. Import is optional
# so sensei-clean stays installable even without master_ai/harvest.py.
try:
    import harvest as _harvest  # type: ignore
except Exception:  # pragma: no cover
    _harvest = None


BANNER = "Sensei Clean — review-first local cleanup"
DEFAULT_ROOTS = ["~/Desktop", "~/Downloads", "~/Documents", "~/Pictures", "~/Videos"]


def _private_reason(item: ItemRecord) -> str:
    if _harvest is None:
        return ""
    try:
        return _harvest._privacy_reason(
            prompt=item.identity.get("path", ""), response=""
        )
    except Exception:
        return ""


def _bump_sensitivity_if_private(items: list[ItemRecord]) -> list[ItemRecord]:
    """If harvest's privacy policy flags an item's path, mark it 'private'
    so policy.requires_monitored_review fires at queue time."""
    bumped = []
    for item in items:
        reason = _private_reason(item)
        if reason and item.sensitivity not in MONITORED_SENSITIVITIES:
            notes = list(item.notes) + [f"privacy:{reason}"]
            item = replace(item, sensitivity="private", notes=notes)
        bumped.append(item)
    return bumped


def build_findings(items: list[ItemRecord], run_id: str) -> list[FindingRecord]:
    findings: list[FindingRecord] = []
    by_hash: dict[str, list[ItemRecord]] = defaultdict(list)
    for item in items:
        sha256 = item.hashes.get("sha256")
        if sha256:
            by_hash[sha256].append(item)
    for sha256, members in by_hash.items():
        if len(members) < 2:
            continue
        item_ids = [item.item_id for item in members]
        finding_id = hashlib.sha1(("dup:" + sha256).encode("utf-8")).hexdigest()
        findings.append(
            FindingRecord(
                schema_version="sensei.finding.v1",
                run_id=run_id,
                finding_id=finding_id,
                finding_type="exact_duplicate",
                item_ids=item_ids,
                confidence=1.0,
                risk=max(item.risk for item in members),
                summary=f"{len(members)} exact duplicates",
                evidence={"sha256": sha256},
                notes=[],
            )
        )
    return findings


def build_actions(
    items: list[ItemRecord],
    findings: list[FindingRecord],
    run_id: str,
    quarantine_root: Path,
) -> list[ActionRecord]:
    """Build quarantine_move actions, routing private/financial/etc.
    items to the 'monitored' lane (approval_required=True).
    Same lane logic as queue_builder so actions.jsonl and queue.json
    don't disagree."""
    actions: list[ActionRecord] = []
    item_by_id = {item.item_id: item for item in items}
    for finding in findings:
        if finding.finding_type != "exact_duplicate":
            continue
        keeper = item_by_id[finding.item_ids[0]]
        for item_id in finding.item_ids[1:]:
            item = item_by_id[item_id]
            destination = quarantine_root / "duplicates" / item.display_name
            action_id = hashlib.sha1(
                (item.item_id + str(destination)).encode("utf-8")
            ).hexdigest()
            is_sensitive = item.sensitivity in MONITORED_SENSITIVITIES
            lane = "monitored" if is_sensitive else "unattended"
            actions.append(
                ActionRecord(
                    schema_version="sensei.action.v1",
                    run_id=run_id,
                    action_id=action_id,
                    action_type="quarantine_move",
                    adapter=item.source["adapter"],
                    item_id=item.item_id,
                    source_path=item.identity["path"],
                    destination_path=str(destination),
                    confidence=1.0,
                    risk=max(item.risk, keeper.risk),
                    reversible=True,
                    lane=lane,
                    reason=f"exact duplicate of {keeper.identity['path']}",
                    approval_required=is_sensitive,
                    metadata={"sensitivity": item.sensitivity},
                )
            )
    return actions


def _build_adapter(adapter_name: str, run_id: str):
    """Construct the right BaseAdapter from a stored action.adapter
    name. Local actions all go through one LocalFSAdapter; each rclone
    remote gets its own RcloneRemoteAdapter."""
    if adapter_name.startswith("rclone:"):
        from sensei_clean.adapters.rclone_remote import RcloneRemoteAdapter

        remote = adapter_name.split(":", 1)[1]
        return RcloneRemoteAdapter(run_id=run_id, remote=remote, list_enabled=True)
    return LocalFSAdapter(
        run_id=run_id,
        roots=DEFAULT_ROOTS,
        quarantine_root=str(Path("~/Sensei-Quarantine").expanduser().resolve()),
    )


def _capability_for(adapter_name: str, capabilities: list):
    """Find the CapabilityReport that matches an action's adapter."""
    for c in capabilities:
        if c.adapter == adapter_name:
            return c
    # Fall back to first available cap if no exact match (shouldn't
    # happen in practice — capabilities.json records one per adapter).
    return capabilities[0] if capabilities else None


def _apply_per_adapter(filtered, capabilities, undo_path: str):
    """Group actions by their adapter, run apply_actions per group with
    the matching capability. Aggregates all ApplyResults."""
    from collections import defaultdict

    groups: dict[str, list] = defaultdict(list)
    for a in filtered:
        groups[a.adapter].append(a)
    results = []
    for adapter_name, group_actions in groups.items():
        cap = _capability_for(adapter_name, capabilities)
        if cap is None:
            results.extend(
                [
                    type(
                        "R",
                        (),
                        {
                            "action_id": a.action_id,
                            "success": False,
                            "message": f"no capability for {adapter_name}",
                        },
                    )()
                    for a in group_actions
                ]
            )
            continue
        adapter = _build_adapter(adapter_name, group_actions[0].run_id)
        results.extend(apply_actions(adapter, group_actions, cap, undo_path))
    return results


def _undo_per_adapter(records):
    """Group undo records by adapter, dispatch each to the right
    adapter. Preserves the caller's ordering."""
    # Preserve order: process records sequentially, building per-adapter
    # adapters lazily.
    adapters: dict[str, object] = {}
    results = []
    for rec in records:
        if rec.adapter not in adapters:
            adapters[rec.adapter] = _build_adapter(rec.adapter, rec.run_id)
        adapter = adapters[rec.adapter]
        results.extend(undo_actions(adapter, [rec]))
    return results


def make_run_dir(raw_run_dir: str | None, run_id: str) -> Path:
    if raw_run_dir:
        return Path(raw_run_dir).expanduser().resolve()
    return Path("~/sensei_runs").expanduser().resolve() / run_id


# ────────────── scan ──────────────


def cmd_scan(args: argparse.Namespace) -> int:
    has_cloud = any(r.startswith("rclone:") for r in args.roots)
    print(f"{BANNER}")
    print(f"  roots: {', '.join(args.roots)}")
    print(
        f"  sha256: {'on' if args.sha256 else 'off (default — pass --sha256 to find duplicates)'}"
    )
    if has_cloud:
        if args.list_cloud:
            print("  cloud listing: ON (rclone lsjson)")
        else:
            print(
                "  cloud listing: off (default — pass --list-cloud to enumerate cloud files)"
            )
    print()

    run_path, capabilities, items, findings, actions = scan_run(
        roots=args.roots,
        sha256=args.sha256,
        quarantine_root=args.quarantine_root,
        run_dir=str(args.run_dir) if args.run_dir else None,
        list_cloud=args.list_cloud,
    )

    monitored = sum(1 for a in actions if a.lane == "monitored")
    unattended = sum(1 for a in actions if a.lane == "unattended")
    reclaim = _estimate_reclaim_bytes(items, findings)

    print(f"Run dir   : {run_path}")
    print(f"Items     : {len(items)}")
    print(f"Findings  : {len(findings)}")
    print(
        f"Actions   : {len(actions)} (monitored={monitored}, unattended={unattended})"
    )
    print(f"Reclaim   : ~{reclaim / 1e6:.1f} MB if all duplicates quarantined")
    print(f"Report    : {run_path / 'reports' / 'summary.md'}")
    print()
    print("Review the report, then:")
    print(f"  sensei-clean apply {run_path}")
    print(
        f"  sensei-clean apply {run_path} --approve-monitored   # include sensitive items"
    )
    return 0


def _estimate_reclaim_bytes(
    items: list[ItemRecord], findings: list[FindingRecord]
) -> int:
    item_size = {it.item_id: (it.size_bytes or 0) for it in items}
    total = 0
    for f in findings:
        if f.finding_type != "exact_duplicate":
            continue
        sizes = sorted((item_size.get(i, 0) for i in f.item_ids), reverse=True)
        total += sum(sizes[1:])  # keep largest, reclaim the rest
    return total


# ────────────── scan-all ──────────────


def cmd_scan_all(args: argparse.Namespace) -> int:
    """Full System Scan: auto-discover every available source (local
    folders, mounted Android, synced cloud folders, rclone cloud
    remotes) and run a single scan against all of them.

    Scan-only. Nothing is moved or deleted. The report at
    <run>/reports/summary.md has the storage waste section + biggest
    + oldest. Apply / undo are separate commands."""
    sources = [s for s in detect_sources() if s.available]
    if not sources:
        print(f"{BANNER}")
        print("  No sources detected. Nothing to scan.")
        return 1

    cloud_sources = [s for s in sources if s.kind in ("cloud_api", "cloud_photo_api")]
    list_cloud = bool(args.list_cloud and cloud_sources)

    roots = [s.path for s in sources]

    print(f"{BANNER} — Full System Scan")
    print(f"  sources discovered : {len(sources)}")
    for s in sources:
        suffix = " (cloud probe-only)" if s in cloud_sources and not list_cloud else ""
        print(f"    - {s.kind:>22s}  {s.path}{suffix}")
    print(
        f"  sha256 hashing     : {'on' if args.sha256 else 'off — pass --sha256 to find duplicates'}"
    )
    if cloud_sources:
        print(
            f"  cloud listing      : {'on' if list_cloud else 'off — pass --list-cloud to enumerate cloud files'}"
        )
    print()

    run_path, capabilities, items, findings, actions = scan_run(
        roots=roots,
        sha256=args.sha256,
        quarantine_root=args.quarantine_root,
        run_dir=str(args.run_dir) if args.run_dir else None,
        list_cloud=list_cloud,
    )

    s = _waste.summary(items, findings, biggest_n=10, oldest_n=10)
    print(f"Run dir          : {run_path}")
    print(f"Files seen       : {s['total_items']:,} / {s['total_bytes_human']}")
    print(f"Duplicate cluster: {s['duplicate_clusters']}")
    print(f"Reclaim ready    : {s['reclaim_bytes_human']}")
    print(f"Report           : {run_path / 'reports' / 'summary.md'}")
    print(f"Review HTML      : {run_path / 'reports' / 'review.html'}")
    print()
    print("Status saved. Run `sensei-clean status` any time to see the headline.")
    return 0


# ────────────── status ──────────────


def cmd_status(_args: argparse.Namespace) -> int:
    print(_status.format_status())
    return 0


# ────────────── open ──────────────


def cmd_open(args: argparse.Namespace) -> int:
    """Open a single item from a prior scan run in its real app
    (xdg-open for local files, browser for cloud URLs)."""
    from sensei_clean.adapters.rclone_remote import RcloneRemoteAdapter
    from sensei_clean.opener import open_item, resolve_open_target

    run_dir = Path(args.run_dir).expanduser().resolve()
    inv = run_dir / "inventory.jsonl"
    if not inv.exists():
        print(f"error: no inventory.jsonl at {inv}", file=sys.stderr)
        return 2
    lines = [ln for ln in inv.read_text().splitlines() if ln.strip()]
    if not lines:
        print(f"error: inventory is empty at {inv}", file=sys.stderr)
        return 2
    items = [ItemRecord(**json.loads(ln)) for ln in lines]

    # Selector: --item N (1-based), --path P, --biggest, --oldest.
    chosen = None
    if args.path:
        wanted = str(Path(args.path).expanduser())
        for it in items:
            if (
                it.identity.get("path") == wanted
                or it.identity.get("path") == args.path
            ):
                chosen = it
                break
        if chosen is None:
            print(f"error: no item with path {args.path}", file=sys.stderr)
            return 2
    elif args.biggest:
        nonempty = [i for i in items if (i.size_bytes or 0) > 0]
        if not nonempty:
            print("error: no items have size > 0", file=sys.stderr)
            return 2
        chosen = max(nonempty, key=lambda i: i.size_bytes or 0)
    elif args.oldest:
        from sensei_clean.waste import oldest_files

        picks = oldest_files(items, n=1)
        if not picks:
            print("error: no items have a timestamp", file=sys.stderr)
            return 2
        chosen = picks[0]
    elif args.item is not None:
        if not 1 <= args.item <= len(items):
            print(
                f"error: --item {args.item} out of range (1..{len(items)})",
                file=sys.stderr,
            )
            return 2
        chosen = items[args.item - 1]
    else:
        print(
            "error: pass one of --item N / --path P / --biggest / --oldest",
            file=sys.stderr,
        )
        return 2

    # Pick the right adapter for this item so cloud URLs resolve.
    adapter_name = chosen.source.get("adapter", "")
    adapter = None
    if adapter_name.startswith("rclone:"):
        remote = adapter_name.split(":", 1)[1]
        adapter = RcloneRemoteAdapter(run_id=chosen.run_id, remote=remote)
    else:
        adapter = LocalFSAdapter(
            run_id=chosen.run_id,
            roots=[chosen.source.get("root", "")],
            quarantine_root=str(Path("~/Sensei-Quarantine").expanduser().resolve()),
        )

    target = resolve_open_target(chosen, adapter=adapter)
    print(f"{BANNER}")
    print(f"  item       : {chosen.display_name}")
    print(f"  category   : {chosen.category_guess}  (sensitivity={chosen.sensitivity})")
    print(f"  resolution : {target.kind}")
    print(f"  target     : {target.target or '(none)'}")
    if target.note:
        print(f"  note       : {target.note}")

    if args.print_only or target.kind == "unknown":
        return 0 if target.kind != "unknown" else 1

    _t, ok, msg = open_item(chosen, adapter=adapter, spawn=True)
    print(f"  spawn      : {msg}")
    return 0 if ok else 1


# ────────────── apply ──────────────


def cmd_apply(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        print(f"error: run dir not found: {run_dir}", file=sys.stderr)
        return 2

    capabilities_file = run_dir / "capabilities.json"
    actions_file = run_dir / "actions.jsonl"
    if not capabilities_file.exists() or not actions_file.exists():
        print(
            f"error: missing capabilities.json or actions.jsonl in {run_dir}",
            file=sys.stderr,
        )
        return 2

    cap_list = json.loads(capabilities_file.read_text())
    if not cap_list:
        print("error: no capabilities recorded in run", file=sys.stderr)
        return 2
    capabilities = [CapabilityReport(**c) for c in cap_list]

    raw_actions = [
        json.loads(line)
        for line in actions_file.read_text().splitlines()
        if line.strip()
    ]
    actions = [ActionRecord(**a) for a in raw_actions]

    if not args.approve_monitored:
        filtered = [a for a in actions if a.lane != "monitored"]
        skipped = len(actions) - len(filtered)
    else:
        filtered = actions
        skipped = 0

    if not filtered:
        print(f"{BANNER}")
        print(f"  run: {run_dir}")
        if skipped:
            print(f"  All {skipped} actions are in the monitored lane.")
            print("  Re-run with --approve-monitored to include them.")
        else:
            print("  No actions to apply.")
        return 0

    print(f"{BANNER}")
    print(f"  run     : {run_dir}")
    print(f"  apply   : {len(filtered)} action(s)")
    if skipped:
        print(
            f"  skipped : {skipped} monitored-lane action(s) (pass --approve-monitored to include)"
        )

    if not args.yes:
        ans = input("  proceed? [yes/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  cancelled.")
            return 1

    # Group by adapter so local and rclone actions dispatch through their
    # own adapter + capability.
    undo_path = run_dir / "undo.jsonl"
    results = _apply_per_adapter(filtered, capabilities, str(undo_path))

    ok = sum(1 for r in results if r.success)
    fail = sum(1 for r in results if not r.success)
    print()
    print(f"  applied : {ok}")
    print(f"  failed  : {fail}")
    print(f"  journal : {undo_path}")
    if fail:
        print()
        print("  Failures:")
        for r in results:
            if not r.success:
                print(f"    {r.action_id[:8]}  {r.message}")
        return 1
    return 0


# ────────────── undo ──────────────


def cmd_undo(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    undo_path = run_dir / "undo.jsonl"
    if not undo_path.exists():
        print(f"error: no journal at {undo_path}", file=sys.stderr)
        return 2

    records = load_undo_records(str(undo_path))
    if not records:
        print(f"{BANNER}")
        print(f"  run: {run_dir}")
        print("  journal is empty — nothing to undo.")
        return 0

    print(f"{BANNER}")
    print(f"  run    : {run_dir}")
    print(f"  undo   : {len(records)} move(s) in reverse order")
    if not args.yes:
        ans = input("  proceed? [yes/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  cancelled.")
            return 1

    results = _undo_per_adapter(list(reversed(records)))
    ok = sum(1 for r in results if r.success)
    fail = sum(1 for r in results if not r.success)
    print()
    print(f"  undone  : {ok}")
    print(f"  failed  : {fail}")
    return 0 if fail == 0 else 1


# ────────────── arg parse ──────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sensei-clean", description=BANNER)
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="inventory files and write review artifacts")
    p_scan.add_argument(
        "--run-dir",
        default=None,
        help="output directory (default: ~/sensei_runs/<ts>/)",
    )
    p_scan.add_argument(
        "--roots", nargs="*", default=DEFAULT_ROOTS, help="local roots to scan"
    )
    p_scan.add_argument(
        "--quarantine-root",
        default="~/Sensei-Quarantine",
        help="where apply would move quarantined files",
    )
    p_scan.add_argument(
        "--sha256", action="store_true", help="hash files (needed to find duplicates)"
    )
    p_scan.add_argument(
        "--list-cloud",
        action="store_true",
        help="actually list files in cloud remotes via rclone lsjson "
        "(default: probe-only, account/quota metadata only)",
    )

    p_all = sub.add_parser(
        "scan-all", help="full system scan — auto-discover and scan every source"
    )
    p_all.add_argument(
        "--run-dir",
        default=None,
        help="output directory (default: ~/sensei_runs/<ts>/)",
    )
    p_all.add_argument(
        "--quarantine-root",
        default="~/Sensei-Quarantine",
        help="where apply would move quarantined files",
    )
    p_all.add_argument(
        "--sha256", action="store_true", help="hash files (needed to find duplicates)"
    )
    p_all.add_argument(
        "--list-cloud",
        action="store_true",
        help="actually list files in detected cloud remotes (default: probe-only)",
    )

    sub.add_parser("status", help="show last full-scan date + reclaim totals")

    p_open = sub.add_parser("open", help="open an item from a scan run in its real app")
    p_open.add_argument("run_dir", help="path to a sensei_runs/<ts>/ directory")
    sel = p_open.add_mutually_exclusive_group()
    sel.add_argument(
        "--item", type=int, default=None, help="1-based index into inventory.jsonl"
    )
    sel.add_argument(
        "--path",
        default=None,
        help="exact identity.path from the inventory (local or rclone:...)",
    )
    sel.add_argument(
        "--biggest", action="store_true", help="open the largest file in the inventory"
    )
    sel.add_argument(
        "--oldest",
        action="store_true",
        help="open the oldest file in the inventory (by modified time)",
    )
    p_open.add_argument(
        "--print-only",
        action="store_true",
        help="resolve the target and print it; do not run xdg-open",
    )

    p_apply = sub.add_parser("apply", help="enact queued actions from a scan run")
    p_apply.add_argument("run_dir", help="path to the run directory from scan")
    p_apply.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    p_apply.add_argument(
        "--approve-monitored",
        action="store_true",
        help="include monitored-lane (sensitive) actions",
    )

    p_undo = sub.add_parser("undo", help="reverse what apply did, newest first")
    p_undo.add_argument("run_dir", help="path to the run directory from a prior apply")
    p_undo.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )

    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        sys.exit(0)
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Back-compat: bare invocation = scan with defaults.
    if argv and argv[0] not in (
        "scan",
        "scan-all",
        "status",
        "open",
        "apply",
        "undo",
        "-h",
        "--help",
    ):
        # Treat legacy flags (--run-dir, --roots, --sha256, --quarantine-root)
        # as `scan` arguments so old callers still work.
        argv = ["scan", *argv]
    args = parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "scan-all":
        return cmd_scan_all(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "open":
        return cmd_open(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    if args.cmd == "undo":
        return cmd_undo(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
