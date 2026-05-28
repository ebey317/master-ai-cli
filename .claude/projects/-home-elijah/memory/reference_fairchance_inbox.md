---
name: fairchance-inbox
description: "Fair Chance relay / proof-of-service inbox (fairchance110@gmail.com) is wired into the email-bridge MCP as account key 'fairchance'. Use for proof-of-service confirmation emails."
metadata: 
  node_type: memory
  type: reference
  created_ts: 2026-05-28T19:40:00Z
  originSessionId: 63a3eb36-b3b5-4425-ae4a-cd59d35eadf0
---

**fairchance110@gmail.com** = the Fair Chance **relay / proof-of-service inbox** (the address job-board "application submitted" confirmations get sent to as legal proof of service for funders — see [[workflow-fairchance-monday-elena]]).

Wired into the email-bridge MCP on 2026-05-28 as account key **`fairchance`** (provider gmail). Use it like the other bridge accounts:
- `mcp__email-bridge__check_inbox` account="fairchance"
- `search_inbox` / `read_email` / `send_email` (from_account="fairchance") all work.

Setup facts (so this isn't redone):
- 2FA is ON for this account; recovery phone `(317) 332-2323` (pre-verified), recovery email Ebey317@gmail.com. Account is `u/1` in the browser's Google multi-login.
- A Gmail **app password** (name "fairchance", created 2026-05-28 ~3:37 PM) is what the bridge uses. It lives ONLY in `~/.config/email_mcp/credentials.json` — never copy secrets into memory.
- If auth ever fails: regenerate the app password at myaccount.google.com/u/1/apppasswords and update that one field in credentials.json.

Bridge config: `~/.config/email_mcp/credentials.json`; server: `~/scripts/email_mcp/server.py`. The server reads the config per call (no restart needed to pick up a new/changed account).
