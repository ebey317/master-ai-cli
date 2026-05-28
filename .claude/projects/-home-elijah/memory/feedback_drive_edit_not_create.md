---
name: feedback_drive_edit_not_create
description: Never create new Drive files to update existing docs — edit in place via browser
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b664b9e8-c3bd-4f93-8896-4d5fa9215463
---

Do NOT use `create_file` to update an existing Google Doc. Drive MCP has no update/edit tool.

**Correct path:** Use sensei to open the doc URL in the browser, click into the doc, and type/paste changes directly. Google Docs auto-saves.

**Wrong path:** `create_file` with updated content → creates a duplicate the user has to manually delete.

**Why:** Operator locked this 2026-05-27 after a duplicate was created for the Applications Log.

**How to apply:** When an existing Google Doc needs updating, open it via `mcp__sensei__browse` using the viewUrl, then use fill/click/type to make edits in the doc body.

See also: [[reference_elijah_asset_index]]
