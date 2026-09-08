#!/usr/bin/env node
// 2026-09-05 — unit tests for stopWorkflowRecordingInTab() in side_panel.js.
//
// Regression under test: stopping a workflow recording used to resolve the tab
// via activeTab() -> chrome.tabs.query({active:true,currentWindow:true}), a raw
// OS-level focus query. A recording is pinned to the tab RECORD_START ran in,
// so if focus moved during the recording (the user switching tabs, or an
// unrelated concurrent agent taking focus — the same mechanism behind the
// task-tab bug in test_session_tab.js), SENSEI_RECORD_STOP went to the wrong
// tab: it answers with no steps or has no content script at all, the recording
// is lost, and the tab that IS recording never gets told to stop.
//
// The START path is deliberately NOT covered here — it legitimately follows
// live focus ("record what I'm looking at").
//
// Same extraction style as test_tab_group.js / test_session_tab.js: pull the
// helper out of side_panel.js and run it in a vm with stubbed chrome/state, so
// the test reads the shipping source rather than a copy of it.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sidePanelPath = path.resolve(__dirname, "..", "side_panel.js");
const src = fs.readFileSync(sidePanelPath, "utf8");
const start = src.indexOf("async function stopWorkflowRecordingInTab");
const end = src.indexOf("async function toggleWorkflowRecording");
if (start < 0 || end < 0 || end <= start) {
  console.error("FAIL: could not locate stopWorkflowRecordingInTab");
  process.exit(1);
}
const chunk = src.slice(start, end) + "\nthis.stopRecording = stopWorkflowRecordingInTab;\n";

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

const RECORDING_TAB = 11;   // where RECORD_START ran
const FOREGROUND_TAB = 99;  // whatever holds focus by the time the user hits Stop

// `replies` maps tab id -> what SENSEI_RECORD_STOP resolves/rejects with there.
// Only the recording tab has a live recorder; anything else behaves like a tab
// with no content script listening.
// NOTE: `recordingTabId` is read with `in` rather than a default parameter, so
// that an explicit `undefined` stays undefined instead of silently defaulting.
function makeCtx(opts = {}) {
  const { replies = {} } = opts;
  const recordingTabId = "recordingTabId" in opts ? opts.recordingTabId : RECORDING_TAB;
  const ctx = {
    state: { workflowRecording: { active: true, tabId: recordingTabId } },
    messagedTabs: [],
    chrome: {
      tabs: {
        sendMessage: async (id, msg) => {
          ctx.messagedTabs.push(id);
          eq(msg.type, "SENSEI_RECORD_STOP", "(wrong message type)");
          const reply = replies[id];
          if (reply instanceof Error) throw reply;
          return reply;  // may legitimately be undefined
        },
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext(chunk, ctx);
  return ctx;
}

const GOOD_STOP = { ok: true, steps: [{ kind: "click", target: "#go" }], events: [], url: "https://x/" };

(async () => {
  // THE BUG. Focus moved to tab 99 mid-recording. Pre-fix this messaged 99,
  // got nothing back, and the recording in tab 11 was lost.
  await check("stop goes to the recording tab, not the focused tab", async () => {
    const ctx = makeCtx({
      replies: {
        [RECORDING_TAB]: GOOD_STOP,
        [FOREGROUND_TAB]: new Error("Could not establish connection. Receiving end does not exist."),
      },
    });
    const stopped = await ctx.stopRecording();
    eq(ctx.messagedTabs.join(","), String(RECORDING_TAB), "(messaged the wrong tab)");
    eq(stopped.ok, true);
    eq(stopped.steps.length, 1, "(recording was lost)");
  });

  // The recording tab keeps being the target even when it is not foregrounded
  // and some other tab would have answered successfully — a plausible-looking
  // but wrong result is worse than an honest failure.
  await check("a responsive foreground tab does not get to answer instead", async () => {
    const ctx = makeCtx({
      replies: {
        [RECORDING_TAB]: GOOD_STOP,
        [FOREGROUND_TAB]: { ok: true, steps: [{ kind: "click", target: "#wrong" }], events: [] },
      },
    });
    const stopped = await ctx.stopRecording();
    eq(ctx.messagedTabs.includes(FOREGROUND_TAB), false, "(foreground tab was messaged)");
    eq(stopped.steps[0].target, "#go", "(returned steps from the wrong tab)");
  });

  // Recording tab closed mid-recording: report it, don't crash and don't
  // silently substitute another tab.
  await check("closed recording tab surfaces the error", async () => {
    const ctx = makeCtx({
      replies: { [RECORDING_TAB]: new Error("No tab with id: 11.") },
    });
    const stopped = await ctx.stopRecording();
    eq(stopped.ok, false);
    eq(stopped.error, "No tab with id: 11.");
    eq(Array.isArray(stopped.steps), true, "(caller does Array.isArray on .steps)");
  });

  // sendMessage can resolve undefined rather than reject; the caller reads
  // stopped.ok / stopped.steps immediately, so a bare undefined would throw.
  await check("undefined reply degrades to a shaped failure", async () => {
    const ctx = makeCtx({ replies: { [RECORDING_TAB]: undefined } });
    const stopped = await ctx.stopRecording();
    eq(stopped.ok, false);
    eq(Array.isArray(stopped.steps), true);
  });

  // No tabId recorded at all — never message a tab on a guess.
  await check("missing recording tab id messages nobody", async () => {
    for (const bad of [null, undefined, 0, -1, NaN]) {
      const ctx = makeCtx({ recordingTabId: bad });
      const stopped = await ctx.stopRecording();
      eq(stopped.ok, false, `(tabId=${String(bad)})`);
      eq(ctx.messagedTabs.length, 0, `(messaged a tab on a guess for tabId=${String(bad)})`);
      eq(Array.isArray(stopped.steps), true);
    }
  });

  if (failures) {
    console.error(`---\n${failures} assertion(s) FAILED`);
    process.exit(1);
  }
  console.log("---\nall workflow-recording stop assertions PASS");
})();
