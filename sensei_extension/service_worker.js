const DEFAULTS = {
  backendUrl: "http://127.0.0.1:8080",
  mode: "review",
  token: "",
  sessionId: "",
  shortcuts: [],
  schedules: [],
  mcpServers: []
};

const NATIVE_HOST_NAME = "com.master_ai.sensei_extension";
const SCHEDULE_ALARM_PREFIX = "sensei-schedule:";

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function storageSet(values) {
  return new Promise((resolve) => chrome.storage.local.set(values, resolve));
}

function sendNativeMessage(message) {
  return new Promise((resolve) => {
    if (!chrome.runtime?.sendNativeMessage) {
      resolve({ ok: false, error: "nativeMessaging unavailable" });
      return;
    }
    chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        resolve({ ok: false, error: err.message || String(err) });
        return;
      }
      resolve(response || { ok: false, error: "empty native response" });
    });
  });
}

function _scheduleAlarmName(id) {
  return `${SCHEDULE_ALARM_PREFIX}${id}`;
}

function _nextScheduleTime(schedule, from = new Date()) {
  const time = String(schedule?.time || "09:00");
  const m = time.match(/^(\d{1,2}):(\d{2})$/);
  const hour = m ? Math.max(0, Math.min(23, Number(m[1]))) : 9;
  const minute = m ? Math.max(0, Math.min(59, Number(m[2]))) : 0;
  const cadence = String(schedule?.cadence || "daily").toLowerCase();
  const next = new Date(from.getTime());
  next.setSeconds(0, 0);
  next.setHours(hour, minute, 0, 0);
  if (next <= from) {
    if (cadence === "weekly") next.setDate(next.getDate() + 7);
    else if (cadence === "monthly") next.setMonth(next.getMonth() + 1);
    else if (cadence === "annual") next.setFullYear(next.getFullYear() + 1);
    else next.setDate(next.getDate() + 1);
  }
  return next.getTime();
}

function _periodMinutes(schedule) {
  const cadence = String(schedule?.cadence || "daily").toLowerCase();
  if (cadence === "weekly") return 7 * 24 * 60;
  if (cadence === "monthly" || cadence === "annual") return null;
  return 24 * 60;
}

async function restoreSchedules() {
  if (!chrome.alarms?.create) return;
  const stored = await storageGet(["schedules"]);
  const schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  for (const schedule of schedules) {
    if (!schedule?.id || schedule.enabled === false) continue;
    const opts = { when: _nextScheduleTime(schedule) };
    const period = _periodMinutes(schedule);
    if (period) opts.periodInMinutes = period;
    await chrome.alarms.create(_scheduleAlarmName(schedule.id), opts);
  }
}

async function saveSchedule(schedule) {
  if (!schedule?.id || !schedule?.shortcutId) {
    return { ok: false, error: "schedule id and shortcutId required" };
  }
  const stored = await storageGet(["schedules"]);
  const schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  const next = [
    ...schedules.filter((item) => item.id !== schedule.id),
    { ...schedule, enabled: schedule.enabled !== false, updatedAt: new Date().toISOString() }
  ];
  await storageSet({ schedules: next });
  await restoreSchedules();
  return { ok: true, schedules: next };
}

async function deleteSchedule(id) {
  const stored = await storageGet(["schedules"]);
  const schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  const next = schedules.filter((item) => item.id !== id);
  await storageSet({ schedules: next });
  if (chrome.alarms?.clear) await chrome.alarms.clear(_scheduleAlarmName(id));
  return { ok: true, schedules: next };
}

function _substituteInputs(text, params = {}) {
  return String(text || "").replace(/<([a-zA-Z0-9_-]+)>/g, (_m, key) =>
    params[key] !== undefined ? String(params[key]) : `<${key}>`
  );
}

async function _ensureContentScriptForTab(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "SENSEI_PING" });
    return true;
  } catch (_err) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content_script.js"] });
      await chrome.tabs.sendMessage(tabId, { type: "SENSEI_PING" });
      return true;
    } catch (_err2) {
      return false;
    }
  }
}

function _wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function _waitForTabSettled(tabId, timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab?.status === "complete") return tab;
    } catch (_err) {
      return null;
    }
    await _wait(150);
  }
  try { return await chrome.tabs.get(tabId); } catch (_err) { return null; }
}

async function executeWorkflowShortcut(shortcut, params = {}, meta = {}) {
  const steps = Array.isArray(shortcut?.steps) ? shortcut.steps : [];
  if (!steps.length) return { ok: false, error: "shortcut has no steps" };
  const firstNav = steps.find((s) => String(s.kind || "").toUpperCase() === "BROWSER_NAV");
  const startUrl = _substituteInputs(firstNav?.target || shortcut.startUrl || "about:blank", params);
  const tab = await chrome.tabs.create({ url: startUrl || "about:blank", active: false });
  try {
    const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
    await chrome.tabGroups.update(groupId, { title: "Sensei schedule", color: "blue" });
  } catch (_err) {
    // Grouping is useful for cleanup but not required for the scheduled run.
  }
  const results = [];
  await _waitForTabSettled(tab.id, 10000);
  for (const rawStep of steps) {
    const kind = String(rawStep.kind || "").toUpperCase();
    const target = _substituteInputs(rawStep.target || "", params);
    const value = _substituteInputs(rawStep.value || "", params);
    try {
      if (kind === "BROWSER_NAV") {
        await chrome.tabs.update(tab.id, { url: target });
        await _waitForTabSettled(tab.id, 10000);
        results.push({ ok: true, kind, target });
        continue;
      }
      if (kind === "BROWSER_WAIT") {
        const ms = Math.max(250, Math.min(Number(target || rawStep.ms || 1000), 15000));
        await _wait(ms);
        results.push({ ok: true, kind, waited_ms: ms });
        continue;
      }
      const ready = await _ensureContentScriptForTab(tab.id);
      if (!ready) throw new Error("content script unavailable");
      const actionTarget = kind === "BROWSER_FILL" && value
        ? `${target} :: ${value}`
        : target;
      const result = await chrome.tabs.sendMessage(tab.id, {
        type: "SENSEI_EXECUTE_ACTION",
        action: { kind, target: actionTarget, extras: { scheduled: true, schedule_id: meta.scheduleId || "" } }
      });
      results.push({ kind, target, result });
    } catch (err) {
      results.push({ ok: false, kind, target, error: String(err?.message || err) });
      break;
    }
    await _wait(250);
  }
  return { ok: results.every((r) => r.ok !== false && r.result?.ok !== false), tabId: tab.id, results };
}

async function runScheduledWorkflow(scheduleId) {
  const stored = await storageGet(["schedules", "shortcuts"]);
  const schedules = Array.isArray(stored.schedules) ? stored.schedules : [];
  const shortcuts = Array.isArray(stored.shortcuts) ? stored.shortcuts : [];
  const schedule = schedules.find((s) => s.id === scheduleId);
  if (!schedule || schedule.enabled === false) return { ok: false, error: "schedule not found or disabled" };
  const shortcut = shortcuts.find((s) => s.id === schedule.shortcutId);
  if (!shortcut) return { ok: false, error: "shortcut not found" };
  const firedAt = Date.now();
  const expected = Number(schedule.nextRunAt || 0);
  const delayed = expected && firedAt - expected > 5 * 60 * 1000;
  const result = await executeWorkflowShortcut(shortcut, schedule.params || {}, { scheduleId });
  schedule.lastRunAt = new Date(firedAt).toISOString();
  schedule.lastResult = result;
  schedule.lastStatus = delayed ? "delayed_execution" : (result.ok ? "success" : "failure");
  schedule.nextRunAt = _nextScheduleTime(schedule, new Date(firedAt));
  await saveSchedule(schedule);
  return result;
}

// ─── AX-tree snapshot (Claude-Chrome-style page read).
// Plan: ~/.claude/plans/https-www-claudechrome-com-blog-how-clau-hidden-bentley.md
// Primary source is the Chrome accessibility tree via CDP, which pierces both
// open and closed Shadow DOM. content_script's DOM-fallback walker covers the
// chrome:// / extension-popup case where the debugger cannot attach.

const PAGE_TREE_BYTE_CAP = 24576;
const AX_DEBUGGER_VERSION = "1.3";
const CROSS_ORIGIN_IFRAME_STRATEGY = "src_title_only";  // future: "debugger_attach_per_frame"

const _attachedTabs = new Set();
const _snapshotRefMaps = new Map();  // tabId → { ref: {backendNodeId, selector} }

async function _ensureDebuggerAttached(tabId) {
  if (_attachedTabs.has(tabId)) return;
  await new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, AX_DEBUGGER_VERSION, () => {
      const err = chrome.runtime.lastError;
      if (err) {
        // "Another debugger is already attached" is fine if it's us; surface otherwise.
        if (String(err.message || "").includes("already attached")) {
          _attachedTabs.add(tabId);
          resolve();
          return;
        }
        reject(new Error(err.message || String(err)));
        return;
      }
      _attachedTabs.add(tabId);
      resolve();
    });
  });
}

function _cdpSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(`${method}: ${err.message || String(err)}`));
        return;
      }
      resolve(result || {});
    });
  });
}

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source?.tabId !== undefined) {
    _attachedTabs.delete(source.tabId);
    _snapshotRefMaps.delete(source.tabId);
    _networkBuffers.delete(source.tabId);
    _networkEnabledTabs.delete(source.tabId);
  }
  // reason is "target_closed" / "canceled_by_user" — both fine, nothing to do.
});

// Phase 5.3 — Network ring buffer per tab. Filled by chrome.debugger.onEvent
// when Network.* events fire. Bounded at NETWORK_BUFFER_LIMIT so a noisy page
// can't blow extension memory. Authorization / Cookie / Set-Cookie / Proxy-
// Authorization headers are redacted before storage.
const NETWORK_BUFFER_LIMIT = 50;
const _networkBuffers = new Map();          // tabId -> Array<event>
const _networkEnabledTabs = new Set();      // tabId set
const _NETWORK_REDACT_HEADERS = new Set([
  "authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key",
]);

function _redactHeaders(headers) {
  if (!headers || typeof headers !== "object") return {};
  const out = {};
  for (const [name, value] of Object.entries(headers)) {
    const lower = String(name || "").toLowerCase();
    out[name] = _NETWORK_REDACT_HEADERS.has(lower) ? "[REDACTED]" : String(value || "").slice(0, 600);
  }
  return out;
}

function _appendNetworkEvent(tabId, event) {
  if (!Number.isFinite(tabId)) return;
  let buf = _networkBuffers.get(tabId);
  if (!buf) { buf = []; _networkBuffers.set(tabId, buf); }
  buf.push(event);
  if (buf.length > NETWORK_BUFFER_LIMIT) buf.splice(0, buf.length - NETWORK_BUFFER_LIMIT);
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId = source?.tabId;
  if (!Number.isFinite(tabId)) return;
  if (method === "Network.requestWillBeSent") {
    const req = params?.request || {};
    _appendNetworkEvent(tabId, {
      phase: "request",
      request_id: params.requestId,
      method: req.method || "",
      url: String(req.url || "").slice(0, 800),
      ts: Date.now(),
      headers: _redactHeaders(req.headers || {}),
      resource_type: params.type || "",
    });
  } else if (method === "Network.responseReceived") {
    const resp = params?.response || {};
    _appendNetworkEvent(tabId, {
      phase: "response",
      request_id: params.requestId,
      url: String(resp.url || "").slice(0, 800),
      ts: Date.now(),
      status: resp.status,
      status_text: resp.statusText,
      mime_type: resp.mimeType,
      headers: _redactHeaders(resp.headers || {}),
    });
  }
});

async function _ensureNetworkEnabled(tabId) {
  if (_networkEnabledTabs.has(tabId)) return true;
  try {
    await _ensureDebuggerAttached(tabId);
    await _cdpSend(tabId, "Network.enable", {});
    _networkEnabledTabs.add(tabId);
    return true;
  } catch (_err) {
    return false;
  }
}

const _aiTabAlerted = new Map();
const AI_WAKE_RELAY = "http://127.0.0.1:8765/wake";

// Narrow watchlist — only fire for AIs in the active work loop.
// Each entry: { name, urlPrefix, readySignal(title, prevTitle) -> bool }
// Add new entries here when adding an AI to the work loop; don't widen blindly.
const AI_WATCHLIST = [
  {
    name: "claude",
    urlPrefix: "https://claude.ai/",
    readySignal: (title) => title.includes("BC waiting"),
  },
  {
    name: "deepseek",
    urlPrefix: "https://chat.deepseek.com/",
    // DeepSeek auto-titles tabs after a reply settles — the title flips from
    // generic "DeepSeek" to a topic-based name. Treat that transition as the
    // ready signal. Less precise than claude's explicit marker but works.
    readySignal: (title, prevTitle) => {
      if (!title || title === prevTitle) return false;
      const generic = /^DeepSeek( - Chat)?$/i;
      return !generic.test(title) && title.length > 4;
    },
  },
  {
    name: "gemini",
    urlPrefix: "https://gemini.google.com/",
    // Same generic-to-topic flip pattern as DeepSeek. Gemini's idle tab is
    // "Gemini" (and sometimes "Gemini Apps"); after a reply lands, the title
    // becomes a topic-based summary of the user's first message.
    readySignal: (title, prevTitle) => {
      if (!title || title === prevTitle) return false;
      const generic = /^(Gemini|Gemini Apps|Gemini Advanced)$/i;
      return !generic.test(title) && title.length > 4;
    },
  },
  {
    name: "aistudio",
    urlPrefix: "https://aistudio.google.com/",
    // AI Studio's idle title is "Google AI Studio" — flips to a content
    // summary after the model responds.
    readySignal: (title, prevTitle) => {
      if (!title || title === prevTitle) return false;
      const generic = /^Google AI Studio$/i;
      return !generic.test(title) && title.length > 4;
    },
  },
  {
    name: "chatgpt",
    urlPrefix: "https://chatgpt.com/",
    // ChatGPT updates the title to a topic summary after the first reply.
    // Idle: "ChatGPT" or "New chat".
    readySignal: (title, prevTitle) => {
      if (!title || title === prevTitle) return false;
      const generic = /^(ChatGPT|New chat)$/i;
      return !generic.test(title) && title.length > 4;
    },
  },
];

const _aiTabPrevTitle = new Map();

chrome.tabs.onRemoved.addListener((tabId) => {
  _attachedTabs.delete(tabId);
  _networkBuffers.delete(tabId);
  _networkEnabledTabs.delete(tabId);
  _snapshotRefMaps.delete(tabId);
  _aiTabAlerted.delete(tabId);
  _aiTabPrevTitle.delete(tabId);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.title && !changeInfo.url) return;
  const url = tab?.url || "";
  const entry = AI_WATCHLIST.find((e) => url.startsWith(e.urlPrefix));
  if (!entry) {
    _aiTabAlerted.delete(tabId);
    _aiTabPrevTitle.delete(tabId);
    return;
  }
  const title = tab?.title || changeInfo.title || "";
  const prevTitle = _aiTabPrevTitle.get(tabId) || "";
  const isReady = entry.readySignal(title, prevTitle);
  const wasAlerted = _aiTabAlerted.get(tabId) === true;
  if (isReady && !wasAlerted) {
    _aiTabAlerted.set(tabId, true);
    fetch(AI_WAKE_RELAY, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        tabId,
        ai: entry.name,
        ts: new Date().toISOString(),
      }),
      keepalive: true,
    }).catch(() => {});
  } else if (!isReady && wasAlerted) {
    _aiTabAlerted.set(tabId, false);
  }
  _aiTabPrevTitle.set(tabId, title);
});

function _truncateNodeName(name, limit = 120) {
  if (!name) return "";
  const collapsed = String(name).replace(/\s+/g, " ").trim();
  return collapsed.length > limit ? `${collapsed.slice(0, limit - 1)}…` : collapsed;
}

function _axNodeRole(node) {
  return node?.role?.value || "";
}

function _axNodeName(node) {
  return _truncateNodeName(node?.name?.value || "");
}

function _axNodeValue(node) {
  const v = node?.value?.value;
  return v === undefined || v === null ? "" : _truncateNodeName(String(v), 240);
}

function _axNodeState(node) {
  const state = {};
  const props = Array.isArray(node?.properties) ? node.properties : [];
  for (const p of props) {
    if (!p || !p.name) continue;
    const v = p.value?.value;
    switch (p.name) {
      case "disabled":
      case "checked":
      case "expanded":
      case "selected":
      case "focused":
      case "invalid":
      case "required":
        if (v === true || v === "true" || v === "mixed") state[p.name] = v === "mixed" ? "mixed" : true;
        break;
      case "current":
        if (v && v !== "false") state.current = String(v);
        break;
      default:
        break;
    }
  }
  return state;
}

async function _resolveBackendToSelector(tabId, backendNodeId) {
  if (!backendNodeId) return "";
  try {
    const res = await _cdpSend(tabId, "DOM.describeNode", { backendNodeId, depth: 0 });
    const n = res?.node || {};
    const attrs = {};
    const list = Array.isArray(n.attributes) ? n.attributes : [];
    for (let i = 0; i + 1 < list.length; i += 2) attrs[list[i]] = list[i + 1];
    const tag = String(n.localName || n.nodeName || "").toLowerCase();
    if (!tag) return "";
    if (attrs["data-testid"]) return `${tag}[data-testid="${_cssEscape(attrs["data-testid"])}"]`;
    if (attrs["data-test"]) return `${tag}[data-test="${_cssEscape(attrs["data-test"])}"]`;
    if (attrs.id) return `#${_cssEscape(attrs.id)}`;
    if (attrs.name) return `${tag}[name="${_cssEscape(attrs.name)}"]`;
    if (attrs["aria-label"]) return `${tag}[aria-label="${_cssEscape(attrs["aria-label"])}"]`;
    return tag;
  } catch (_err) {
    return "";
  }
}

function _cssEscape(value) {
  return String(value || "").replace(/["\\]/g, "\\$&");
}

function _bucketForRole(role) {
  if (role === "heading") return "headings";
  if (role === "link") return "links";
  if (role === "button") return "buttons";
  if (["textbox", "combobox", "checkbox", "radio", "searchbox", "spinbutton", "slider", "switch"].includes(role)) return "inputs";
  if (["navigation", "main", "complementary", "banner", "contentinfo", "search", "region", "form"].includes(role)) return "landmarks";
  if (role === "dialog" || role === "alertdialog") return "dialogs";
  if (role === "list" || role === "grid" || role === "tree" || role === "table") return "lists";
  if (role === "row" || role === "gridcell" || role === "treeitem" || role === "listitem") return "rows";
  return "other";
}

async function _walkAxNodes(tabId, nodes, refMap, refSeq, page, byteBudget) {
  const byId = new Map();
  for (const n of nodes) byId.set(n.nodeId, n);

  const result = {
    headings: [],
    landmarks: [],
    buttons: [],
    links: [],
    inputs: [],
    dialogs: [],
    lists: [],
    file_folder_rows: [],
    iframes: [],
    truncation: { dropped_nodes: 0, reason: null }
  };

  // Iframes — record only metadata (cross-origin default per CROSS_ORIGIN_IFRAME_STRATEGY).
  // Same-origin walk is Phase 1.5; we still record the node so the model knows the frame exists.
  const ROLES_OF_INTEREST = new Set([
    "heading", "link", "button", "textbox", "combobox", "checkbox", "radio",
    "searchbox", "spinbutton", "slider", "switch",
    "navigation", "main", "complementary", "banner", "contentinfo", "search", "region", "form",
    "dialog", "alertdialog", "list", "grid", "tree", "table",
    "row", "gridcell", "treeitem", "listitem"
  ]);

  for (const n of nodes) {
    const role = _axNodeRole(n);
    if (!ROLES_OF_INTEREST.has(role)) continue;
    if (n.ignored) continue;
    const name = _axNodeName(n);
    if (!name && !["textbox", "combobox", "checkbox", "radio", "searchbox"].includes(role)) continue;

    refSeq.value += 1;
    const ref = `r-${refSeq.value}`;
    const backendNodeId = n.backendDOMNodeId || 0;
    const selector = await _resolveBackendToSelector(tabId, backendNodeId);
    refMap[ref] = { backendNodeId, selector };

    const node = { role, name, ref };
    const state = _axNodeState(n);
    if (Object.keys(state).length) node.state = state;
    const value = _axNodeValue(n);
    if (value) node.value = value;
    if (selector) node.selector = selector;
    if (role === "heading") {
      const levelProp = (n.properties || []).find((p) => p?.name === "level");
      const lvl = levelProp?.value?.value;
      if (lvl) node.level = Number(lvl);
    }

    const bucket = _bucketForRole(role);
    if (bucket === "headings") result.headings.push(node);
    else if (bucket === "landmarks") result.landmarks.push(node);
    else if (bucket === "buttons") result.buttons.push(node);
    else if (bucket === "links") result.links.push(node);
    else if (bucket === "inputs") result.inputs.push(node);
    else if (bucket === "dialogs") result.dialogs.push(node);
    else if (bucket === "lists") result.lists.push(node);
    else if (bucket === "rows") {
      // Drive-style file/folder rows surface here when they carry role=row + aria-label.
      const kind = /folder/i.test(name) ? "folder" : "file";
      result.file_folder_rows.push({ ref, kind, name, selector, role, state });
    }
  }

  return result;
}

function _serializedBytes(obj) {
  try {
    return new TextEncoder().encode(JSON.stringify(obj)).byteLength;
  } catch (_err) {
    return 0;
  }
}

function _enforceByteCap(snapshot) {
  // Truncation order: drop list rows past 50 → buttons/links past 60 each →
  // landmarks past 12 → headings past 30. Tree-shape preservation is more
  // important than completeness; the model can BROWSER_OBSERVE again with
  // narrower scope if it needs more.
  const before = _serializedBytes(snapshot);
  if (before <= PAGE_TREE_BYTE_CAP) {
    snapshot.truncation = { ...(snapshot.truncation || {}), bytes_before: before, bytes_after: before };
    return snapshot;
  }
  let dropped = 0;
  const trim = (arr, max) => {
    if (Array.isArray(arr) && arr.length > max) {
      dropped += arr.length - max;
      arr.length = max;
    }
  };
  trim(snapshot.file_folder_rows, 50);
  trim(snapshot.lists, 20);
  trim(snapshot.buttons, 60);
  trim(snapshot.links, 60);
  trim(snapshot.inputs, 60);
  trim(snapshot.landmarks, 12);
  trim(snapshot.headings, 30);
  const after = _serializedBytes(snapshot);
  snapshot.truncation = {
    ...(snapshot.truncation || {}),
    reason: "byte_cap",
    dropped_nodes: dropped,
    bytes_before: before,
    bytes_after: after
  };
  return snapshot;
}

async function buildAxSnapshot(tabId) {
  await _ensureDebuggerAttached(tabId);
  await _cdpSend(tabId, "DOM.enable", {});
  await _cdpSend(tabId, "Accessibility.enable", {});
  const axResult = await _cdpSend(tabId, "Accessibility.getFullAXTree", {});
  const nodes = Array.isArray(axResult?.nodes) ? axResult.nodes : [];

  const refMap = {};
  const refSeq = { value: 0 };
  const buckets = await _walkAxNodes(tabId, nodes, refMap, refSeq, null, PAGE_TREE_BYTE_CAP);

  let url = "";
  let title = "";
  try {
    const tab = await new Promise((resolve) => chrome.tabs.get(tabId, (t) => resolve(t || null)));
    url = tab?.url || "";
    title = tab?.title || "";
  } catch (_err) { /* tabId may have closed */ }

  const snapshot = {
    url,
    title,
    source: "ax_tree",
    headings: buckets.headings,
    landmarks: buckets.landmarks,
    buttons: buckets.buttons,
    links: buckets.links,
    inputs: buckets.inputs,
    dialogs: buckets.dialogs,
    lists: buckets.lists,
    file_folder_rows: buckets.file_folder_rows,
    iframes: buckets.iframes,
    truncation: buckets.truncation
  };

  _enforceByteCap(snapshot);
  _snapshotRefMaps.set(tabId, refMap);
  return { snapshot, refMap };
}

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await storageGet(Object.keys(DEFAULTS));
  const next = {};
  for (const [key, value] of Object.entries(DEFAULTS)) {
    if (stored[key] === undefined) next[key] = value;
  }
  if (!stored.sessionId) next.sessionId = `sensei-${crypto.randomUUID()}`;
  if (Object.keys(next).length) await storageSet(next);

  if (chrome.sidePanel?.setPanelBehavior) {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  }
  await restoreSchedules();
});

chrome.runtime.onStartup?.addListener(() => {
  restoreSchedules().catch(() => {});
});

chrome.alarms?.onAlarm.addListener((alarm) => {
  const name = String(alarm?.name || "");
  if (!name.startsWith(SCHEDULE_ALARM_PREFIX)) return;
  const scheduleId = name.slice(SCHEDULE_ALARM_PREFIX.length);
  runScheduledWorkflow(scheduleId).catch(() => {});
});

chrome.action.onClicked.addListener(async (tab) => {
  if (!chrome.sidePanel?.open || !tab?.id) return;
  try {
    await chrome.sidePanel.open({ tabId: tab.id });
  } catch (_err) {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "SENSEI_ACTIVE_TAB") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      sendResponse({ tab: tabs?.[0] || null });
    });
    return true;
  }

  if (message?.type === "SENSEI_BUILD_AX_SNAPSHOT") {
    const requestedTabId = Number.isInteger(message.tabId) ? message.tabId : null;
    const resolveTab = requestedTabId !== null
      ? Promise.resolve(requestedTabId)
      : new Promise((resolve) => chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs?.[0]?.id ?? null)));
    resolveTab
      .then((tabId) => {
        if (!Number.isInteger(tabId)) {
          sendResponse({ ok: false, error: "no active tab to snapshot" });
          return;
        }
        return buildAxSnapshot(tabId).then((result) => {
          sendResponse({ ok: true, ...result });
        });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err?.message || err) });
      });
    return true;
  }

  if (message?.type === "SENSEI_RESOLVE_REF") {
    const tabId = Number.isInteger(message.tabId) ? message.tabId : null;
    const ref = String(message.ref || "");
    if (tabId === null || !ref) {
      sendResponse({ ok: false, error: "tabId and ref required" });
      return false;
    }
    const map = _snapshotRefMaps.get(tabId) || {};
    const hit = map[ref];
    sendResponse(hit ? { ok: true, ...hit } : { ok: false, error: `unknown ref ${ref}` });
    return false;
  }

  // Wave 1.4 (2026-05-17 PM) — file-input upload via CDP. Content script
  // forwards BROWSER_UPLOAD_FILE actions here because chrome.debugger only
  // works from the service worker context. Two-step CDP: Runtime.evaluate to
  // resolve the selector → objectId, then DOM.setFileInputFiles to push the
  // file by absolute path. After the set, dispatch input+change events so
  // page-side handlers react. Returns the post-set files.length as proof.
  if (message?.type === "SENSEI_UPLOAD_FILE") {
    // tabId comes either explicitly (side_panel.js calling directly) OR
    // implicitly from the sender (content_script.js forwarding a directive).
    const tabId = Number.isInteger(message.tabId)
      ? message.tabId
      : (Number.isInteger(_sender?.tab?.id) ? _sender.tab.id : null);
    const selector = String(message.selector || "").trim();
    const absolutePath = String(message.path || "").trim();
    if (tabId === null || !selector || !absolutePath) {
      sendResponse({ ok: false, error: "tabId, selector, and path required" });
      return false;
    }
    if (!absolutePath.startsWith("/") && !absolutePath.startsWith("~")) {
      sendResponse({ ok: false, error: `path must be absolute (got ${absolutePath})` });
      return false;
    }
    (async () => {
      try {
        await _ensureDebuggerAttached(tabId);
        // Resolve selector → objectId via Runtime.evaluate. JSON.stringify
        // gives us a safely-quoted JS string literal for the selector.
        const evalResult = await _cdpSend(tabId, "Runtime.evaluate", {
          expression: `document.querySelector(${JSON.stringify(selector)})`,
          returnByValue: false,
        });
        const objectId = evalResult?.result?.objectId;
        if (!objectId) {
          sendResponse({ ok: false, error: `element not found: ${selector}` });
          return;
        }
        // Push the file. CDP accepts an array of absolute paths; the browser
        // reads the file from disk and treats it as a user-selected file.
        await _cdpSend(tabId, "DOM.setFileInputFiles", {
          objectId,
          files: [absolutePath],
        });
        // Dispatch input + change events so page-side validators / framework
        // listeners (React onChange, Vue v-on:change, vanilla form-validators)
        // see the file land. CDP setFileInputFiles does NOT auto-fire these.
        const verifyExpr = `(() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return { ok: false, error: "selector resolved during set but missing during verify" };
          try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch (_e) {}
          try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch (_e) {}
          const f = el.files && el.files[0];
          return {
            ok: true,
            files_length: (el.files && el.files.length) || 0,
            file_name: f ? f.name : "",
            file_size: f ? f.size : 0,
            file_type: f ? (f.type || "") : "",
          };
        })()`;
        const verifyResult = await _cdpSend(tabId, "Runtime.evaluate", {
          expression: verifyExpr,
          returnByValue: true,
        });
        const value = verifyResult?.result?.value;
        if (!value || value.ok !== true) {
          sendResponse({ ok: false, error: (value && value.error) || "verify step failed" });
          return;
        }
        sendResponse({
          ok: true,
          selector,
          path: absolutePath,
          files_length: value.files_length,
          file_name: value.file_name,
          file_size: value.file_size,
          file_type: value.file_type,
        });
      } catch (err) {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
    })();
    return true;
  }

  // Phase 5.3 — return the captured Network ring buffer for a tab. Filter is
  // "all" (default), "xhr" / "fetch", or "subresource" (everything else).
  if (message?.type === "SENSEI_READ_NETWORK_EVENTS") {
    const tabId = Number.isInteger(message.tabId) ? message.tabId : null;
    if (tabId === null) {
      sendResponse({ ok: false, error: "tabId required" });
      return false;
    }
    _ensureNetworkEnabled(tabId)
      .then((enabled) => {
        if (!enabled) {
          sendResponse({ ok: false, error: "Network domain could not be enabled (debugger may be in use)" });
          return;
        }
        const filter = String(message.filter || "all").toLowerCase();
        const all = _networkBuffers.get(tabId) || [];
        let events = all;
        if (filter && filter !== "all") {
          events = all.filter((e) => {
            const rt = String(e?.resource_type || "").toLowerCase();
            if (filter === "xhr") return rt === "xhr";
            if (filter === "fetch") return rt === "fetch";
            if (filter === "subresource") return !["xhr", "fetch", "document"].includes(rt);
            return true;
          });
        }
        sendResponse({ ok: true, count: events.length, events });
      })
      .catch((err) => sendResponse({ ok: false, error: String(err?.message || err) }));
    return true;
  }

  if (message?.type === "SENSEI_CAPTURE_VISIBLE_TAB") {
    const capture = (windowId) => chrome.tabs.captureVisibleTab(windowId, { format: "png" }, (dataUrl) => {
      const err = chrome.runtime.lastError;
      if (err) {
        sendResponse({ ok: false, error: err.message || String(err) });
        return;
      }
      sendResponse({ ok: true, dataUrl });
    });

    if (Number.isInteger(message.windowId)) {
      capture(message.windowId);
      return true;
    }

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs?.[0] || null;
      if (!Number.isInteger(tab?.windowId)) {
        sendResponse({ ok: false, error: "no active browser window available for screenshot" });
        return;
      }
      capture(tab.windowId);
    });
    return true;
  }

  if (message?.type === "SENSEI_NATIVE_PING") {
    sendNativeMessage({ type: "ping", id: crypto.randomUUID() })
      .then(sendResponse);
    return true;
  }

  if (message?.type === "SENSEI_NATIVE_TOOL_REQUEST") {
    sendNativeMessage({
      type: "tool_request",
      id: message.id || crypto.randomUUID(),
      token: message.token || "",
      payload: message.payload || {}
    }).then(sendResponse);
    return true;
  }

  if (message?.type === "SENSEI_SAVE_SCHEDULE") {
    saveSchedule(message.schedule || {}).then(sendResponse);
    return true;
  }

  if (message?.type === "SENSEI_DELETE_SCHEDULE") {
    deleteSchedule(String(message.id || "")).then(sendResponse);
    return true;
  }

  if (message?.type === "SENSEI_RUN_SHORTCUT") {
    executeWorkflowShortcut(message.shortcut || {}, message.params || {}, { manual: true })
      .then(sendResponse);
    return true;
  }

  return false;
});
