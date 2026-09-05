#!/usr/bin/env node
// 2026-09-05 — unit tests for the task-tab resolver in side_panel.js.
//
// Regression under test: task-scoped commands used to resolve their tab via
// activeTab() -> chrome.tabs.query({active:true,currentWindow:true}), a raw
// OS-level focus query. A concurrently-running, unrelated agent that opened a
// tab and took focus silently stole every subsequent read/extract/screenshot
// ("Frame with ID 0 is showing error page"). sessionTab() anchors to the tab
// this session created / switched to / last acted on instead.
//
// Same extraction style as test_tab_group.js: pull the helper block out of
// side_panel.js and run it in a vm with stubbed chrome/state globals, so the
// tests read the shipping source rather than a copy of it.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sidePanelPath = path.resolve(__dirname, "..", "side_panel.js");
const src = fs.readFileSync(sidePanelPath, "utf8");
const start = src.indexOf("function rememberSessionTab");
const end = src.indexOf("function canInjectIntoTab");
if (start < 0 || end < 0 || end <= start) {
  console.error("FAIL: could not locate the task-tab resolver block");
  process.exit(1);
}
const chunk = src.slice(start, end) +
  "\nthis.sessionTab = sessionTab;" +
  "\nthis.rememberSessionTab = rememberSessionTab;" +
  "\nthis.forgetSessionTab = forgetSessionTab;" +
  "\nthis.focusTabForCapture = focusTabForCapture;\n";

let failures = 0;
function check(label, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => console.log(`ok ${label}`))
    .catch((err) => { console.error(`FAIL ${label} — ${err.message}`); failures += 1; });
}
function eq(a, b, label = "") {
  if (a !== b) throw new Error(`expected ${JSON.stringify(b)} got ${JSON.stringify(a)} ${label}`);
}

// Build a fresh sandbox with a fake browser. `tabs` is id -> {id,url,active,groupId}.
// `focusedId` is what Chrome's raw active-tab query would return — the thing a
// foreign agent can move at any moment.
function makeCtx({ tabs = {}, focusedId = null, sessionTabId = null, groupId = null } = {}) {
  const ctx = {
    state: { sessionTabId, sessionTabGroup: groupId ? { groupId } : null },
    activeTabCalls: 0,
    raised: [],
    chrome: {
      tabs: {
        get: async (id) => {
          if (!tabs[id]) throw new Error("No tab with id: " + id);
          return tabs[id];
        },
        query: async ({ groupId: g }) => Object.values(tabs).filter((t) => t.groupId === g),
        update: async (id, props) => {
          ctx.raised.push(id);
          if (tabs[id]) Object.assign(tabs[id], props);
          return tabs[id];
        },
      },
      runtime: {
        sendMessage: async () => ({ tab: focusedId == null ? null : tabs[focusedId] || null }),
      },
    },
  };
  // activeTab() lives outside the extracted chunk; supply the real behaviour.
  ctx.activeTab = async () => {
    ctx.activeTabCalls += 1;
    const r = await ctx.chrome.runtime.sendMessage({ type: "SENSEI_ACTIVE_TAB" });
    return r?.tab || null;
  };
  vm.createContext(ctx);
  vm.runInContext(chunk, ctx);
  return ctx;
}

const TASK = { id: 11, url: "https://remoteok.com/", active: false, groupId: 7 };
// The real-world offender: another agent's tab at a malformed URL, holding focus.
const FOREIGN = { id: 99, url: "https://file///tmp/dashboard_preview_final.png", active: true, groupId: -1 };

(async () => {
  // THE BUG. Session owns tab 11; an unrelated tab holds focus. Pre-fix this
  // resolved to 99 and every command failed against a broken page.
  await check("focus theft does not move the task tab", async () => {
    const ctx = makeCtx({
      tabs: { 11: TASK, 99: FOREIGN }, focusedId: 99, sessionTabId: 11, groupId: 7,
    });
    const tab = await ctx.sessionTab();
    eq(tab.id, 11, "(resolved the foreign focused tab)");
    eq(ctx.activeTabCalls, 0, "(should not consult the focus query at all)");
  });

  // No anchor yet, but the session group has a tab — use it over raw focus.
  await check("falls back to the session group before raw focus", async () => {
    const ctx = makeCtx({
      tabs: { 11: TASK, 99: FOREIGN }, focusedId: 99, sessionTabId: null, groupId: 7,
    });
    eq((await ctx.sessionTab()).id, 11);
    eq(ctx.state.sessionTabId, 11, "(should record the anchor)");
  });

  // Group active tab wins over the newest when several are grouped.
  await check("prefers the session group's active tab", async () => {
    const ctx = makeCtx({
      tabs: {
        11: TASK,
        12: { id: 12, url: "https://b/", active: true, groupId: 7 },
        13: { id: 13, url: "https://c/", active: false, groupId: 7 },
      },
      focusedId: 12, sessionTabId: null, groupId: 7,
    });
    eq((await ctx.sessionTab()).id, 12);
  });

  // Pure-human session: nothing owned, no group -> byte-identical to old behaviour.
  await check("degrades to the focus query when the session owns nothing", async () => {
    const ctx = makeCtx({ tabs: { 99: FOREIGN }, focusedId: 99, sessionTabId: null, groupId: null });
    eq((await ctx.sessionTab()).id, 99);
    eq(ctx.activeTabCalls, 1);
    eq(ctx.state.sessionTabId, 99, "(adopting focus must set the anchor so it sticks next time)");
  });

  // A closed anchor must not pin us to a dead id.
  await check("dead anchor is dropped and re-resolved", async () => {
    const ctx = makeCtx({ tabs: { 99: FOREIGN }, focusedId: 99, sessionTabId: 11, groupId: null });
    eq((await ctx.sessionTab()).id, 99);
    eq(ctx.state.sessionTabId, 99);
  });

  // An explicit tab id on the action beats both anchor and focus.
  await check("explicit action tab_id wins", async () => {
    const ctx = makeCtx({
      tabs: { 11: TASK, 99: FOREIGN }, focusedId: 99, sessionTabId: 11, groupId: 7,
    });
    eq((await ctx.sessionTab({ tab_id: 99 })).id, 99);
    eq(ctx.state.sessionTabId, 99, "(explicit target re-anchors the task)");
  });

  await check("bogus explicit tab_id falls through instead of failing", async () => {
    const ctx = makeCtx({
      tabs: { 11: TASK, 99: FOREIGN }, focusedId: 99, sessionTabId: 11, groupId: 7,
    });
    eq((await ctx.sessionTab({ tab_id: 4242 })).id, 11);
  });

  // Anchor bookkeeping used by BROWSER_TAB_CREATE / _SWITCH / _CLOSE.
  await check("remember/forget anchor bookkeeping", async () => {
    const ctx = makeCtx({ tabs: {}, focusedId: null });
    ctx.rememberSessionTab(21);
    eq(ctx.state.sessionTabId, 21);
    ctx.rememberSessionTab({ id: 22 });
    eq(ctx.state.sessionTabId, 22);
    ctx.forgetSessionTab(21);
    eq(ctx.state.sessionTabId, 22, "(forgetting a different tab must not clear the anchor)");
    ctx.forgetSessionTab(22);
    eq(ctx.state.sessionTabId, null);
    ctx.rememberSessionTab(null);
    eq(ctx.state.sessionTabId, null, "(garbage must not become an anchor)");
  });

  // captureVisibleTab photographs a WINDOW, not a tab id — the task tab has to
  // be raised first or a screenshot still returns the foreign tab's pixels.
  await check("screenshot raises the task tab when it is not visible", async () => {
    const ctx = makeCtx({
      tabs: { 11: { ...TASK, active: false }, 99: FOREIGN }, focusedId: 99, sessionTabId: 11, groupId: 7,
    });
    const tab = await ctx.sessionTab();
    await ctx.focusTabForCapture(tab);
    eq(ctx.raised.join(","), "11", "(task tab was not raised before capture)");
  });

  await check("screenshot does not churn focus when already visible", async () => {
    const ctx = makeCtx({
      tabs: { 11: { ...TASK, active: true } }, focusedId: 11, sessionTabId: 11, groupId: 7,
    });
    await ctx.focusTabForCapture(await ctx.sessionTab());
    eq(ctx.raised.length, 0);
  });

  // Everything gone: no tabs at all.
  await check("returns null when there is no tab anywhere", async () => {
    const ctx = makeCtx({ tabs: {}, focusedId: null, sessionTabId: 11, groupId: 7 });
    eq(await ctx.sessionTab(), null);
  });

  if (failures) {
    console.error(`---\n${failures} assertion(s) FAILED`);
    process.exit(1);
  }
  console.log("---\nall task-tab resolver assertions PASS");
})();
