# Sensei Extension Auto Loop Smoke Checklist

Goal: make "Auto either loops or does not" a 5-minute deterministic check.

This test uses only a local static page:

`~/scripts/sensei_extension/test/loop_smoke.html`

## Setup

1. Confirm backend is active:

   ```bash
   systemctl --user is-active master-ai-ui.service
   ```

   Expected output: `active`

2. Reload the unpacked extension in `chrome://extensions`.

3. Open the test page in Chrome:

   ```text
   file:///home/elijah/scripts/sensei_extension/test/loop_smoke.html
   ```

4. Open DevTools on the test page.

   Required panels:
   - `Console`
   - `Network`

5. In another terminal, watch backend/audit output:

   ```bash
   journalctl --user -u master-ai-ui.service -f
   ```

   ```bash
   tail -f ~/.master_ai_audit_typed.jsonl
   ```

## Test Prompt

1. Open the Sensei side panel.
2. Set mode to `Auto`.
3. Send exactly:

   ```text
   fill the name field with hello then click submit
   ```

## Expected `/chat` Request

In DevTools `Network`, select the `POST /chat` request.

Request body should include:

```json
{
  "mode": "auto",
  "source": "chrome_extension",
  "page_context": {
    "url": "file:///home/elijah/scripts/sensei_extension/test/loop_smoke.html",
    "title": "Sensei Loop Smoke",
    "interactive_elements": "..."
  }
}
```

`page_context.interactive_elements` should mention all three known elements:

```text
textbox "Name field" selector=#nameField
button "Submit button" selector=#submitButton
link "Example link" selector=#docsLink
```

If `interactive_elements` is missing, the content script did not inject or page context failed.

## Expected `/chat` Response

The response should contain two browser actions:

```json
{
  "actions": [
    {
      "kind": "BROWSER_FILL",
      "target": "#nameField => hello"
    },
    {
      "kind": "BROWSER_CLICK",
      "target": "#submitButton"
    }
  ],
  "done": false
}
```

Acceptable target variants:

```text
Name field => hello
{"selector":"#nameField","value":"hello"}
Submit button
```

Failure signs:

- No `BROWSER_FILL`
- No `BROWSER_CLICK`
- `done: true` while actions are present
- Action rows appear and wait for approval in `Auto` mode for these non-protected actions

## Expected Page Result

The extension should auto-run both actions.

On the page:

```text
Submitted name: hello
```

In DevTools `Console`:

```text
[Sensei loop smoke] submit clicked {"submitted":true,"name":"hello","clicks":1}
```

If the field fills but the button does not click, the loop/action dispatch stalled after `BROWSER_FILL`.

If the button clicks with an empty name, action ordering is wrong.

## Expected Audit Lines

In `~/.master_ai_audit_typed.jsonl`, expect one row per executed action:

```json
{"kind":"extension_action_result","verdict":"accept","result":"success","raw":{"action":{"kind":"BROWSER_FILL"}}}
{"kind":"extension_action_result","verdict":"accept","result":"success","raw":{"action":{"kind":"BROWSER_CLICK"}}}
```

The exact JSON has more fields, but these fields must be present:

```text
kind=extension_action_result
verdict=accept
result=success
raw.action.kind=BROWSER_FILL
raw.action.kind=BROWSER_CLICK
raw.final_state.permission=auto
```

If audit rows are missing, the extension executed locally but failed to report back to the backend.

## Expected `/chat/continue` Request

After both actions report success, DevTools `Network` should show `POST /chat/continue`.

Request body should include:

```json
{
  "parent_turn_id": "<turn id from /chat>",
  "source": "chrome_extension",
  "session_id": "<same session id>",
  "action_results": [
    {
      "verdict": "accept",
      "result": "success",
      "action": {
        "kind": "BROWSER_FILL"
      },
      "final_state": {
        "permission": "auto"
      }
    },
    {
      "verdict": "accept",
      "result": "success",
      "action": {
        "kind": "BROWSER_CLICK"
      },
      "final_state": {
        "permission": "auto"
      }
    }
  ]
}
```

Failure signs:

- No `/chat/continue` after both action audit rows
- `action_results` missing one of the actions
- `parent_turn_id` missing
- `result` is `failure`
- `final_state.permission` is not `auto`

## Expected `/chat/continue` Response

Expected behavior:

```json
{
  "actions": [],
  "done": true
}
```

The assistant reply should summarize the completed page state, for example:

```text
Submitted the name field with hello.
```

Failure signs:

- More `BROWSER_FILL` / `BROWSER_CLICK` actions retry the same successful target
- Loop stops without a final assistant message
- Backend returns duplicate-failure short-circuit for successful actions

## Backend Log Checkpoints

`journalctl --user -u master-ai-ui.service -f` should show no Python tracebacks, disconnect loops, or `/chat` 500 responses.

`~/.master_ai_audit_typed.jsonl` should show:

```text
extension_action_result BROWSER_FILL success
extension_action_result BROWSER_CLICK success
turn_terminal
```

There should be no `page_context_sanitize` row for this clean page.

## If It Stalls

Use this decision table:

| Symptom | Read This | Likely Break |
| --- | --- | --- |
| `/chat` missing | Side panel Console + Network | Backend URL/token/config |
| `/chat` has no `interactive_elements` | Side panel Console | content script injection/page context |
| `/chat` has no browser actions | `/chat` response body | model/directive parsing |
| Actions render with buttons in Auto | `side_panel.js shouldAutoRunAction` behavior | Auto execution policy |
| Field fills but no click | page Console + audit JSONL | action dispatch sequencing |
| Both actions succeed but no `/chat/continue` | audit JSONL + side panel Console | loop pending/result accounting |
| `/chat/continue` happens but retries same action | `/chat/continue` request/response | backend result formatting/model loop |
| Final answer claims success but page unchanged | page Console + DOM state | action execution did not really happen |

## Pass Criteria

Pass only if all are true:

- `mode` in `/chat` is `auto`
- `/chat` response contains `BROWSER_FILL` and `BROWSER_CLICK`
- No manual approval is required
- Page shows `Submitted name: hello`
- Console logs `[Sensei loop smoke] submit clicked`
- Audit JSONL records both action successes
- `/chat/continue` includes both action results
- Final response completes without retrying the same action

If any line fails, Auto loop is not verified.
