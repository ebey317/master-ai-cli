# Sensei Extension — Backend API Contract

The extension requires a backend server implementing the endpoints below.
All requests carry the `X-Master-AI-Token` header (value set in Options).
Endpoints marked **optional** — the extension gracefully degrades if they
return 404 or connection timeout.

## Core Chat

**POST /chat**
```json
Request:  { "message": "...", "session_id": "...", "source": "extension",
            "page_context": { "url": "...", "title": "...", "text": "...",
                              "interactive_elements": [...] },
            "mode": "auto|plan|review" }
Response: { "reply": "...", "actions": [...], "turn_id": "..." }
```

**POST /chat/continue**
```json
Request:  { "session_id": "...", "turn_id": "...",
            "results": [...], "mode": "auto|plan|review" }
Response: { "reply": "...", "actions": [...] }
```

## Mode Control

**GET /mode**
```json
Response: { "mode": "auto|plan|review" }
```

**POST /mode**
```json
Request:  { "mode": "hybrid|local" }
```

## Health

**GET /health**
```json
Response: { "status": "ok" }
```

## Action Feedback

**POST /extension/action_result**
```json
Request:  { "session_id": "...", "turn_id": "...", "action_id": "...",
            "status": "completed|failed|rejected",
            "result": "...", "observed_tab_url": "..." }
```

## Local File Access (optional)

**POST /extension/resolve_local_file**
```json
Request:  { "path": "/absolute/or/~/relative/path" }
Response: { "path": "...", "exists": true }
```

**POST /extension/read_local_file** (optional)
```json
Request:  { "path": "..." }
Response: { "path": "...", "content": "...", "ok": true }
          { "path": "...", "ok": false, "error": "..." }
```

## Domain Classification (optional)

**POST /extension/classify_domain** (optional)
```json
Request:  { "host": "example.com" }
Response: { "category": "safe|sensitive|blocked", "reason": "...", "ttl_s": 300 }
```

## Permission Approval (optional)

**POST /extension/approve_action** (optional)
```json
Request:  { "session_id": "...", "action_id": "..." }
```

## Speech-to-Text (optional)

**POST /stt** (optional)
- Request: `multipart/form-data` with field `audio` (audio/webm blob)
```json
Response: { "text": "transcribed text" }
```

## Agent Sidecar (optional — separate port, see Options)

**GET /agent/health** (optional)
```json
Response: { "status": "ok", "active_tasks": 0 }
```

**GET /agent/stats** (optional)
```json
Response: { "by_status": { "executing": 0, "completed": 42, "failed": 1 } }
```

---

## Error Shapes

**HTTP 503 — backend busy**
```json
{ "error": "system_busy", "retry_after_s": 15 }
```
The side panel surfaces this as a friendly retry message instead of a raw error.

---

## Compatibility Notes

- Default port `8080` (main backend) and `8001` (agent sidecar) are configurable
  in the Options page — no code change needed to point at a different host.
- Blank Agent Sidecar URL in Options disables `/agent/health` and `/agent/stats` requests.
- Blank Wake Relay URL disables AI tab readiness notifications.
- The extension strips trailing slashes from all configured URLs before appending paths.
