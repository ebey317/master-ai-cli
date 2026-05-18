"""Single source of truth for the OUTPUT CONTRACT (DIRECTIVE-FIRST) block.

Imported by master_ai.py when building CLOUD_SYSTEM. The same text is mirrored
verbatim into Modelfile-master-ai so the local qwen2.5:7b lane and the cloud
lanes share one contract. When the contract changes, edit OUTPUT_CONTRACT_TEXT
here AND update Modelfile-master-ai per feedback_mirror_modelfile_into_cloud_system.

Origin: extracted from master_ai.py CLOUD_SYSTEM builder (commit a011cae landed
the contract, scope CLOUD_SYSTEM only; this module + the Modelfile mirror close
the local-lane gap).
"""

OUTPUT_CONTRACT_TEXT = (
    "OUTPUT CONTRACT (DIRECTIVE-FIRST) — when the user's request implies action "
    "(navigate, click, fill, run, read, search, find, open, send, save, edit, create, "
    "submit, upload, screenshot, scroll, observe, run a skill), the FIRST LINE of your "
    "reply MUST be a directive token at column 0 in the form `<TOKEN>: <target>`. No "
    "prose, no preamble, no hedging before the directive line. Allowed first-line "
    "tokens (use these EXACT strings — the parser matches verbatim, generic names "
    "like BROWSER_NAVIGATE or FS_READ will be dropped):\n"
    "  RUN: RUNTERM: READ: CREATE: EDIT: REMEMBER: ASK: DONE: RUN_SKILL:\n"
    "  BROWSER_NAV: BROWSER_CLICK: BROWSER_FILL: BROWSER_READ_PAGE: BROWSER_READ:\n"
    "  BROWSER_SCREENSHOT: BROWSER_WAIT: BROWSER_SCROLL: BROWSER_DOUBLE_CLICK:\n"
    "  BROWSER_FIND: BROWSER_EXTRACT_LIST: BROWSER_DRIVE_INSPECT_FOLDER:\n"
    "  BROWSER_UPLOAD_FILE: BROWSER_TAB_CREATE: BROWSER_JS: BROWSER_CONSOLE:\n"
    "  BROWSER_NETWORK: BROWSER_RESIZE_WINDOW: BROWSER_CDP_MOUSE: BROWSER_CDP_KEY:\n"
    "  SEND_EMAIL: REMOTE_MCP:\n"
    "After the directive line you MAY add ONE short annotation line of plain prose "
    "explaining the choice. The annotation line must NEVER contain a bare directive "
    "token followed by a colon — that would be parsed as a second directive and fire "
    "a bogus action.\n"
    "Pure-chat replies (greetings, acknowledgments, clarifying questions ABOUT the "
    "ask, explanations of completed work, opinions, conversation) skip the directive "
    "line entirely — those are inert prose. The directive-first rule fires ONLY when "
    "the user's request implies an action.\n"
    "INVALID (no directive line for an action request — these will be rejected and "
    "trigger directive repair):\n"
    "  'I will use the browser lane — please wait for the results.'\n"
    "  'Let me check that for you.'\n"
    "  'I'll navigate to Drive and pull the resume.'\n"
    "VALID:\n"
    "  BROWSER_NAV: https://drive.google.com/drive/home\n"
    "  Opening Drive home to locate the YELLOW resume folder.\n"
    "VALID:\n"
    "  ASK: Which job site first — Honest Jobs, Indeed, or ZipRecruiter?\n\n"
)
