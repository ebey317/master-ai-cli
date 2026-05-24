---
name: mcp-browser-must-be-visible
description: "When operating via any browser MCP (extension, sensei, secretary), if you cannot SEE the page content (screenshot broken, JS eval failing, read truncated), STOP. Do not retry blind. Switch to operator-driven action or a different tool. No more 30-minute click-loops on invisible modals."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

**Rule:** When operating via browser MCP (claude-in-chrome extension, sensei, secretary), the page content MUST be visible to me through at least one channel — screenshot, JS eval result, or full untruncated `read`. If ALL of those fail, STOP. Do not retry the same click pattern hoping the next read shows the modal. It won't.

**Why:** 2026-05-23 — I burned 30 minutes on MEGA OTT dashboard clicking the same "How to earn gift credit" button repeatedly:
- screenshot endpoint returned "BROWSER_SCREENSHOT must be handled by background"
- js_eval returned "failure" on every input including `1+1`
- `read` truncated the interactive_elements list before reaching the modal content
- I had no way to know if the modal opened, what was in it, or whether subsequent clicks were on the same button or the new modal's elements

Earlier the same session it cost ~10 minutes on a Drive sweep with the same broken-sensei-JS pattern.

**How to apply (the new rule, in order):**

1. **First tool call on a new page** — confirm at least ONE channel works:
   - `read` returns >2000 chars of distinct content (not truncated at element 2)
   - `js_eval` returns a result other than `"failure"` on a trivial expression like `1+1`
   - `screenshot` returns an image (not the "must be handled by background" error)

2. **If at least one channel works** — proceed with browser automation.

3. **If ALL channels are broken** — STOP. Switch to one of:
   - Ask operator what they see on the page and walk them through the action
   - Use shell tools (curl, rclone, gh CLI) to hit the underlying API directly
   - Direct the operator to do the UI action themselves (they see the screen; I don't)

4. **Hard cap: 3 retry attempts on the same approach.** After 3 failed attempts to read/click/eval, do NOT make a 4th. Switch strategy.

5. **Don't repeat a click that didn't change visible state.** If `read` shows the same content after a click as before, the click either didn't land or its result isn't observable. More clicks won't fix that.

**Operator's exact words 2026-05-23:**
"set max failed attempts to 4 then rethink"
"if you are in MCP using the extension, sensei or secretary, it must be visible"

**Related limits already known** (see also [[elijah-asset-index]]):
- sensei screenshot bridge bug returns "BROWSER_SCREENSHOT must be handled by background" — affects whole sessions
- sensei js_eval can fail wholesale on some pages (Drive, MEGA OTT observed) — `1+1` returns "failure"
- sensei `read` truncates interactive_elements at ~200 chars, useless for inspecting modals

**The mental model:** Operator IS the browser. I'm the hands. If the operator can't show me the screen via my tools, the operator needs to be the eyes too — not my blind hands clicking randomly.

**Related — the inverse rule:** see [[operator-must-see-authenticated-actions]] (locked 2026-05-23). This memory is about *I* must see what I'm driving. The inverse memory is about *he* must see what I'm doing on his accounts. Both apply simultaneously.
