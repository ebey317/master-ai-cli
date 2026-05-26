# TOOL_ROUTING.md — Canonical Tool Routing Spec

> Single source of truth. Which tool for which action. What counts as verified. No deliberation at runtime.

---

## BROWSER ACTIONS — always `mcp__sensei__*`

| Action                  | Tool                              | Notes                              |
|-------------------------|-----------------------------------|------------------------------------|
| Navigate to URL         | `mcp__sensei__browse(url)`        |                                    |
| Click element           | `mcp__sensei__click(what)`        | label or CSS selector              |
| Fill form field         | `mcp__sensei__fill(where, text)`  | If fails → xdotool fallback        |
| Read page content       | `mcp__sensei__read()`             | `read_full` if truncated           |
| Screenshot              | `mcp__sensei__screenshot()`       | **REQUIRED after every nav/click** |
| Google search           | `mcp__sensei__search(query)`      |                                    |
| Run JS on page          | `mcp__sensei__js_eval(code)`      |                                    |
| Scroll                  | `mcp__sensei__scroll(direction)`  |                                    |
| Key press               | `mcp__sensei__key_press(key)`     |                                    |
| Console logs            | `mcp__sensei__console_logs()`     |                                    |
| Network requests        | `mcp__sensei__network_requests()` |                                    |
| List tabs               | `mcp__sensei__tab_list()`         |                                    |
| New tab                 | `mcp__sensei__tab_create(url)`    |                                    |
| Close tab               | `mcp__sensei__tab_close(tab_id)`  |                                    |
| Run shell via bridge    | `mcp__sensei__run(cmd)`           |                                    |
| Drag element            | `mcp__sensei__drag(from, to)`     |                                    |
| Double-click            | `mcp__sensei__double_click(what)` |                                    |
| Hover                   | `mcp__sensei__hover(what)`        |                                    |
| Upload file             | `mcp__sensei__upload_file(sel, path)` |                               |
| Resize window           | `mcp__sensei__resize_window(w,h)` |                                    |
| Wait                    | `mcp__sensei__wait(ms)`           |                                    |
| Batch actions           | `mcp__sensei__batch(actions_json)`| atomic multi-step                  |

---

## ⛔ DEAD — NEVER USE

| Path | Reason |
|------|--------|
| `mcp__claude-in-chrome__*` | Extension does not connect. DEAD as of 2026-05-24. (§NON-NEGOTIABLE #1) |
| `google-chrome <url>` | Creates bare tab outside MCP group. (§NON-NEGOTIABLE #1) |

---

## FILE / SHELL ACTIONS

| Action                  | Tool              |
|-------------------------|-------------------|
| Read local file         | `Read` tool       |
| Write/create local file | `Write` tool      |
| Edit file (partial)     | `Edit` tool       |
| Run shell command       | `Bash` tool       |
| Search codebase         | `Grep` / `Glob`   |
| Search web              | `WebSearch` tool  |

---

## EMAIL — `mcp__email-bridge__*`

| Action                  | Tool                                    |
|-------------------------|-----------------------------------------|
| List accounts           | `mcp__email-bridge__list_accounts`      |
| Check inbox             | `mcp__email-bridge__check_inbox`        |
| Read email              | `mcp__email-bridge__read_email`         |
| Search inbox            | `mcp__email-bridge__search_inbox`       |
| Send email              | `mcp__email-bridge__send_email`         |
| Reply to email          | `mcp__email-bridge__reply_to`           |
| Mark read               | `mcp__email-bridge__mark_read`          |

---

## GOOGLE WORKSPACE

| Action                  | Tool                                                  |
|-------------------------|-------------------------------------------------------|
| Search Drive            | `mcp__claude_ai_Google_Drive__search_files`           |
| Read Drive file         | `mcp__claude_ai_Google_Drive__read_file_content`      |
| List recent Drive files | `mcp__claude_ai_Google_Drive__list_recent_files`      |
| Get file metadata       | `mcp__claude_ai_Google_Drive__get_file_metadata`      |
| Create Drive file       | `mcp__claude_ai_Google_Drive__create_file`            |
| Gmail search            | `mcp__claude_ai_Gmail__search_threads`                |
| Gmail get thread        | `mcp__claude_ai_Gmail__get_thread`                    |
| Gmail create draft      | `mcp__claude_ai_Gmail__create_draft`                  |
| Calendar list events    | `mcp__claude_ai_Google_Calendar__list_events`         |
| Calendar create event   | `mcp__claude_ai_Google_Calendar__create_event`        |

---

## ACT-FIRST RULE — NON-NEGOTIABLE

When operator says **"open X"**, **"go to X"**, **"check X"**, **"click X"**, **"find X"**:

```
1. EXECUTE immediately — no narration, no "I'll now…", no plan
2. mcp__sensei__screenshot() — always after navigation or click
3. Show screenshot — this IS verification
4. If screenshot shows wrong state → fix it, show new screenshot
```

No preamble. No "here's what I'm going to do." Just do it.

---

## VERIFIED = EVIDENCE SHOWN

**"Done" is NEVER said without one of:**
- Screenshot showing the result on screen
- `grep` / `Read` output showing the actual data
- Tool return value with real content (not just `{ok: true}`)

If you can't produce evidence → you are not done.

---

## OBSERVABILITY FALLBACK CHAIN

```
screenshot fails attempt 1  →  retry once (attempt 2)  →  STOP → hand to operator
read() fails                →  try js_eval              →  STOP if both fail
ALL 3 channels dead         →  operator_eyes. Zero more attempts.
```

Per §11 retry schema: hard cap = 3 attempts per (operation_id, tool). Observability tools cap at 2.

---

## SECRETARY — `mcp__secretary__*`

| Use                     | Condition                                         |
|-------------------------|---------------------------------------------------|
| ✅ Spawn secretary       | Operator says "do this autonomously" / multi-step, no need to watch UI |
| ❌ Do NOT spawn          | Immediate browser action / operator wants to watch / anything visual |

---

*Last updated: 2026-05-26. Canonical path: `/home/elijah/MD/TOOL_ROUTING.md`*
