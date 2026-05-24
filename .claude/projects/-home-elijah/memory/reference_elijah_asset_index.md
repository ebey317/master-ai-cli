---
name: elijah-asset-index
description: "Deep-recall index for Elijah's authenticated connectors. When he says \"I have it\" or mentions an asset, search the wired connectors FIRST before asking where it is or generating new content."
metadata: 
  node_type: memory
  type: reference
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

The "deep recall" index. When Elijah says "I have it" or "I uploaded it" or names an asset, **search the wired connectors BEFORE asking where it is or generating new content.**

Authenticated connectors (wired, do NOT prompt for accounts, do NOT WebFetch to bypass auth):

| Connector | Tool prefix | What to use it for |
|-----------|-------------|--------------------|
| Google Drive | `mcp__claude_ai_Google_Drive__*` | search_files (fulltext + title), read_file_content, create_file, download as PDF/DOCX |
| Canva | `mcp__canva__*` | search-designs, search-folders, get-design-content, start-editing-transaction, perform-editing-operations, export-design |
| Google Photos | `sensei photos_search` | Photos search — WebFetch 302s to Google login, won't work directly |
| Gmail | `mcp__claude_ai_Gmail__*` | search_threads, get_thread, create_draft, label management |
| Calendar | `mcp__claude_ai_Google_Calendar__*` | create_event, list_events, suggest_time |
| Todoist | `mcp__claude_ai_Todoist__*` | tasks, projects, comments |
| Indeed | `mcp__claude_ai_Indeed__*` | search_jobs, get_company_data |
| ZipRecruiter | `mcp__claude_ai_ZipRecruiter__*` | search_jobs |
| Hugging Face | `mcp__claude_ai_Hugging_Face__*` | space search, paper search, generate images via Z-Image |

**Search-first rule:** When operator mentions an asset, project, or says "I have it / uploaded it / it's somewhere," issue parallel searches across Drive + Canva + Photos BEFORE asking where it is.

**Why:** Operator pays for every wasted round-trip. Asking "where did you put it?" when the answer is one search away costs tokens and breaks his momentum. Especially true for assets — he's a visual creator with stuff scattered across Drive folders and Canva designs.

**How to apply:**
- If he says "find my X" — parallel-search Drive (title + fulltext) + Canva designs in one tool-call batch
- If he says "I have a [type of file]" — search for it before generating a new one
- If WebFetch 302s to Google login — switch to the connector tool, don't ask him to log in

**Drive MCP limits to remember:**
- No delete tool. For sweep operations, use rclone (hits shared-OAuth rate limit ~1 minute) OR operator manual UI.
- Files > ~200K chars auto-dump to disk. Process via shell tools, not context.

**Canva MCP limits to remember:**
- `get-assets` returns ONLY thumbnail URLs (133×200 watermarked), not originals. Re-upload via `upload-asset-from-url` from those URLs degrades asset quality permanently.
- `start-editing-transaction` rejects designs with >~150 pages ("Editing a Canva Design with a size of 183 pages is not currently supported").
- Responsive pages (`is_responsive: true`) limit operations to update_title, replace_text, update_fill, delete_element, find_and_replace_text only.

Known canonical sources (built via the consolidation pattern):
- BioVega → see [[biovega-real-identity]]. Canonical Drive Doc `1xdF-GDfbpm2lvdoEi6A6Lldb9tnd2tWn-Qi3ypHrczo`. 11 Canva source designs preserved (could not be safely consolidated due to thumbnail limit).
- [Future consolidations link here]

**Consolidation pattern (proven 2026-05-23 with BioVega):**
1. Inventory all sources across Drive + Canva (paginate to exhaustion)
2. Read content into receipts directory with sha256 per source
3. Assemble single canonical Drive Doc with verbatim merge
4. Run 6-pass verification (section word counts, n-gram coverage, key-phrase + context, random spot checks, asset cross-check, operator gate)
5. Local snapshot tarball before deletion (insurance)
6. Drive sweep via rclone (hit rate limit at 1-2 deletions; switch to operator manual UI)
7. Skip Canva sweep if assets can't be safely preserved (Canva MCP thumbnail-only limit)

See [[canva-image-swap-before-text]] for the parallel lesson on Canva design rebuilds.
