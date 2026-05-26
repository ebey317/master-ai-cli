# Plan: Sensei Extension — Ref-ID Grounding System (Phase 2)

## Context

Phase 1 (standalone hardening) is complete — all hardcoded URLs are gone, `API_CONTRACT.md` exists.

The root cause of every "target not found," false completion, and silent click failure is a single architectural gap: **the model is acting on a text description of the page, not a live, grounded map of it.** `interactiveElements()` returns strings like `1. button "Submit" selector=button[type="submit"]`. The model rephrases that into a target string. `findElement()` tries to match that string against the live DOM — and misses.

Fix: stamp every interactive element with a stable `data-pupil-id` ref at observation time, expose a `window.__pupilElementMap` lookup table, and make every action dispatch resolve through that map first. The model gets ref IDs like `p_001`, not fragile text selectors. After each action, a receipt block confirms what actually changed.

This plan covers all 6 components described in the operator's technical brief, in build order (each step depends on the previous).

---

## Step 1 — Ref-ID Map Builder (`content_script.js`)

**Target:** `interactiveElements()` at line 423; `findElement()` (text-based, multiple locations).

### What to build

1. **Stamp refs.** In `interactiveElements()`, after `collectDeep()` + `filter(isVisible)`, walk candidates and call:
   ```js
   if (!el.hasAttribute("data-pupil-id")) {
     el.setAttribute("data-pupil-id", `p_${String(++window.__pupilCounter).padStart(3,"0")}`);
   }
   ```
   `window.__pupilCounter` initialized to 0 at script load if not already set.

2. **Build the map.** After stamping, populate:
   ```js
   window.__pupilElementMap = window.__pupilElementMap || {};
   window.__pupilElementMap[ref] = { type: role, label: name, placeholder, value, el };
   ```

3. **Return structured JSON, not text.** Change the return type of `interactiveElements()` from a joined string to an array of objects:
   ```js
   return candidates.map((el) => ({
     ref: el.getAttribute("data-pupil-id"),
     type: elementRole(el),
     label: elementName(el) || "",
     placeholder: el.getAttribute("placeholder") || "",
     selector: safeSelectorFor(el),
     constraints: _extractFieldConstraints(el),
   }));
   ```
   Update `pageContext()` at line 538 — `interactive_elements` is already sent to the model; the shape change propagates automatically.

4. **Fast-path `findElement()`.** Prepend a ref-ID lookup before the existing selector/text search:
   ```js
   if (target && target.startsWith("p_") && window.__pupilElementMap?.[target]) {
     return window.__pupilElementMap[target].el;
   }
   ```
   The rest of `findElement()`'s fallback chain (CSS selector, text match, fuzzy) stays intact as a safety net.

**Files changed:** `content_script.js` (interactiveElements, findElement, pageContext call site, counter init at top of script).

---

## Step 2 — Action Receipt Reporter (`content_script.js`)

**Target:** After the `el.click()` / `setElementValue()` / `el.dispatchEvent()` in each `BROWSER_CLICK`, `BROWSER_FILL`, and `BROWSER_KEY` handler.

### What to build

After each action, compute and return an `action_receipt` object:
```js
const receipt = {
  ref: action.target,       // the ref that was resolved
  action: kind,
  url_before, url_after,    // location.href snapshot before and after
  value_after: currentElementValue(el),  // readback from filled field
  dom_added, dom_removed,   // count of interactive elements added/removed since last map
  submit_detected: Boolean(submitSignals.length >= 2),
};
```

**Implementation:**
- Before dispatch: snapshot `location.href`, count `document.querySelectorAll(ACTION_TARGETS).length`
- After dispatch + `waitForPageStable()`: recount, diff
- Attach `action_receipt` to the existing return value of each handler (alongside existing `ok`, `clicked`, `page_context` keys)

The receipt feeds back to the backend via the existing `/extension/action_result` path. No new network call needed — it rides the existing return payload.

**Files changed:** `content_script.js` (BROWSER_CLICK handler ~line 2535, BROWSER_FILL handler ~line 2676, BROWSER_KEY handler).

---

## Step 3 — Stale Map Guard (`content_script.js`)

**Target:** The existing `MutationObserver` at lines 85-86 that fires `scheduleMutationBump`.

### What to build

Extend `scheduleMutationBump` to also track element count delta and rebuild the ref map when the delta exceeds 5:

```js
let _lastInteractiveCount = 0;

const scheduleMutationBump = () => {
  clearTimeout(mutationTimer);
  mutationTimer = setTimeout(() => {
    bumpPageObservation("mutation.debounced");
    const currentCount = document.querySelectorAll(ACTION_TARGETS).length;
    if (Math.abs(currentCount - _lastInteractiveCount) > 5) {
      _lastInteractiveCount = currentCount;
      // Invalidate map — next interactiveElements() call rebuilds it
      window.__pupilElementMap = {};
      window.__pupilCounter = 0;
      // Remove all existing stamps so refs are re-assigned fresh
      document.querySelectorAll("[data-pupil-id]").forEach(
        (el) => el.removeAttribute("data-pupil-id")
      );
    }
  }, PAGE_STABLE_DEBOUNCE_MS);
};
```

The map rebuild is lazy (triggered on next `interactiveElements()` call), not eager. This avoids thrashing on rapid SPA transitions.

**Files changed:** `content_script.js` (scheduleMutationBump ~line 80, counter init).

---

## Step 4 — Iframe Bridge (manifest.json + content_script.js + service_worker.js)

**Target:** Cross-origin job-application iframes (Workday, Greenhouse, Lever, iCIMS). Currently `iframeSummaries()` marks them `cross_origin: true, unobserved_reason: "cross-origin frame; content script records metadata only"` — no interaction possible.

### What to build

**Note:** `manifest.json` has no `content_scripts` array — the content script is injected via `scripting.executeScript` in the service worker. Iframe injection follows the same pattern.

1. **`scripting.executeScript` with `allFrames: true`** (service_worker.js). When the content script is injected for a tab, also inject with `allFrames: true` so each cross-origin iframe frame gets the content script. Add to the existing injection call:
   ```js
   await chrome.scripting.executeScript({
     target: { tabId, allFrames: true },
     files: ["content_script.js"],
   });
   ```

2. **Iframe → background message relay** (content_script.js). Inside the content script, detect if running in a frame (`window !== window.top`) and install a relay:
   ```js
   if (window !== window.top) {
     window.addEventListener("message", (evt) => {
       if (evt.data?.type === "SENSEI_IFRAME_ACTION") {
         chrome.runtime.sendMessage({ type: "IFRAME_ACTION", ...evt.data });
       }
     });
   }
   ```

3. **Background worker routes iframe actions** (service_worker.js). On `chrome.runtime.onMessage`, if `msg.type === "IFRAME_ACTION"`, forward to the target frame via `chrome.tabs.sendMessage` with `frameId`.

4. **Side panel targets iframes** (side_panel.js). When `action.target` contains `iframe::p_NNN`, split on `::` and dispatch to the correct `frameId` rather than the top frame.

**Files changed:** `service_worker.js` (executeScript call, onMessage handler), `content_script.js` (frame-detection relay block), `side_panel.js` (iframe target routing).

---

## Step 5 — `upload_file` Tool (`content_script.js` + `side_panel.js`)

**Target:** `<input type="file">` elements. Currently the BROWSER_FILL handler tries `setElementValue()` on file inputs, which browsers block.

### What to build

Add a new `BROWSER_UPLOAD_FILE` action kind. Schema:
```json
{
  "kind": "BROWSER_UPLOAD_FILE",
  "target": "p_007",
  "file_path": "/home/elijah/Documents/resume.pdf"
}
```

**Handler in content_script.js:**
```js
if (kind === "BROWSER_UPLOAD_FILE") {
  const el = findElement(action.target);
  if (!el || el.type !== "file") return { ok: false, error: "not a file input" };
  // Backend reads the file via /extension/read_local_file, sends base64 back.
  // Side panel fetches from backend, creates a File object, sets via DataTransfer.
  // This handler receives the pre-fetched base64 from the side panel:
  const { filename, mime, base64 } = action;
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  const file = new File([bytes], filename, { type: mime });
  const dt = new DataTransfer();
  dt.items.add(file);
  el.files = dt.files;
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("input", { bubbles: true }));
  return { ok: true, uploaded: filename, ref: action.target };
}
```

**Side panel pre-fetch** (side_panel.js): Before dispatching a `BROWSER_UPLOAD_FILE` action, fetch the file from `${backendUrl}/extension/read_local_file`, get base64, inject `filename`/`mime`/`base64` into the action payload before sending to content_script.

**Files changed:** `content_script.js` (new BROWSER_UPLOAD_FILE handler), `side_panel.js` (pre-fetch + action injection).

---

## Step 6 — `expected_outcome` Field + Receipt Verification (`side_panel.js`)

**Target:** Action schemas and the side panel's action result handler.

### What to build

1. **Add `expected_outcome` to click/fill schemas.** Model must emit:
   ```json
   {
     "kind": "BROWSER_FILL",
     "target": "p_003",
     "value": "John Smith",
     "expected_outcome": "field value becomes 'John Smith'"
   }
   ```
   Teach the model via system prompt that `expected_outcome` is required and must be falsifiable.

2. **Verify in side panel.** After receiving `action_receipt` from the content script, compare against `expected_outcome`:
   ```js
   const receiptOk = verifyReceipt(action, receipt);
   if (!receiptOk) {
     // Surface "⚠️ Action may not have taken effect" card to the operator
     // and inject mismatch into the next round's context
   }
   ```

3. **Inject mismatches into continuation context.** If receipt doesn't match expectation, add to the next `/chat/continue` results payload:
   ```json
   { "action_id": "...", "status": "completed_with_warning",
     "result": "...", "receipt_mismatch": "expected field=John Smith, got field empty" }
   ```

**Files changed:** `side_panel.js` (receipt verification after action dispatch, mismatch card rendering, continuation payload enrichment). Backend system prompt update for `expected_outcome` teaching (out of scope for extension files, noted for `stt_server.py`).

---

## Files Modified

| File | Steps |
|------|-------|
| `content_script.js` | 1, 2, 3, 4 (frame relay), 5 |
| `service_worker.js` | 4 (allFrames inject, message routing) |
| `side_panel.js` | 4 (iframe target routing), 5 (pre-fetch), 6 |
| `manifest.json` | No change needed — scripting API handles allFrames |

## Build Order

Steps 1 → 2 → 3 can land in one commit (all in content_script.js, each builds on previous).
Steps 4 + 5 land in a second commit (multi-file, each is independently testable).
Step 6 lands last (depends on receipt data from Step 2 being stable).

## Verification

**Step 1:** Open `file:///…/test/job_app_smoke.html` in side panel. Check `window.__pupilElementMap` in DevTools console — should have 10 entries. Confirm `interactive_elements` payload in Network tab shows `ref: "p_001"` etc. instead of text strings.

**Step 2:** After any FILL action, check action_result payload in Network tab — should contain `action_receipt.value_after` matching the filled value.

**Step 3:** Navigate to a SPA route that adds a form. Check DevTools: `window.__pupilCounter` should reset, old `data-pupil-id` attributes gone.

**Step 4:** Load a Greenhouse or Lever job page. The iframe fields should appear in `interactive_elements` with `iframe::p_NNN` refs.

**Step 5:** On a form with a file input, send `BROWSER_UPLOAD_FILE` — the file input `files` property should have one entry in DevTools.

**Step 6:** Intentionally fill a wrong ref. Receipt mismatch card should appear in side panel.

**Regression:** `python3 ~/scripts/test_extension_e2e_smoke.py` must still pass after all steps.
