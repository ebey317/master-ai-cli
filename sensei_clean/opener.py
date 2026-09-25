"""
Open-in-app surface.

Given an ItemRecord, resolve where it actually lives (a local path
or a provider web view URL) and (optionally) spawn the right app via
xdg-open. Local items resolve to a `file://` URI; cloud items resolve
to whatever URL the adapter's open_view returned (e.g. Drive's
`https://drive.google.com/file/d/<id>/view`).

Tests can use `resolve_open_target` directly (pure function) and
monkey-patch `_xdg_open` so they never actually spawn a browser.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schemas import ItemRecord


@dataclass(frozen=True)
class OpenTarget:
    kind: str  # "local_file", "local_dir", "cloud_url", "unknown"
    target: str  # local path or URL
    display: str  # short label for confirmation prompts
    note: str = ""  # extra context shown to the customer


def _looks_local(path: str) -> bool:
    if not path:
        return False
    return not path.startswith(("rclone:", "http://", "https://"))


def resolve_open_target(item: ItemRecord, adapter=None) -> OpenTarget:
    """Map an ItemRecord to a concrete OpenTarget without performing
    any action. Pure function — safe to call in tests."""
    path = (item.identity or {}).get("path", "")
    name = item.display_name or path or "(unnamed)"
    if _looks_local(path):
        p = Path(path).expanduser()
        if p.exists() and p.is_dir():
            return OpenTarget(
                kind="local_dir", target=str(p), display=f"open folder: {name}"
            )
        return OpenTarget(
            kind="local_file", target=str(p), display=f"open file: {name}"
        )
    # Cloud / non-local: ask the adapter for its viewable URL.
    url = ""
    if adapter is not None and hasattr(adapter, "open_view"):
        try:
            url = adapter.open_view(item) or ""
        except Exception:
            url = ""
    if url:
        return OpenTarget(
            kind="cloud_url",
            target=url,
            display=f"open in browser: {name}",
            note=f"provider URL ({adapter.name if adapter else 'cloud'})",
        )
    return OpenTarget(
        kind="unknown",
        target=path,
        display=f"no opener for: {name}",
        note="adapter did not return a view URL",
    )


def _xdg_open(arg: str) -> tuple[bool, str]:
    """Spawn xdg-open in the background. Returns (ok, message). Never
    raises — the caller has already approved this action."""
    bin_ = shutil.which("xdg-open") or "xdg-open"
    if not shutil.which(bin_):
        return (
            False,
            "xdg-open not installed; install xdg-utils or open the path manually",
        )
    try:
        # Detach so closing the CLI doesn't kill the spawned app.
        subprocess.Popen(
            [bin_, arg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, f"xdg-open: {arg}"
    except OSError as e:
        return False, f"xdg-open failed: {e}"


def open_item(
    item: ItemRecord,
    adapter=None,
    *,
    spawn: bool = True,
) -> tuple[OpenTarget, bool, str]:
    """Resolve + (optionally) spawn the app. Returns
    (target, ok, message). When spawn=False, only the resolution
    happens (used by the GUI/HTML pages that want the URL/path but
    don't run xdg-open themselves)."""
    target = resolve_open_target(item, adapter=adapter)
    if not spawn:
        return target, True, "resolved"
    if target.kind == "unknown" or not target.target:
        return target, False, target.note or "no resolvable target"
    if target.kind == "local_file":
        p = Path(target.target)
        if not p.exists():
            return target, False, f"local target missing: {p}"
    ok, msg = _xdg_open(target.target)
    return target, ok, msg


def review_link_href(item: ItemRecord, adapter=None) -> str:
    """Render-side helper used by review.html: returns the href
    attribute value (file://... or https://...). Empty when no
    resolvable target."""
    t = resolve_open_target(item, adapter=adapter)
    if t.kind == "local_file" or t.kind == "local_dir":
        # file:// URIs need quoting; keep simple and rely on the
        # browser to handle most paths. Spaces -> %20 minimally.
        return f"file://{t.target.replace(' ', '%20')}"
    if t.kind == "cloud_url":
        return t.target
    return ""
