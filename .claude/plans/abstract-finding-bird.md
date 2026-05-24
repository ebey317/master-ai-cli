# Plan: Client Resume → Drive → Autofill Pipeline

## Context

This is a **business workflow**, not Elijah's personal applications. The operator helps clients find jobs. Each client's resume lives in a Google Drive subfolder. When applying to a job for a client, the workflow should be:

1. Navigate to the client's Drive subfolder by name
2. Read their resume (Google Doc or DOCX)
3. Extract structured profile data (LLM handles this — resumes are unstructured)
4. Save as a temp JSON matching the `master_ai_profile.json` schema
5. Run `autofill_job_form(profile_path=...)` on the live job application

The autofill tool already accepts a `profile_path` override — so the existing autofill pipeline works unchanged. Only the profile sourcing changes: Drive instead of `~/.master_ai_profile.json`.

---

## Drive Folder Convention

**Root resume folder:** `Resume/` (Drive ID: `0B0uiO2R0bvqtRVo1N09wT1ZuTXc`)

Each client gets a subfolder:
```
Resume/
  John Smith/
    John Smith Resume.docx   ← or Google Doc
    (optional) certifications, transcripts, etc.
  Jane Doe/
    Jane Doe Resume.pdf
```

Naming: operator creates the subfolder using the client's full name. The workflow searches by that name. No IDs needed — search by title match.

---

## What Gets Built

### 1. New sensei tool: `read_client_resume`

**File:** `/home/elijah/scripts/sensei_mcp_server.py`

**New function:** `tool_read_client_resume(args)` — added alongside existing tools.

**What it does:**
- Takes `client_name` (string) — e.g. `"John Smith"`
- Searches Drive for a subfolder of the Resume folder matching that name
- Lists files in the subfolder
- Reads the resume document (prefers Google Doc; falls back to DOCX; skips PDFs/images)
- Returns raw resume text + Drive file metadata

**Why a new tool instead of using Drive MCP directly:**
- Callable from the MCP Inspector — operator can fire it from the UI
- Callable from secretary tasks — enables fully automated pipeline
- Keeps the Drive folder ID in one place (the tool, not scattered across prompts)

**Implementation sketch:**
```python
def tool_read_client_resume(args):
    client_name = args.get("client_name", "").strip()
    resume_folder_id = "0B0uiO2R0bvqtRVo1N09wT1ZuTXc"
    # 1. Search for subfolder by name within resume_folder_id
    # 2. List files in that subfolder
    # 3. Pick best resume file (Google Doc > DOCX > PDF)
    # 4. Read content via Drive API
    # 5. Return {"client_name": ..., "file_name": ..., "text": ..., "file_id": ...}
```

The tool uses the Drive MCP connector (already wired) via Python subprocess or HTTP — **or** it returns the file ID and delegates reading to the LLM using the Drive MCP tools. The simpler path: return Drive file ID + name, let LLM call `mcp__claude_ai_Google_Drive__read_file_content` directly. This avoids needing a Drive API client inside sensei.

**Simpler variant (preferred):** `read_client_resume` just does the Drive **folder search** and returns the file ID + name. The LLM then calls the Drive MCP `read_file_content` tool itself. This keeps the sensei tool small and avoids embedding Drive auth inside sensei.

**Format handling:** Clients upload whatever they have (DOCX, PDF, Google Doc). Tool picks best available in priority order: Google Doc → DOCX → PDF. Drive MCP `read_file_content` handles all three natively.

### 2. Profile extraction (LLM step — no code)

After reading the resume text, the LLM structures it into the `master_ai_profile.json` schema:

```json
{
  "personal": { "first_name": "...", "last_name": "...", "email": "...", "phone": "...", ... },
  "experience": [{ "title": "...", "employer": "...", "start": "...", "end": "...", "summary": "..." }],
  "education": [{ "school": "...", "degree": "...", "year_graduated": "..." }],
  "skills": ["...", "..."],
  "disclosures": { "authorized_to_work": "Yes", "requires_sponsorship": "No", ... },
  "custom_answers": { "cover_letter_template": "Dear Hiring Manager,\n\n..." }
}
```

Fields not in the resume default to blanks (not `?` — the autofill tool skips `?` values, but blanks are fine to leave empty).

### 3. Profile save (existing `write_file` tool)

```
sensei write_file → /tmp/profile_{client_name_slug}.json
```

No new code. This already exists.

### 4. Autofill (existing tool, unchanged)

```
autofill_job_form(profile_path="/tmp/profile_john_smith.json")
```

The `profile_path` override already exists in the tool. No changes needed.

---

## Files to Modify

| File | Change |
|---|---|
| `/home/elijah/scripts/sensei_mcp_server.py` | Add `tool_read_client_resume` function + register it in the tool dispatch dict + add to `tools/list` schema |

That's the only file. ATS maps, fingerprint logic, and autofill itself are **unchanged**.

## Files to Read (no changes)
- `/home/elijah/scripts/ats_maps/greenhouse.py` — profile key paths to use in extracted JSON
- `/home/elijah/scripts/ats_maps/lever.py` — same
- `~/.master_ai_profile.json` — schema reference

---

## End-to-End Workflow (after this is built)

```
Operator: "Apply for John Smith at [job URL]"

1. Claude → read_client_resume(client_name="John Smith")
   ← returns Drive file ID + name

2. Claude → Drive MCP: read_file_content(file_id)
   ← returns resume text

3. Claude extracts profile JSON from resume text (in-context, no tool)

4. Claude → sensei write_file: /tmp/profile_john_smith.json

5. Claude → sensei browse: [job application URL]

6. Claude → sensei autofill_job_form(profile_path="/tmp/profile_john_smith.json")
   ← fills standard fields, returns unfilled essay fields

7. Claude generates essay answers for remaining fields
   (operator sees everything in the browser — visibility rule applies)
```

---

## Verification

1. Create a test client subfolder in Drive: `Resume/Test Client/` with a sample Google Doc resume
2. Call `read_client_resume(client_name="Test Client")` from the MCP Inspector — confirm it returns the file ID and name
3. Read the file via Drive MCP — confirm text comes back
4. Manually construct a profile JSON and save it to `/tmp/profile_test_client.json`
5. Navigate to a Greenhouse or Lever job (test posting is fine)
6. Call `autofill_job_form(profile_path="/tmp/profile_test_client.json", dry_run="true")` — confirm it reports the correct fields it would fill
7. Run without `dry_run` — confirm fields populate in the browser

---

## Open Questions (operator should confirm before build)

1. **Drive subfolder naming:** Exact client name (e.g. "John Smith") or something else?
2. **Resume format preference:** Are client resumes uploaded as Google Docs, DOCX, or PDF? (Google Doc is easiest to read; PDFs require extra handling)
3. **Disclosures defaults for clients:** Work auth, sponsorship, EEO — operator fills these per-client, or do we prompt the client?
4. ~~**Secretary automation scope**~~ → **Answered: single secretary task.** Full pipeline runs autonomously: find → read → extract → save → autofill. Operator watches the browser.
