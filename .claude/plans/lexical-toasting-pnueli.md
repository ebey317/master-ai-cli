# Plan: Apply Sensei Extension Industry-Standard Wiring

## Context
User provided a complete implementation plan (in the conversation) and selected option "2" to apply it. Two edits already landed on service_worker.js before plan mode triggered. Resuming implementation.

## Work Already Done
- Added `_tagMcpTab()` helper + `_mcpTabs`/`_mcpGroupId` state to service_worker.js
- Extended `chrome.tabs.onRemoved` listener to clean up MCP state

## Remaining Edits

### service_worker.js
1. Add `_tagMcpTab(tabId)` call at top of existing `BROWSER_NAV` handler
2. Add 5 new message handlers before the final `return false`:
   - `SENSEI_BROWSER_NETWORK` — reads `_networkBuffers` ring buffer
   - `SENSEI_BROWSER_TAB_LIST` — `chrome.tabs.query({})`
   - `SENSEI_BROWSER_TAB_CREATE` — create tab + `_tagMcpTab()`
   - `SENSEI_BROWSER_TAB_CLOSE` / `BROWSER_CLOSE_TAB` alias
   - `SENSEI_BROWSER_RESIZE_WINDOW` — `chrome.windows.update()`

### content_script.js
1. Inject MCP indicator CSS (`body.sensei-mcp-active::after` orange border) on load
2. Add activate/idle-reset at top of `executeBrowserAction`
3. Add `BROWSER_CONSOLE` case (console ring buffer already exists at lines 150–183)

## Files Modified
- `~/scripts/sensei_extension/service_worker.js`
- `~/scripts/sensei_extension/content_script.js`

## Verification
After reload at chrome://extensions:
1. tab_list → JSON array with id/title/url
2. console_logs → JSON array of {level, msg, ts}
3. network_requests → JSON array of request records
4. tab_create → new tab in orange "MCP" group
5. tab_close → tab closes cleanly
6. resize_window → window resizes
7. browse() → orange border appears at page edge
8. Extension icon badge shows "MCP" on controlled tabs
