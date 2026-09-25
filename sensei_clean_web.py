#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp

from sensei_clean import status as clean_status
from sensei_clean.apply import load_undo_records
from sensei_clean.connectors import detect_sources, supported_connector_catalog
from sensei_clean.engine import scan_run
from sensei_clean.runner import apply_per_adapter, undo_per_adapter
from sensei_clean.schemas import ActionRecord, CapabilityReport

APP_TITLE = "Sensei Clean"
JOBS: dict[str, dict] = {}


def _json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _human_action(action: ActionRecord) -> dict:
    if action.action_type == "quarantine_move":
        label = "Move extra copy"
        destination = "Safe Quarantine"
    elif action.action_type == "cloud_move":
        label = "Move extra cloud copy"
        destination = "Cloud Quarantine"
    elif action.action_type == "archive_move":
        label = "Organize file"
        destination = "Organized folder"
    else:
        label = "Review move"
        destination = "New place"
    return {
        "id": action.action_id,
        "label": label,
        "file": Path(action.source_path).name,
        "from": action.source_path,
        "to": action.destination_path or "",
        "destination": destination,
        "needsExtraYes": action.lane == "monitored",
        "reason": action.reason,
        "type": action.action_type,
    }


def _load_actions(run_dir: Path) -> list[ActionRecord]:
    path = run_dir / "actions.jsonl"
    if not path.exists():
        return []
    actions: list[ActionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            actions.append(ActionRecord(**json.loads(line)))
    return actions


def _load_capabilities(run_dir: Path) -> list[CapabilityReport]:
    path = run_dir / "capabilities.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [CapabilityReport(**row) for row in data]


def _load_waste(run_dir: Path) -> dict:
    path = run_dir / "waste.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _friendly_kind(kind: str) -> str:
    return {
        "local": "This computer",
        "photo_library": "Photos",
        "media_library": "Videos and music",
        "synced_cloud_folder": "Cloud folder on this computer",
        "cloud_api": "Connected cloud account",
        "cloud_photo_api": "Connected photo account",
        "android_mounted_storage": "Phone storage",
        "removable_storage": "USB or SD card",
    }.get(kind, "Files")


def _friendly_path(path: str) -> str:
    if path.startswith("rclone:"):
        remote = path.removeprefix("rclone:").rstrip(":")
        return f"Cloud account: {remote}"
    return path


def _friendly_notes(notes: list[str] | tuple[str, ...]) -> list[str]:
    replacements = {
        "real cloud API connector via rclone": "connected through your cloud login",
        "file listing is optional": "you choose when Sensei looks inside",
        "moves go to cloud quarantine and need extra approval": "cloud moves need extra YES",
        "local sync folder": "already synced to this computer",
        "not OAuth/API": "works like a normal folder",
        "customer-visible files": "files you see on the desktop",
        "sensitive: asks for approval": "private: review before moving",
    }
    return [replacements.get(note, note) for note in notes]


def _run_scan(job_id: str, payload: dict) -> None:
    job = JOBS[job_id]
    roots = [str(r) for r in payload.get("roots") or [] if str(r).strip()]
    if not roots:
        job.update({"state": "error", "message": "Pick at least one place to scan."})
        return

    mode = payload.get("mode") or "duplicates"
    sha256 = mode in {"duplicates", "both", "office"}
    organize = mode in {"organize", "both", "office"}
    include_previews = bool(payload.get("includePreviews", True))
    list_cloud = bool(payload.get("listCloud", False))
    run_dir = mkdtemp(prefix="sensei_clean_web_", dir="/tmp")

    suffix_allowlist = None
    if mode == "office":
        suffix_allowlist = {
            ".doc",
            ".docx",
            ".odt",
            ".rtf",
            ".xls",
            ".xlsx",
            ".ods",
            ".csv",
            ".ppt",
            ".pptx",
            ".odp",
            ".pdf",
            ".txt",
            ".md",
        }

    def progress(phase: str, done: int, total: int) -> None:
        job["phase"] = phase
        job["done"] = done
        job["total"] = total

    try:
        job.update({"state": "running", "phase": "scan", "done": 0, "total": 0})
        run_path, caps, items, findings, actions = scan_run(
            roots=roots,
            sha256=sha256,
            quarantine_root=str((Path.home() / "Sensei-Quarantine").resolve()),
            run_dir=run_dir,
            include_previews=include_previews,
            suffix_allowlist=suffix_allowlist,
            organize=organize,
            organize_root=str((Path.home() / "Sensei-Organized").resolve()),
            list_cloud=list_cloud,
            progress=progress,
        )
        waste = _load_waste(run_path)
        job.update(
            {
                "state": "done",
                "phase": "done",
                "runDir": str(run_path),
                "reviewUrl": f"/report?run={urllib.parse.quote(str(run_path))}&file=review.html",
                "summaryUrl": f"/report?run={urllib.parse.quote(str(run_path))}&file=summary.md",
                "filesScanned": len(items),
                "duplicateGroups": len(findings),
                "movesReady": sum(1 for a in actions if a.lane != "monitored"),
                "movesNeedExtra": sum(1 for a in actions if a.lane == "monitored"),
                "actions": [_human_action(a) for a in actions[:200]],
                "waste": waste,
                "capabilities": [c.to_dict() for c in caps],
                "message": "Scan finished.",
            }
        )
    except Exception as exc:
        job.update({"state": "error", "message": str(exc)})


def _source_payload() -> list[dict]:
    rows = []
    for src in detect_sources():
        rows.append(
            {
                "id": src.connector_id,
                "label": src.label,
                "path": src.path,
                "displayPath": _friendly_path(src.path),
                "kind": src.kind,
                "kindLabel": _friendly_kind(src.kind),
                "available": src.available,
                "notes": _friendly_notes(src.notes),
                "selected": src.available
                and src.connector_id in {"downloads", "pictures"},
            }
        )
    return rows


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensei Clean</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f6fa;
      color: #172033;
    }
    button, input, select { font: inherit; }
    .shell { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }
    aside {
      background: #121a2a;
      color: white;
      padding: 22px 18px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .brand h1 { margin: 0; font-size: 26px; letter-spacing: 0; }
    .brand p { margin: 6px 0 0; color: #bdc8da; line-height: 1.45; }
    .status-box {
      border: 1px solid #2c3a52;
      border-radius: 8px;
      padding: 14px;
      background: #18233a;
    }
    .status-box b { display: block; margin-bottom: 8px; color: #eef4ff; }
    .status-box span { display: block; color: #c8d3e4; font-size: 13px; line-height: 1.45; }
    nav { display: grid; gap: 8px; }
    nav a {
      color: #dbe7f8;
      text-decoration: none;
      border-radius: 8px;
      padding: 10px 12px;
      background: #18233a;
      border: 1px solid #2c3a52;
    }
    main { padding: 24px; overflow: auto; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 16px;
      margin-bottom: 20px;
    }
    .topbar h2 { margin: 0 0 6px; font-size: 30px; letter-spacing: 0; }
    .topbar p { margin: 0; color: #5d6b82; line-height: 1.45; max-width: 720px; }
    .primary-actions { display: flex; gap: 10px; flex-wrap: wrap; }
    .btn {
      border: 1px solid #cbd5e1;
      background: white;
      color: #1f2a44;
      border-radius: 8px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 700;
    }
    .btn.primary { background: #1d4ed8; border-color: #1d4ed8; color: white; }
    .btn.warn { background: #fff7ed; border-color: #fed7aa; color: #92400e; }
    .btn:disabled { opacity: .5; cursor: not-allowed; }
    .notice {
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      border-radius: 8px;
      padding: 14px 16px;
      margin-bottom: 18px;
      color: #1e3a8a;
      line-height: 1.45;
    }
    .notice strong { display: block; margin-bottom: 3px; color: #172554; }
    .grid { display: grid; gap: 16px; }
    .metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 18px; }
    .metric {
      background: white;
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 16px;
      min-height: 94px;
    }
    .metric b { display: block; font-size: 28px; margin-bottom: 5px; }
    .metric span { color: #607087; font-size: 13px; line-height: 1.4; }
    .panel {
      background: white;
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }
    .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .panel h3 { margin: 0; font-size: 18px; }
    .panel p { color: #607087; line-height: 1.45; }
    .source-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
    .source-card {
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 8px;
      background: #fbfcff;
      min-height: 128px;
    }
    .source-card.selected { border-color: #1d4ed8; box-shadow: 0 0 0 2px rgba(29, 78, 216, .12); }
    .source-card .kind { color: #607087; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
    .source-card strong { font-size: 15px; }
    .source-card small { color: #607087; line-height: 1.35; overflow-wrap: anywhere; }
    .controls { display: flex; flex-wrap: wrap; gap: 12px; }
    .control {
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 12px;
      background: #fbfcff;
      min-width: 220px;
    }
    .control label { display: block; font-weight: 700; margin-bottom: 8px; }
    .control select { width: 100%; padding: 9px; border: 1px solid #cbd5e1; border-radius: 8px; background: white; }
    .check { display: flex; gap: 8px; align-items: start; color: #344258; line-height: 1.35; }
    .progress {
      height: 12px;
      border-radius: 999px;
      background: #e2e8f0;
      overflow: hidden;
      margin: 12px 0 6px;
    }
    .bar { height: 100%; width: 0%; background: #16a34a; transition: width .2s ease; }
    .steps {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .step {
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 10px;
      background: white;
      color: #607087;
      min-height: 72px;
    }
    .step b { display: block; color: #26344d; margin-bottom: 4px; }
    .step.active { border-color: #1d4ed8; box-shadow: 0 0 0 2px rgba(29, 78, 216, .12); }
    .step.done { border-color: #86efac; background: #f0fdf4; }
    .review-list { display: grid; gap: 10px; }
    .move-row {
      border: 1px solid #d9e2ef;
      border-radius: 8px;
      padding: 12px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      background: #fbfcff;
    }
    .move-row h4 { margin: 0 0 7px; font-size: 15px; }
    .move-row p { margin: 0; color: #607087; }
    code {
      display: block;
      margin-top: 6px;
      padding: 8px;
      border: 1px solid #d9e2ef;
      border-radius: 6px;
      background: white;
      color: #334155;
      overflow-wrap: anywhere;
      font-size: 12px;
    }
    .pill {
      border-radius: 999px;
      padding: 5px 9px;
      height: fit-content;
      border: 1px solid #bbf7d0;
      background: #ecfdf3;
      color: #166534;
      font-size: 12px;
      font-weight: 700;
    }
    .pill.extra { border-color: #fed7aa; background: #fff7ed; color: #92400e; }
    .catalog { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
    .catalog-item { border: 1px solid #d9e2ef; border-radius: 8px; padding: 12px; background: #fbfcff; }
    .catalog-item b { display: block; margin-bottom: 4px; }
    .catalog-item span { color: #607087; font-size: 13px; line-height: 1.35; }
    @media (max-width: 920px) {
      .shell { grid-template-columns: 1fr; }
      aside { position: static; }
      .metrics { grid-template-columns: 1fr 1fr; }
      .topbar { display: grid; }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .metrics { grid-template-columns: 1fr; }
      .steps { grid-template-columns: 1fr; }
      .move-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">
        <h1>Sensei Clean</h1>
        <p>Scan, review, move extras to Safe Quarantine, undo when needed.</p>
      </div>
      <div class="status-box">
        <b>Last full clean</b>
        <span id="lastClean">Loading...</span>
      </div>
      <nav>
        <a href="#scan">Scan</a>
        <a href="#review">Review</a>
        <a href="#connectors">Connectors</a>
      </nav>
    </aside>
    <main>
      <section class="topbar">
        <div>
          <h2>Clean your computer and cloud</h2>
          <p>Pick what to check. Sensei scans first, shows the exact plan, then waits for your OK before anything moves.</p>
        </div>
        <div class="primary-actions">
          <button class="btn primary" id="scanBtn">Start scan</button>
          <button class="btn" id="openReviewBtn" disabled>Open review page</button>
        </div>
      </section>

      <section class="notice">
        <strong>No surprise deletes.</strong>
        A scan only reads file info. Safe moves go to Safe Quarantine. Cloud or private moves need an extra YES.
      </section>

      <section class="grid metrics">
        <div class="metric"><b id="metricFiles">0</b><span>files checked</span></div>
        <div class="metric"><b id="metricDupes">0</b><span>duplicate groups</span></div>
        <div class="metric"><b id="metricReady">0</b><span>safe moves ready</span></div>
        <div class="metric"><b id="metricSpace">0 B</b><span>space that duplicate cleanup could free</span></div>
      </section>

      <section class="panel" id="scan">
        <div class="panel-head">
          <h3>Places to scan</h3>
          <button class="btn" id="selectSafeBtn">Pick safe starter scan</button>
        </div>
        <div id="sources" class="source-grid"></div>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>Scan settings</h3></div>
        <div class="controls">
          <div class="control">
            <label for="mode">What to look for</label>
            <select id="mode">
              <option value="duplicates">Find duplicate files</option>
              <option value="organize">Organize Downloads</option>
              <option value="both" selected>Duplicates + organize</option>
              <option value="office">Office, PDF, and LibreOffice files</option>
            </select>
          </div>
          <div class="control">
            <label>Cloud</label>
            <label class="check"><input type="checkbox" id="listCloud"> Look inside selected connected cloud accounts</label>
          </div>
          <div class="control">
            <label>Review</label>
            <label class="check"><input type="checkbox" id="includePreviews" checked> Build a page with file previews when possible</label>
          </div>
        </div>
        <div class="progress"><div class="bar" id="bar"></div></div>
        <p id="jobText">Ready.</p>
        <div class="steps" id="steps">
          <div class="step active" data-step="pick"><b>1. Pick</b><span>Choose folders or accounts.</span></div>
          <div class="step" data-step="scan"><b>2. Scan</b><span>Sensei checks files.</span></div>
          <div class="step" data-step="review"><b>3. Review</b><span>You see the plan.</span></div>
          <div class="step" data-step="move"><b>4. Move</b><span>Files move only after YES.</span></div>
        </div>
      </section>

      <section class="panel" id="review">
        <div class="panel-head">
          <h3>What Sensei found</h3>
          <div class="primary-actions">
            <button class="btn primary" id="moveSafeBtn" disabled>Move safe files</button>
            <button class="btn warn" id="moveAllBtn" disabled>Move cloud/private files too</button>
            <button class="btn" id="undoBtn" disabled>Undo last move</button>
          </div>
        </div>
        <div id="reviewList" class="review-list">
          <p>Run a scan to see duplicate files and planned moves here.</p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h3>Largest and oldest files</h3></div>
        <div class="grid metrics">
          <div class="metric"><b id="totalSize">0 B</b><span>total scanned size</span></div>
          <div class="metric"><b id="largestName">None yet</b><span>largest file found</span></div>
          <div class="metric"><b id="oldestName">None yet</b><span>oldest file found</span></div>
          <div class="metric"><b id="runDir">No run</b><span>last scan folder</span></div>
        </div>
      </section>

      <section class="panel" id="connectors">
        <div class="panel-head"><h3>What Sensei can connect to</h3></div>
        <div id="catalog" class="catalog"></div>
      </section>
    </main>
  </div>

  <script>
    let SOURCES = [];
    let SELECTED = new Set();
    let CURRENT_JOB = null;
    let CURRENT_RUN = null;
    let REVIEW_URL = null;

    const $ = (id) => document.getElementById(id);
    const fmt = (n) => (n || 0).toLocaleString();

    function setStep(activeName) {
      const order = ["pick", "scan", "review", "move"];
      for (const el of document.querySelectorAll(".step")) {
        const idx = order.indexOf(el.dataset.step);
        const activeIdx = order.indexOf(activeName);
        el.classList.toggle("active", el.dataset.step === activeName);
        el.classList.toggle("done", idx >= 0 && idx < activeIdx);
      }
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    function renderSources() {
      const root = $("sources");
      root.innerHTML = "";
      for (const s of SOURCES) {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "source-card" + (SELECTED.has(s.path) ? " selected" : "");
        card.disabled = !s.available;
        card.innerHTML = `
          <span class="kind">${s.kindLabel || s.kind.replaceAll("_", " ")}</span>
          <strong>${s.label}</strong>
          <small>${s.displayPath || s.path}</small>
          <small>${(s.notes || []).join(" · ")}</small>
        `;
        card.onclick = () => {
          if (SELECTED.has(s.path)) SELECTED.delete(s.path);
          else SELECTED.add(s.path);
          renderSources();
        };
        root.appendChild(card);
      }
    }

    function renderCatalog(rows) {
      const root = $("catalog");
      root.innerHTML = rows.map(r => `
        <div class="catalog-item">
          <b>${r.group}</b>
          <span>${r.name}</span><br>
          <span>${r.status}</span>
        </div>
      `).join("");
    }

    function renderActions(actions) {
      const root = $("reviewList");
      if (!actions || actions.length === 0) {
        root.innerHTML = "<p>No file moves planned for this scan.</p>";
        return;
      }
      root.innerHTML = actions.map(a => `
        <div class="move-row">
          <div>
            <h4>${a.label}: ${a.file}</h4>
            <p>${a.destination}</p>
            <code>From: ${a.from}</code>
            <code>To: ${a.to}</code>
          </div>
          <span class="pill ${a.needsExtraYes ? "extra" : ""}">${a.needsExtraYes ? "Needs extra YES" : "Ready"}</span>
        </div>
      `).join("");
    }

    function updateWaste(waste) {
      if (!waste) waste = {};
      $("metricSpace").textContent = waste.reclaim_bytes_human || "0 B";
      $("totalSize").textContent = waste.total_bytes_human || "0 B";
      const biggest = (waste.biggest || [])[0];
      const oldest = (waste.oldest || [])[0];
      $("largestName").textContent = biggest ? biggest.name : "None yet";
      $("oldestName").textContent = oldest ? oldest.name : "None yet";
    }

    async function load() {
      const data = await api("/api/status");
      SOURCES = data.sources;
      SELECTED = new Set(SOURCES.filter(s => s.selected).map(s => s.path));
      renderSources();
      renderCatalog(data.catalog);
      const st = data.state || {};
      $("lastClean").textContent = st.last_full_scan_iso
        ? `${st.last_full_scan_iso} · ${fmt(st.last_total_items)} files · ${st.last_reclaim_bytes_human || ""} reclaim ready`
        : "No full clean recorded yet.";
    }

    async function startScan() {
      if (SELECTED.size === 0) {
        $("jobText").textContent = "Pick at least one place to scan.";
        return;
      }
      $("scanBtn").disabled = true;
      setStep("scan");
      $("bar").style.width = "12%";
      $("jobText").textContent = "Starting scan...";
      const data = await api("/api/scan", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          roots: Array.from(SELECTED),
          mode: $("mode").value,
          listCloud: $("listCloud").checked,
          includePreviews: $("includePreviews").checked
        })
      });
      CURRENT_JOB = data.jobId;
      pollJob();
    }

    async function pollJob() {
      if (!CURRENT_JOB) return;
      const job = await api(`/api/job?id=${encodeURIComponent(CURRENT_JOB)}`);
      const done = job.done || 0;
      const total = job.total || 0;
      const pct = total > 0 ? Math.min(95, Math.round((done / total) * 90)) : Math.min(80, 12 + Math.floor(done / 200));
      $("bar").style.width = (job.state === "done" ? 100 : pct) + "%";
      $("jobText").textContent = job.message || `${job.phase || "scan"}: ${fmt(done)} files`;
      if (job.state === "done") {
        $("scanBtn").disabled = false;
        setStep("review");
        CURRENT_RUN = job.runDir;
        REVIEW_URL = job.reviewUrl;
        $("openReviewBtn").disabled = !REVIEW_URL;
        $("moveSafeBtn").disabled = !job.movesReady;
        $("moveAllBtn").disabled = !job.movesNeedExtra;
        $("undoBtn").disabled = false;
        $("metricFiles").textContent = fmt(job.filesScanned);
        $("metricDupes").textContent = fmt(job.duplicateGroups);
        $("metricReady").textContent = fmt(job.movesReady);
        $("runDir").textContent = job.runDir || "No run";
        updateWaste(job.waste);
        renderActions(job.actions);
        await load();
        return;
      }
      if (job.state === "error") {
        $("scanBtn").disabled = false;
        setStep("pick");
        $("jobText").textContent = job.message || "Scan failed.";
        return;
      }
      setTimeout(pollJob, 800);
    }

    async function moveFiles(includeExtra) {
      if (!CURRENT_RUN) return;
      const confirmText = includeExtra
        ? "Move the files that need extra YES too?"
        : "Move the safe files now?";
      if (!confirm(confirmText + "\n\nNothing is deleted. Files move to Safe Quarantine or organized folders.")) return;
      const result = await api("/api/move", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({runDir: CURRENT_RUN, includeExtra})
      });
      setStep("move");
      $("jobText").textContent = `Moved: ${result.moved}. Problems: ${result.failed}.`;
      $("undoBtn").disabled = false;
      await load();
    }

    async function undo() {
      if (!CURRENT_RUN) return;
      if (!confirm("Put moved files back where they were?")) return;
      const result = await api("/api/undo", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({runDir: CURRENT_RUN})
      });
      $("jobText").textContent = `Moved back: ${result.undone}. Problems: ${result.failed}.`;
      await load();
    }

    $("scanBtn").onclick = startScan;
    $("openReviewBtn").onclick = () => { if (REVIEW_URL) window.open(REVIEW_URL, "_blank"); };
    $("moveSafeBtn").onclick = () => moveFiles(false);
    $("moveAllBtn").onclick = () => moveFiles(true);
    $("undoBtn").onclick = undo;
    $("selectSafeBtn").onclick = () => {
      SELECTED = new Set(SOURCES
        .filter(s => ["downloads", "pictures", "desktop"].includes(s.id))
        .filter(s => s.available)
        .map(s => s.path));
      renderSources();
    };
    load().catch(err => { $("jobText").textContent = err.message; });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _fmt: str, *_args) -> None:
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, default=_json_default).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
            return
        if parsed.path == "/api/status":
            state = clean_status.load_state()
            if state.get("last_reclaim_bytes") is not None:
                from sensei_clean.waste import human_bytes

                state["last_reclaim_bytes_human"] = human_bytes(
                    state.get("last_reclaim_bytes", 0)
                )
            self._json(
                {
                    "title": APP_TITLE,
                    "state": state,
                    "sources": _source_payload(),
                    "catalog": supported_connector_catalog(),
                }
            )
            return
        if parsed.path == "/api/job":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = (query.get("id") or [""])[0]
            self._json(
                JOBS.get(job_id) or {"state": "missing", "message": "Scan not found."}
            )
            return
        if parsed.path == "/report":
            query = urllib.parse.parse_qs(parsed.query)
            run = Path((query.get("run") or [""])[0]).resolve()
            name = (query.get("file") or ["review.html"])[0]
            if name not in {"review.html", "summary.md", "previews.md"}:
                self._send(403, "text/plain; charset=utf-8", b"blocked")
                return
            path = run / "reports" / name
            if not path.exists():
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            ctype = (
                "text/html; charset=utf-8"
                if name.endswith(".html")
                else "text/plain; charset=utf-8"
            )
            self._send(200, ctype, path.read_bytes())
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        payload = _read_json(self)
        if parsed.path == "/api/scan":
            job_id = str(int(time.time() * 1000))
            JOBS[job_id] = {"state": "queued", "message": "Queued."}
            thread = threading.Thread(
                target=_run_scan, args=(job_id, payload), daemon=True
            )
            thread.start()
            self._json({"jobId": job_id})
            return
        if parsed.path == "/api/move":
            run_dir = Path(str(payload.get("runDir") or "")).expanduser().resolve()
            include_extra = bool(payload.get("includeExtra", False))
            actions = _load_actions(run_dir)
            caps = _load_capabilities(run_dir)
            selected = (
                actions
                if include_extra
                else [a for a in actions if a.lane != "monitored"]
            )
            results = apply_per_adapter(selected, caps, str(run_dir / "undo.jsonl"))
            moved = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            try:
                clean_status.record_apply(
                    run_dir=str(run_dir), applied=moved, failed=failed
                )
            except Exception:
                pass
            self._json(
                {
                    "moved": moved,
                    "failed": failed,
                    "messages": [r.message for r in results if not r.success],
                }
            )
            return
        if parsed.path == "/api/undo":
            run_dir = Path(str(payload.get("runDir") or "")).expanduser().resolve()
            records = load_undo_records(str(run_dir / "undo.jsonl"))
            results = undo_per_adapter(list(reversed(records)))
            undone = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            self._json(
                {
                    "undone": undone,
                    "failed": failed,
                    "messages": [r.message for r in results if not r.success],
                }
            )
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")


def _pick_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free local port found")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sensei Clean web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    port = _pick_port(args.port)
    server = ThreadingHTTPServer((args.host, port), Handler)
    url = f"http://{args.host}:{port}/"
    print(f"Sensei Clean web UI: {url}")
    if args.open and not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
