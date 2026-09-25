from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import status as _status_module
from . import waste as _waste_module
from .adapters.local_fs import LocalFSAdapter
from .policy import MONITORED_SENSITIVITIES
from .previews import write_preview_files
from .queue_builder import build_queue
from .reports import write_jsonl, write_review_html, write_summary
from .schemas import ActionRecord, CapabilityReport, FindingRecord, ItemRecord

# Optional: source-of-truth privacy policy is harvest.py when present.
_harvest: Any = None
try:
    import harvest as _harvest  # type: ignore
except Exception:  # pragma: no cover
    pass


def _private_reason(path: str) -> str:
    if _harvest is None:
        return ""
    try:
        # Source-of-truth lives in harvest; we call into it rather than
        # duplicating path/secret rules here.
        return _harvest._privacy_reason(prompt=path, response="")
    except Exception:
        return ""


def bump_sensitivity_if_private(items: Iterable[ItemRecord]) -> list[ItemRecord]:
    """If harvest's privacy policy flags an item's path, mark it 'private'
    so policy.requires_monitored_review fires at queue time."""
    bumped: list[ItemRecord] = []
    for item in items:
        reason = _private_reason(item.identity.get("path", ""))
        if reason and item.sensitivity not in MONITORED_SENSITIVITIES:
            notes = list(item.notes) + [f"privacy:{reason}"]
            item = replace(item, sensitivity="private", notes=notes)
        bumped.append(item)
    return bumped


def build_findings(items: list[ItemRecord], run_id: str) -> list[FindingRecord]:
    """Group items by best-available hash. Cloud items typically have
    only md5 (Drive) or sha1 (other providers), not sha256. Within-source
    dedup works for cloud; cross-source dedup needs matching algorithms,
    which is fine — only same-algo groups can prove exactness."""
    findings: list[FindingRecord] = []
    by_hash: dict[tuple[str, str], list[ItemRecord]] = defaultdict(list)
    for item in items:
        sha256 = item.hashes.get("sha256")
        md5 = item.hashes.get("md5")
        sha1 = item.hashes.get("sha1")
        if sha256:
            by_hash[("sha256", sha256)].append(item)
        elif md5:
            by_hash[("md5", md5)].append(item)
        elif sha1:
            by_hash[("sha1", sha1)].append(item)
    for (algo, value), members in by_hash.items():
        if len(members) < 2:
            continue
        item_ids = [item.item_id for item in members]
        finding_id = hashlib.sha1((f"dup:{algo}:" + value).encode("utf-8")).hexdigest()
        findings.append(
            FindingRecord(
                schema_version="sensei.finding.v1",
                run_id=run_id,
                finding_id=finding_id,
                finding_type="exact_duplicate",
                item_ids=item_ids,
                confidence=1.0,
                risk=max(item.risk for item in members),
                summary=f"{len(members)} exact duplicates ({algo})",
                evidence={algo: value},
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
    """Build quarantine_move actions, routing sensitive items to the
    monitored lane so apply requires explicit approval."""
    actions: list[ActionRecord] = []
    item_by_id = {item.item_id: item for item in items}
    for finding in findings:
        if finding.finding_type != "exact_duplicate":
            continue
        keeper = item_by_id[finding.item_ids[0]]
        for item_id in finding.item_ids[1:]:
            item = item_by_id[item_id]
            adapter_name = item.source.get("adapter", "")
            # Cloud items get an in-remote quarantine destination so
            # nothing leaves the provider; local items use the local
            # quarantine root. Action type tracks the dispatch path.
            if adapter_name.startswith("rclone:"):
                from .adapters.rclone_remote import RcloneRemoteAdapter

                remote = adapter_name.split(":", 1)[1]
                destination_str = RcloneRemoteAdapter.cloud_quarantine_destination(
                    remote, item.display_name
                )
                action_type = "cloud_move"
                # All cloud actions go to monitored lane in this round:
                # the customer must explicitly approve any cloud mutation.
                is_monitored = True
            else:
                destination_str = str(
                    quarantine_root / "duplicates" / item.display_name
                )
                action_type = "quarantine_move"
                is_monitored = item.sensitivity in MONITORED_SENSITIVITIES
            action_id = hashlib.sha1(
                (item.item_id + destination_str).encode("utf-8")
            ).hexdigest()
            lane = "monitored" if is_monitored else "unattended"
            actions.append(
                ActionRecord(
                    schema_version="sensei.action.v1",
                    run_id=run_id,
                    action_id=action_id,
                    action_type=action_type,
                    adapter=adapter_name,
                    item_id=item.item_id,
                    source_path=item.identity["path"],
                    destination_path=destination_str,
                    confidence=1.0,
                    risk=max(item.risk, keeper.risk),
                    reversible=True,
                    lane=lane,
                    reason=f"exact duplicate of {keeper.identity['path']}",
                    approval_required=is_monitored,
                    metadata={"sensitivity": item.sensitivity},
                )
            )
    return actions


def _organize_bucket_for_path(path: Path) -> str | None:
    suf = path.suffix.lower()
    if suf in {".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pdf"}:
        return "Documents"
    if suf in {".xls", ".xlsx", ".ods", ".csv"}:
        return "Spreadsheets"
    if suf in {".ppt", ".pptx", ".odp"}:
        return "Presentations"
    if suf in {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}:
        return "Archives"
    if suf in {".deb", ".rpm", ".appimage", ".dmg", ".pkg", ".msi", ".exe", ".apk"}:
        return "Installers"
    if suf in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff"}:
        return "Images"
    if suf in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
        return "Videos"
    if suf in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
        return "Audio"
    return None


def build_organize_actions(
    items: list[ItemRecord],
    run_id: str,
    organize_root: Path,
    *,
    unattended_root_basenames: set[str] | None = None,
    exclude_item_ids: set[str] | None = None,
) -> list[ActionRecord]:
    """Build archive_move actions to organize files into buckets under
    organize_root. This is intentionally conservative:
      - only high-confidence items
      - only buckets with clear extension rules
      - optionally only for specific scan roots like Downloads
    """
    actions: list[ActionRecord] = []
    excluded = exclude_item_ids or set()
    for item in items:
        if item.item_id in excluded:
            continue
        if item.kind != "file":
            continue
        if item.confidence < 0.85:
            continue
        src_root = str(item.source.get("root", ""))
        try:
            root_base = Path(src_root).name
        except Exception:
            root_base = ""

        try:
            src_path = Path(item.identity.get("path", ""))
        except Exception:
            continue
        bucket = _organize_bucket_for_path(src_path)
        if not bucket:
            continue

        dest = organize_root / bucket / src_path.name
        action_id = hashlib.sha1((item.item_id + str(dest)).encode("utf-8")).hexdigest()
        is_sensitive = item.sensitivity in MONITORED_SENSITIVITIES
        allowed_unattended = (
            unattended_root_basenames is None or root_base in unattended_root_basenames
        )
        lane = "monitored" if is_sensitive or not allowed_unattended else "unattended"
        actions.append(
            ActionRecord(
                schema_version="sensei.action.v1",
                run_id=run_id,
                action_id=action_id,
                action_type="archive_move",
                adapter=item.source["adapter"],
                item_id=item.item_id,
                source_path=item.identity["path"],
                destination_path=str(dest),
                confidence=item.confidence,
                risk=item.risk,
                reversible=True,
                lane=lane,
                reason=f"organize into {bucket}",
                approval_required=lane == "monitored",
                metadata={"sensitivity": item.sensitivity},
            )
        )
    return actions


def make_run_dir(raw_run_dir: str | None, run_id: str) -> Path:
    if raw_run_dir:
        return Path(raw_run_dir).expanduser().resolve()
    return Path("~/sensei_runs").expanduser().resolve() / run_id


def scan_run(
    *,
    roots: list[str],
    sha256: bool,
    quarantine_root: str,
    run_dir: str | None = None,
    include_text_snippets: bool = False,
    include_previews: bool = False,
    suffix_allowlist: set[str] | None = None,
    organize: bool = False,
    organize_root: str | None = None,
    list_cloud: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[
    Path,
    list[CapabilityReport],
    list[ItemRecord],
    list[FindingRecord],
    list[ActionRecord],
]:
    """Run scan pipeline and write run artifacts to run_dir.

    progress(phase, done, total) is best-effort: callers may pass total=0
    when unknown.
    """
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = make_run_dir(run_dir, run_id)
    reports_dir = out_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Split roots into local vs cloud. Local goes through one
    # LocalFSAdapter (matches its multi-root constructor); each cloud
    # remote gets its own adapter so probe() reports the right account.
    local_roots = [r for r in roots if not r.startswith("rclone:")]
    rclone_roots = [r for r in roots if r.startswith("rclone:")]

    adapter = LocalFSAdapter(
        run_id=run_id, roots=local_roots or [], quarantine_root=quarantine_root
    )
    capabilities: list[CapabilityReport] = []
    if local_roots:
        capabilities.append(adapter.probe())

    items: list[ItemRecord] = []
    if local_roots:
        for idx, item in enumerate(adapter.scan(), start=1):
            if suffix_allowlist is not None:
                try:
                    suffix = Path(item.identity.get("path", "")).suffix.lower()
                except Exception:
                    suffix = ""
                if suffix not in suffix_allowlist:
                    continue
            items.append(item)
            if progress and (idx % 200 == 0):
                progress("scan", idx, 0)

    if rclone_roots:
        # Cloud probe-only by default. Pass list_cloud=True (via
        # `sensei-clean scan --list-cloud` or the GUI checkbox) to
        # actually call `rclone lsjson --hash` and emit ItemRecords.
        from .adapters.rclone_remote import RcloneRemoteAdapter

        for cloud_root in rclone_roots:
            rest = cloud_root[len("rclone:") :]
            remote, _, path_in_remote = rest.partition(":")
            cloud_adapter = RcloneRemoteAdapter(
                run_id=run_id,
                remote=remote,
                path_in_remote=path_in_remote,
                list_enabled=list_cloud,
            )
            capabilities.append(cloud_adapter.probe())
            for cloud_item in cloud_adapter.scan():  # empty unless list_cloud=True
                items.append(cloud_item)

    if sha256 and local_roots:
        jobs = ["sha256", "screenshot"]
        if include_text_snippets:
            jobs.append("text_snippet")
        enriched: list[ItemRecord] = []
        for idx, item in enumerate(items, start=1):
            if item.source.get("adapter") == adapter.name:
                enriched.append(adapter.enrich(item, jobs))
            else:
                enriched.append(item)  # non-local items skip local enrichment
            if progress and (idx % 50 == 0):
                progress("hash", idx, len(items))
        items = enriched

    items = bump_sensitivity_if_private(items)
    findings = build_findings(items, run_id)
    actions = build_actions(
        items, findings, run_id, Path(quarantine_root).expanduser().resolve()
    )
    if organize:
        move_claimed_ids = {action.item_id for action in actions}
        org_root = Path(organize_root or "~/Sensei-Organized").expanduser().resolve()
        actions.extend(
            build_organize_actions(
                items,
                run_id,
                org_root,
                unattended_root_basenames={"Downloads"},
                exclude_item_ids=move_claimed_ids,
            )
        )
    queue = build_queue(items, actions, capabilities)

    write_jsonl(str(out_dir / "inventory.jsonl"), items)
    write_jsonl(str(out_dir / "findings.jsonl"), findings)
    write_jsonl(str(out_dir / "actions.jsonl"), actions)
    write_summary(
        str(reports_dir / "summary.md"), capabilities, items, findings, actions
    )
    write_review_html(
        str(reports_dir / "review.html"), capabilities, items, findings, actions
    )
    write_preview_files(out_dir, items, include_content=include_previews)
    (out_dir / "queue.json").write_text(
        json.dumps(queue, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "capabilities.json").write_text(
        json.dumps([c.to_dict() for c in capabilities], indent=2) + "\n",
        encoding="utf-8",
    )

    # Storage waste rollup as a structured artifact (status command +
    # GUI read this without re-loading inventory.jsonl) and last-clean
    # state for the menu/banner.
    waste = _waste_module.summary(items, findings, biggest_n=20, oldest_n=20)
    (out_dir / "waste.json").write_text(
        json.dumps(waste, indent=2) + "\n", encoding="utf-8"
    )
    try:
        _status_module.record_full_scan(
            run_dir=str(out_dir),
            total_items=waste["total_items"],
            total_bytes=waste["total_bytes"],
            reclaim_bytes=waste["reclaim_bytes"],
            duplicate_clusters=waste["duplicate_clusters"],
            sources=list(roots),
        )
    except Exception:
        # Non-fatal: status is a convenience, not a correctness gate.
        pass

    if progress:
        progress("done", len(items), len(items))

    return out_dir, capabilities, items, findings, actions
