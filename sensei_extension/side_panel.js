const CONFIG_SCHEMA = {
  backendUrl: "http://127.0.0.1:8080",
  agentUrl: "http://127.0.0.1:8001",
  wakeRelayUrl: "http://127.0.0.1:8765/wake",
  token: "",
  mode: "review",
  sessionId: "",
  actionPermissionMode: "ask",
  approvedOrigins: [],
  blockedOrigins: [
    "https://accounts.google.com",
    "https://pay.google.com",
  ],
  permissionHistory: [],
  resumePath: "",
  allowedUploadDirs: [],
  shortcuts: [],
  schedules: [],
  mcpServers: []
};

const DEFAULT_CONFIG = { ...CONFIG_SCHEMA };

const REQUEST_TIMEOUTS = {
  default: 60000,
  health: 3500,
  chat: 600000,
  stt: 180000,
  audit: 5000
};
const PAGE_CONTEXT_TTL_MS = 1500;
const PAGE_CONTEXT_TEXT_LIMIT = 1800;
const PAGE_TREE_BYTE_LIMIT = 24 * 1024;
const AX_TREE_TEXT_LIMIT = 14 * 1024;
const PAGE_STABLE_WAIT_MS = 650;
const PAGE_CONTEXT_PROMPT_RE =
  /\b(application|apply|browser|button|click|current\s+(page|tab|site)|drive|folder|field|fill|form|indeed|job|link|page|read|resume|résumé|screen|screenshot|search|select|selected|selection|simplify|tab|this|upload|visible|website|ziprecruiter)\b/i;
const READONLY_BROWSER_KINDS = new Set([
  "BROWSER_READ",
  "BROWSER_READ_PAGE",
  "BROWSER_OBSERVE",
  "BROWSER_SCREENSHOT",
  "BROWSER_WAIT",
  "BROWSER_SCROLL",
  "BROWSER_FIND",
  "BROWSER_EXTRACT_LIST",
  "BROWSER_DRIVE_INSPECT_FOLDER",
  "BROWSER_CONSOLE",
  "BROWSER_NETWORK",
  "BROWSER_TAB_LIST",   // Phase 5.5 — read-only tab enumeration
]);
const CONTEXT_SETTLING_BROWSER_KINDS = new Set([
  "BROWSER_NAV",
  "BROWSER_CLICK",
  "BROWSER_DOUBLE_CLICK",
  "BROWSER_FILL",
  "BROWSER_SCROLL",
  "BROWSER_WAIT",
]);

const state = {
  config: { ...DEFAULT_CONFIG },
  mediaRecorder: null,
  mediaStream: null,
  chunks: [],
  injectedTabs: new Set(),
  contextCache: null,
  auditQueue: [],
  auditFlushing: false,
  workflowRecording: {
    active: false,
    tabId: null,
    startedAt: null,
    rrwebEvents: []
  },
  rrwebStop: null,
  scheduleShortcutId: null,
  // Phase 1.1 — Domain classifier cache. host -> {result, ts}. Result shape:
  // {category, reason, matched, host, ttl_s}. Pre-warmed before renderActions
  // for the active-tab origin and any BROWSER_NAV targets in the round.
  domainClassCache: new Map(),
  // Phase 1.3 — Last-observed origin per session. Set every time the model
  // reads the page (BROWSER_READ_PAGE / BROWSER_OBSERVE / BROWSER_READ) and
  // after a confirmed BROWSER_NAV. Mutating actions abort if the active tab
  // has drifted away from this host between observation and dispatch.
  lastObservedOrigin: null,
  // Phase 4 — Chrome Tab Group for this session. Restored from
  // chrome.storage.local on init when the same Chrome session is still up,
  // else created on demand when the first BROWSER_TAB_CREATE fires.
  sessionTabGroup: null,
  // M9 — Agentic Continuation Loop (scaffold 2026-05-12). After /chat returns
  // proposed actions, the user approves/rejects each one. As each result is
  // reported to /extension/action_result, it's also appended to loop.results.
  // When all actions in a round have a verdict AND loop.active AND none were
  // rejected, the next round fires automatically via /chat/continue. Stop
  // button hard-interrupts. Budget caps runaway loops.
  loop: {
    active: false,
    turn_id: null,
    round_num: 0,
    round_budget: 3,
    pending: 0,
    rejected: false,
    results: [],
    stopped: false
  }
};

const $ = (selector) => document.querySelector(selector);

function chromeGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function chromeSet(values) {
  return new Promise((resolve) => chrome.storage.local.set(values, resolve));
}

async function loadConfig() {
  const stored = await chromeGet(Object.keys(DEFAULT_CONFIG));
  state.config = { ...DEFAULT_CONFIG, ...stored };
  if (!Array.isArray(state.config.approvedOrigins)) state.config.approvedOrigins = [];
  if (!Array.isArray(state.config.blockedOrigins)) state.config.blockedOrigins = [];
  if (!Array.isArray(state.config.permissionHistory)) state.config.permissionHistory = [];
  if (!Array.isArray(state.config.allowedUploadDirs)) state.config.allowedUploadDirs = [];
  if (!Array.isArray(state.config.shortcuts)) state.config.shortcuts = [];
  if (!Array.isArray(state.config.schedules)) state.config.schedules = [];
  if (!Array.isArray(state.config.mcpServers)) state.config.mcpServers = [];
  if (!["ask", "act"].includes(state.config.actionPermissionMode)) state.config.actionPermissionMode = "ask";
  if (!state.config.sessionId) {
    state.config.sessionId = `sensei-${crypto.randomUUID()}`;
    await chromeSet({ sessionId: state.config.sessionId });
  }
  state.config.backendUrl = String(state.config.backendUrl || DEFAULT_CONFIG.backendUrl).replace(/\/+$/, "");
  $("#modeSelect").value = state.config.mode || "review";
  document.body.className = `mode-${state.config.mode || "review"}`;
}

async function saveMode(mode) {
  state.config.mode = mode;
  await chromeSet({ mode });
  document.body.className = `mode-${mode}`;
  // Phase 4.1 — the tab group color follows the mode stoplight so users
  // can spot which mode created a tab without opening the panel.
  await syncTabGroupColorToMode();
}

// Phase 4.1 — TabGroupManager. Maps the Sensei mode to a Chrome tab-group
// color per feedback_mode_stoplight_colors.md (Plan=red / Review=orange /
// Auto=green). One group per side-panel session, restored across panel
// close/open as long as the Chrome session keeps the group alive.
const SESSION_TAB_GROUP_TITLE = "Sensei";

function tabGroupColorForMode(mode) {
  const m = String(mode || "").toLowerCase();
  if (m === "plan") return "red";
  if (m === "review") return "orange";
  if (m === "auto") return "green";
  return "blue";
}

function _currentMode() {
  return (document.getElementById("modeSelect")?.value ||
          state.config.mode || "review").toLowerCase();
}

async function _verifyTabGroupAlive(groupId) {
  if (!groupId || !chrome.tabGroups?.get) return false;
  try {
    await chrome.tabGroups.get(groupId);
    return true;
  } catch (_err) {
    return false;
  }
}

async function restoreSessionTabGroup() {
  if (!chrome.tabGroups?.get) return;
  const stored = await chromeGet(["sessionTabGroupId"]);
  const storedId = Number(stored?.sessionTabGroupId);
  if (!Number.isFinite(storedId) || storedId <= 0) return;
  if (!(await _verifyTabGroupAlive(storedId))) {
    await chromeSet({ sessionTabGroupId: null });
    return;
  }
  state.sessionTabGroup = { groupId: storedId, color: null, tabIds: new Set() };
  await syncTabGroupColorToMode();
}

async function addTabToSessionGroup(tabId) {
  if (!tabId || !chrome.tabs?.group) return null;
  const color = tabGroupColorForMode(_currentMode());
  let groupId = state.sessionTabGroup?.groupId || null;
  if (groupId && !(await _verifyTabGroupAlive(groupId))) {
    state.sessionTabGroup = null;
    groupId = null;
  }
  if (groupId) {
    await chrome.tabs.group({ groupId, tabIds: [tabId] });
  } else {
    groupId = await chrome.tabs.group({ tabIds: [tabId] });
    state.sessionTabGroup = { groupId, color, tabIds: new Set() };
    try {
      await chrome.tabGroups.update(groupId, { title: SESSION_TAB_GROUP_TITLE, color });
    } catch (_err) { /* user may have ungrouped immediately; non-fatal */ }
    await chromeSet({ sessionTabGroupId: groupId });
  }
  state.sessionTabGroup.tabIds.add(tabId);
  return groupId;
}

async function syncTabGroupColorToMode() {
  if (!state.sessionTabGroup?.groupId || !chrome.tabGroups?.update) return;
  const color = tabGroupColorForMode(_currentMode());
  if (state.sessionTabGroup.color === color) return;
  try {
    await chrome.tabGroups.update(state.sessionTabGroup.groupId, { color });
    state.sessionTabGroup.color = color;
  } catch (_err) {
    // Group may have been removed; clear so next addTab makes a new one.
    state.sessionTabGroup = null;
  }
}

// Phase 4.3 — enumerate the session group's tabs into the
// tabs_context payload field. Empty array when no group is established
// yet OR when chrome.tabs.query rejects (the next /chat round just
// won't see open tabs; not an error). Capped at 20 to bound prompt size.
async function gatherTabsContext() {
  const groupId = state.sessionTabGroup?.groupId;
  if (!groupId || !chrome.tabs?.query) return [];
  try {
    const tabs = await chrome.tabs.query({ groupId });
    if (!Array.isArray(tabs)) return [];
    return tabs.slice(0, 20).map((t) => ({
      tab_id: t.id,
      url: t.url || "",
      title: t.title || "",
      active: Boolean(t.active),
      in_session_group: true,
      status: t.status || "",
    }));
  } catch (_err) {
    return [];
  }
}

function backendHeaders(extra = {}) {
  return {
    "X-Master-AI-Token": state.config.token || "",
    ...extra
  };
}

function requestTimeout(path, options = {}) {
  if (Number.isFinite(options.timeoutMs)) return options.timeoutMs;
  if (path === "/health") return REQUEST_TIMEOUTS.health;
  if (path === "/stt") return REQUEST_TIMEOUTS.stt;
  if (path === "/tool/find" || path === "/tool/describe_step") return 20000;
  if (path === "/extension/action_result") return REQUEST_TIMEOUTS.audit;
  if (path === "/chat" || path === "/chat/continue") return REQUEST_TIMEOUTS.chat;
  return REQUEST_TIMEOUTS.default;
}

async function backendFetch(path, options = {}) {
  let body = options.body;
  const headers = backendHeaders(options.headers || {});
  const timeoutMs = requestTimeout(path, options);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  if (body !== undefined && !(body instanceof Blob) && typeof body !== "string") {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    body = JSON.stringify(body);
  } else if (body instanceof Blob) {
    headers["Content-Type"] = body.type || "application/octet-stream";
  }

  let res;
  let text;
  try {
    res = await fetch(`${state.config.backendUrl}${path}`, {
      method: options.method || (body === undefined ? "GET" : "POST"),
      headers,
      body,
      signal: controller.signal
    });
    text = await res.text();
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(`${path} timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_err) {
    data = { raw: text };
  }
  if (!res.ok) {
    // HTTP 503 from wedge protection is a temporary backend busy signal
    // (stt_server.py raises ApiHandleBusy when a runaway local inference
    // holds _API_HANDLE_LOCK past _API_HANDLE_LOCK_TIMEOUT_S). Surface
    // it as a recoverable retry message, not a raw error — the user
    // just has to wait and re-send. Same shape `_chat` endpoint emits.
    if (res.status === 503 && data?.error === "system_busy") {
      const retryAfter = Number(data.retry_after_s) || 15;
      const err = new Error(
        `Sensei is busy with another task on the local model — ` +
        `please retry in about ${retryAfter} seconds. ` +
        `(For an immediate answer, prefix your prompt with "fast:" ` +
        `to route to the cloud lane.)`
      );
      err.code = "system_busy";
      err.retryAfterS = retryAfter;
      throw err;
    }
    const message = data.error || `${res.status} ${res.statusText}`;
    throw new Error(message);
  }
  return data;
}

function setConnection(text, tone = "") {
  const el = $("#connectionState");
  el.textContent = text;
  el.dataset.tone = tone;
}

function cleanReply(text) {
  // DONE and ASK are directives the model emits, not chat prose. The model is
  // not allowed to self-certify completion in the chat bubble — DONE/FAILED
  // status is synthesized later from action_result outcomes (see
  // summarizeRoundResults). Strip them here so model-authored "DONE:" can
  // never reach the operator's eyes regardless of what the model wrote.
  const lines = String(text || "").split(/\n/);
  const cleaned = lines.filter((line) => {
    return !/^\s*(RUNTERM|RUN|READ|CREATE|EDIT|REMEMBER|BROWSER_[A-Z_]+|DONE|ASK):/i.test(line)
      && !/^\s*<<<(CONTENT|FIND|REPLACE)\s*$/i.test(line)
      && !/^\s*>>>(CONTENT|FIND|REPLACE)\s*$/i.test(line);
  }).join("\n").trim();
  return cleaned || "Action ready for review.";
}

function appendMessage(role, text, meta = "") {
  const log = $("#chatLog");
  const item = document.createElement("article");
  item.className = `message ${role}`;
  item.textContent = text;
  if (meta) {
    const small = document.createElement("span");
    small.className = "meta";
    small.textContent = meta;
    item.appendChild(small);
  }
  log.appendChild(item);
  log.scrollTop = log.scrollHeight;
  return item;
}

function appendError(text) {
  appendMessage("error", text);
}

// Phase A.2 — visible warning when the model's reply claims a browser
// action but emits no directive (silent-failure case from 2026-05-14
// trace). Catches "I took a screenshot" / "I clicked the button" prose
// with empty actions[]. Pattern is intentionally narrower than just
// "screenshot|click|fill" — pairs the verb with a target noun to reduce
// false positives on incidental chat use.
// Three patterns OR'd together:
//   (1) self-contained verb phrases that include their own target noun
//       (e.g., "took a screenshot", "navigated to", "opened the page")
//   (2) verb-then-target pairs within 40 chars (e.g., "clicked the
//       Sign in button" — the noun isn't adjacent to the verb).
//   (3) any literal BROWSER_<verb> mention. Covers [BROWSER_SCREENSHOT],
//       "BROWSER_SCREENSHOT", `BROWSER_SCREENSHOT`, and anywhere the
//       model wraps/quotes the directive instead of emitting it at
//       column 0 (witnessed live 2026-05-14 from local master-ai on
//       pinterest.com — model returned "[BROWSER_SCREENSHOT] url: ...").
//       The parser only matches ^\s*BROWSER_X: so any non-column-0 or
//       wrapped form needs the warning path.
const CLAIMED_BROWSER_ACTION_RE = /(?:\b(?:captur(?:ed|ing)|took (?:a |the )?(?:screenshot|snapshot|picture|photo)|taking (?:a |the )?(?:screenshot|snapshot|picture|photo)|screenshot(?:ted|ed)?|navigated to|opened (?:the |this )?(?:page|tab|link|url|site|website))\b)|(?:\b(?:clicked|filled (?:in |out )?(?:the )?|typed (?:into )?(?:the )?|pressed (?:the )?|scrolled (?:to |up |down )?)\b.{0,40}\b(?:button|link|field|form|input|tab|key|enter|return|escape|element|icon|email|password|down|up|bottom|top)\b)|(?:BROWSER_[A-Z_]+)/i;

function appendWarning(text) {
  appendMessage("error", `⚠ ${text}`);
}

// DONE: directives at column 0 mean the loop terminated cleanly; the
// model is reporting on already-completed actions, not trying to emit
// new ones. False-positives in those rounds (e.g., "DONE: Screenshot
// captured successfully") were firing the warning even though the
// actual action already ran in an earlier round.
const DONE_DIRECTIVE_RE = /^\s*DONE:\s*\S/m;

function maybeWarnSilentClaim(reply, actions, data) {
  if (Array.isArray(actions) && actions.length > 0) return;
  const text = String(reply || "");
  if (DONE_DIRECTIVE_RE.test(text)) return;
  if (data && (data.done === true || data.terminal_reason)) return;
  if (!CLAIMED_BROWSER_ACTION_RE.test(text)) return;
  appendWarning(
    "Model claimed a browser action but didn't emit a directive. " +
    "Try rephrasing concretely (e.g., \"take a screenshot of this page\", " +
    "\"click the Sign in button\") or switch the mode dropdown to " +
    "\"Act without asking.\""
  );
}

async function timed(timings, key, fn) {
  const start = performance.now();
  try {
    return await fn();
  } finally {
    timings[key] = Math.round(performance.now() - start);
  }
}

function formatMeta(data, timings = {}) {
  const parts = [data.route, data.model];
  if (Number.isFinite(data.latency_ms)) parts.push(`${data.latency_ms} ms server`);
  if (Number.isFinite(timings.context)) parts.push(`ctx ${timings.context} ms`);
  if (Number.isFinite(timings.total)) parts.push(`${timings.total} ms client`);
  return parts.filter(Boolean).join(" | ");
}

function promptNeedsPageContext(prompt) {
  return PAGE_CONTEXT_PROMPT_RE.test(String(prompt || ""));
}

async function activeTab() {
  const result = await chrome.runtime.sendMessage({ type: "SENSEI_ACTIVE_TAB" });
  return result?.tab || null;
}

function canInjectIntoTab(tab) {
  return Boolean(tab?.id && /^(https?:|file:)/i.test(String(tab.url || "")));
}

function contextCacheKey(tab, includeVisibleText) {
  return `${tab.id}:${tab.url || ""}:${includeVisibleText ? "full" : "shell"}`;
}

function invalidatePageContext(tabId = null) {
  if (!tabId || state.contextCache?.tabId === tabId) state.contextCache = null;
  if (tabId) state.injectedTabs.delete(tabId);
}

async function ensureContentScript(tab, timings = {}) {
  if (!canInjectIntoTab(tab)) return false;
  if (state.injectedTabs.has(tab.id)) return true;

  try {
    const ping = await chrome.tabs.sendMessage(tab.id, { type: "SENSEI_PING" });
    if (ping?.ok) {
      state.injectedTabs.add(tab.id);
      return true;
    }
  } catch (_err) {
    // Missing content script is expected with lazy injection.
  }

  await timed(timings, "inject", async () => {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["vendor/rrweb-record-lite.js", "content_script.js"] });
  });
  state.injectedTabs.add(tab.id);
  return true;
}

async function ensureWorkflowRecorderSupport(tab, timings = {}) {
  if (!canInjectIntoTab(tab)) return false;
  await ensureContentScript(tab, timings);
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["vendor/rrweb-record-lite.js"] });
  } catch (_err) {
    // The DOM event recorder still produces replayable steps if rrweb injection
    // is refused on the current page.
  }
  return true;
}

function jsonByteLength(value) {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).length;
  } catch (_err) {
    return String(JSON.stringify(value || "")).length;
  }
}

function clipStringField(obj, field, limit) {
  if (typeof obj?.[field] === "string" && obj[field].length > limit) {
    obj[field] = `${obj[field].slice(0, limit).trim()}...`;
    return true;
  }
  return false;
}

function trimPageContextToBudget(context, budgetBytes = PAGE_TREE_BYTE_LIMIT) {
  if (!context || typeof context !== "object") return context || {};
  const out = typeof structuredClone === "function" ? structuredClone(context) : JSON.parse(JSON.stringify(context));
  out.context_budget = {
    bytes: budgetBytes,
    truncated: false,
    strategy: "24KB local-lane cap; trim visible text, semantic tail, then low-priority interactives",
  };
  const mark = () => { out.context_budget.truncated = true; };

  if (jsonByteLength(out) <= budgetBytes) return out;
  if (clipStringField(out, "visible_text", 2400)) mark();
  if (jsonByteLength(out) <= budgetBytes) return out;

  if (out.semantic_tree?.text) {
    out.semantic_tree.text = `${String(out.semantic_tree.text).slice(0, 9000).trim()}...`;
    out.semantic_tree.truncated = true;
    mark();
  }
  if (jsonByteLength(out) <= budgetBytes) return out;

  if (Array.isArray(out.semantic_fallback)) {
    out.semantic_fallback = out.semantic_fallback.slice(0, 60);
    mark();
  }
  if (jsonByteLength(out) <= budgetBytes) return out;

  if (typeof out.interactive_elements === "string") {
    out.interactive_elements = `${out.interactive_elements.slice(0, 5000).trim()}...`;
    mark();
  }
  if (Array.isArray(out.iframes) && out.iframes.length > 12) {
    out.iframes = out.iframes.slice(0, 12);
    mark();
  }
  if (jsonByteLength(out) <= budgetBytes) return out;

  if (out.dom_state?.forms) {
    out.dom_state.forms = out.dom_state.forms.slice(0, 8).map((form) => ({
      ...form,
      fields: Array.isArray(form.fields) ? form.fields.slice(0, 18) : []
    }));
    mark();
  }
  if (jsonByteLength(out) <= budgetBytes) return out;

  if (clipStringField(out, "visible_text", 1200)) mark();
  if (out.console_state) {
    out.console_state = Array.isArray(out.console_state) ? out.console_state.slice(-8) : [];
    mark();
  }
  return out;
}

function axValue(raw) {
  if (!raw) return "";
  if (typeof raw.value === "string" || typeof raw.value === "number" || typeof raw.value === "boolean") {
    return String(raw.value);
  }
  return "";
}

function axProp(node, name) {
  const prop = Array.isArray(node?.properties)
    ? node.properties.find((entry) => entry?.name === name)
    : null;
  return axValue(prop?.value);
}

function axRole(node) {
  return axValue(node?.role) || "";
}

function axName(node) {
  return axValue(node?.name) || axProp(node, "label") || "";
}

function keepAxNode(node) {
  if (!node || node.ignored) return false;
  const role = axRole(node);
  const name = axName(node);
  const value = axValue(node.value);
  if (name || value) return true;
  return /^(RootWebArea|main|navigation|banner|contentinfo|search|form|dialog|alert|heading|button|link|textbox|searchbox|combobox|checkbox|radio|switch|tab|menuitem|row|cell|grid|table|list|listitem|iframe)$/i.test(role);
}

function compactAccessibilityTree(nodes, maxBytes = AX_TREE_TEXT_LIMIT) {
  if (!Array.isArray(nodes) || !nodes.length) return null;
  const byId = new Map(nodes.map((node) => [String(node.nodeId), node]));
  const root = nodes.find((node) => axRole(node) === "RootWebArea") || nodes[0];
  const lines = [];
  const seen = new Set();
  let bytes = 0;
  let truncated = false;

  const addLine = (line) => {
    const nextBytes = new TextEncoder().encode(`${line}\n`).length;
    if (bytes + nextBytes > maxBytes) {
      truncated = true;
      return false;
    }
    lines.push(line);
    bytes += nextBytes;
    return true;
  };

  const visit = (node, depth) => {
    if (!node || seen.has(node.nodeId) || truncated) return;
    seen.add(node.nodeId);
    const kept = keepAxNode(node);
    if (kept) {
      const role = axRole(node) || "node";
      const name = axName(node);
      const value = axValue(node.value);
      const states = [
        axProp(node, "checked") ? `checked=${axProp(node, "checked")}` : "",
        axProp(node, "selected") ? `selected=${axProp(node, "selected")}` : "",
        axProp(node, "expanded") ? `expanded=${axProp(node, "expanded")}` : "",
        axProp(node, "disabled") ? `disabled=${axProp(node, "disabled")}` : "",
      ].filter(Boolean).join(" ");
      const label = [
        `${"  ".repeat(Math.min(depth, 6))}- ${role}`,
        name ? `"${name.slice(0, 220)}"` : "",
        value ? `value="${value.slice(0, 160)}"` : "",
        states
      ].filter(Boolean).join(" ");
      if (!addLine(label)) return;
    }
    for (const childId of node.childIds || []) {
      visit(byId.get(String(childId)), kept ? depth + 1 : depth);
      if (truncated) break;
    }
  };

  visit(root, 0);
  return {
    source: "chrome_accessibility_tree",
    budget_bytes: maxBytes,
    truncated,
    node_count: nodes.length,
    retained_lines: lines.length,
    text: lines.join("\n")
  };
}

function summarizeAxSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return "";
  const buckets = [
    ["heading", snapshot.headings],
    ["landmark", snapshot.landmarks],
    ["button", snapshot.buttons],
    ["link", snapshot.links],
    ["input", snapshot.inputs],
    ["dialog", snapshot.dialogs],
    ["row", snapshot.file_folder_rows],
    ["iframe", snapshot.iframes],
  ];
  const lines = [];
  for (const [kind, items] of buckets) {
    if (!Array.isArray(items)) continue;
    for (const item of items.slice(0, 80)) {
      const role = item.role || kind;
      const name = item.name || item.label || item.text || item.title || item.src || "";
      const ref = item.ref ? ` ref=${item.ref}` : "";
      const selector = item.selector ? ` selector=${item.selector}` : "";
      const hidden = item.cross_origin ? " cross_origin=true" : "";
      lines.push(`- ${role} "${String(name).slice(0, 220)}"${ref}${selector}${hidden}`);
      if (lines.join("\n").length > AX_TREE_TEXT_LIMIT) {
        lines.push("...");
        return lines.join("\n");
      }
    }
  }
  return lines.join("\n");
}

// Phase 3.1 — CDP-driven mouse dispatch. Parses BROWSER_CDP_MOUSE targets in
// JSON shape ({action:"click"|"move"|"press"|"release"|"wheel", x, y, button?,
// modifiers?, deltaX?, deltaY?, clickCount?}) or positional shorthand
// ("click 300 400 [button] [count] [modifiers]" / "wheel x y deltaX deltaY").
// Composite "click" expands to mousePressed + mouseReleased on the same point.
const _CDP_BUTTON_NAMES = new Set(["none", "left", "middle", "right", "back", "forward"]);
const _CDP_BUTTON_BITS = { none: 0, left: 1, right: 2, middle: 4, back: 8, forward: 16 };

function _parseCdpMouseTarget(rawTarget) {
  const raw = String(rawTarget || "").trim();
  if (!raw) return null;
  // JSON form.
  if (raw.startsWith("{")) {
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object") return obj;
    } catch (_err) { /* fall through to positional */ }
  }
  // Positional form: "action x y [button|deltaX|toX] [count|deltaY|toY] [modifiers]".
  const parts = raw.split(/\s+/);
  if (parts.length < 3) return null;
  const action = parts[0].toLowerCase();
  const x = Number(parts[1]);
  const y = Number(parts[2]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  if (action === "wheel") {
    return {
      action: "wheel",
      x, y,
      deltaX: Number(parts[3] || 0),
      deltaY: Number(parts[4] || 0),
    };
  }
  if (action === "drag") {
    // Phase 3.3 — drag x1 y1 x2 y2 [button=left] [modifiers=0]
    const toX = Number(parts[3]);
    const toY = Number(parts[4]);
    if (!Number.isFinite(toX) || !Number.isFinite(toY)) return null;
    const out = { action: "drag", x, y, toX, toY };
    if (parts[5] && _CDP_BUTTON_NAMES.has(parts[5].toLowerCase())) {
      out.button = parts[5].toLowerCase();
    }
    if (parts[6]) {
      const n = Number(parts[6]);
      if (Number.isFinite(n)) out.modifiers = n;
    }
    return out;
  }
  const out = { action, x, y };
  if (parts[3]) {
    if (_CDP_BUTTON_NAMES.has(parts[3].toLowerCase())) out.button = parts[3].toLowerCase();
  }
  if (parts[4]) {
    const n = Number(parts[4]);
    if (Number.isFinite(n)) out.clickCount = n;
  }
  if (parts[5]) {
    const n = Number(parts[5]);
    if (Number.isFinite(n)) out.modifiers = n;
  }
  return out;
}

async function dispatchCdpMouse(tab, action, timings = {}) {
  const parsed = _parseCdpMouseTarget(action?.target);
  if (!parsed || !Number.isFinite(Number(parsed.x)) || !Number.isFinite(Number(parsed.y))) {
    return { ok: false, error: "BROWSER_CDP_MOUSE target must include numeric x and y" };
  }
  const op = String(parsed.action || "click").toLowerCase();
  const x = Number(parsed.x);
  const y = Number(parsed.y);
  const button = _CDP_BUTTON_NAMES.has(String(parsed.button || "").toLowerCase())
    ? String(parsed.button).toLowerCase()
    : (op === "wheel" ? "none" : "left");
  const buttonsMask = _CDP_BUTTON_BITS[button] ?? 0;
  const modifiers = Number.isFinite(Number(parsed.modifiers)) ? Number(parsed.modifiers) : 0;
  const clickCount = Number.isFinite(Number(parsed.clickCount)) ? Number(parsed.clickCount) : 1;

  try {
    return await withChromeDebugger(tab, timings, async (send) => {
      if (op === "move") {
        await send("Input.dispatchMouseEvent", {
          type: "mouseMoved", x, y, button: "none", buttons: 0, modifiers,
        });
        return { ok: true, action: "move", x, y };
      }
      if (op === "wheel") {
        await send("Input.dispatchMouseEvent", {
          type: "mouseWheel", x, y,
          button: "none", buttons: 0, modifiers,
          deltaX: Number(parsed.deltaX || 0),
          deltaY: Number(parsed.deltaY || 0),
        });
        return { ok: true, action: "wheel", x, y,
                 deltaX: Number(parsed.deltaX || 0),
                 deltaY: Number(parsed.deltaY || 0) };
      }
      if (op === "press") {
        await send("Input.dispatchMouseEvent", {
          type: "mousePressed", x, y, button, buttons: buttonsMask, modifiers, clickCount,
        });
        return { ok: true, action: "press", x, y, button, clickCount };
      }
      if (op === "release") {
        await send("Input.dispatchMouseEvent", {
          type: "mouseReleased", x, y, button, buttons: 0, modifiers, clickCount,
        });
        return { ok: true, action: "release", x, y, button, clickCount };
      }
      if (op === "drag") {
        // Phase 3.3 — drag composite: press at (x,y), step-move to (toX,toY), release.
        const toX = Number(parsed.toX);
        const toY = Number(parsed.toY);
        if (!Number.isFinite(toX) || !Number.isFinite(toY)) {
          return { ok: false, error: "drag requires numeric toX and toY" };
        }
        await send("Input.dispatchMouseEvent", {
          type: "mousePressed", x, y, button, buttons: buttonsMask, modifiers, clickCount: 1,
        });
        // Step the move so SPAs that listen for mousemove updates see motion.
        const STEPS = 5;
        for (let i = 1; i <= STEPS; i += 1) {
          const stepX = x + ((toX - x) * i) / STEPS;
          const stepY = y + ((toY - y) * i) / STEPS;
          await send("Input.dispatchMouseEvent", {
            type: "mouseMoved", x: stepX, y: stepY,
            button, buttons: buttonsMask, modifiers,
          });
        }
        await send("Input.dispatchMouseEvent", {
          type: "mouseReleased", x: toX, y: toY, button, buttons: 0, modifiers, clickCount: 1,
        });
        return { ok: true, action: "drag", from: { x, y }, to: { x: toX, y: toY }, button };
      }
      // Default + "click": composite press + release at the same point.
      await send("Input.dispatchMouseEvent", {
        type: "mousePressed", x, y, button, buttons: buttonsMask, modifiers, clickCount,
      });
      await send("Input.dispatchMouseEvent", {
        type: "mouseReleased", x, y, button, buttons: 0, modifiers, clickCount,
      });
      return { ok: true, action: "click", x, y, button, clickCount };
    });
  } catch (err) {
    return { ok: false, error: `cdp mouse dispatch failed: ${err?.message || err}` };
  }
}

// Phase 3.2 — CDP-driven keyboard dispatch. Parses BROWSER_CDP_KEY targets:
//   shorthand: "type <text>"           — emit char events for every char
//   shorthand: "press <key> [modifiers]" — keyDown + keyUp on one key
//   shorthand: "down <key> [modifiers]"  — keyDown only
//   shorthand: "up <key> [modifiers]"    — keyUp only
//   JSON: {"action":"press","key":"Enter","modifiers":4,"code":"Enter"}
// Modifiers bitmask (CDP): Alt=1, Ctrl=2, Meta=4, Shift=8.
function _keyToCode(key) {
  const k = String(key || "");
  if (k.length === 1) {
    if (/[a-zA-Z]/.test(k)) return "Key" + k.toUpperCase();
    if (/[0-9]/.test(k)) return "Digit" + k;
  }
  const named = {
    Enter: "Enter", Tab: "Tab", Escape: "Escape", Backspace: "Backspace",
    Space: "Space", " ": "Space",
    ArrowUp: "ArrowUp", ArrowDown: "ArrowDown",
    ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
    Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
    Delete: "Delete", Insert: "Insert",
    F1:"F1", F2:"F2", F3:"F3", F4:"F4", F5:"F5", F6:"F6", F7:"F7", F8:"F8",
    F9:"F9", F10:"F10", F11:"F11", F12:"F12",
  };
  return named[k] || k;
}

function _parseCdpKeyTarget(rawTarget) {
  const raw = String(rawTarget || "").trim();
  if (!raw) return null;
  if (raw.startsWith("{")) {
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object") return obj;
    } catch (_err) { /* fall through */ }
  }
  // First word is the action verb; rest is action-specific.
  const m = raw.match(/^(\S+)\s+([\s\S]+)$/);
  if (!m) return null;
  const action = m[1].toLowerCase();
  const rest = m[2];
  if (action === "type") {
    return { action: "type", text: rest };
  }
  if (action === "press" || action === "down" || action === "up") {
    const parts = rest.trim().split(/\s+/);
    const key = parts[0];
    const modifiers = parts[1] !== undefined ? Number(parts[1]) : 0;
    return { action, key, modifiers: Number.isFinite(modifiers) ? modifiers : 0 };
  }
  return null;
}

async function dispatchCdpKey(tab, action, timings = {}) {
  const parsed = _parseCdpKeyTarget(action?.target);
  if (!parsed) {
    return { ok: false, error: "BROWSER_CDP_KEY target malformed; use `type <text>`, `press <key> [modifiers]`, or JSON" };
  }
  const op = String(parsed.action || "press").toLowerCase();
  const modifiers = Number.isFinite(Number(parsed.modifiers)) ? Number(parsed.modifiers) : 0;
  try {
    return await withChromeDebugger(tab, timings, async (send) => {
      if (op === "type") {
        const text = String(parsed.text || "");
        if (!text) return { ok: false, error: "type action requires non-empty text" };
        for (const ch of text) {
          await send("Input.dispatchKeyEvent", { type: "char", text: ch });
        }
        return { ok: true, action: "type", chars: text.length };
      }
      const key = String(parsed.key || "");
      if (!key) return { ok: false, error: "press/down/up requires key" };
      const code = String(parsed.code || _keyToCode(key));
      const base = { key, code, modifiers };
      if (op === "down" || op === "press") {
        await send("Input.dispatchKeyEvent", { ...base, type: "keyDown" });
      }
      if (op === "up" || op === "press") {
        await send("Input.dispatchKeyEvent", { ...base, type: "keyUp" });
      }
      return { ok: true, action: op, key, modifiers };
    });
  } catch (err) {
    return { ok: false, error: `cdp key dispatch failed: ${err?.message || err}` };
  }
}

// Phase 5.1 — BROWSER_JS. Runs arbitrary JS in the page context via CDP
// Runtime.evaluate. The script source is the action.target string (capped at
// 256KB so a runaway model can't blow the message bus). returnByValue:true so
// the result serializes; awaitPromise:true so async returns wait properly.
// PermissionManager.typeFor maps this to a dedicated EXEC_JAVASCRIPT type.
const BROWSER_JS_MAX_SOURCE_LEN = 256 * 1024;

async function dispatchBrowserJs(tab, action, timings = {}) {
  const source = String(action?.target || "");
  if (!source.trim()) {
    return { ok: false, error: "BROWSER_JS target must be a non-empty script source" };
  }
  if (source.length > BROWSER_JS_MAX_SOURCE_LEN) {
    return { ok: false, error: `BROWSER_JS source exceeds ${BROWSER_JS_MAX_SOURCE_LEN} byte cap` };
  }
  try {
    return await withChromeDebugger(tab, timings, async (send) => {
      await send("Runtime.enable");
      const evaluation = await send("Runtime.evaluate", {
        expression: source,
        returnByValue: true,
        awaitPromise: true,
        userGesture: false,
        timeout: 30000,
      });
      if (evaluation?.exceptionDetails) {
        return {
          ok: false,
          error: String(evaluation.exceptionDetails.text ||
                        evaluation.exceptionDetails.exception?.description ||
                        "runtime exception").slice(0, 600),
          exception_text: evaluation.exceptionDetails.text || "",
        };
      }
      const value = evaluation?.result?.value;
      const type = evaluation?.result?.type || "undefined";
      // Truncate large return payloads so the audit trail stays bounded.
      let serialized;
      try { serialized = JSON.stringify(value); }
      catch (_err) { serialized = "[unserializable]"; }
      const truncated = serialized && serialized.length > 8192;
      return {
        ok: true,
        type,
        value: truncated ? JSON.parse(serialized.slice(0, 8192) + "\"") : value,
        value_truncated: truncated,
      };
    });
  } catch (err) {
    return { ok: false, error: `BROWSER_JS dispatch failed: ${err?.message || err}` };
  }
}

// Phase 5.2 — BROWSER_CONSOLE. Reads the ring buffer of console events the
// content script has been capturing since page load. action.target is the
// optional level filter ("error" | "warn" | "log" | "all"); empty → "all".
async function dispatchBrowserConsole(tab, action, timings = {}) {
  const filter = String(action?.target || "all").trim().toLowerCase();
  try {
    const events = await timed(timings, "console_read", () => chrome.tabs.sendMessage(
      tab.id,
      { type: "SENSEI_READ_CONSOLE_EVENTS", filter },
    ));
    if (!events || !Array.isArray(events)) {
      return { ok: false, error: "content script did not return console events; the page may not allow injection" };
    }
    return { ok: true, count: events.length, events, filter };
  } catch (err) {
    return { ok: false, error: `BROWSER_CONSOLE failed: ${err?.message || err}` };
  }
}

// Phase 5.3 — BROWSER_NETWORK. Attaches the debugger (if not already), enables
// the Network domain, and returns the ring buffer of recent network requests
// captured by service_worker.js. action.target accepts an optional filter
// ("xhr" | "fetch" | "all"); empty → "all". Authorization and Cookie headers
// are redacted before being returned.
async function dispatchBrowserNetwork(tab, action, timings = {}) {
  const filter = String(action?.target || "all").trim().toLowerCase();
  try {
    const response = await timed(timings, "network_read", () => chrome.runtime.sendMessage({
      type: "SENSEI_READ_NETWORK_EVENTS",
      tabId: tab.id,
      filter,
    }));
    if (!response?.ok) {
      return { ok: false, error: response?.error || "network capture unavailable" };
    }
    return {
      ok: true,
      count: response.count || 0,
      events: response.events || [],
      filter,
    };
  } catch (err) {
    return { ok: false, error: `BROWSER_NETWORK failed: ${err?.message || err}` };
  }
}

// Phase 5.4 — BROWSER_RESIZE_WINDOW. Accepts "WxH" shorthand or JSON
// {width, height}. Resizes the current browser window via chrome.windows.update.
async function dispatchBrowserResizeWindow(tab, action, timings = {}) {
  const raw = String(action?.target || "").trim();
  let width, height;
  if (raw.startsWith("{")) {
    try {
      const obj = JSON.parse(raw);
      width = Number(obj.width); height = Number(obj.height);
    } catch (_err) {
      return { ok: false, error: "BROWSER_RESIZE_WINDOW target must be 'WxH' or JSON {width,height}" };
    }
  } else {
    const m = raw.match(/^(\d+)\s*[x×]\s*(\d+)$/i);
    if (!m) return { ok: false, error: "BROWSER_RESIZE_WINDOW target must be 'WxH' or JSON {width,height}" };
    width = Number(m[1]); height = Number(m[2]);
  }
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return { ok: false, error: "width and height must be positive numbers" };
  }
  // Safety clamps: don't let a runaway value hide the user's window off-screen.
  width = Math.min(Math.max(width, 320), 4096);
  height = Math.min(Math.max(height, 240), 4096);
  try {
    await chrome.windows.update(tab.windowId, { width, height });
    return { ok: true, width, height, windowId: tab.windowId };
  } catch (err) {
    return { ok: false, error: `resize failed: ${err?.message || err}` };
  }
}

async function withChromeDebugger(tab, timings, fn) {
  if (!chrome.debugger?.attach || !tab?.id) throw new Error("chrome.debugger unavailable");
  const target = { tabId: tab.id };
  let attached = false;
  await timed(timings, "debugger_attach", () => chrome.debugger.attach(target, "1.3"));
  attached = true;
  const send = (method, params = {}) => timed(timings, `cdp_${method.split(".").pop()}`, () =>
    chrome.debugger.sendCommand(target, method, params)
  );
  try {
    return await fn(send);
  } finally {
    if (attached) {
      try { await chrome.debugger.detach(target); } catch (_err) {}
    }
  }
}

async function accessibilityTreeContext(tab, timings = {}) {
  try {
    const response = await timed(timings, "ax_snapshot", () => chrome.runtime.sendMessage({
      type: "SENSEI_BUILD_AX_SNAPSHOT",
      tabId: tab.id,
    }));
    if (response?.ok && response.snapshot) {
      return {
        tree: response.snapshot,
        semantic_tree: {
          source: "chrome_accessibility_tree",
          budget_bytes: PAGE_TREE_BYTE_LIMIT,
          snapshot: response.snapshot,
          text: summarizeAxSnapshot(response.snapshot),
          truncated: Boolean(response.snapshot?.truncation),
        },
        browser_read_source: "accessibility_tree_primary",
      };
    }
  } catch (_err) {
    // Older service-worker builds do not expose SENSEI_BUILD_AX_SNAPSHOT.
    // Fall through to a direct debugger read from the side panel.
  }

  try {
    return await withChromeDebugger(tab, timings, async (send) => {
      await send("Accessibility.enable");
      const result = await send("Accessibility.getFullAXTree");
      const semanticTree = compactAccessibilityTree(result?.nodes || []);
      if (!semanticTree) return null;
      return {
        semantic_tree: semanticTree,
        browser_read_source: "accessibility_tree_primary",
      };
    });
  } catch (err) {
    return {
      browser_read_source: "content_script_fallback",
      accessibility_error: String(err?.message || err).slice(0, 300),
    };
  }
}

async function contentScriptPageContext(tab, options, timings = {}) {
  await ensureContentScript(tab, timings);
  const response = await timed(timings, "context", () => chrome.tabs.sendMessage(tab.id, {
    type: "SENSEI_PAGE_CONTEXT",
    options
  }));
  return response?.page_context || {};
}

async function pageContext(prompt = "", timings = {}) {
  const tab = await timed(timings, "tab", activeTab);
  if (!tab?.id) return {};
  const fallback = { url: tab.url || "", title: tab.title || "" };
  const includeVisibleText = promptNeedsPageContext(prompt);
  const key = contextCacheKey(tab, includeVisibleText);
  if (state.contextCache?.key === key && performance.now() - state.contextCache.ts < PAGE_CONTEXT_TTL_MS) {
    return state.contextCache.value;
  }

  if (!includeVisibleText || !canInjectIntoTab(tab)) {
    state.contextCache = { key, tabId: tab.id, ts: performance.now(), value: fallback };
    return fallback;
  }

  try {
    const [domContext, axContext] = await Promise.all([
      contentScriptPageContext(tab, {
        includeVisibleText,
        includeInteractiveElements: true,
        visibleTextLimit: PAGE_CONTEXT_TEXT_LIMIT,
        waitForStableMs: PAGE_STABLE_WAIT_MS,
        maxWaitMs: 4500,
      }, timings).catch(() => fallback),
      accessibilityTreeContext(tab, timings),
    ]);
    const value = trimPageContextToBudget({ ...fallback, ...(domContext || {}), ...(axContext || {}) });
    state.contextCache = { key, tabId: tab.id, ts: performance.now(), value };
    return value;
  } catch (_err) {
    return fallback;
  }
}

async function freshPageContextForContinuation(timings = {}) {
  const tab = await timed(timings, "tab", activeTab);
  if (!tab?.id) return {};
  const fallback = { url: tab.url || "", title: tab.title || "" };
  if (!canInjectIntoTab(tab)) return fallback;

  try {
    await waitForTabSettled(tab.id, 6000);
    const current = await chrome.tabs.get(tab.id);
    const [domContext, axContext] = await Promise.all([
      contentScriptPageContext(current, {
        includeVisibleText: true,
        includeInteractiveElements: true,
        visibleTextLimit: PAGE_CONTEXT_TEXT_LIMIT,
        waitForStableMs: PAGE_STABLE_WAIT_MS,
        maxWaitMs: 4500,
      }, timings).catch(() => fallback),
      accessibilityTreeContext(current, timings),
    ]);
    return trimPageContextToBudget({ ...fallback, ...(domContext || {}), ...(axContext || {}) });
  } catch (_err) {
    return fallback;
  }
}

function normalizeUrl(target) {
  const raw = String(target || "").trim();
  if (/^https?:\/\//i.test(raw)) return raw;
  if (/^[\w.-]+\.[a-z]{2,}(\/.*)?$/i.test(raw)) return `https://${raw}`;
  return raw;
}

function parseFillTargetForBridge(action) {
  const raw = String(action?.target || "").trim();
  const extras = action?.extras || {};
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      return {
        selector: String(parsed.selector || parsed.target || "").trim(),
        value: String(parsed.value || parsed.text || "").trim(),
      };
    } catch (_err) {
      // Fall through to delimiter parsing.
    }
  }
  const match = raw.match(/^(.*?)\s*(?:=>|:=|::)\s*([\s\S]*)$/);
  if (match) return { selector: match[1].trim(), value: match[2].trim() };
  return {
    selector: raw,
    value: String(extras.value || extras.text || "").trim(),
  };
}

function localPathFromFillValue(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^file:\/\//i.test(raw)) {
    try {
      return decodeURIComponent(new URL(raw).pathname);
    } catch (_err) {
      return raw.replace(/^file:\/\//i, "");
    }
  }
  if ((raw.startsWith("~/") || raw.startsWith("/")) &&
      /\.(pdf|docx?|odt|rtf|txt|csv|jpe?g|png|webp|gif|zip)$/i.test(raw)) {
    return raw;
  }
  return "";
}

function basenameForPath(path) {
  return String(path || "").split(/[\\/]/).filter(Boolean).pop() || "upload.bin";
}

function parseUploadTarget(rawTarget) {
  const raw = String(rawTarget || "").trim();
  const match = raw.match(/^(.+?)\s*(?:::|=>|:=)\s*(.+)$/);
  if (!match) return { selector: "", path: "" };
  let path = String(match[2] || "").trim();
  if ((path.startsWith('"') && path.endsWith('"')) ||
      (path.startsWith("'") && path.endsWith("'"))) {
    path = path.slice(1, -1).trim();
  }
  return {
    selector: String(match[1] || "").trim(),
    path,
  };
}

function configuredUploadDirs() {
  return (Array.isArray(state.config?.allowedUploadDirs) ? state.config.allowedUploadDirs : [])
    .map((dir) => String(dir || "").trim().replace(/[\\/]+$/, ""))
    .filter(Boolean);
}

function uploadAllowlistDirs() {
  return ["~/Documents", "~/Desktop", "~/Downloads", ...configuredUploadDirs()];
}

function pathWithinUploadAllowlist(path, allowedDirs = uploadAllowlistDirs()) {
  const rawPath = String(path || "").trim().replace(/[\\/]+$/, "");
  if (!rawPath) return false;
  const normalizedPath = rawPath.replace(/\\/g, "/");
  if (/^~\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) return true;
  if (/^\/home\/[^/]+\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) return true;
  if (/^\/Users\/[^/]+\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) return true;
  return allowedDirs.some((dir) => {
    const normalizedDir = String(dir || "").trim().replace(/[\\/]+$/, "").replace(/\\/g, "/");
    if (!normalizedDir) return false;
    return normalizedPath === normalizedDir || normalizedPath.startsWith(`${normalizedDir}/`);
  });
}

function assertUploadAllowlist(path) {
  if (pathWithinUploadAllowlist(path)) return { valid: true, allowedDirs: uploadAllowlistDirs() };
  return {
    valid: false,
    reason: "path outside upload allowlist",
    allowedDirs: uploadAllowlistDirs(),
  };
}

function promptNeedsLocalFileHints(prompt) {
  return /\b(resume|résumé|cv|cover\s+letter|ai\s+query|transcript|certificate|certification|pdf|docx?|upload|application)\b/i
    .test(String(prompt || ""));
}

async function localFileHintsForPrompt(prompt) {
  if (!promptNeedsLocalFileHints(prompt)) return null;
  try {
    const preferred = [state.config.resumePath].filter(Boolean);
    const data = await backendFetch("/extension/resolve_local_file", {
      method: "POST",
      body: { query: prompt, preferred_paths: preferred },
      timeoutMs: 9000,
    });
    if (!data?.ok || !Array.isArray(data.candidates) || !data.candidates.length) return null;
    return {
      query: data.query || prompt,
      ambiguous: Boolean(data.ambiguous),
      candidates: data.candidates.slice(0, 5),
    };
  } catch (_err) {
    return null;
  }
}

async function attachLocalFilePayload(action) {
  const kind = String(action?.kind || "").toUpperCase();
  if (kind !== "BROWSER_FILL") return action;
  const parsed = parseFillTargetForBridge(action);
  const path = localPathFromFillValue(parsed.value);
  if (!path) return action;
  const uploadGate = assertUploadAllowlist(path);
  if (!uploadGate.valid) {
    throw new Error(uploadGate.reason);
  }

  const file = await backendFetch("/extension/read_local_file", {
    method: "POST",
    body: { path },
    timeoutMs: 60000,
  });
  if (!file?.ok || !file?.base64) {
    throw new Error(file?.error || `local file read failed: ${path}`);
  }
  return {
    ...action,
    extras: {
      ...(action.extras || {}),
      fileUpload: {
        path: file.path || path,
        name: basenameForPath(file.path || path),
        mime: file.mime || "application/octet-stream",
        size: file.size || 0,
        base64: file.base64,
      },
    },
  };
}

async function tryDebuggerFileUpload(tab, action, timings = {}) {
  const kind = String(action?.kind || "").toUpperCase();
  if (kind !== "BROWSER_FILL") return null;
  const parsed = parseFillTargetForBridge(action);
  const requestedPath = localPathFromFillValue(parsed.value);
  if (!requestedPath || !parsed.selector) return null;
  const uploadGate = assertUploadAllowlist(requestedPath);
  if (!uploadGate.valid) throw new Error(uploadGate.reason);

  const file = await backendFetch("/extension/read_local_file", {
    method: "POST",
    body: { path: requestedPath },
    timeoutMs: 60000,
  });
  if (!file?.ok) throw new Error(file?.error || `local file read failed: ${requestedPath}`);
  const path = file.path || requestedPath;

  try {
    const uploaded = await withChromeDebugger(tab, timings, async (send) => {
      await send("DOM.enable");
      const expression = `document.querySelector(${JSON.stringify(parsed.selector)})`;
      const evaluated = await send("Runtime.evaluate", {
        expression,
        objectGroup: "sensei-file-upload",
        includeCommandLineAPI: false,
      });
      const objectId = evaluated?.result?.objectId;
      if (!objectId) throw new Error("file input selector did not resolve in debugger");
      const node = await send("DOM.requestNode", { objectId });
      if (!node?.nodeId) throw new Error("debugger could not resolve file input node");
      await send("DOM.setFileInputFiles", {
        nodeId: node.nodeId,
        files: [path],
      });
      await send("Runtime.evaluate", {
        expression: `(() => { const el = document.querySelector(${JSON.stringify(parsed.selector)}); if (!el) return false; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); return true; })()`,
        includeCommandLineAPI: false,
        returnByValue: true,
      }).catch(() => {});
      await send("Runtime.releaseObject", { objectId }).catch(() => {});
      return {
        ok: true,
        filled: parsed.selector,
        file_upload: {
          method: "debugger.DOM.setFileInputFiles",
          path,
          file_name: basenameForPath(path),
          file_size: file.size || 0,
          mime: file.mime || "application/octet-stream",
        },
      };
    });
    return uploaded;
  } catch (err) {
    return {
      ok: false,
      debugger_file_upload_failed: true,
      error: String(err?.message || err),
      fallback_action: {
        ...action,
        extras: {
          ...(action.extras || {}),
          fileUpload: {
            path,
            name: basenameForPath(path),
            mime: file.mime || "application/octet-stream",
            size: file.size || 0,
            base64: file.base64,
          },
        },
      }
    };
  }
}

function currentExecutionMode() {
  const selected = $("#modeSelect")?.value || state.config.mode || "review";
  if (selected === "auto") return "act";
  return state.config.actionPermissionMode || "ask";
}

function originForUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.origin;
  } catch (_err) {
    return "";
  }
}

function actionOrigin(action, tab) {
  const kind = String(action?.kind || "").toUpperCase();
  if (kind === "BROWSER_NAV") {
    const target = normalizeUrl(action.target);
    const origin = originForUrl(target);
    if (origin) return origin;
  }
  return originForUrl(tab?.url || "");
}

// Phase 1.2 — PermissionManager taxonomy. Eight named types per the
// Claude-for-Chrome reference (claude.com/chrome), two durations. This
// module labels the existing approvedOrigins + permissionDecision flow
// rather than replacing it — the binary "is this origin allowed for this
// action" check still gates dispatch, and "always" still maps to
// state.config.approvedOrigins. The taxonomy gives audit logs and
// reportAction() a stable vocabulary that maps to the spec.
const PermissionType = Object.freeze({
  NAVIGATE: "NAVIGATE",
  READ_PAGE_CONTENT: "READ_PAGE_CONTENT",
  CLICK: "CLICK",
  TYPE: "TYPE",
  UPLOAD_IMAGE: "UPLOAD_IMAGE",
  PLAN_APPROVAL: "PLAN_APPROVAL",
  REMOTE_MCP: "REMOTE_MCP",
  DOMAIN_TRANSITION: "DOMAIN_TRANSITION",
  // Phase 5.1 — sandboxed JS execution via CDP Runtime.evaluate.
  // Treated as a distinct type because the blast radius is broader than
  // CLICK or TYPE: a single BROWSER_JS call can read+write arbitrary
  // DOM state, call fetch, attach event listeners, etc.
  EXEC_JAVASCRIPT: "EXEC_JAVASCRIPT",
});

const PermissionDuration = Object.freeze({
  ONCE: "once",     // tied to action.id; auto-revoked when the action completes
  ALWAYS: "always", // persistent per-domain via state.config.approvedOrigins
});

const PermissionManager = {
  // Maps an action to one of the 8 permission types. Returns null for
  // actions that don't carry a permission gate (e.g. backend-only directives
  // that never reach the extension).
  typeFor(action, contextOrigin = "") {
    const kind = String(action?.kind || "").toUpperCase();
    const target = String(action?.target || "").toLowerCase();
    if (kind === "REMOTE_MCP") return PermissionType.REMOTE_MCP;
    if (kind === "BROWSER_NAV" || kind === "BROWSER_TAB_CREATE") {
      // DOMAIN_TRANSITION when the target is an explicit URL whose origin
      // differs from the current tab. Bareword / relative-path targets stay
      // NAVIGATE — the backend's normalizeUrl already injects the scheme,
      // and being conservative here avoids false DOMAIN_TRANSITION labels
      // on intra-origin navigations. TAB_CREATE follows the same rule
      // even though it doesn't unload the current tab.
      if (/^https?:/i.test(target)) {
        try {
          const targetOrigin = new URL(target).origin;
          if (contextOrigin && targetOrigin && contextOrigin !== targetOrigin) {
            return PermissionType.DOMAIN_TRANSITION;
          }
        } catch (_err) { /* malformed → still NAVIGATE */ }
      }
      return PermissionType.NAVIGATE;
    }
    if (kind === "BROWSER_READ_PAGE" || kind === "BROWSER_OBSERVE" || kind === "BROWSER_READ") {
      return PermissionType.READ_PAGE_CONTENT;
    }
    if (kind === "BROWSER_CONSOLE" || kind === "BROWSER_NETWORK") {
      return PermissionType.READ_PAGE_CONTENT;
    }
    if (kind === "BROWSER_JS") return PermissionType.EXEC_JAVASCRIPT;
    if (kind === "BROWSER_RESIZE_WINDOW") return PermissionType.NAVIGATE;
    if (kind === "BROWSER_CLICK" || kind === "BROWSER_DOUBLE_CLICK" ||
        kind === "BROWSER_SCROLL" || kind === "BROWSER_DRIVE_INSPECT_FOLDER" ||
        kind === "BROWSER_CDP_MOUSE") {
      return PermissionType.CLICK;
    }
    if (kind === "BROWSER_CDP_KEY") {
      return PermissionType.TYPE;
    }
    if (kind === "BROWSER_FILL") {
      // File upload uses CDP DOM.setFileInputFiles; UPLOAD_IMAGE per spec
      // even when the file isn't strictly an image (the type covers any
      // local-file payload landing in the page).
      if (action?.file_payload || /file:\/\//.test(target)) return PermissionType.UPLOAD_IMAGE;
      return PermissionType.TYPE;
    }
    if (kind === "BROWSER_SCREENSHOT") return PermissionType.READ_PAGE_CONTENT;
    return null;
  },

  // True when the action is permitted to run without an additional approval
  // gate. Used by audit + diagnostic surfaces; the dispatch path itself
  // still uses the existing classifyBrowserAction + approvedOrigins logic.
  isGranted(action, origin = "") {
    if (!origin) return false;
    return Array.isArray(state.config.approvedOrigins) &&
           state.config.approvedOrigins.includes(origin);
  },

  // Map an existing "permissionDecision" string to a duration.
  durationFor(decision) {
    if (decision === "always_allow_site") return PermissionDuration.ALWAYS;
    if (decision === "auto") return PermissionDuration.ONCE;
    if (decision === "allow_once") return PermissionDuration.ONCE;
    return PermissionDuration.ONCE;
  },

  // Build the audit envelope a reportAction() consumer can store.
  envelopeFor(action, origin, decision) {
    const type = this.typeFor(action, origin);
    return {
      permission_type: type,
      permission_duration: this.durationFor(decision),
      permission_decision: decision || "",
      origin: origin || "",
    };
  },
};

async function rememberPermission(decision, origin, action) {
  const envelope = PermissionManager.envelopeFor(action, origin, decision);
  const entry = {
    ts: new Date().toISOString(),
    decision,
    origin: origin || "",
    kind: action?.kind || "",
    target: String(action?.target || "").slice(0, 300),
    permission_type: envelope.permission_type,
    permission_duration: envelope.permission_duration,
  };
  state.config.permissionHistory = [entry, ...(state.config.permissionHistory || [])].slice(0, 100);
  await chromeSet({ permissionHistory: state.config.permissionHistory });
}

async function allowOrigin(origin, action) {
  if (!origin) return;
  if (!state.config.approvedOrigins.includes(origin)) {
    state.config.approvedOrigins = [...state.config.approvedOrigins, origin].sort();
    await chromeSet({ approvedOrigins: state.config.approvedOrigins });
  }
  await rememberPermission("always_allow_site", origin, action);
}

// Phase 1.1 domain-classifier wiring. The backend at /extension/classify_domain
// returns one of four categories per host; we cache the result per host with a
// TTL hint from the response. Categories 1/2 are blocked at classifyBrowserAction
// time; category 3 forces per-action confirm even on otherwise-safe kinds.
const DOMAIN_CLASS_CACHE_TTL_MS = 5 * 60 * 1000;

function hostFromOriginOrUrl(input) {
  const raw = String(input || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw.startsWith("http") ? raw : `https://${raw}`);
    return (url.hostname || "").toLowerCase();
  } catch (_err) {
    return raw.toLowerCase().split("/")[0].split(":")[0];
  }
}

async function classifyOrigin(originOrUrl) {
  const host = hostFromOriginOrUrl(originOrUrl);
  if (!host) return { category: 0, reason: "no host", matched: "", host: "" };
  const now = Date.now();
  const cached = state.domainClassCache.get(host);
  if (cached && (now - cached.ts) < DOMAIN_CLASS_CACHE_TTL_MS) return cached.result;
  try {
    const result = await backendFetch("/extension/classify_domain", {
      method: "POST",
      body: { domain: host },
      timeoutMs: 4000
    });
    if (result && typeof result.category === "number") {
      state.domainClassCache.set(host, { result, ts: now });
      return result;
    }
  } catch (_err) {
    // Fail open. Classifier unreachable means we don't add classifier-based
    // blocks; the local blockedOrigins list + category regex below still apply.
  }
  return { category: 0, reason: "classifier unreachable", matched: "", host };
}

async function ensureOriginsClassified(origins = []) {
  const hosts = [...new Set(origins.filter(Boolean).map(hostFromOriginOrUrl).filter(Boolean))];
  if (!hosts.length) return;
  await Promise.all(hosts.map((host) => classifyOrigin(host)));
}

function originDomainClass(originOrUrl) {
  const host = hostFromOriginOrUrl(originOrUrl);
  if (!host) return null;
  const cached = state.domainClassCache.get(host);
  if (!cached) return null;
  if ((Date.now() - cached.ts) > DOMAIN_CLASS_CACHE_TTL_MS) return null;
  return cached.result;
}

// Phase 1.3 — URL verification mid-action. Mutating actions check that the
// active tab's host still matches the host the model read. Reads + NAV are
// exempt (reads ARE the snapshot; NAV is the intentional shift).
const MUTATING_BROWSER_KINDS = new Set([
  "BROWSER_CLICK",
  "BROWSER_FILL",
  "BROWSER_DOUBLE_CLICK",
  "BROWSER_SCROLL",
  "BROWSER_DRIVE_INSPECT_FOLDER",
  "BROWSER_CDP_MOUSE",
  "BROWSER_CDP_KEY",
  "BROWSER_JS",
]);

function recordObservedOrigin(tab) {
  if (!tab?.url) return;
  const host = hostFromOriginOrUrl(tab.url);
  if (!host) return;
  state.lastObservedOrigin = { host, url: tab.url, ts: Date.now() };
}

function verifyTabOriginUnchanged(tab, kind) {
  const upper = String(kind || "").toUpperCase();
  if (!MUTATING_BROWSER_KINDS.has(upper)) return { ok: true };
  const snapshot = state.lastObservedOrigin;
  if (!snapshot) return { ok: true };
  const currentHost = hostFromOriginOrUrl(tab?.url || "");
  if (!currentHost) return { ok: true };
  if (currentHost === snapshot.host) return { ok: true };
  return {
    ok: false,
    reason: `domain shifted mid-action: observed ${snapshot.host}, tab now on ${currentHost}`,
    observed_host: snapshot.host,
    current_host: currentHost,
  };
}

function originBlockedReason(originOrUrl = "") {
  const raw = String(originOrUrl || "");
  const origin = originForUrl(raw) || raw;
  // Phase 1.1 — classifier verdict wins over local-only signals when present.
  const verdict = originDomainClass(raw);
  if (verdict && verdict.category === 1) {
    return "domain_classifier:cat1_malicious";
  }
  if (verdict && verdict.category === 2) {
    return "domain_classifier:cat2_sensitive_auth";
  }
  const blocked = state.config.blockedOrigins || [];
  if (blocked.some((entry) => origin === entry || origin.endsWith(`.${String(entry).replace(/^https?:\/\//, "")}`))) {
    return "site_blocklist:user_blocked_site";
  }
  const host = (() => {
    try { return new URL(origin.startsWith("http") ? origin : `https://${origin}`).hostname; }
    catch (_err) { return raw; }
  })().toLowerCase();
  const categoryPatterns = [
    ["financial", /\b(bank|banking|paypal|stripe|venmo|cashapp|coinbase|robinhood|fidelity|vanguard|schwab|chase|wellsfargo|capitalone|amex|visa|mastercard)\b/i],
    ["adult", /\b(adult|porn|xxx|onlyfans)\b/i],
    ["piracy", /\b(torrent|pirate|crack|warez)\b/i],
  ];
  const matched = categoryPatterns.find(([, pattern]) => pattern.test(host));
  return matched ? `site_blocklist:high_risk_${matched[0]}` : "";
}

async function sendToContent(tab, action, timings = {}) {
  const ready = await ensureContentScript(tab, timings);
  if (!ready) throw new Error("cannot access this tab");
  const bridgedAction = await attachLocalFilePayload(action);

  // ── Iframe bridge routing ──────────────────────────────────────────────────
  // Targets prefixed with "iframe::" (e.g. "iframe::p_007") originate from
  // cross-origin child frames. Route through the top-frame content script's
  // SENSEI_IFRAME_DISPATCH handler, which posts into each iframe; the relay in
  // each frame forwards to the service worker's SENSEI_IFRAME_ACTION handler.
  const rawTarget = String(bridgedAction?.target || "");
  if (rawTarget.startsWith("iframe::")) {
    const iframeRef = rawTarget.slice("iframe::".length).trim();
    const iframeAction = { ...bridgedAction, target: iframeRef };
    return chrome.tabs.sendMessage(tab.id, {
      type: "SENSEI_IFRAME_DISPATCH",
      action: iframeAction,
      actionId: crypto.randomUUID(),
    });
  }
  // ──────────────────────────────────────────────────────────────────────────

  return chrome.tabs.sendMessage(tab.id, { type: "SENSEI_EXECUTE_ACTION", action: bridgedAction });
}

function actionWantsSemanticFind(action) {
  const extras = action?.extras || {};
  if (extras.semantic === true || extras.semantic_find === true) return true;
  const raw = String(action?.target || "");
  if (/^\s*\{/.test(raw)) {
    try {
      const parsed = JSON.parse(raw);
      return Boolean(parsed.semantic || parsed.semantic_find);
    } catch (_err) {
      return false;
    }
  }
  return /\bsemantic\s*:\s*true\b/i.test(raw);
}

function semanticFindQuery(action) {
  const raw = String(action?.target || "").trim();
  if (/^\s*\{/.test(raw)) {
    try {
      const parsed = JSON.parse(raw);
      return String(parsed.query || parsed.text || parsed.target || "").trim();
    } catch (_err) {
      return raw;
    }
  }
  return raw.replace(/\bsemantic\s*:\s*true\b/ig, "").trim();
}

async function semanticFind(tab, action, timings = {}) {
  const ax = await timed(timings, "semantic_find_ax", () => accessibilityTreeContext(tab, timings));
  const snapshot = ax?.tree || ax?.semantic_tree?.snapshot || ax?.semantic_tree || {};
  const query = semanticFindQuery(action);
  const data = await timed(timings, "semantic_find", () => backendFetch("/tool/find", {
    method: "POST",
    body: { query, ax_tree: snapshot },
    timeoutMs: 20000,
  }));
  return {
    ok: Boolean(data?.ok),
    query,
    count: Array.isArray(data?.matches) ? data.matches.length : 0,
    matches: Array.isArray(data?.matches) ? data.matches : [],
    semantic: true,
    text: Array.isArray(data?.matches)
      ? data.matches.map((m) => `${m.ref || m.selector || "match"} ${m.role || ""} "${m.name || ""}"`).join("\n")
      : "",
  };
}

function parseRemoteMcpTarget(action) {
  const raw = String(action?.target || "").trim();
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") return parsed;
    } catch (_err) {
      // Fall through to shorthand.
    }
  }
  const parts = raw.split(/\s+/);
  return {
    server: parts[0] || "",
    method: parts[1] || "tools/list",
    params: {},
  };
}

function configuredMcpServer(serverRef, origin = "") {
  const servers = Array.isArray(state.config.mcpServers) ? state.config.mcpServers : [];
  const activeHost = hostFromOriginOrUrl(origin);
  return servers.find((srv) => {
    if (!srv || !srv.url) return false;
    if (serverRef && serverRef !== srv.url && serverRef !== srv.name) return false;
    const scopes = Array.isArray(srv.scopes) ? srv.scopes.filter(Boolean) : [];
    if (!scopes.length || !activeHost) return true;
    return scopes.some((scope) => activeHost === hostFromOriginOrUrl(scope));
  }) || null;
}

async function dispatchRemoteMcpAction(action, origin = "") {
  const req = parseRemoteMcpTarget(action);
  const method = String(req.method || "tools/list");
  if (!["tools/list", "tools/call"].includes(method)) {
    return { ok: false, error: "remote MCP method must be tools/list or tools/call" };
  }
  const server = configuredMcpServer(String(req.server || ""), origin);
  if (!server) {
    return { ok: false, error: "no configured MCP server for this origin" };
  }
  const endpoint = String(server.url || "").replace(/\/+$/, "");
  const body = {
    jsonrpc: "2.0",
    id: crypto.randomUUID(),
    method,
    params: req.params && typeof req.params === "object" ? req.params : {},
  };
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed = {};
  try { parsed = JSON.parse(text); } catch (_err) { parsed = { text }; }
  if (!res.ok || parsed.error) {
    return {
      ok: false,
      error: parsed.error?.message || `${res.status} ${res.statusText}`,
      response: parsed,
      server: server.name || server.url,
      method,
    };
  }
  return {
    ok: true,
    server: server.name || server.url,
    method,
    response: parsed.result !== undefined ? parsed.result : parsed,
  };
}

function truncateDataUrlForAudit(dataUrl) {
  const text = String(dataUrl || "");
  return text.length > 200 ? text.slice(0, 200) : text;
}

function renderScreenshot(row, dataUrl) {
  if (!row || !dataUrl) return;
  const existing = row.querySelector(".screenshot-preview");
  if (existing) existing.remove();
  const img = document.createElement("img");
  img.className = "screenshot-preview";
  img.alt = "Captured browser tab screenshot";
  img.src = dataUrl;
  row.appendChild(img);
}

function reportAction(action, verdict, result, finalState = {}) {
  // LAYER 3 DEAD-MAN'S SWITCH (SENSEI HARDENING 2026-05-20): force failure
  // when BROWSER_FILL post-condition shows expected !== observed. Even if
  // Layer 1 (content_script) AND Layer 2 (dispatch site) both leak a false
  // success through, the observed/expected mismatch is ground truth and
  // wins here. The audit log will never lie about a BROWSER_FILL.
  if (String(action?.kind || "").toUpperCase() === "BROWSER_FILL"
      && finalState
      && typeof finalState === "object"
      && "expected" in finalState
      && "observed" in finalState
      && finalState.expected !== finalState.observed) {
    result = "failure";
    finalState = {
      ...finalState,
      ok: false,
      result: "failure",
      reason: finalState.reason || "value_did_not_persist",
    };
  }
  // session_id MUST be on every action_result the bridge sees. For actions
  // pulled from the MCP queue we tag them with their origin session at pull
  // time (see pollMcpQueue). For panel-driven /chat actions we fall back to
  // the panel's own session id. Without this the audit log shows sid=<empty>
  // and downstream tooling can't correlate actions to sessions.
  const session_id = action?._session_id
    || action?.session_id
    || state.config.sessionId
    || "panel-default";
  state.auditQueue.push({
    action_id: action.id,
    session_id,
    action,
    verdict,
    result,
    final_state: finalState,
    gated_by: action?.gated_by || action?.classification?.gated_by || null
  });
  flushAuditQueue();
}

async function flushAuditQueue() {
  if (state.auditFlushing) return;
  state.auditFlushing = true;
  try {
    while (state.auditQueue.length) {
      const payload = state.auditQueue[0];
      try {
        await backendFetch("/extension/action_result", {
          method: "POST",
          body: payload,
          timeoutMs: REQUEST_TIMEOUTS.audit
        });
      } catch (err) {
        appendError(`Action audit failed: ${err.message}`);
      } finally {
        state.auditQueue.shift();
      }
    }
  } finally {
    state.auditFlushing = false;
  }
}

function setActionStatus(row, text) {
  const status = row.querySelector(".status");
  if (status) status.textContent = text;
}

function classifyBrowserAction(action, origin = "") {
  const kind = String(action?.kind || "").toUpperCase();
  const target = String(action?.target || "").toLowerCase();
  if (kind === "REMOTE_MCP") {
    return { safe: false, requires_confirm: true, gated_by: "permission:remote_mcp" };
  }
  if (kind === "BROWSER_UPLOAD_FILE") {
    const upload = parseUploadTarget(action?.target);
    const uploadGate = assertUploadAllowlist(upload.path);
    if (!upload.selector || !upload.path || !uploadGate.valid) {
      return {
        safe: false,
        requires_confirm: true,
        blocked: true,
        gated_by: "local_upload_allowlist:path_outside_upload_allowlist",
        reason: uploadGate.reason || "path outside upload allowlist",
      };
    }
  }
  const checkUrl = (kind === "BROWSER_NAV" || kind === "BROWSER_TAB_CREATE")
    ? target : origin;
  const blockedReason = originBlockedReason(checkUrl);
  if (blockedReason) {
    return {
      safe: false,
      requires_confirm: true,
      blocked: true,
      gated_by: blockedReason,
      reason: `Blocked by pilot safety policy: ${blockedReason.split(":").pop().replace(/_/g, " ")}`
    };
  }

  // Phase 1.1 — category 3 ("force confirm every action") wins over an
  // otherwise-safe verdict but does NOT block. Re-checked after the existing
  // heuristics so explicit purchase/delete/auth gating still surfaces its
  // own gated_by string.
  const verdict = originDomainClass(checkUrl);
  const cat3 = verdict && verdict.category === 3 ? verdict : null;
  const applyCat3 = (result) => {
    if (!cat3 || result.blocked || result.requires_confirm) return result;
    return {
      ...result,
      safe: false,
      requires_confirm: true,
      gated_by: result.gated_by || "domain_classifier:cat3_force_confirm",
      reason: result.reason || `High-friction domain — ${cat3.reason || cat3.matched}`
    };
  };

  const PURCHASE_RE = /\b(buy|purchase|pay|checkout|order|subscribe|add to cart)\b/i;
  const DELETE_RE = /\b(delete|remove|destroy|uninstall|erase|wipe|cancel.*(account|subscription))\b/i;
  const AUTH_RE = /\b(sign[-_\s]*up|sign[-_\s]*in|log[-_\s]*in|log[-_\s]*out|register|authorize|grant\s*access|oauth|api\s*key)\b/i;
  // Anthropic-spec hard-limit list (https://support.anthropic.com/en/collections/13228104-claude-for-chrome):
  // never enter passwords, SSNs, financial account numbers, passport numbers,
  // or medical data. Existing terms (password / ssn / credit card / etc.)
  // covered the first three; passport + medical-class terms + driver license
  // + date of birth close the documented gap.
  const SENSITIVE_RE = /\b(password|ssn|social.*security|credit.*card|cvv|cvc|api.*key|bank.*account|routing.*number|passport|passport.*number|medical(.*record)?|diagnosis|health.*insurance|patient.*id|driver.*license|driver.*licence|date.*of.*birth|dob)\b/i;
  const PASSWORD_SEL = /type=["']?password["']?|name=["']?(password|pwd|passwd)["']?/i;

  if (kind === "BROWSER_FILL" && (PASSWORD_SEL.test(target) || SENSITIVE_RE.test(target))) {
    return { safe: false, requires_confirm: true, gated_by: "irreversible_heuristic:sensitive_fill" };
  }
  if (kind === "BROWSER_CLICK") {
    if (PURCHASE_RE.test(target)) return { safe: false, requires_confirm: true, gated_by: "irreversible_heuristic:purchase" };
    if (DELETE_RE.test(target)) return { safe: false, requires_confirm: true, gated_by: "irreversible_heuristic:delete" };
    if (AUTH_RE.test(target)) return { safe: false, requires_confirm: true, gated_by: "irreversible_heuristic:auth" };
  }
  if (kind === "BROWSER_NAV") {
    if (PURCHASE_RE.test(target) || /\/(checkout|cart|pay|order)\b/i.test(target)) {
      return { safe: false, requires_confirm: true, gated_by: "irreversible_heuristic:purchase_url" };
    }
  }
  if (READONLY_BROWSER_KINDS.has(kind)) {
    return applyCat3({ safe: true, requires_confirm: false, gated_by: null });
  }
  return applyCat3({ safe: true, requires_confirm: false, gated_by: null });
}

function gateLabel(gatedBy) {
  if (!gatedBy) return "";
  const reason = String(gatedBy).split(":").pop().replace(/_/g, " ");
  return `gated_by: ${reason}`;
}

function shouldAutoRunAction(action, origin = "") {
  // Phase 1 commit 1.3: respect the backend's mode-aware status taxonomy.
  // The dispatcher (not the model, not the client) decides safe vs sensitive.
  // Previously this auto-ran BROWSER_READ/SCREENSHOT regardless of mode —
  // Plan-mode contract violation. Closed.
  const status = String(action?.status || "").toLowerCase();
  const kind = String(action?.kind || "").toUpperCase();

  // Backend status takes precedence. waiting_for_approval / pending_approval
  // (legacy name) / blocked all mean "do NOT auto-run."
  if (status === "waiting_for_approval" || status === "pending_approval" || status === "blocked") {
    return false;
  }

  // Non-browser actions never dispatch from the side panel. In Auto the
  // backend already dispatched them server-side; in Review they go through
  // /extension/approve_action (commit 1.4).
  if (kind === "REMOTE_MCP") return false;
  if (!kind.startsWith("BROWSER_")) return false;

  // Plan + Review: never auto-run, regardless of action kind. Closes the
  // BROWSER_READ/SCREENSHOT auto-run-in-Plan loophole at the old line 464.
  const mode = ($("#modeSelect")?.value || state.config.mode || "review").toLowerCase();
  if (mode !== "auto") return false;

  // Auto: dispatcher already marked sensitive ones as gated_by; respect it.
  if (action?.classification?.requires_confirm) return false;
  if (action?.gated_by) return false;

  // Auto-mode safe BROWSER_*: approved-origin auto-run, plus read-only
  // observation/navigation-assist actions that do not mutate page data.
  if (origin && state.config.approvedOrigins.includes(origin)) return true;
  if (READONLY_BROWSER_KINDS.has(kind)) return true;
  return false;
}

async function approveAction(action, row, permissionDecision = "allow_once") {
  const kind = String(action.kind || "").toUpperCase();
  const timings = {};
  let tab = null;
  setActionStatus(row, "Running");
  row.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });

  if (kind === "REMOTE_MCP") {
    try {
      tab = await activeTab().catch(() => null);
      const origin = actionOrigin(action, tab);
      if (permissionDecision !== "auto") await rememberPermission(permissionDecision, origin, action);
      const result = await dispatchRemoteMcpAction(action, origin);
      const ok = Boolean(result?.ok);
      setActionStatus(row, ok ? "Done" : (result?.error || "Failed"));
      const finalState = {
        ...(result || {}),
        permission: permissionDecision,
        origin,
        permission_envelope: PermissionManager.envelopeFor(action, origin, permissionDecision),
      };
      // LAYER 2 (SENSEI HARDENING 2026-05-20): respect content_script's
      // explicit result field. ok==true is necessary but not sufficient —
      // if finalState.result is "failure", honor it (content_script knows
      // the post-condition truth even when ok happens to be true).
      const _derivedResult = (ok && finalState?.result !== "failure") ? "success" : "failure";
      reportAction(action, "accept", _derivedResult, finalState);
      recordLoopResult(action, "accept", _derivedResult, finalState);
    } catch (err) {
      setActionStatus(row, err.message);
      reportAction(action, "accept", "failure", { error: err.message });
      recordLoopResult(action, "accept", "failure", { error: err.message });
    }
    return;
  }

  if (!kind.startsWith("BROWSER_")) {
    setActionStatus(row, "Backend-only — switch the mode dropdown to \"Act without asking\" to dispatch");
    reportAction(action, "accept", "blocked", { reason: "non-browser action; backend dispatches in auto mode" });
    recordLoopResult(action, "accept", "blocked", { reason: "non-browser action; backend dispatches in auto mode" });
    return;
  }

  try {
    tab = await activeTab();
    if (!tab?.id) throw new Error("no active tab");
    const origin = actionOrigin(action, tab);
    const blockedReason = originBlockedReason(kind === "BROWSER_NAV" ? action.target : origin);
    if (blockedReason) {
      throw new Error(`blocked by pilot safety policy: ${blockedReason}`);
    }
    // Phase 1.3 — URL verification mid-action. Mutating kinds abort if the
    // active tab drifted off the host the model read in this round.
    const verify = verifyTabOriginUnchanged(tab, kind);
    if (!verify.ok) {
      throw new Error(`[TOOL BLOCKED: ${verify.reason}]`);
    }
    if (permissionDecision !== "auto") await rememberPermission(permissionDecision, origin, action);

    let result;
    if (kind === "BROWSER_SCREENSHOT") {
      const capture = await chrome.runtime.sendMessage({
        type: "SENSEI_CAPTURE_VISIBLE_TAB",
        windowId: tab.windowId
      });
      if (!capture?.ok || !capture?.dataUrl) {
        throw new Error(capture?.error || "screenshot capture failed; try reopening the side panel from this tab");
      }
      renderScreenshot(row, capture.dataUrl);
      result = {
        ok: true,
        b64: capture.dataUrl.replace(/^data:[^;]+;base64,/, ""),
        screenshot: "visible_tab_png",
        dataUrl_preview: truncateDataUrlForAudit(capture.dataUrl),
        context_lifetime: "one_turn"
      };
    } else if (kind === "BROWSER_NAV") {
      const url = normalizeUrl(action.target);
      await chrome.tabs.update(tab.id, { url });
      await waitForTabSettled(tab.id, 8000);
      // Phase 1.3 — the intentional navigation is the new baseline.
      try {
        const freshTab = await chrome.tabs.get(tab.id);
        recordObservedOrigin(freshTab);
      } catch (_err) { /* best-effort */ }
      result = { ok: true, navigated: url };
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_READ_PAGE" || kind === "BROWSER_OBSERVE") {
      // Phase 1.3 — reads ARE the snapshot. Update the observed-origin baseline.
      recordObservedOrigin(tab);
      result = {
        ok: true,
        page_context: await freshPageContextForContinuation(timings),
        text: "Page observed with semantic tree and current interactives."
      };
    } else if (kind === "BROWSER_DRIVE_INSPECT_FOLDER") {
      result = await sendToContent(tab, action, timings);
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_TAB_CREATE") {
      // Phase 4.2 — open a new tab in the session's tab group. URL is
      // classified just like BROWSER_NAV via originBlockedReason +
      // domain-classifier verdict (the classify gate above used `target`).
      const url = normalizeUrl(action.target);
      const newTab = await chrome.tabs.create({ url, active: false });
      if (newTab?.id) {
        await addTabToSessionGroup(newTab.id);
      }
      result = {
        ok: true,
        tab_created: { id: newTab?.id, url, windowId: newTab?.windowId,
                       group_id: state.sessionTabGroup?.groupId || null },
      };
    } else if (kind === "BROWSER_TAB_LIST") {
      // Phase 5.5 — list all open tabs across all windows. Returns id, url,
      // title, windowId, active, and groupId for each tab. Read-only.
      const tabs = await chrome.tabs.query({});
      result = {
        ok: true,
        count: tabs.length,
        tabs: tabs.map((t) => ({
          id: t.id,
          url: t.url || "",
          title: t.title || "",
          windowId: t.windowId,
          active: t.active,
          groupId: t.groupId ?? -1,
        })),
      };
    } else if (kind === "BROWSER_TAB_CLOSE") {
      // Phase 5.5 — close a tab by numeric ID. action.target is the tab ID
      // as a string or number. Never closes the side-panel's own tab.
      const targetId = parseInt(String(action.target || ""), 10);
      if (!Number.isInteger(targetId) || targetId <= 0) {
        result = { ok: false, error: "BROWSER_TAB_CLOSE: target must be a valid tab ID integer" };
      } else if (targetId === tab.id) {
        result = { ok: false, error: "BROWSER_TAB_CLOSE: cannot close the current active tab" };
      } else {
        try {
          await chrome.tabs.remove(targetId);
          result = { ok: true, closed_tab_id: targetId };
        } catch (err) {
          result = { ok: false, error: `BROWSER_TAB_CLOSE failed: ${err?.message || err}` };
        }
      }
    } else if (kind === "BROWSER_CDP_MOUSE") {
      result = await dispatchCdpMouse(tab, action, timings);
      if (result?.ok) {
        // CDP clicks can cause navigation; settle and invalidate so the next
        // action reads a fresh page_context.
        await waitForTabSettled(tab.id, 4000);
      }
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_CDP_KEY") {
      result = await dispatchCdpKey(tab, action, timings);
      if (result?.ok) {
        // Enter / Escape / shortcut keys can submit forms or trigger SPA
        // routes. Treat the same way as a CDP mouse click.
        await waitForTabSettled(tab.id, 4000);
      }
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_JS") {
      result = await dispatchBrowserJs(tab, action, timings);
      // BROWSER_JS can mutate the page, navigate, or open modals. Settle
      // before the next observation so the model sees the new state.
      if (result?.ok) await waitForTabSettled(tab.id, 4000);
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_CONSOLE") {
      result = await dispatchBrowserConsole(tab, action, timings);
    } else if (kind === "BROWSER_NETWORK") {
      result = await dispatchBrowserNetwork(tab, action, timings);
    } else if (kind === "BROWSER_RESIZE_WINDOW") {
      result = await dispatchBrowserResizeWindow(tab, action, timings);
    } else if (kind === "BROWSER_FIND") {
      const regexResult = await sendToContent(tab, action, timings);
      if (actionWantsSemanticFind(action) || !regexResult?.count) {
        const semanticResult = await semanticFind(tab, action, timings);
        result = {
          ...semanticResult,
          regex_matches: regexResult?.matches || [],
          regex_count: regexResult?.count || 0,
        };
      } else {
        result = regexResult;
      }
      if (result?.ok) recordObservedOrigin(tab);
      invalidatePageContext(tab.id);
    } else if (kind === "BROWSER_FILL") {
      const uploadAttempt = await tryDebuggerFileUpload(tab, action, timings);
      if (uploadAttempt?.ok) {
        result = uploadAttempt;
      } else if (uploadAttempt?.fallback_action) {
        result = await sendToContent(tab, uploadAttempt.fallback_action, timings);
        if (!result?.ok) result = { ...result, debugger_fallback_error: uploadAttempt.error };
      } else {
        result = await sendToContent(tab, action, timings);
      }
      if (CONTEXT_SETTLING_BROWSER_KINDS.has(kind)) await waitForTabSettled(tab.id, 4000);
      invalidatePageContext(tab.id);
    } else {
      result = await sendToContent(tab, action, timings);
      if (CONTEXT_SETTLING_BROWSER_KINDS.has(kind)) await waitForTabSettled(tab.id, 4000);
      // Phase 1.3 — BROWSER_READ and other read-only catchall observations
      // refresh the baseline. The check above already blocked mutating kinds
      // that drifted; this keeps the snapshot honest for the next mutating
      // call in the same round.
      if (READONLY_BROWSER_KINDS.has(kind) && result?.ok) recordObservedOrigin(tab);
      invalidatePageContext(tab.id);
    }

    const ok = Boolean(result?.ok);
    const suffix = timings.inject ? ` (${timings.inject} ms inject)` : "";
    setActionStatus(row, ok ? `Done${suffix}` : (result?.error || "Failed"));
    const observedTabUrl = await readObservedTabUrl(tab);
    const finalState = {
      ...(result || {}),
      permission: permissionDecision,
      origin,
      observed_tab_url: observedTabUrl,
      // Phase 1.2 — typed permission envelope alongside the legacy decision
      // string. Backend / audit can read either field.
      permission_envelope: PermissionManager.envelopeFor(action, origin, permissionDecision),
    };
    // LAYER 2 (SENSEI HARDENING 2026-05-20): see comment on the sibling
    // dispatch site (~line 2011). Honor content_script's explicit failure.
    const _derivedResult = (ok && finalState?.result !== "failure") ? "success" : "failure";
    reportAction(action, "accept", _derivedResult, finalState);
    recordLoopResult(action, "accept", _derivedResult, finalState);
  } catch (err) {
    const observedTabUrl = await readObservedTabUrl(tab);
    setActionStatus(row, err.message);
    reportAction(action, "accept", "failure", { error: err.message, observed_tab_url: observedTabUrl });
    recordLoopResult(action, "accept", "failure", { error: err.message, observed_tab_url: observedTabUrl });
  }
}

// Ground-truth tab URL after an action settles. For BROWSER_NAV the
// chrome.tabs.update promise resolves before the actual navigation may
// complete; small settle delay covers the common case. Returns null on
// failure rather than throwing — observability shouldn't block dispatch.
async function readObservedTabUrl(tab) {
  if (!tab?.id) return null;
  try {
    await new Promise((r) => setTimeout(r, 75));
    const refreshed = await chrome.tabs.get(tab.id);
    return refreshed?.url || null;
  } catch (_err) {
    return null;
  }
}

function normalizeDriveTerm(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function parseDriveInspectTarget(action) {
  const raw = String(action?.target || "").trim();
  let parsed = {};
  if (raw.startsWith("{")) {
    try {
      parsed = JSON.parse(raw);
    } catch (_err) {
      parsed = {};
    }
  }
  const query = String(parsed.query || parsed.folder || raw || "resume").trim();
  const variants = Array.isArray(parsed.variants) ? parsed.variants : [];
  const terms = [query, ...variants].filter(Boolean).map(String);
  if (/\b(resume|cv|career)\b/i.test(normalizeDriveTerm(query)) || !terms.length) {
    terms.push("Resume", "resume", "résumé", "CV", "career");
  }
  const uniqueTerms = [];
  const seen = new Set();
  for (const term of terms) {
    const cleaned = String(term || "").trim();
    const key = normalizeDriveTerm(cleaned);
    if (!cleaned || seen.has(key)) continue;
    seen.add(key);
    uniqueTerms.push(cleaned);
  }
  return {
    query: query || uniqueTerms[0] || "resume",
    variants: uniqueTerms.length ? uniqueTerms : ["resume"],
    folderOnly: parsed.folderOnly !== false,
    maxSearches: Math.max(1, Math.min(Number(parsed.maxSearches || 5), 8)),
  };
}

async function sleepMs(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForTabSettled(tabId, timeoutMs = 10000) {
  const start = performance.now();
  while (performance.now() - start < timeoutMs) {
    try {
      const current = await chrome.tabs.get(tabId);
      if (current?.status === "complete") break;
    } catch (_err) {
      break;
    }
    await sleepMs(250);
  }
  // Google Drive is an SPA; complete fires before the result grid finishes.
  await sleepMs(1600);
}

async function driveExtract(tab, action, phase, query) {
  const target = JSON.stringify({ phase, query });
  const result = await sendToContent(tab, {
    ...action,
    kind: "BROWSER_DRIVE_INSPECT_FOLDER",
    target,
  });
  return result?.drive_state || result?.list_state || result || {};
}

function scoreDriveItem(item, terms, folderOnly) {
  if (!item || typeof item !== "object") return 0;
  const name = normalizeDriveTerm(item.name || "");
  const haystack = normalizeDriveTerm([
    item.name,
    item.aria_label,
    item.text,
    item.kind
  ].filter(Boolean).join(" "));
  let score = item.selected ? 35 : 0;
  if (item.is_folder) score += 20;
  if (folderOnly && item.kind === "file") score -= 20;
  for (const term of terms) {
    const t = normalizeDriveTerm(term);
    if (!t) continue;
    if (name === t) score += 80;
    else if (name.includes(t)) score += 45;
    else if (haystack.includes(t)) score += 25;
  }
  return score;
}

function pickDriveItem(state, opts) {
  const items = Array.isArray(state?.items) ? state.items : [];
  const scored = items
    .map((item) => ({ item, score: scoreDriveItem(item, opts.variants, opts.folderOnly) }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score);
  return scored[0]?.item || null;
}

function summarizeDriveState(state) {
  if (!state || typeof state !== "object") return "No Drive state returned.";
  if (state.empty) {
    return `Drive folder appears empty (${state.empty_reason || "empty-state text visible"}).`;
  }
  const items = Array.isArray(state.items) ? state.items : [];
  if (!items.length) return state.summary || "No visible Drive items were extracted.";
  const names = items.slice(0, 12).map((item) => {
    const suffix = item.kind && item.kind !== "unknown" ? ` (${item.kind})` : "";
    return `${item.name || item.aria_label || "unnamed"}${suffix}`;
  });
  const extra = items.length > names.length ? ` and ${items.length - names.length} more` : "";
  return `Drive items: ${names.join("; ")}${extra}.`;
}

async function openDriveItem(tab, item) {
  if (item?.href && /^https:\/\/drive\.google\.com\//i.test(item.href)) {
    await chrome.tabs.update(tab.id, { url: item.href });
    invalidatePageContext(tab.id);
    return { method: "href", href: item.href };
  }
  if (!item?.selector) throw new Error("matching Drive item has no selector");
  const result = await sendToContent(tab, {
    kind: "BROWSER_DOUBLE_CLICK",
    target: item.selector,
  });
  if (!result?.ok) throw new Error(result?.error || "Drive item open failed");
  invalidatePageContext(tab.id);
  return { method: "double_click", selector: item.selector };
}

async function inspectDriveFolderWorkflow(tab, action) {
  const opts = parseDriveInspectTarget(action);
  const searchStates = [];
  let lastUrl = tab?.url || "";

  for (const variant of opts.variants.slice(0, opts.maxSearches)) {
    const searchUrl = `https://drive.google.com/drive/search?q=${encodeURIComponent(variant)}`;
    await chrome.tabs.update(tab.id, { url: searchUrl });
    invalidatePageContext(tab.id);
    await waitForTabSettled(tab.id);
    tab = await chrome.tabs.get(tab.id);
    lastUrl = tab?.url || searchUrl;
    const searchState = await driveExtract(tab, action, "search", variant);
    searchStates.push({
      variant,
      url: searchState.url || lastUrl,
      summary: summarizeDriveState(searchState),
      empty: Boolean(searchState.empty),
      items: Array.isArray(searchState.items) ? searchState.items.slice(0, 20) : [],
    });
    const match = pickDriveItem(searchState, opts);
    if (!match) continue;

    const openResult = await openDriveItem(tab, match);
    await waitForTabSettled(tab.id);
    tab = await chrome.tabs.get(tab.id);
    const folderState = await driveExtract(tab, action, "folder", variant);
    const observedUrl = await readObservedTabUrl(tab);
    const summary = summarizeDriveState(folderState);
    return {
      ok: true,
      drive_workflow: "folder_inspected",
      query: opts.query,
      matched_variant: variant,
      found_item: match,
      open_result: openResult,
      observed_tab_url: observedUrl,
      search_states: searchStates,
      folder_state: folderState,
      text: summary,
    };
  }

  return {
    ok: true,
    drive_workflow: "no_matching_folder",
    query: opts.query,
    observed_tab_url: lastUrl,
    search_states: searchStates,
    text: `No matching Drive folder was extracted after searching: ${opts.variants.slice(0, opts.maxSearches).join(", ")}.`,
  };
}

async function rejectAction(action, row) {
  row.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
  setActionStatus(row, "Rejected");
  const tab = await activeTab().catch(() => null);
  await rememberPermission("decline", actionOrigin(action, tab), action);
  reportAction(action, "reject", "blocked", {});
  recordLoopResult(action, "reject", "blocked", {});
}

async function renderActions(actions = [], blockedActions = []) {
  const dock = $("#actionDock");
  const list = $("#actionList");
  list.textContent = "";
  const tab = await activeTab().catch(() => null);

  // Phase 1.1 — pre-warm the domain classifier for every origin we're about
  // to classify (active-tab origin + any BROWSER_NAV targets in this round).
  // Synchronous classifyBrowserAction below reads from state.domainClassCache;
  // failing to pre-warm just means classifier verdicts won't apply this round
  // (we still fall back to the local blockedOrigins + regex categories).
  try {
    const candidateOrigins = [];
    if (tab) candidateOrigins.push(actionOrigin({}, tab));
    for (const action of actions) {
      candidateOrigins.push(actionOrigin(action, tab));
      if (String(action?.kind || "").toUpperCase() === "BROWSER_NAV") {
        candidateOrigins.push(String(action?.target || ""));
      }
    }
    await ensureOriginsClassified(candidateOrigins);
  } catch (_err) {
    // Pre-warm is best-effort; never block the render.
  }

  const all = [
    ...actions.map((action) => {
      const origin = actionOrigin(action, tab);
      const classification = classifyBrowserAction(action, origin);
      return {
        ...action,
        blocked: Boolean(classification.blocked),
        reason: classification.reason || action.reason,
        classification,
        gated_by: classification.gated_by
      };
    }),
    ...blockedActions.map((action) => ({ ...action, blocked: true }))
  ];

  dock.hidden = all.length === 0;
  if (!all.length) return;

  // Mode-aware rendering. Plan mode = inert preview cards (no buttons,
  // "Plan only" footer). Review / Auto = current behavior, with backend
  // status taxonomy already honored by shouldAutoRunAction.
  const currentMode = ($("#modeSelect")?.value || state.config.mode || "review").toLowerCase();

  // Anthropic-spec "Ask before acting" UI: when the model emits 3+
  // pending browser actions in one round AND none of them auto-flow
  // yet (no Always-allow on the origin, none read-only), surface a
  // single "Approve All" header card at the top of the dock instead
  // of forcing the user to approve 11 cards one at a time. This
  // matches "Review Claude's approach once, then let it run" without
  // depending on whether the model emitted a literal <PLAN>…</PLAN>
  // block in its reply text (the cloud lane emits the block; the
  // local 7B doesn't reliably). The actions are the same data either
  // way — this is render, not a workaround.
  const tabOrigin = tab ? actionOrigin(all[0] || {}, tab) : "";
  const pendingActions = all.filter((action) => {
    if (action.blocked) return false;
    const isPlanned = String(action?.status || "").toLowerCase() === "planned";
    const isPlanInert = currentMode === "plan" && isPlanned;
    if (isPlanInert) return false;
    const o = actionOrigin(action, tab);
    if (shouldAutoRunAction(action, o)) return false;
    return true;
  });
  const showApproveAll = currentMode !== "plan" && pendingActions.length >= 3;

  // Row lookup map for the Approve-All click handler — keyed by the
  // action object identity, so we don't depend on stable action.id
  // being present on every dispatcher version.
  const _actionRows = new Map();

  if (showApproveAll) {
    const planCard = document.createElement("section");
    planCard.className = "action-item plan-card";
    const heading = document.createElement("div");
    heading.className = "action-main";
    const headLabel = document.createElement("span");
    headLabel.className = "kind";
    headLabel.textContent = "PLAN";
    const headSummary = document.createElement("span");
    headSummary.className = "target";
    headSummary.textContent =
      `${pendingActions.length} actions on ${tabOrigin || "this page"} — review and approve once`;
    heading.append(headLabel, headSummary);

    const stepsList = document.createElement("ol");
    stepsList.className = "plan-steps";
    pendingActions.forEach((action) => {
      const li = document.createElement("li");
      const k = String(action.kind || "").replace(/^BROWSER_/, "").toLowerCase();
      const t = String(action.target || "").slice(0, 100);
      li.textContent = `${k}: ${t}`;
      stepsList.appendChild(li);
    });

    const buttons = document.createElement("div");
    buttons.className = "action-buttons";
    const approveAll = document.createElement("button");
    approveAll.className = "primary";
    approveAll.type = "button";
    approveAll.textContent = "Approve all";
    approveAll.addEventListener("click", async () => {
      buttons.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
      // Remember the origin once so subsequent actions on this page
      // flow through auto-run on future rounds too (Phase 4 semantics).
      const origin = tabOrigin || actionOrigin(pendingActions[0], tab);
      if (origin) {
        await allowOrigin(origin, pendingActions[0]).catch(() => null);
      }
      for (const action of pendingActions) {
        const row = _actionRows.get(action);
        if (row) await approveAction(action, row, "always_allow_site");
      }
    });
    buttons.appendChild(approveAll);

    const declineAll = document.createElement("button");
    declineAll.className = "reject";
    declineAll.type = "button";
    declineAll.textContent = "Decline all";
    declineAll.addEventListener("click", () => {
      buttons.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
      pendingActions.forEach((action) => {
        const row = _actionRows.get(action);
        if (row) rejectAction(action, row);
      });
    });
    buttons.appendChild(declineAll);

    heading.appendChild(buttons);
    planCard.append(heading, stepsList);
    list.appendChild(planCard);
  }

  for (const action of all) {
    const row = document.createElement("section");
    row.className = "action-item";
    const origin = actionOrigin(action, tab);
    const backendStatus = String(action?.status || "").toLowerCase();
    const isPlanned = backendStatus === "planned";
    const isPlanInert = currentMode === "plan" && isPlanned && !action.blocked;
    const autoRun = !action.blocked && !isPlanInert && shouldAutoRunAction(action, origin);

    const main = document.createElement("div");
    main.className = "action-main";

    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = action.kind || "ACTION";

    const buttons = document.createElement("div");
    buttons.className = "action-buttons";

    // Plan-mode preview cards get no buttons; everything else with a
    // pending status gets Allow/Decline.
    //
    // Button-order rule: in "Act without asking" mode (auto), make
    // "Always allow site" the PRIMARY (filled-accent) button so the
    // path to the spec's auto-flow UX is obvious — observed 2026-05-14:
    // Elijah set Auto, hit Google Drive, saw 11 "Action ready for
    // review" cards in a row because drive.google.com wasn't yet on
    // approvedOrigins. The button was always there but ranked
    // secondary, easy to miss. In Review/Plan modes "Allow once" stays
    // primary — those modes EXPECT per-action gating, so promoting
    // "Always allow site" would push users out of their chosen mode.
    if (!action.blocked && !autoRun && !isPlanInert) {
      const canAlways = origin && !action.classification?.requires_confirm;
      const promoteAlways = canAlways && currentMode === "auto";

      const approve = document.createElement("button");
      approve.className = promoteAlways ? "secondary" : "primary";
      approve.type = "button";
      approve.textContent = "Allow once";
      approve.addEventListener("click", () => approveAction(action, row, "allow_once"));

      let always = null;
      if (canAlways) {
        always = document.createElement("button");
        always.className = promoteAlways ? "primary" : "secondary";
        always.type = "button";
        always.textContent = promoteAlways
          ? `Always allow ${origin.replace(/^https?:\/\//, "")}`
          : "Always allow site";
        always.addEventListener("click", async () => {
          await allowOrigin(origin, action);
          approveAction(action, row, "always_allow_site");
        });
      }

      // Order: primary button first. In Auto, that's "Always allow…";
      // in Review/Plan, "Allow once".
      if (promoteAlways) {
        buttons.appendChild(always);
        buttons.appendChild(approve);
      } else {
        buttons.appendChild(approve);
        if (always) buttons.appendChild(always);
      }

      const reject = document.createElement("button");
      reject.className = "reject";
      reject.type = "button";
      reject.textContent = "Decline";
      reject.addEventListener("click", () => rejectAction(action, row));
      buttons.appendChild(reject);
    }

    main.append(kind, buttons);

    const target = document.createElement("div");
    target.className = "target";
    target.textContent = action.target || action.reason || "";

    const gatedBy = action.gated_by || action.classification?.gated_by || null;
    const policy = document.createElement("div");
    policy.className = "policy-marker";
    policy.hidden = !gatedBy;
    policy.textContent = gateLabel(gatedBy);

    const status = document.createElement("div");
    status.className = "status";
    if (action.blocked) {
      status.textContent = action.reason || "Blocked";
    } else if (isPlanInert) {
      status.textContent = "Plan only — preview, not executed";
    } else if (autoRun) {
      status.textContent = "Queued";
    } else {
      status.textContent = "Waiting for permission";
    }

    row.append(main, target, policy, status);
    list.appendChild(row);
    _actionRows.set(action, row);

    if (autoRun) {
      setTimeout(() => approveAction(action, row, "auto"), 0);
    }
  }
}

// Phase 6 — Quick Mode parser + executor + screenshot-feedback loop.
// The model emits one single-letter command per reply terminated by the
// literal token `<<END>>`. We parse, run, capture a screenshot, and send the
// next /chat round with the screenshot as page_context — up to 8 rounds.
const QUICK_MODE_MAX_ROUNDS = 8;

function parseQuickCommand(reply) {
  if (!reply) return null;
  // Drop everything from the literal token onward; first non-empty line wins.
  const text = String(reply).split("<<END>>")[0].trim();
  if (!text) return null;
  const line = text.split(/\r?\n/).find((l) => l.trim()) || "";
  const trimmed = line.trim();
  if (!trimmed) return null;
  if (/^DONE\s*:/i.test(trimmed)) {
    return { op: "done", summary: trimmed.replace(/^DONE\s*:\s*/i, "") };
  }
  const m = trimmed.match(/^(C|T|K|N|J|W|ST)\b\s*([\s\S]*)$/i);
  if (!m) return null;
  const op = m[1].toUpperCase();
  const rest = (m[2] || "").trim();
  if (op === "C") {
    const parts = rest.split(/\s+/);
    const x = Number(parts[0]); const y = Number(parts[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { op: "click", x, y };
  }
  if (op === "T") return { op: "type", text: rest };
  if (op === "K") return { op: "key", key: rest.split(/\s+/)[0] || "" };
  if (op === "N") return { op: "nav", url: rest };
  if (op === "J") return { op: "js", source: rest };
  if (op === "W") {
    const ms = Number(rest.split(/\s+/)[0]);
    return { op: "wait", ms: Number.isFinite(ms) ? Math.min(Math.max(ms, 0), 10000) : 500 };
  }
  if (op === "ST") {
    const tabId = Number(rest.split(/\s+/)[0]);
    return Number.isFinite(tabId) ? { op: "switch_tab", tabId } : null;
  }
  return null;
}

async function dispatchQuickCommand(tab, cmd, timings = {}) {
  if (!cmd) return { ok: false, error: "no command parsed" };
  if (cmd.op === "click") {
    return dispatchCdpMouse(tab, { target: JSON.stringify({ action: "click", x: cmd.x, y: cmd.y }) }, timings);
  }
  if (cmd.op === "type") {
    return dispatchCdpKey(tab, { target: `type ${cmd.text}` }, timings);
  }
  if (cmd.op === "key") {
    if (!cmd.key) return { ok: false, error: "K command requires a key name" };
    return dispatchCdpKey(tab, { target: `press ${cmd.key}` }, timings);
  }
  if (cmd.op === "nav") {
    if (!cmd.url) return { ok: false, error: "N command requires a URL" };
    // Reuse the Phase 1 classifier guard via originBlockedReason.
    await ensureOriginsClassified([cmd.url]);
    const reason = originBlockedReason(cmd.url);
    if (reason) return { ok: false, error: `nav blocked: ${reason}` };
    await chrome.tabs.update(tab.id, { url: normalizeUrl(cmd.url) });
    await waitForTabSettled(tab.id, 8000);
    try { recordObservedOrigin(await chrome.tabs.get(tab.id)); } catch (_err) {}
    invalidatePageContext(tab.id);
    return { ok: true, navigated: cmd.url };
  }
  if (cmd.op === "js") {
    return dispatchBrowserJs(tab, { target: cmd.source }, timings);
  }
  if (cmd.op === "wait") {
    await new Promise((resolve) => setTimeout(resolve, cmd.ms));
    return { ok: true, waited_ms: cmd.ms };
  }
  if (cmd.op === "switch_tab") {
    try {
      await chrome.tabs.update(cmd.tabId, { active: true });
      return { ok: true, switched_to: cmd.tabId };
    } catch (err) {
      return { ok: false, error: `switch_tab failed: ${err?.message || err}` };
    }
  }
  return { ok: false, error: `unknown op ${cmd.op}` };
}

async function runQuickModeLoop(originalPrompt, firstData, timings) {
  let data = firstData;
  for (let round = 1; round <= QUICK_MODE_MAX_ROUNDS; round += 1) {
    const cmd = parseQuickCommand(data?.reply || "");
    if (!cmd) {
      appendMessage("assistant", "[Quick Mode] no parsable command; stopping loop.");
      break;
    }
    if (cmd.op === "done") {
      appendMessage("assistant", `[Quick Mode] DONE round ${round}: ${cmd.summary || "(no summary)"}`);
      break;
    }
    const tab = await activeTab().catch(() => null);
    if (!tab?.id) {
      appendMessage("assistant", "[Quick Mode] no active tab; stopping loop.");
      break;
    }
    const result = await dispatchQuickCommand(tab, cmd, timings);
    appendMessage("assistant",
      `[Quick Mode r${round}] ${cmd.op}${result?.ok ? " ✓" : ` ✗ ${result?.error || "failed"}`}`);
    if (!result?.ok) break;
    // Capture screenshot for the next round.
    let screenshotDataUrl = "";
    try {
      const capture = await chrome.runtime.sendMessage({
        type: "SENSEI_CAPTURE_VISIBLE_TAB", windowId: tab.windowId,
      });
      if (capture?.ok) screenshotDataUrl = capture.dataUrl || "";
    } catch (_err) { /* non-fatal */ }
    if (round >= QUICK_MODE_MAX_ROUNDS) {
      appendMessage("assistant", `[Quick Mode] hit ${QUICK_MODE_MAX_ROUNDS}-round cap; stopping.`);
      break;
    }
    // Continuation round.
    const tabsContext = await gatherTabsContext();
    const nextBody = {
      prompt: originalPrompt,
      mode: "quick",
      source: "chrome_extension",
      session_id: state.config.sessionId,
      parent_turn_id: data?.turn_id || null,
      page_context: { screenshot_data_url: screenshotDataUrl.slice(0, 200000) },
    };
    if (tabsContext.length) nextBody.tabs_context = tabsContext;
    try {
      data = await backendFetch("/chat", { method: "POST", body: nextBody });
      appendMessage("assistant", cleanReply(data.reply || ""));
    } catch (err) {
      appendMessage("assistant", `[Quick Mode] backend error: ${err.message}`);
      break;
    }
  }
}

async function sendPrompt() {
  const input = $("#promptInput");
  const prompt = input.value.trim();
  if (!prompt) return;

  // Bridge-state gate: if the heartbeat shows the backend unreachable,
  // refuse with a structured "would have sent" message rather than fake
  // success or a generic network-error stack trace. The user sees both
  // their prompt (so they know it was received) and the honest failure.
  // Phase 1 negative-test gate per
  // ~/.claude/plans/reactive-waddling-papert.md.
  const bridge = bridgeState();
  if (!bridge.ok) {
    appendMessage("user", prompt);
    input.value = "";
    const detail = bridge.lastError || "no heartbeat";
    appendError(
      `Bridge unreachable (${detail}). Would have sent: "${prompt}". ` +
      `Restart your Sensei backend to reconnect, then try again.`,
    );
    setConnection("Bridge unreachable", "error");

    // Queue a refusal record so this attempt leaves an audit row when the
    // bridge recovers. Honest-failure principle: every dispatch attempt
    // should leave evidence, even when the bridge was unreachable at the
    // moment of refusal. crypto.randomUUID assigns a stable
    // correlation_id at refusal time so client clock skew never reorders
    // the audit trail.
    state.refusalQueue = state.refusalQueue || [];
    state.refusalQueue.push({
      correlation_id: crypto.randomUUID(),
      ts: new Date().toISOString(),
      blocked_reason: "bridge_unreachable",
      prompt: prompt,
      source: "chrome_extension",
      session_id: state.config.sessionId,
      capabilities_fired: [],
      bridge_error: detail,
    });
    // Optimistic flush attempt — almost certainly fails (bridge is down),
    // but if /health happened to recover between bridgeState() and now,
    // we don't want to wait for the next heartbeat tick. The heartbeat
    // recovery edge retries either way.
    flushRefusalQueue().catch(() => {});
    return;
  }

  const timings = {};
  const totalStart = performance.now();

  // Fresh user prompt clears any active loop from a prior goal.
  resetLoop();

  input.value = "";
  appendMessage("user", prompt);
  $("#sendButton").disabled = true;
  $("#micButton").disabled = true;
  setConnection("Thinking");

  try {
    const [ctx, localFileHints] = await Promise.all([
      pageContext(prompt, timings),
      localFileHintsForPrompt(prompt),
    ]);
    const body = {
      prompt,
      mode: $("#modeSelect").value,
      source: "chrome_extension",
      session_id: state.config.sessionId,
      page_context: ctx,
      client_timings: { ...timings }
    };
    // Phase 2.1: surface the configured local résumé path to the model so it
    // can reference it in file-upload BROWSER_FILL targets. Empty when unset.
    if (state.config.resumePath) body.resume_path = state.config.resumePath;
    if (localFileHints) body.local_file_hints = localFileHints;
    // Phase 4.3: list the session group's tabs so the model knows what's open.
    const tabsContext = await gatherTabsContext();
    if (tabsContext.length) body.tabs_context = tabsContext;
    const data = await timed(timings, "chat", () => backendFetch("/chat", { method: "POST", body }));
    timings.total = Math.round(performance.now() - totalStart);
    const meta = formatMeta(data, timings);
    $("#routeMeta").textContent = meta;
    // Phase 6 — Quick Mode keeps existing prose+loop path unchanged.
    if (body.mode === "quick") {
      appendMessage("assistant", cleanReply(data.reply), meta);
      await runQuickModeLoop(prompt, data, timings);
    } else {
      // ONE PIPE: enqueue parsed actions to the shared bridge queue → poll loop
      // executes via sendToContent → synthesize reply from ground-truth result.
      // Model prose is NEVER shown. Zero fake DONE.
      const chatActions = data.actions || [];
      if (!chatActions.length) {
        appendMessage("assistant", "Not sure what to do. Rephrase?");
      } else {
        const workingEl = appendMessage("assistant", "Working...");
        let first = true;
        for (const action of chatActions) {
          const actionId = await enqueue(action);
          if (!actionId) {
            const errText = "Couldn't queue that action.";
            if (first) { workingEl.textContent = errText; first = false; }
            else appendMessage("assistant", errText);
            continue;
          }
          const rec = await waitForActionResult(actionId);
          const reply = synthesizeReply(action, rec);
          if (first) { workingEl.textContent = reply; first = false; }
          else appendMessage("assistant", reply);
        }
      }
    }
    setConnection("Backend ready");
  } catch (err) {
    appendError(err.message);
    setConnection("Backend error", "error");
  } finally {
    $("#sendButton").disabled = false;
    $("#micButton").disabled = false;
    input.focus();
  }
}

// ── M9 Agentic Continuation Loop ──────────────────────────────────────────

function resetLoop() {
  state.loop = {
    active: false,
    turn_id: null,
    round_num: 0,
    round_budget: 3,
    pending: 0,
    rejected: false,
    results: [],
    stopped: false
  };
  const bar = $("#loopBar");
  if (bar) bar.hidden = true;
  const counter = $("#roundCounter");
  if (counter) counter.textContent = "";
}

function startLoop(data) {
  const actions = data.actions || [];
  // Browser actions are pending work even if a model mistakenly emitted DONE.
  const willContinue = actions.length > 0 && (data.round_remaining || 0) > 0;
  state.loop.turn_id = data.turn_id || null;
  state.loop.round_num = data.round_num || 1;
  state.loop.round_budget = data.round_budget || 3;
  state.loop.pending = actions.length;
  state.loop.rejected = false;
  state.loop.results = [];
  state.loop.stopped = false;
  state.loop.active = willContinue;
  // Ack pre-filter (Wave 1.1, 2026-05-17 PM): if the model's reply is a pure
  // conversational acknowledgment with no actions queued, treat it as a
  // closure signal. Without this, the auto-continuation loop POSTs another
  // /chat/continue and the model echoes another ack, which re-fires the
  // loop ("nice → 'Action ready for review' → nice → ..."). Pair with the
  // server-side short-circuit in commit 555cc09 — server catches the model's
  // ack-shaped reply on its side; this catches it on the client side as
  // belt-and-suspenders. Pattern is intentionally narrow: short replies
  // that match a small whitelist of pure-ack tokens with no surrounding
  // sentence structure. Anything longer or more substantive bypasses.
  const _ackPattern = /^\s*(ok|okay|nice|cool|thanks|thank you|got it|good|great|sure|fine|alright|yeah|yep|nope|no thanks|sounds good|perfect|all good|no problem|np|done|👍|✅)[\s.!]*$/i;
  const _replyText = (data && typeof data.reply === "string") ? data.reply.trim() : "";
  const _replyIsAck = _replyText.length > 0 && _replyText.length < 40 && _ackPattern.test(_replyText);
  const _noActionsQueued = !data || !data.actions || data.actions.length === 0;
  // Wave 2.1 (2026-05-17 PM) — `terminal_authority` is the server's
  // unconditional stop signal. The server-side dispatcher (Codex's pending
  // half) sets `terminal_authority: true` whenever it decides the loop must
  // not continue (DONE emitted, terminal_reason "no_actions", server-side
  // policy refusal, etc.). Extension reads it defensively: if absent or
  // false, behavior unchanged. If true, force-stop via the existing
  // continueLoop guard. Belt-and-suspenders alongside last_done /
  // last_terminal_reason — the server's authority overrides any local state.
  const _serverAuthority = !!(data && data.terminal_authority === true);
  state.loop.last_done = !!(data && (data.done === true || (_replyIsAck && _noActionsQueued) || _serverAuthority));
  state.loop.last_terminal_reason = (data && data.terminal_reason) ||
    (_serverAuthority ? "server_terminal_authority" :
     (_replyIsAck && _noActionsQueued ? "ack_reply" : ""));

  const bar = $("#loopBar");
  if (bar) bar.hidden = !willContinue;
  const counter = $("#roundCounter");
  if (counter) {
    counter.textContent = willContinue
      ? `Round ${state.loop.round_num}/${state.loop.round_budget}`
      : "";
  }
}

function summarizeRoundResults(results) {
  // Walk state.loop.results and produce ONE honest status line. DONE/FAILED
  // come from action_result outcomes only — never from model prose. Returns
  // null if there were no actions in this round (pure conversational reply).
  if (!Array.isArray(results) || results.length === 0) return null;
  const total = results.length;
  const successes = results.filter(r => r.verdict === "accept" && r.result === "success");
  const failures = results.filter(r =>
    r.verdict !== "reject" &&
    (r.verdict !== "accept" || r.result === "failure" || r.result === "error")
  );
  const rejections = results.filter(r => r.verdict === "reject");
  if (rejections.length > 0) {
    return `STOPPED: ${rejections.length} of ${total} action(s) rejected.`;
  }
  if (failures.length === 0 && successes.length === total) {
    return `DONE: ${total} of ${total} action(s) completed (verified by action_results).`;
  }
  const reasons = failures.slice(0, 5).map(r => {
    const kind = r?.action?.kind || "?";
    const reason = r?.final_state?.reason
      || r?.final_state?.error
      || r?.result
      || "unknown";
    return `${kind}: ${reason}`;
  });
  const extra = failures.length > 5 ? `; +${failures.length - 5} more` : "";
  return `FAILED: ${failures.length} of ${total} action(s) did not complete. Reasons: ${reasons.join("; ")}${extra}`;
}

function recordLoopResult(action, verdict, result, finalState) {
  if (!state.loop.active && state.loop.pending === 0) return;
  state.loop.results.push({
    action_id: action.id,
    verdict,
    result,
    final_state: finalState || {},
    action: { kind: action.kind, target: action.target },
    gated_by: action?.gated_by || action?.classification?.gated_by || null
  });
  if (verdict === "reject") state.loop.rejected = true;
  state.loop.pending = Math.max(0, state.loop.pending - 1);
  if (state.loop.pending === 0 && state.loop.active) {
    // The round closed — synthesize an honest status bubble from
    // action_result evidence BEFORE deferring continuation. This is the
    // only place a "DONE" or "FAILED" reaches the operator's eyes.
    const summary = summarizeRoundResults(state.loop.results);
    if (summary) appendMessage("assistant", summary);
    // Defer the continuation so UI status updates flush first.
    setTimeout(() => continueLoop(), 50);
  }
}

async function continueLoop() {
  if (state.loop.stopped || !state.loop.active || !state.loop.turn_id) {
    resetLoop();
    return;
  }
  // LOOP TERMINATION gate — if the prior turn closed (server emitted done=true
  // or any terminal_reason like "no_actions"/"done_directive"), do NOT fire a
  // fresh /chat/continue. Last night's 17-turn runaway was the absence of
  // this gate: per-turn round cap was 6, but new turns kept getting spawned
  // because nothing checked the prior turn's closure signal.
  if (state.loop.last_done === true || state.loop.last_terminal_reason) {
    resetLoop();
    setConnection("Backend ready");
    return;
  }
  // If the user rejected anything in this round, treat as a stop signal.
  // Model already heard "no" — don't burn round budget paraphrasing.
  if (state.loop.rejected) {
    appendMessage("assistant", "(Loop stopped — at least one action rejected.)");
    resetLoop();
    setConnection("Backend ready");
    return;
  }

  const counter = $("#roundCounter");
  if (counter) counter.textContent = `Round ${state.loop.round_num + 1}/${state.loop.round_budget} (continuing)`;
  setConnection("Continuing");
  const timings = {};
  const totalStart = performance.now();

  try {
    const ctx = await freshPageContextForContinuation(timings);
    const body = {
      parent_turn_id: state.loop.turn_id,
      source: "chrome_extension",
      session_id: state.config.sessionId,
      action_results: state.loop.results,
      page_context: ctx,
      client_timings: {
        audit_queue_depth: state.auditQueue.length
      }
    };
    // Phase 4.3 — continuation rounds also carry the latest tab list so the
    // model sees newly-opened or closed tabs without waiting for a fresh turn.
    const tabsContext = await gatherTabsContext();
    if (tabsContext.length) body.tabs_context = tabsContext;
    const data = await timed(timings, "chat", () => backendFetch("/chat/continue", { method: "POST", body }));
    timings.total = Math.round(performance.now() - totalStart);
    const meta = formatMeta(data, timings);
    startLoop(data);
    appendMessage("assistant", cleanReply(data.reply), meta);
    $("#routeMeta").textContent = meta;
    await renderActions(data.actions || [], data.blocked_actions || []);
    maybeWarnSilentClaim(data.reply, data.actions || [], data);
    if (!(data.actions || []).length) {
      resetLoop();
      setConnection("Backend ready");
    } else if (!state.loop.active) {
      setConnection("Backend ready");
    }
  } catch (err) {
    appendError(`Loop continuation failed: ${err.message}`);
    resetLoop();
    setConnection("Backend error", "error");
  }
}

function stopLoop() {
  state.loop.stopped = true;
  state.loop.active = false;
  appendMessage("assistant", "(Loop stopped by user.)");
  resetLoop();
  setConnection("Backend ready");
}

// Bridge heartbeat — recurring /health probe so the side panel knows in
// near-real-time whether the local backend is reachable. Gates dispatch so
// prompt submissions don't fake success when the bridge is down. Phase 1 of
// the agentic-follow-through layer per ~/.claude/plans/reactive-waddling-papert.md.
const HEARTBEAT_INTERVAL_MS = 7000;
const HEARTBEAT_STALE_MS = 20000;
let _heartbeatTimer = null;

// MCP→Chrome live action transport. The bridge exposes /extension/queue
// where any MCP client (sensei_mcp_server.py via Claude Code, Codex,
// Cline, or a bare stdio client) can POST structured BROWSER_* actions.
// This polls the queue and feeds pulled actions into the same renderActions
// pipeline that /chat replies use, so safety/classification/auto-flow all
// still apply. Session id `mcp-default` is the well-known bucket the MCP
// server pushes to.
const MCP_QUEUE_POLL_MS = 3000;
const MCP_QUEUE_SESSION = "mcp-default";
const CHAT_QUEUE_SESSION = "chat-default";
const CHAT_RESULT_POLL_MS = 500;
const CHAT_RESULT_TIMEOUT_MS = 15000;
let _mcpQueuePollTimer = null;
let _mcpQueuePollInflight = false;

// Enqueue a single action to the bridge's shared queue under chat-default session.
// Returns the action_id assigned by the bridge, or null on failure.
async function enqueue(action) {
  const a = { ...action, _session_id: CHAT_QUEUE_SESSION };
  try {
    const data = await backendFetch("/extension/queue", {
      method: "POST",
      body: { session_id: CHAT_QUEUE_SESSION, actions: [a] },
    });
    return (data?.action_ids || [])[0] || null;
  } catch (_err) {
    return null;
  }
}

// Short-poll /extension/result until Chrome's outcome lands or we time out.
// Returns the stored result record (from /extension/action_result) or null.
async function waitForActionResult(actionId) {
  const deadline = performance.now() + CHAT_RESULT_TIMEOUT_MS;
  while (performance.now() < deadline) {
    await new Promise((r) => setTimeout(r, CHAT_RESULT_POLL_MS));
    try {
      const data = await backendFetch(`/extension/result?action_id=${encodeURIComponent(actionId)}`);
      if (data?.ok && data?.result) return data.result;
    } catch (_err) { /* retry */ }
  }
  return null;
}

// Pure function: map a completed action_result record → one natural-language line.
// Never calls the model. Always derived from Chrome's ground-truth outcome only.
function synthesizeReply(action, rec) {
  const kind = String(action?.kind || "").toUpperCase();
  if (!rec) return "No response from browser. Is the side panel polling?";
  const isOk = rec.result === "success";
  const fs = rec.final_state || {};
  const reason = fs.reason || "";
  if (kind === "BROWSER_NAV") {
    const url = action.target || "";
    return isOk ? `Opened ${url}.` : `Couldn't open that.${reason ? " " + reason : ""}`;
  }
  if (kind === "BROWSER_FILL") {
    const target = action.target || "field";
    if (!isOk) {
      return reason === "value_did_not_persist"
        ? "That field didn't keep the value. Might need clicking first."
        : `Couldn't fill that.${reason ? " " + reason : ""}`;
    }
    return `Filled ${target}.`;
  }
  if (kind === "BROWSER_READ" || kind === "BROWSER_READ_PAGE" || kind === "BROWSER_OBSERVE") {
    if (!isOk) return `Can't read this page.${reason ? " " + reason : ""}`;
    const pc = fs.page_context || {};
    const title = pc.title || "";
    const text = String(pc.text || "").slice(0, 100);
    return `I see: ${title || "(no title)"}${text ? " — " + text : ""}`;
  }
  return isOk ? "Done." : `Action failed.${reason ? " " + reason : ""}`;
}

function bridgeState() {
  const last = state.bridge?.lastOkAt || 0;
  const sinceMs = last > 0 ? performance.now() - last : Infinity;
  return {
    ok: last > 0 && sinceMs < HEARTBEAT_STALE_MS,
    sinceMs,
    lastError: state.bridge?.lastError || null,
  };
}

async function flushRefusalQueue() {
  // Drains queued bridge-unreachable refusal records to
  // /extension/refusal_audit. Called on heartbeat recovery so audit rows
  // get ingested as soon as the bridge is reachable again. Each record
  // carries its own correlation_id assigned at refusal time so client
  // and server clocks don't have to agree on ordering.
  if (!Array.isArray(state.refusalQueue) || state.refusalQueue.length === 0) return;
  const batch = state.refusalQueue.slice();
  for (const rec of batch) {
    try {
      await backendFetch("/extension/refusal_audit", { method: "POST", body: rec });
      const idx = state.refusalQueue.indexOf(rec);
      if (idx >= 0) state.refusalQueue.splice(idx, 1);
    } catch (_err) {
      // Bridge flapped down again or endpoint missing on this server
      // version. Leave the records in queue; next recovery edge will retry.
      break;
    }
  }
}

async function heartbeat() {
  state.bridge = state.bridge || { lastOkAt: 0, lastError: null };
  const wasOk = state.bridge.lastOkAt > 0
    && (performance.now() - state.bridge.lastOkAt) < HEARTBEAT_STALE_MS;
  try {
    const data = await backendFetch("/health");
    if (data.ok) {
      state.bridge.lastOkAt = performance.now();
      state.bridge.lastError = null;
      setConnection("Backend ready");
      // Recovery edge — flush any refusal records queued during the
      // unreachable window. Fire-and-forget; flush failures will retry
      // on the next recovery edge.
      if (!wasOk) {
        flushRefusalQueue().catch(() => {});
      }
    } else {
      state.bridge.lastError = "degraded";
      setConnection("Backend degraded", "error");
    }
  } catch (err) {
    state.bridge.lastError = err.message || String(err);
    setConnection(`Bridge unreachable: ${state.bridge.lastError}`, "error");
  }
}

function startHeartbeat() {
  heartbeat();
  if (_heartbeatTimer) clearInterval(_heartbeatTimer);
  _heartbeatTimer = setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat() {
  if (_heartbeatTimer) {
    clearInterval(_heartbeatTimer);
    _heartbeatTimer = null;
  }
}

async function pollMcpQueue() {
  // Skip when the bridge isn't healthy — the heartbeat already covers the
  // unreachable case and there's no point hammering a dead port.
  if (_mcpQueuePollInflight) return;
  const bridgeOk = state.bridge?.lastOkAt > 0
    && (performance.now() - state.bridge.lastOkAt) < HEARTBEAT_STALE_MS;
  if (!bridgeOk) return;

  _mcpQueuePollInflight = true;
  try {
    // Drain both MCP and chat-default sessions in one pass (shared poll loop,
    // shared executeAction path — Test C: MCP still works, Test A/B: chat works).
    for (const sid of [MCP_QUEUE_SESSION, CHAT_QUEUE_SESSION]) {
      let data;
      try {
        data = await backendFetch(
          `/extension/queue?session_id=${encodeURIComponent(sid)}`
        );
      } catch (_err) {
        continue;
      }
      const actions = Array.isArray(data?.actions) ? data.actions : [];
      for (const a of actions) {
        if (!a || typeof a !== "object") continue;
        if (!a._session_id) a._session_id = data?.session_id || sid;
        let tab = await activeTab().catch(() => null);
        if (!tab?.id || !canInjectIntoTab(tab)) {
          // Active tab may be a chrome:// page or user is in terminal — find
          // any injectable http/https tab in the current window.
          const allTabs = await chrome.tabs.query({ currentWindow: true }).catch(() => []);
          tab = allTabs.find(t => canInjectIntoTab(t)) || null;
        }
        if (!tab?.id) {
          // No injectable tab at all — for navigate actions, create one.
          const navUrl = (a.kind === "BROWSER_NAV" || a.type === "BROWSER_NAV")
            && /^https?:/i.test(String(a.target || "")) ? a.target : null;
          if (navUrl) {
            // VISIBILITY INVARIANT: every action tab must be on screen.
            const newTab = await chrome.tabs.create({ url: navUrl, active: true }).catch(() => null);
            reportAction(a, "accept", newTab ? "success" : "failure",
              newTab ? { ok: true, url: navUrl, tabId: newTab.id } : { error: "tab create failed" });
          } else {
            reportAction(a, "reject", "failure", { error: "no injectable tab for MCP action" });
          }
          continue;
        }
        // VISIBILITY INVARIANT: bring the target tab AND its window to the
        // foreground before any action fires. Operator must see every move.
        await chrome.tabs.update(tab.id, { active: true }).catch(() => {});
        await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
        try {
          let result;
          const actionKind = String(a.kind || a.type || "").toUpperCase();
          if (actionKind === "BROWSER_SCREENSHOT") {
            // captureVisibleTab can ONLY run in the service worker — route via
            // SENSEI_CAPTURE_VISIBLE_TAB. Content script correctly refuses this
            // with "BROWSER_SCREENSHOT must be handled by background".
            const capture = await chrome.runtime.sendMessage({
              type: "SENSEI_CAPTURE_VISIBLE_TAB",
              windowId: tab.windowId
            });
            if (capture?.ok && capture?.dataUrl) {
              // Strip data-URL prefix so sensei_mcp_server.py tool_screenshot
              // can base64-decode via inner.get("b64", "").
              const b64 = capture.dataUrl.replace(/^data:[^;]+;base64,/, "");
              result = { ok: true, b64, screenshot: "visible_tab_png", context_lifetime: "one_turn" };
            } else {
              result = { ok: false, error: capture?.error || "screenshot capture failed" };
            }
          } else {
            await ensureContentScript(tab);
            result = await sendToContent(tab, a);
          }
          const ok = result?.ok !== false;
          reportAction(a, "accept", ok ? "success" : "failure", result || {});
        } catch (err) {
          reportAction(a, "accept", "failure", { error: String(err?.message || err) });
        }
      }
    }
  } catch (_err) {
    // Bridge flap or 4xx — silently retry next tick. The heartbeat surface
    // is responsible for telling the user the bridge is down.
  } finally {
    _mcpQueuePollInflight = false;
  }
}

async function syncClafBadge() {
  const badge = document.getElementById("clafBadge");
  if (!badge) return;
  try {
    const r = await fetch(`${state.config.backendUrl}/mode`, { signal: AbortSignal.timeout(2000) });
    if (!r.ok) throw new Error("bad status");
    const d = await r.json();
    const authLabel = d.auth === "api_key" ? "API" : "Max Anthropic";

    // Also check secretary health
    let secLabel = "";
    try {
      const sr = await fetch(`${state.config.agentUrl}/agent/health`, { signal: AbortSignal.timeout(1500) });
      if (sr.ok) {
        const sd = await sr.json();
        const active = sd.active_tasks || 0;
        secLabel = active > 0 ? ` · sec:${active}` : " · sec:ok";
      }
    } catch (_) {
      secLabel = " · sec:off";
    }

    badge.textContent = `${authLabel} · ${d.claf_mode} · ${d.model}${secLabel}`;
    badge.style.color = d.auth === "api_key" ? "#e67e22" : "#27ae60";
    badge.title = `Account: ${authLabel} | CLAF: ${d.claf_mode} | Model: ${d.model}${secLabel}`;
  } catch (_) {
    badge.textContent = "bridge?";
    badge.style.color = "#999";
  }
}

// ── Secretary debug panel ────────────────────────────────────────────────────
let _debugPollTimer = null;
const _debugLines = [];
const DEBUG_MAX_LINES = 40;

async function pollSecretaryDebug() {
  const statusEl = document.getElementById("debugStatus");
  const logEl = document.getElementById("debugLog");
  if (!statusEl || !logEl) return;
  try {
    const r = await fetch(`${state.config.agentUrl}/agent/stats`, { signal: AbortSignal.timeout(2000) });
    if (!r.ok) throw new Error("no response");
    const d = await r.json();
    const byStatus = d.by_status || {};
    const executing = byStatus.executing || 0;
    const completed = byStatus.completed || 0;
    const failed = byStatus.failed || 0;
    statusEl.textContent = executing > 0 ? `running ${executing}` : `idle · ${completed} done`;
    statusEl.className = "debug-status" + (executing > 0 ? " active" : "");

    // Show recent events
    const events = (d.recent_events || []).slice(0, 5);
    const lines = events.map(e => {
      const ts = (e.ts || "").slice(11, 19);
      const tid = (e.task_id || "").slice(0, 6);
      const type = e.event_type || "";
      const pay = e.payload ? JSON.parse(e.payload || "{}") : {};
      return `${ts} [${tid}] ${type}${pay.status ? " → " + pay.status : ""}`;
    });
    logEl.textContent = lines.join("\n") || "no events";
  } catch (_) {
    statusEl.textContent = "off";
    statusEl.className = "debug-status";
    logEl.textContent = "secretary not reachable";
  }
}

function startSecretaryDebug() {
  pollSecretaryDebug();
  if (_debugPollTimer) clearInterval(_debugPollTimer);
  _debugPollTimer = setInterval(pollSecretaryDebug, 4000);
}

function startMcpQueuePoll() {
  pollMcpQueue();
  if (_mcpQueuePollTimer) clearInterval(_mcpQueuePollTimer);
  _mcpQueuePollTimer = setInterval(pollMcpQueue, MCP_QUEUE_POLL_MS);
}

function stopMcpQueuePoll() {
  if (_mcpQueuePollTimer) {
    clearInterval(_mcpQueuePollTimer);
    _mcpQueuePollTimer = null;
  }
}

// Back-compat: callers that used the one-shot healthCheck() get the new
// heartbeat path. Future code should call startHeartbeat() directly.
async function healthCheck() {
  return heartbeat();
}

async function transcribe(blob) {
  setConnection("Transcribing");
  const data = await backendFetch("/stt", { method: "POST", body: blob });
  $("#promptInput").value = data.text || "";
  $("#promptInput").focus();
  setConnection("Backend ready");
}

function shortcutInputs(steps = []) {
  const keys = new Set();
  for (const step of steps) {
    for (const value of [step?.target, step?.value]) {
      String(value || "").replace(/<([a-zA-Z0-9_-]+)>/g, (_m, key) => {
        keys.add(key);
        return "";
      });
    }
  }
  return Array.from(keys).sort();
}

async function describeWorkflowSteps(steps, transcript = "") {
  const out = [];
  for (const step of steps.slice(0, 80)) {
    try {
      const data = await backendFetch("/tool/describe_step", {
        method: "POST",
        body: { step, transcript },
        timeoutMs: 20000,
      });
      out.push({ ...step, description: data.description || step.label || step.kind });
    } catch (_err) {
      out.push({ ...step, description: step.label || step.kind });
    }
  }
  return out;
}

async function saveShortcut(shortcut) {
  const stored = await chromeGet(["shortcuts"]);
  const shortcuts = Array.isArray(stored.shortcuts) ? stored.shortcuts : [];
  const next = [shortcut, ...shortcuts.filter((item) => item.id !== shortcut.id)].slice(0, 50);
  state.config.shortcuts = next;
  await chromeSet({ shortcuts: next });
  renderShortcuts();
}

async function toggleWorkflowRecording() {
  const button = $("#recordButton");
  if (state.workflowRecording.active) {
    const tab = await activeTab().catch(() => null);
    let stopped = { ok: false, steps: [], events: [] };
    if (tab?.id) {
      stopped = await chrome.tabs.sendMessage(tab.id, { type: "SENSEI_RECORD_STOP" }).catch((err) => ({
        ok: false,
        error: err.message || String(err),
        steps: [],
        events: [],
      }));
    }
    state.workflowRecording.active = false;
    button.textContent = "Record";
    const transcript = $("#promptInput").value.trim();
    const rawSteps = Array.isArray(stopped.steps) ? stopped.steps : [];
    if (!stopped.ok || !rawSteps.length) {
      appendError(stopped.error || "Recording stopped with no replayable steps");
      return;
    }
    const steps = await describeWorkflowSteps(rawSteps, transcript);
    const title = transcript || stopped.title || `Workflow ${new Date().toLocaleString()}`;
    const shortcut = {
      id: `shortcut-${crypto.randomUUID()}`,
      name: title.slice(0, 80),
      createdAt: new Date().toISOString(),
      startUrl: stopped.url || rawSteps[0]?.target || "",
      transcript,
      inputs: shortcutInputs(steps),
      steps,
      rrweb_events: [
        ...(Array.isArray(stopped.rrweb_events) ? stopped.rrweb_events.slice(-500) : []),
      ],
      recorded_events: Array.isArray(stopped.events) ? stopped.events.slice(-250) : [],
    };
    await saveShortcut(shortcut);
    appendMessage("assistant", `Saved shortcut: ${shortcut.name}`);
    return;
  }

  const tab = await activeTab().catch(() => null);
  if (!tab?.id) {
    appendError("No active tab to record");
    return;
  }
  await ensureWorkflowRecorderSupport(tab, {});
  const started = await chrome.tabs.sendMessage(tab.id, { type: "SENSEI_RECORD_START" });
  if (!started?.ok) {
    appendError(started?.error || "Recording could not start");
    return;
  }
  state.workflowRecording = {
    active: true,
    tabId: tab.id,
    startedAt: new Date().toISOString(),
    rrwebEvents: [],
  };
  button.textContent = "Stop";
  setConnection("Recording workflow");
}

function collectShortcutParams(shortcut) {
  const params = {};
  for (const key of shortcut.inputs || []) {
    const value = window.prompt(`Value for <${key}>`, "");
    if (value === null) return null;
    params[key] = value;
  }
  return params;
}

async function replayShortcut(shortcut) {
  const params = collectShortcutParams(shortcut);
  if (params === null) return;
  setConnection("Running shortcut");
  const result = await chrome.runtime.sendMessage({
    type: "SENSEI_RUN_SHORTCUT",
    shortcut,
    params,
  });
  appendMessage("assistant", result?.ok
    ? `Shortcut finished: ${shortcut.name}`
    : `Shortcut failed: ${result?.error || "see action result"}`);
  setConnection("Backend ready");
}

function openScheduleDialog(shortcut) {
  state.scheduleShortcutId = shortcut.id;
  $("#scheduleCadence").value = "daily";
  $("#scheduleTime").value = "09:00";
  const dialog = $("#scheduleDialog");
  if (dialog?.showModal) dialog.showModal();
}

async function saveScheduleFromDialog(event) {
  event.preventDefault();
  const shortcutId = state.scheduleShortcutId;
  if (!shortcutId) return;
  const schedule = {
    id: `schedule-${crypto.randomUUID()}`,
    shortcutId,
    cadence: $("#scheduleCadence").value,
    time: $("#scheduleTime").value || "09:00",
    enabled: true,
  };
  schedule.nextRunAt = nextScheduleTime(schedule);
  const response = await chrome.runtime.sendMessage({ type: "SENSEI_SAVE_SCHEDULE", schedule });
  if (!response?.ok) {
    appendError(response?.error || "Schedule save failed");
    return;
  }
  const stored = await chromeGet(["schedules"]);
  state.config.schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  $("#scheduleDialog")?.close();
  renderShortcuts();
}

function nextScheduleTime(schedule, from = new Date()) {
  const [h, m] = String(schedule.time || "09:00").split(":").map((n) => Number(n));
  const next = new Date(from.getTime());
  next.setSeconds(0, 0);
  next.setHours(Number.isFinite(h) ? h : 9, Number.isFinite(m) ? m : 0, 0, 0);
  if (next <= from) {
    const cadence = String(schedule.cadence || "daily");
    if (cadence === "weekly") next.setDate(next.getDate() + 7);
    else if (cadence === "monthly") next.setMonth(next.getMonth() + 1);
    else if (cadence === "annual") next.setFullYear(next.getFullYear() + 1);
    else next.setDate(next.getDate() + 1);
  }
  return next.getTime();
}

async function renderShortcuts() {
  const dock = $("#shortcutDock");
  const list = $("#shortcutList");
  if (!dock || !list) return;
  const stored = await chromeGet(["shortcuts", "schedules"]);
  const shortcuts = Array.isArray(stored.shortcuts) ? stored.shortcuts : [];
  const schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  state.config.shortcuts = shortcuts;
  state.config.schedules = schedules;
  list.textContent = "";
  dock.hidden = shortcuts.length === 0;
  for (const shortcut of shortcuts) {
    const row = document.createElement("section");
    row.className = "shortcut-item";
    const main = document.createElement("div");
    main.className = "action-main";
    const name = document.createElement("span");
    name.className = "kind";
    name.textContent = shortcut.name || "Shortcut";
    const buttons = document.createElement("div");
    buttons.className = "action-buttons";
    const run = document.createElement("button");
    run.className = "primary";
    run.type = "button";
    run.textContent = "Run";
    run.addEventListener("click", () => replayShortcut(shortcut));
    const schedule = document.createElement("button");
    schedule.className = "secondary";
    schedule.type = "button";
    schedule.textContent = "Schedule";
    schedule.addEventListener("click", () => openScheduleDialog(shortcut));
    buttons.append(run, schedule);
    main.append(name, buttons);
    const meta = document.createElement("div");
    meta.className = "shortcut-meta";
    const linked = schedules.filter((s) => s.shortcutId === shortcut.id && s.enabled !== false);
    meta.textContent = `${(shortcut.steps || []).length} steps${linked.length ? ` · ${linked.length} schedule(s)` : ""}`;
    row.append(main, meta);
    list.appendChild(row);
  }
}

async function toggleMic() {
  if (state.mediaRecorder?.state === "recording") {
    state.mediaRecorder.stop();
    $("#micButton").textContent = "Mic";
    return;
  }

  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.chunks = [];
    const options = MediaRecorder.isTypeSupported("audio/webm") ? { mimeType: "audio/webm" } : {};
    state.mediaRecorder = new MediaRecorder(state.mediaStream, options);
    state.mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data?.size) state.chunks.push(event.data);
    });
    state.mediaRecorder.addEventListener("stop", async () => {
      const blob = new Blob(state.chunks, { type: state.mediaRecorder.mimeType || "audio/webm" });
      state.mediaStream?.getTracks().forEach((track) => track.stop());
      try {
        await transcribe(blob);
      } catch (err) {
        appendError(`Transcription failed: ${err.message}`);
        setConnection("Backend error", "error");
      }
    });
    state.mediaRecorder.start();
    $("#micButton").textContent = "Stop";
    setConnection("Recording");
  } catch (err) {
    appendError(`Microphone unavailable: ${err.message}`);
  }
}

function installTabCacheInvalidation() {
  chrome.tabs.onUpdated?.addListener((tabId, changeInfo) => {
    if (changeInfo.url || changeInfo.status === "loading") invalidatePageContext(tabId);
    if (changeInfo.url || changeInfo.status === "complete") refreshDomainBlockBanner();
  });
  chrome.tabs.onRemoved?.addListener((tabId) => invalidatePageContext(tabId));
  chrome.tabs.onActivated?.addListener(() => {
    state.contextCache = null;
    refreshDomainBlockBanner();
  });
}

// Phase 1.4 — Domain-block banner. Mirrors the classifier verdict for the
// active tab into a top-of-panel alert when category 1 (malicious) or 2
// (sensitive auth surface) fires. Category 3 stays silent here; its
// per-action force-confirm already surfaces in each card.
async function refreshDomainBlockBanner() {
  const banner = document.getElementById("domainBlockBanner");
  if (!banner) return;
  let tab = null;
  try {
    tab = await activeTab();
  } catch (_err) {
    banner.hidden = true;
    return;
  }
  if (!tab?.url || !/^https?:/i.test(tab.url)) {
    banner.hidden = true;
    return;
  }
  let verdict = null;
  try {
    verdict = await classifyOrigin(tab.url);
  } catch (_err) {
    verdict = null;
  }
  if (!verdict || (verdict.category !== 1 && verdict.category !== 2)) {
    banner.hidden = true;
    return;
  }
  const title = document.getElementById("domainBlockTitle");
  const reason = document.getElementById("domainBlockReason");
  if (title) {
    title.textContent = verdict.category === 1
      ? "Sensei refuses: this page is on the malicious list."
      : "Sensei refuses: sensitive auth surface.";
  }
  if (reason) {
    const matched = verdict.matched ? `${verdict.matched}` : "(matched)";
    const why = verdict.reason ? ` — ${verdict.reason}` : "";
    reason.textContent = `Matched ${matched}${why}`;
  }
  banner.hidden = false;
}

async function prewarmActiveTab() {
  try {
    const tab = await activeTab();
    await ensureContentScript(tab, {});
  } catch (_err) {
    // Prewarming is best-effort; restricted browser pages still use fallback context.
  }
}

async function init() {
  await loadConfig();
  installTabCacheInvalidation();
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    sendPrompt();
  });
  $("#promptInput").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      sendPrompt();
    }
  });
  $("#modeSelect").addEventListener("change", (event) => {
    saveMode(event.target.value);
    const extMode = event.target.value;
    const clafMode = extMode === "auto" ? "hybrid" : extMode === "plan" ? "local" : "local";
    fetch(`${state.config.backendUrl}/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: clafMode })
    }).catch(() => {});
  });
  $("#recordButton").addEventListener("click", toggleWorkflowRecording);
  $("#micButton").addEventListener("click", toggleMic);
  $("#clearActions").addEventListener("click", () => {
    $("#actionList").textContent = "";
    $("#actionDock").hidden = true;
  });
  $("#openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("#stopButton")?.addEventListener("click", stopLoop);
  $("#refreshShortcuts")?.addEventListener("click", renderShortcuts);
  $("#scheduleForm")?.addEventListener("submit", saveScheduleFromDialog);
  $("#cancelSchedule")?.addEventListener("click", () => $("#scheduleDialog")?.close());
  prewarmActiveTab();
  refreshDomainBlockBanner();
  restoreSessionTabGroup();
  renderShortcuts();
  startHeartbeat();
  startMcpQueuePoll();
  startSecretaryDebug();
  syncClafBadge();
  setInterval(syncClafBadge, 15000);
  appendMessage("assistant", "Ready.");
}

init().catch((err) => appendError(err.message));
