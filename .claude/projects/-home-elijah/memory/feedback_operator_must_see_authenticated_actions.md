---
name: operator-must-see-authenticated-actions
description: "HARD RULE — when ANY tool uses operator's login credentials (Canva, Drive, Gmail, Photos, Calendar, Todoist, sensei, secretary, etc.), the operator MUST see the action happen live in his browser. Post-hoc thumbnails are NOT acceptable. Drive the action through a visible browser tab whenever possible; if forced to use a headless API path, open the editor URL in a visible tab BEFORE starting so operator can watch."
metadata:
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

**HARD RULE — non-negotiable, locked across every session.**

When ANY tool call uses the operator's authenticated login (any account he is signed into), the operator MUST be able to SEE the action happen as it happens. Not after. Not via a thumbnail after the change committed. Live, on his screen, in his browser, as the change happens.

**Operator's exact words 2026-05-23:**
> "if you're using my login credentials. I must see everything that goes on. That is a hard rule. Confirm that that rule is locked and it said, and it persists on every thread."

## What counts as "operator's login credentials"

ANY of these tool surfaces uses his account:

| Tool prefix | Account |
|-------------|---------|
| `mcp__canva__*` | his Canva |
| `mcp__claude_ai_Google_Drive__*` | his Google Drive |
| `mcp__claude_ai_Gmail__*` | his Gmail |
| `mcp__claude_ai_Google_Calendar__*` | his Calendar |
| `mcp__claude_ai_Todoist__*` | his Todoist |
| `mcp__claude_ai_Indeed__*`, `mcp__claude_ai_ZipRecruiter__*` | his job-board accounts |
| `mcp__claude_ai_Hugging_Face__*`, `mcp__hugging-face__*` | his HF |
| `mcp__claude_ai_Base44__*` | his Base44 |
| `mcp__sensei__*` (browse/click/fill/photos_search/js_eval) | whichever account is logged into the sensei-driven browser |
| `mcp__claude-in-chrome__*` | whichever account is logged into Chrome |
| `mcp__secretary__*` when its task uses any of the above | his account by transitive use |

## Why this rule exists

2026-05-23 — Phase 2 DAHKgK7YGSg Canva rebuild. I called `start-editing-transaction`, then ran `perform-editing-operations` to swap 14 image fills and rewrite text on 9 pages. All of this happened via API. The operator saw nothing live — only the small thumbnails I posted back after each batch. From his side it looked like silent activity in his account. He stopped me and re-asserted the rule.

The Canva MCP is the worst-case offender: a "transaction" is a draft API state that doesn't render in any visible Canva tab until committed (and arguably not even then in real time). Operations that touch his design are happening *invisibly to him*.

The general pattern this rule prevents: an automated tool burns through dozens of changes on his account while he has no live window into what's happening. Even if everything is recoverable (drafts can be cancelled, etc.), that's not the point. The point is he is the operator. He audits. He cannot audit what he cannot see.

## How to apply

**Order of preference for every authenticated-account action:**

1. **Drive it through a visible browser tab.** If sensei or claude-in-chrome can perform the same action by clicking and typing in his real browser window, prefer that path over the headless API. He sees every click and every keystroke.

2. **If a headless API path is the only option:** BEFORE making the first API call, open the relevant view URL in a sensei-managed browser tab on his screen so he can watch the effects appear. Examples:
   - Before a Canva MCP transaction: open the design's edit URL (`https://www.canva.com/d/<short_link>` from the start-editing-transaction response) in a sensei tab. He may see live edits appear, or at minimum the design rendered.
   - Before a Drive MCP create/update: open the parent folder in a sensei tab.
   - Before a Gmail MCP draft creation: open Gmail's Drafts in a sensei tab.

3. **Narrate every authenticated action BEFORE issuing it.** Single line, in the chat, naming the account, the object, and the change. "Editing Canva design DAHKgK7YGSg — swapping image fill on page 1 (asset MAELmyO9FfU → MAG9lRTBCVU)." If he objects, he stops me before the call lands.

4. **Pause between batches on long sequences.** Don't fire 60 ops in one block when 60 individual narrated ops would let him follow along. Trade efficiency for visibility; he chose this trade.

5. **No commit / no irreversible action without explicit operator approval in chat.** Drafts stay drafts until he says commit. (This was already the rule per [[canva-image-swap-before-text]] — this memory generalizes it to all authenticated tools.)

6. **If a tool genuinely cannot be made visible** (e.g. some MCP that has no corresponding browser surface), STOP and surface that fact to the operator before using it. He decides whether to proceed blind or skip.

## What's already-allowed (read-only, low-impact)

Reading data from his accounts — `search_files`, `search_threads`, `list_events`, `get-design-content`, `find-tasks`, etc. — is fine to fire without a visible tab. The rule is about CHANGES to state in his accounts (creates, updates, deletes, sends, commits, image/text edits, label changes, etc.).

Searches/reads still get a one-line narration so he knows what I'm pulling, but don't require a parallel visible tab.

## Cross-references

- [[mcp-browser-must-be-visible]] — the inverse: I must be able to see the page I'm driving. This memory is about *he* must be able to see what I'm doing.
- [[canva-image-swap-before-text]] — the page-1-thumbnail-before-commit rule. Compatible with this memory; this memory tightens the upstream requirement.
- [[elijah-asset-index]] — the connector catalog. Every entry there is subject to this rule.
- [[attention-signal-tiers]] — speak.sh / TV / Jazz/Gospel signals for getting his attention if he's looking away when an authenticated action is about to fire.

## Status

Locked 2026-05-23. Persists across every session via auto-memory. If a future session attempts an authenticated-account change without visible operator audit, that is a defect — escalate to operator immediately.
