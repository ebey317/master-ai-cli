from __future__ import annotations

import html
import json
from collections.abc import Iterable
from pathlib import Path

from .schemas import ActionRecord, CapabilityReport, FindingRecord, ItemRecord
from .waste import summary as waste_summary


def write_jsonl(path: str, records: Iterable[object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=True) + "\n")


def write_summary(
    path: str,
    capabilities: list[CapabilityReport],
    items: list[ItemRecord],
    findings: list[FindingRecord],
    actions: list[ActionRecord],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sensei Scan Summary",
        "",
        f"- Adapters: {len(capabilities)}",
        f"- Items: {len(items)}",
        f"- Findings: {len(findings)}",
        f"- Actions: {len(actions)}",
        "- Preview index: previews.md",
        "",
        "## Adapters",
    ]
    for capability in capabilities:
        lines.append(
            f"- {capability.adapter}: capability={capability.capability} available={capability.available} blockers={','.join(capability.blockers) or 'none'}"
        )
    if findings:
        lines.extend(["", "## Findings"])
        for finding in findings[:50]:
            count = len(finding.item_ids)
            lines.append(f"- {finding.summary}")
            lines.append(f"    safety  : {finding.risk}")
            lines.append(f"    matches : {count} file(s)")
    if actions:
        lines.extend(["", "## Planned Actions"])
        for action in actions[:100]:
            what, why, safety = _plain_action(action)
            lines.append(f"- **{what}**")
            lines.append(f"    why     : {why}")
            lines.append(f"    safety  : {safety}")
            lines.append(f"    where   : `{action.source_path}`")
            if action.destination_path:
                lines.append(f"    moves to: `{action.destination_path}`")

    # Storage Waste Report — biggest files, oldest files, by-category
    # breakdown, reclaim totals. No new mutations; pure analytics over
    # the items + findings we already have.
    s = waste_summary(items, findings, biggest_n=15, oldest_n=10)
    lines.extend(
        [
            "",
            "## Storage Waste Report",
            "",
            f"- Total seen     : {s['total_items']:,} files / {s['total_bytes_human']}",
            f"- Reclaim ready  : {s['reclaim_bytes_human']} ({s['duplicate_clusters']} duplicate clusters)",
        ]
    )
    if s["by_category"]:
        lines.extend(["", "### By category"])
        for row in s["by_category"][:15]:
            lines.append(
                f"- {row['category']:<16s} {row['count']:>6,} files  {row['bytes_human']}"
            )
    if s["biggest"]:
        lines.extend(["", "### Biggest files"])
        for row in s["biggest"]:
            lines.append(f"- {row['bytes_human']:>10s}  {row['name']}  ({row['path']})")
    if s["oldest"]:
        lines.extend(["", "### Oldest files (forgotten?)"])
        for row in s["oldest"]:
            age = f"{row['age_days']}d" if row.get("age_days") is not None else "?"
            lines.append(
                f"- {age:>5s} ago  {row['bytes_human']:>10s}  {row['name']}  ({row['path']})"
            )

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plain_action(action: ActionRecord) -> tuple[str, str, str]:
    if action.action_type == "quarantine_move":
        return (
            "Move extra copy",
            "Sensei found another copy of this same file. This move keeps one copy and puts the extra one in Safe Quarantine.",
            "Safe" if action.lane != "monitored" else "Needs extra YES",
        )
    if action.action_type == "cloud_move":
        return (
            "Move extra cloud copy",
            "Sensei found another copy in cloud storage. This move keeps one copy and puts the extra one in Cloud Quarantine.",
            "Needs extra YES",
        )
    if action.action_type == "archive_move":
        return (
            "Organize file",
            "Sensei will move this file into a matching folder, like Documents, Images, or Spreadsheets.",
            "Safe" if action.lane != "monitored" else "Needs extra YES",
        )
    return (
        "Review change",
        "Sensei planned a file move. Read the from and to paths before moving it.",
        "Needs extra YES" if action.lane == "monitored" else "Safe",
    )


def _review_href(path_str: str) -> str:
    """Render-side helper: file:// URI for local paths, the URL as-is
    for cloud (https://drive.google.com/...) or http(s) entries. Empty
    string when there's no usable target."""
    if not path_str:
        return ""
    if path_str.startswith(("http://", "https://")):
        return path_str
    if path_str.startswith("rclone:"):
        # rclone-only roots have no direct browser URL until the
        # adapter's open_view fills it in; skip in the review page.
        return ""
    return f"file://{path_str.replace(' ', '%20')}"


def write_review_html(
    path: str,
    capabilities: list[CapabilityReport],
    items: list[ItemRecord],
    findings: list[FindingRecord],
    actions: list[ActionRecord],
) -> None:
    """Write a customer-facing review page.

    This is intentionally plain-language. The app can still keep exact
    JSON/Markdown artifacts for debugging, but customers should see rows:
    what file, where it is now, where it will go, and whether it needs
    extra approval.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    item_by_id = {item.item_id: item for item in items}
    safe_count = sum(1 for action in actions if action.lane != "monitored")
    extra_count = sum(1 for action in actions if action.lane == "monitored")
    duplicate_count = len(findings)

    rows: list[str] = []
    for idx, action in enumerate(actions, start=1):
        item = item_by_id.get(action.item_id)
        title, detail, approval = _plain_action(action)
        name = item.display_name if item else Path(action.source_path).name
        source = action.source_path
        destination = action.destination_path or "(not set)"
        badge_class = "badge safe" if action.lane != "monitored" else "badge extra"
        src_href = _review_href(source)
        # "From" links to the file's current location so a click in the
        # browser opens the file in the user's default app (xdg-open
        # equivalent). Local paths become file:// links; cloud URLs are
        # passed through. No href when the source isn't directly
        # openable.
        src_html = (
            f'<a class="open-link" href="{html.escape(src_href)}">{html.escape(source)}</a>'
            if src_href
            else f"<code>{html.escape(source)}</code>"
        )
        rows.append(f"""
        <section class="move-row">
          <div class="row-num">{idx}</div>
          <div class="row-main">
            <div class="row-top">
              <h3>{html.escape(title)}: {html.escape(name)}</h3>
              <span class="{badge_class}">{html.escape(approval)}</span>
            </div>
            <p>{html.escape(detail)}</p>
            <div class="path-grid">
              <div>
                <span class="label">From (click to open)</span>
                {src_html}
              </div>
              <div>
                <span class="label">To</span>
                <code>{html.escape(destination)}</code>
              </div>
            </div>
          </div>
        </section>
        """)

    if not rows:
        rows.append("""
        <section class="empty">
          <h3>No file moves planned</h3>
          <p>Sensei scanned the selected places and did not find anything it should move.</p>
        </section>
        """)

    source_cards = []
    for cap in capabilities:
        state = "Connected" if cap.available else "Needs attention"
        cls = "source-ok" if cap.available else "source-warn"
        source_cards.append(f"""
        <div class="source-card {cls}">
          <strong>{html.escape(cap.account_label or cap.provider)}</strong>
          <span>{html.escape(cap.capability)}</span>
          <small>{html.escape(state)}</small>
        </div>
        """)

    css = """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f7fb;
      color: #18202f;
    }
    header {
      background: #162033;
      color: white;
      padding: 28px 32px 24px;
    }
    header h1 { margin: 0 0 8px; font-size: 32px; letter-spacing: 0; }
    header p { margin: 0; max-width: 760px; color: #d9e3f7; font-size: 16px; line-height: 1.5; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .steps {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: -18px auto 24px;
    }
    .step {
      background: white;
      border: 1px solid #d9e0ed;
      border-radius: 8px;
      padding: 14px;
      min-height: 96px;
      box-shadow: 0 4px 14px rgba(18, 32, 51, .06);
    }
    .step strong { display: block; font-size: 14px; margin-bottom: 6px; color: #26364f; }
    .step span { color: #53627a; font-size: 13px; line-height: 1.45; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 22px;
    }
    .metric {
      background: white;
      border: 1px solid #d9e0ed;
      border-radius: 8px;
      padding: 16px;
    }
    .metric b { display: block; font-size: 28px; }
    .metric span { color: #61708a; font-size: 13px; }
    .sources { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px; }
    .source-card {
      display: grid;
      gap: 2px;
      min-width: 170px;
      background: white;
      border: 1px solid #d9e0ed;
      border-left: 5px solid #4f7cff;
      border-radius: 8px;
      padding: 12px;
    }
    .source-card span, .source-card small { color: #61708a; }
    .source-warn { border-left-color: #d28a1e; }
    .move-row {
      display: grid;
      grid-template-columns: 44px 1fr;
      gap: 14px;
      align-items: start;
      background: white;
      border: 1px solid #d9e0ed;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 12px;
      box-shadow: 0 2px 10px rgba(18, 32, 51, .04);
    }
    .row-num {
      width: 36px;
      height: 36px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background: #edf2ff;
      color: #284cbd;
      font-weight: 700;
    }
    .row-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    h2 { margin: 26px 0 12px; font-size: 22px; }
    h3 { margin: 0; font-size: 17px; line-height: 1.3; }
    p { color: #53627a; line-height: 1.5; }
    .badge {
      flex: 0 0 auto;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid;
    }
    .safe { color: #166534; background: #ecfdf3; border-color: #bbf7d0; }
    .extra { color: #92400e; background: #fff7ed; border-color: #fed7aa; }
    .path-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 12px;
    }
    .label {
      display: block;
      font-size: 12px;
      color: #61708a;
      margin-bottom: 5px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    code {
      display: block;
      width: 100%;
      min-height: 38px;
      padding: 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border-radius: 6px;
      background: #f7f9fd;
      border: 1px solid #dfe6f2;
      color: #26364f;
      font-size: 12px;
    }
    a.open-link {
      display: block;
      width: 100%;
      min-height: 38px;
      padding: 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border-radius: 6px;
      background: #eef4ff;
      border: 1px solid #c7d6f5;
      color: #1a3d8c;
      font-family: ui-monospace, monospace;
      font-size: 12px;
      text-decoration: none;
    }
    a.open-link:hover { background: #dbe7ff; text-decoration: underline; }
    .empty {
      background: white;
      border: 1px solid #d9e0ed;
      border-radius: 8px;
      padding: 22px;
    }
    @media (max-width: 780px) {
      .steps, .summary, .path-grid { grid-template-columns: 1fr; }
      .move-row { grid-template-columns: 1fr; }
      .row-top { display: grid; }
    }
    """

    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensei Clean Review</title>
  <style>{css}</style>
</head>
<body>
  <header>
    <h1>Sensei Clean Review</h1>
    <p>Nothing has moved yet. This page shows exactly what Sensei found and where each file would go if you choose to move it.</p>
  </header>
  <main>
    <section class="steps">
      <div class="step"><strong>1. Scan</strong><span>Sensei looks at the places you picked.</span></div>
      <div class="step"><strong>2. Review</strong><span>You check the list before anything moves.</span></div>
      <div class="step"><strong>3. Move</strong><span>Extra copies go to Safe Quarantine. Organized files go to Sensei-Organized.</span></div>
      <div class="step"><strong>4. Undo</strong><span>If it looks wrong, Sensei can put moved files back.</span></div>
    </section>

    <section class="summary">
      <div class="metric"><b>{len(items)}</b><span>files scanned</span></div>
      <div class="metric"><b>{duplicate_count}</b><span>duplicate groups found</span></div>
      <div class="metric"><b>{safe_count}</b><span>moves ready</span></div>
      <div class="metric"><b>{extra_count}</b><span>moves needing extra YES</span></div>
    </section>

    <h2>Places Checked</h2>
    <section class="sources">{"".join(source_cards)}</section>

    <h2>What Sensei Wants To Move</h2>
    {"".join(rows)}
  </main>
</body>
</html>
"""
    output.write_text(doc, encoding="utf-8")
