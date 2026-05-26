# Plan: Wire Sensei Extension to Industry Standard

## Context

The sensei Chrome extension has a solid CDP/automation core but is incomplete in three critical ways:
1. **6 directives emitted by the MCP server are never handled** by the extension — they go in the queue and silently fail
2. **No visual indicator** on tabs the agent controls — operator can't tell which tabs Claude is driving
3. **Tab groups not applied to MCP actions** — `tabGroups` permission is declared and the code exists but only fires for scheduled workflows, never for operator-triggered actions

This plan wires every directive end-to-end, adds the glowing MCP border, and groups MCP tabs so the operator always knows what Claude is touching.

---

## Gap Summary (from code audit)

### Directive gaps — emitted by MCP, never handled in extension

| Directive | MCP Tool | Handler exists? | Where it needs to go |
|-----------|----------|-----------------|----------------------|
| `BROWSER_CONSOLE` | `console_logs` | ❌ | `content_script.js` (reads captured log buffer) |
| `BROWSER_NETWORK` | `network_requests` | ❌ | `service_worker.js` (reads `_networkBuffers`) |
| `BROWSER_TAB_LIST` | `tab_list` | ❌ | `service_worker.js` (calls `chrome.tabs.query`) |
| `BROWSER_TAB_CREATE` | `tab_create` | ❌ | `service_worker.js` (calls `chrome.tabs.create`) |
| `BROWSER_TAB_CLOSE` | `tab_close` | ❌ | `service_worker.js` (`BROWSER_CLOSE_TAB` is handled, `BROWSER_TAB_CLOSE` is not — MCP emits the wrong name) |
| `BROWSER_RESIZE_WINDOW` | `resize_window` | ❌ | `service_worker.js` (calls `chrome.windows.update`) |

### Visual gaps
- No CSS overlay/border injected on MCP-controlled tabs
- No `chrome.action.setBadgeText` on controlled tabs
- Tab groups only on scheduled workflows — not on `BROWSER_NAV`, `BROWSER_CLICK`, etc.

---

## What Gets Built

### Fix 1 — `service_worker.js`: Add 5 missing action handlers

The service_worker processes actions arriving from the side panel. Add these cases to the existing action dispatch block:

**BROWSER_NETWORK** (~20 lines):
```js
case "BROWSER_NETWORK": {
  const buf = _networkBuffers.get(tabId) || [];
  const filter = action.target || "all";
  const filtered = filter === "all" ? buf
    : buf.filter(e => e.resource_type === filter || e.phase === filter);
  resolve({ ok: true, result: JSON.stringify(filtered.slice(-50)) });
  break;
}
```
Reads from the existing `_networkBuffers` ring buffer (already populated by CDP `Network.*` events).

**BROWSER_TAB_LIST** (~10 lines):
```js
case "BROWSER_TAB_LIST": {
  const tabs = await chrome.tabs.query({});
  const out = tabs.map(t => ({ id: t.id, title: t.title, url: t.url, active: t.active }));
  resolve({ ok: true, result: JSON.stringify(out) });
  break;
}
```

**BROWSER_TAB_CREATE** (~15 lines):
```js
case "BROWSER_TAB_CREATE": {
  const tab = await chrome.tabs.create({ url: action.target || "about:blank" });
  // Group it as MCP tab (see Fix 3)
  await _tagMcpTab(tab.id);
  resolve({ ok: true, result: JSON.stringify({ id: tab.id, url: tab.url }) });
  break;
}
```

**BROWSER_TAB_CLOSE** (~8 lines) — also add alias so both `BROWSER_CLOSE_TAB` and `BROWSER_TAB_CLOSE` work:
```js
case "BROWSER_TAB_CLOSE":
case "BROWSER_CLOSE_TAB": {
  await chrome.tabs.remove(Number(action.target));
  resolve({ ok: true, result: "closed" });
  break;
}
```

**BROWSER_RESIZE_WINDOW** (~10 lines):
```js
case "BROWSER_RESIZE_WINDOW": {
  const [w, h] = (action.target || "1280x720").split("x").map(Number);
  const win = await chrome.windows.getCurrent();
  await chrome.windows.update(win.id, { width: w, height: h });
  resolve({ ok: true, result: `resized to ${w}x${h}` });
  break;
}
```

---

### Fix 2 — `content_script.js`: Add BROWSER_CONSOLE handler

Content script already captures `console.error` and `console.warn` into a local buffer. Add:
- A `_consoleLog` array (ring buffer, cap 200)
- Hook `console.log`, `console.info`, `console.warn`, `console.error` into it
- Handle `BROWSER_CONSOLE` action: return filtered slice

```js
// Near top of content_script.js, after existing console hooks
const _consoleLog = [];
const _CONSOLE_CAP = 200;
["log","info","warn","error","debug"].forEach(level => {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    _consoleLog.push({ level, msg: args.map(String).join(" "), ts: Date.now() });
    if (_consoleLog.length > _CONSOLE_CAP) _consoleLog.shift();
    orig(...args);
  };
});

// In action switch block:
case "BROWSER_CONSOLE": {
  const pattern = action.target && action.target !== "all" ? action.target : null;
  const out = pattern
    ? _consoleLog.filter(e => e.msg.includes(pattern) || e.level === pattern)
    : _consoleLog.slice(-100);
  return { ok: true, result: JSON.stringify(out) };
}
```

---

### Fix 3 — `service_worker.js`: `_tagMcpTab()` helper + tab group on every MCP nav

Add a helper that:
1. Groups the tab into the "MCP" tab group (orange, titled "MCP")
2. Stores the tab ID in `_mcpTabs` Set so we know which tabs we own

```js
const _mcpTabs = new Set();
let _mcpGroupId = null;

async function _tagMcpTab(tabId) {
  _mcpTabs.add(tabId);
  try {
    if (_mcpGroupId !== null) {
      // add to existing group
      await chrome.tabs.group({ tabIds: [tabId], groupId: _mcpGroupId });
    } else {
      _mcpGroupId = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(_mcpGroupId, { title: "MCP", color: "orange" });
    }
  } catch (_) { /* tab groups not supported in this Chrome version */ }
}
```

Call `_tagMcpTab(tabId)` at the start of `BROWSER_NAV` handling so every navigation Claude makes gets grouped immediately.

---

### Fix 4 — `content_script.js`: MCP visual indicator (glowing border)

Inject a thin glowing border on the page when Claude is actively driving it. Toggle on/off via message.

**CSS injected once on load:**
```js
const _mcpStyle = document.createElement("style");
_mcpStyle.id = "sensei-mcp-indicator";
_mcpStyle.textContent = `
  body.sensei-mcp-active::after {
    content: "";
    position: fixed;
    inset: 0;
    border: 3px solid #ff6b2b;
    pointer-events: none;
    z-index: 2147483647;
    box-shadow: inset 0 0 12px rgba(255,107,43,0.4);
  }
`;
document.head.appendChild(_mcpStyle);
```

**Activate on first action, deactivate on `BROWSER_DONE` or idle timeout (30s):**
```js
// In action handler — before dispatching any action:
document.body.classList.add("sensei-mcp-active");
clearTimeout(_mcpIdleTimer);
_mcpIdleTimer = setTimeout(() => document.body.classList.remove("sensei-mcp-active"), 30000);

// On BROWSER_DONE or explicit deactivate message:
document.body.classList.remove("sensei-mcp-active");
```

This gives operator the glowing orange border whenever Claude is actively operating the tab. Fades after 30s of inactivity.

---

### Fix 5 — `service_worker.js`: Extension badge on active MCP tabs

```js
// In _tagMcpTab(), after grouping:
chrome.action.setBadgeText({ text: "MCP", tabId });
chrome.action.setBadgeBackgroundColor({ color: "#ff6b2b", tabId });

// Clear badge when tab is closed or MCP releases it:
chrome.tabs.onRemoved.addListener(tabId => {
  _mcpTabs.delete(tabId);
  if (_mcpGroupId && _mcpTabs.size === 0) _mcpGroupId = null;
});
```

---

## Files Modified

| File | Change |
|------|--------|
| `~/scripts/sensei_extension/service_worker.js` | Add 5 action handlers (BROWSER_NETWORK, BROWSER_TAB_LIST, BROWSER_TAB_CREATE, BROWSER_TAB_CLOSE alias, BROWSER_RESIZE_WINDOW) + `_tagMcpTab()` helper + badge logic |
| `~/scripts/sensei_extension/content_script.js` | Add BROWSER_CONSOLE handler + console ring buffer hooks + MCP visual indicator CSS + activate/deactivate logic |

No manifest changes needed — `tabGroups`, `debugger`, `tabs` permissions already declared.

---

## Dependency Order

```
content_script.js console buffer hooks   (no deps)
         ↓
content_script.js BROWSER_CONSOLE handler  (reads the buffer)
         ↓
service_worker.js _tagMcpTab() helper      (no deps)
         ↓
service_worker.js BROWSER_TAB_CREATE       (calls _tagMcpTab)
service_worker.js BROWSER_NAV (modify)     (calls _tagMcpTab)
service_worker.js BROWSER_NETWORK          (reads _networkBuffers)
service_worker.js BROWSER_TAB_LIST         (calls chrome.tabs.query)
service_worker.js BROWSER_TAB_CLOSE alias  (no deps)
service_worker.js BROWSER_RESIZE_WINDOW    (calls chrome.windows.update)
         ↓
content_script.js MCP visual indicator    (no deps, activated by any action)
service_worker.js badge logic             (calls chrome.action.setBadgeText)
```

---

## Verification (end-to-end)

After implementation, reload the extension in Chrome (`chrome://extensions` → Reload), then test each:

1. **BROWSER_CONSOLE** — call `mcp__sensei__console_logs()` → should return JSON array of console entries, not `{ok: false}`
2. **BROWSER_NETWORK** — call `mcp__sensei__network_requests()` after navigating a page → should return request/response log
3. **BROWSER_TAB_LIST** — call `mcp__sensei__tab_list()` → should return array of open tabs with id/title/url
4. **BROWSER_TAB_CREATE** — call `mcp__sensei__tab_create(url)` → new tab opens AND appears in orange "MCP" group
5. **BROWSER_TAB_CLOSE** — call `mcp__sensei__tab_close(id)` → tab closes
6. **BROWSER_RESIZE_WINDOW** — call `mcp__sensei__resize_window(1280, 720)` → window resizes
7. **Visual indicator** — call `mcp__sensei__browse("https://google.com")` → orange glowing border appears on the page
8. **Tab group** — any `mcp__sensei__browse()` call → tab appears in orange "MCP" group in Chrome tab bar
9. **Badge** — any MCP-controlled tab shows "MCP" badge on the extension icon

---

## Industry Standard Baseline Met

| Capability | Playwright | Selenium | Puppeteer | Sensei (after) |
|-----------|-----------|----------|-----------|----------------|
| Navigate | ✓ | ✓ | ✓ | ✓ |
| Click/Fill | ✓ | ✓ | ✓ | ✓ |
| Screenshot | ✓ | ✓ | ✓ | ✓ |
| Console logs | ✓ | ✓ | ✓ | ✓ (Fix 2) |
| Network logs | ✓ | ✓ | ✓ | ✓ (Fix 1) |
| Tab management | ✓ | ✓ | ✓ | ✓ (Fix 1) |
| Window resize | ✓ | ✓ | ✓ | ✓ (Fix 1) |
| Visual indicator | ✗ | ✗ | ✗ | ✓ (Fix 4 — exceeds standard) |
| Tab grouping | ✗ | ✗ | ✗ | ✓ (Fix 3 — exceeds standard) |
