---
name: project_reentry_desk
description: reentry-desk MCP server — 6 tools for client intake and form completion tracking
metadata: 
  node_type: memory
  type: project
  originSessionId: 68bb3b00-d3ea-4116-8bfb-10b5d6e3f678
---

# reentry-desk MCP Server

**Path:** `~/projects/reentry-desk/`
**Registered:** User-scope (available in ALL projects and sessions)
**Stack:** Python + FastMCP, JSON flat files

## 6 Tools
- `create_client` — intake a new client, profile → `clients/{id}.json`
- `get_client` — retrieve by ID or name
- `list_forms` — shows pending vs completed forms
- `fill_form` — returns merged client + template payload for browser autofill
- `mark_complete` — logs form done with timestamp
- `get_status` — full pipeline view

## Form Templates (in `templates/`)
- `snap_enrollment.json`
- `housing_intake.json`
- `job_application.json`
- `ssi.json`
- `id_replacement.json`

## Registration
```bash
claude mcp add --scope user reentry-desk -- python3 /home/elijah/projects/reentry-desk/server.py
```
Removes: `claude mcp remove "reentry-desk" -s user`

## Notes
- `clients/` excluded from git via `.gitignore`
- Client profiles are private; templates are version-controlled
- Next step: wire Pupil to this server

**Why:** Fair Chance project — autofill job apps + social services for friends/family
**How to apply:** Call `fill_form` → get handoff JSON → pass to sensei browser automation
