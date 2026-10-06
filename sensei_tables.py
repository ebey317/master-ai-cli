"""Extracted constant tables for Sensei (`master_ai.py`).

Pure lookup tables and limits: word lists, regex patterns, ANSI colour codes,
size caps, display strings. No behaviour, no I/O, no dependency on the agent loop.

Split out of `master_ai.py` (2026-09-29) when the monolith reached 29,256 lines and
its module head was no longer readable.

Selection runs four passes, all of which have already caught a real breakage:

  1. HARD FILTERS. A name is a candidate only if it is bound exactly once at module
     scope, is never named in a `global` statement, and is CONSTANT-shaped. This is
     what keeps `DEFAULT_LOCAL_MODEL`, `MODE`, `PINNED_MODEL` and `HINTS` behind --
     they are reassigned at runtime, not constants, and moving them broke evaluation
     order (the alias table got built before the hardware model was picked).
  2. FORWARD. Every name a value references must be stdlib or also moving.
  3. REVERSE. Nothing left behind in master_ai.py may reference a name that left.
  4. `__all__` is mandatory below, not cosmetic: `import *` skips single-underscore
     names, and most of these tables are `_`-prefixed, so without it the star-import
     silently loses them and the agent dies with NameError at startup.

47 names stayed behind on purpose -- they call `_pfile()` or `load_keys()` at import
time, so they are derived from live profile state rather than being constants, and
moving them would create an import cycle and risk resolving paths before the active
profile is known.

"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

__all__ = [
    "ACTIVE_MODEL_FILE",
    "ACTIVE_PROJECT",
    "ACTIVE_PROJECT_FILE",
    "ACTIVE_TASK",
    "ACTIVE_TASK_FILE",
    "ALTER_WORDS",
    "AMBER",
    "ARIA_VOICE_CONFIG",
    "ASK_CLOUD_BARE_PROVIDERS",
    "ATTACHMENT_MAX_CHARS",
    "AUDIT_LOG",
    "AUDIT_LOG_JSONL",
    "AUTO_NUDGE_MAX",
    "AUTO_SAVE_EVERY_TURN",
    "AUTO_SAVE_THRESHOLD",
    "BC",
    "BEHAVIOR_FILE",
    "BG",
    "BLOCKED_PATTERNS",
    "BM",
    "BO",
    "BOLD",
    "BTN_C",
    "BTN_G",
    "BTN_R",
    "BTN_Y",
    "BW",
    "BY",
    "C",
    "CHARS_PER_TOKEN",
    "CHARS_SINCE_REMIND",
    "CLOUD_MODEL_KEYS",
    "CLOUD_MODEL_NAMES",
    "CODE_WORDS",
    "COMPLEX_WORDS",
    "CONTEXT_FILL_RATIO",
    "CONTEXT_WATERMARK_FLOOR",
    "D",
    "DIMB",
    "DRIFT_REMINDER_CHARS",
    "FREE_SUFFIX",
    "G",
    "GLOBAL_HISTORY",
    "KEYS_FILE",
    "LAST_MODEL",
    "LAST_ROUTE",
    "LOCAL_DIRECTIVE_HINT",
    "LOCAL_NUM_CTX",
    "LOCAL_REQUEST_TIMEOUT_OVERRIDE",
    "LOG_FILE",
    "LOOP_MAX_CYCLES",
    "LOOP_MAX_SECONDS",
    "M",
    "MASTER_AI_IDENTITY_SYSTEM",
    "MAX_CONTINUATION_TURNS",
    "MODE_CONTRACTS",
    "MODE_FILE",
    "MODE_HINT_TITLES",
    "OLLAMA_URL",
    "PENDING_CONTINUATION",
    "PENDING_PLAN_REQUEST",
    "PENDING_PLAN_TEXT",
    "PENDING_USER_NOTE",
    "PIPER_MODEL",
    "PLAN_DEBATE_FALLBACK",
    "PLAN_DEBATE_MAX_ROUNDS",
    "PLAN_DEBATE_MERGER",
    "PLAN_DEBATE_PLANNER_A",
    "PLAN_DEBATE_PLANNER_B",
    "PRODUCT_UPDATE_COMMANDS",
    "PROJECTS_MD_FILE",
    "PROVIDER_CONTEXT_TOKENS",
    "R",
    "REASONING_WORDS",
    "REPLY_SHAPES_SYSTEM_ADDITION",
    "RESUME_FLAG",
    "RESUME_FLAG_MAX_AGE",
    "ROUTER_METRICS_FILE",
    "ROUTER_METRICS_MAX_SCAN",
    "SESSION_TS",
    "SURVIVAL_WORDS",
    "THREAD_FILE",
    "TOOL_REQUIRED_PHRASES",
    "TTS_ENABLED",
    "TTS_MAX_CHARS",
    "VISION_WORDS",
    "W",
    "WEATHER_WORDS",
    "WEB_WORDS",
    "WHISPER_MODEL",
    "X",
    "Y",
    "_ACKNOWLEDGMENT_RESPONSES",
    "_ACTION_VERBS",
    "_ACTIVE_PROFILE_FILE",
    "_AGENT_POLICY_COMMAND_RULES",
    "_AGENT_POLICY_EXFIL_TOKENS",
    "_AGENT_POLICY_REQUEST_RULES",
    "_ANSI_RE",
    "_APPROVED_DEFAULT_TTL_S",
    "_APPROVED_GLOBAL_SCOPE",
    "_APP_INSTALLED_RE",
    "_APP_SHAPE_WORDS",
    "_ARG_XML_TAG_RE",
    "_ATTACHMENT_DOTFILE_SUFFIXES",
    "_ATTACHMENT_SUFFIXES",
    "_AUTO_CONTEXT_FILE_ALIASES",
    "_AUTO_CONTEXT_MAX_FILES",
    "_A_APPEAR",
    "_A_AUTO",
    "_A_BOW",
    "_A_PLAN",
    "_A_PUNCH",
    "_A_SAFE",
    "_A_SHURIKEN",
    "_A_VANISH",
    "_BARE_KEYWORD_ARG_RE",
    "_BARE_KEYWORD_LINE_RE",
    "_BROWSER_DIRECTIVE_RE",
    "_BROWSER_READONLY_KINDS",
    "_BROWSER_SEP_RE",
    "_CD_SEPARATORS",
    "_CEREBRAS_MODELS_CACHE",
    "_CHAIN_SUDO_ACKS",
    "_CLEANUP_PROTECTED_PATHS",
    "_CLEANUP_SAFE_DELETE_HINTS",
    "_CLOUD_CIRCUITS",
    "_CLOUD_HARD_TIMEOUT",
    "_CLOUD_STORAGE_SERVICE_NAMES",
    "_CODE_SYNTHESIS_ARTIFACT_WORDS",
    "_CODE_SYNTHESIS_VERBS",
    "_COMPACT_KEEP_RECENT",
    "_CRONTAB_INVOCATION_RE",
    "_CRON_INSTALL_REDIRECT_RE",
    "_DEFAULT_FALLBACK_ORDER",
    "_DESKTOP_APP_ALIASES",
    "_DESKTOP_APP_COMMANDS",
    "_DESKTOP_DOC_SUFFIXES",
    "_DESTRUCTIVE_PATTERNS",
    "_DIM",
    "_DIRECTIVE_KEYWORDS_RE",
    "_DIRECTIVE_NAMES",
    "_DOWNLOAD_INSTALL_RE",
    "_EMAIL_PROVIDERS",
    "_EMBEDDED_DIRECTIVE_RE",
    "_ERROR_MARKERS",
    "_FALLBACK_ORDER_FILE",
    "_FEW_SHOT_SETTINGS_FILE",
    "_FILEISH_WORD_HINTS",
    "_FILE_INTENT_PATTERNS",
    "_FILE_LOCAL_CONTEXT_PHRASES",
    "_GATE_KIND_BY_LIST",
    "_GEMINI_MODEL_CHAIN",
    "_GOOGLE_WORKSPACE_BARE_NAV_COMMAND",
    "_GOOGLE_WORKSPACE_BARE_NAV_RE",
    "_GREETINGS",
    "_GROQ_MAX_INPUT_CHARS",
    "_GROQ_MODELS_CACHE",
    "_HELP_HIDDEN_FILE",
    "_IMAGE_PATH_RE",
    "_INSTRUCTION_VERB_BLACKLIST",
    "_INTERACTIVE_RUN_WORDS",
    "_JSON_TOOL_CALL_RE",
    "_LAST_BLOCKED_ACTION",
    "_LAST_BUSY_CLEARED_TS",
    "_LAST_DENIED_ACTION",
    "_LAST_HOOK_BLOCK",
    "_LAST_LIVE_TYPED_ACTIONS",
    "_LAST_MEMORY_SLICE_AT_S",
    "_LAST_MEMORY_SLICE_HASH",
    "_LIVE_TYPED_ACTIONS_CAP",
    "_LOCAL_FIND_HINTS",
    "_LOCAL_HARD_TIMEOUT",
    "_MARKDOWN_SKILL_DIRS",
    "_MAX_AUTO_CONTINUATIONS",
    "_MAX_LINE_REPEATS",
    "_MISTRAL_TOOL_CALLS_RE",
    "_MULTI_STEP_PHRASES",
    "_NONLOCAL_ROUTE_TIERS",
    "_NON_CODE_SYNTHESIS_HINTS",
    "_NON_CODE_VERIFY_EXTENSIONS",
    "_NOOP_TOKENS",
    "_NVIDIA_MODELS_CACHE",
    "_OLLAMA_CLOUD_MODELS_CACHE",
    "_OLLAMA_LOCAL_CACHE",
    "_OLLAMA_LOCAL_TTL",
    "_OPENCODE_GO_MODELS_CACHE",
    "_OPENCODE_GO_MODELS_TTL",
    "_OPENCODE_ZEN_MODELS_CACHE",
    "_OPENCODE_ZEN_MODELS_TTL",
    "_OPENROUTER_MODELS_CACHE",
    "_OPENROUTER_MODELS_TTL",
    "_OPEN_ANYWHERE_RE",
    "_PLACEHOLDER_HOSTS",
    "_PLACEHOLDER_URL_RE",
    "_PLAN_APPROVAL_PHRASES",
    "_POOLSIDE_MODELS_CACHE",
    "_POST_REPLY_GRACE",
    "_PREFIX_ANSI_RE",
    "_PRONOUNS_NEED_ANTECEDENT",
    "_PROSE_LEAK_RE",
    "_PROSE_TARGET_RE",
    "_PROVIDER_MODELS_TTL",
    "_PROVIDER_PICKER_ORDER",
    "_QUESTION_LEAD_WORDS",
    "_QUESTION_MARKERS",
    "_QWEN_MODELS_CACHE",
    "_READ_ALLOWED_ROOTS",
    "_READ_DENY_PATTERNS",
    "_READ_TARGET_FILLER_WORDS",
    "_REAL_CTX_FILE",
    "_REASON_DEPTHS",
    "_RECALL_TRIGGERS",
    "_REF_DENSITY_EXPAND_ABOVE",
    "_REF_DENSITY_TIGHT_BELOW",
    "_RELOAD_CARRY_FILE",
    "_ROUTE_HISTORY_BUDGETS",
    "_SECURITY_AUDIT_NAME_ALIASES",
    "_SENSEI_BRIDGE_URL",
    "_SETTINGS",
    "_SHELL_SYNTAX_MARKER_RE",
    "_SHOW_ACTIVITY_LIMIT",
    "_SKILL_SESSION_MARKER",
    "_SLICER_MAX_CHARS",
    "_SLICER_MAX_SLICES_PER_FILE",
    "_SLICER_POST_LINES",
    "_SLICER_PRE_LINES",
    "_STALL_TIMEOUT_S",
    "_STRAY_EMOJI_RE",
    "_SYMBOL_MIN_LENGTH",
    "_SYMBOL_PATTERNS",
    "_SYS_CAP_CACHE",
    "_SYS_CAP_TTL_S",
    "_TERMINAL_VISUAL_ACTION_RE",
    "_TERMINAL_VISUAL_BARE_REQUESTS",
    "_THINK",
    "_THINKING_T0",
    "_THINK_REJECT_CACHE",
    "_THINK_TAG_RE",
    "_THINK_TIPS",
    "_TIGHTER_INTENTS_RE",
    "_TIME_PHRASES",
    "_TIME_WORDS",
    "_TOOL_CALL_TAG_RE",
    "_TURN_DEPTH",
    "_TURN_EDITED_PATHS",
    "_TURN_PRIVATE_REASONS",
    "_TURN_VERIFIED_SINCE_EDIT",
    "_VALID_FALLBACK_NAMES",
    "_VERIFY_COMMAND_MARKERS",
    "_VERIFY_STOP_ATTEMPTS",
    "_VERIFY_STOP_MAX_ATTEMPTS",
    "_VISION_INTENT_RE",
    "_VISION_NEGATION_LOOKBACK",
    "_VISION_NEGATION_RE",
    "_VISUAL_RUN_WORDS",
    "_VOICE_FILE",
    "_WATERMARK_HEADROOM",
    "_WHATSNEW_STATE",
    "_WHOLE_FILE_CLOUD_BIAS_AT",
    "_WHOLE_FILE_MAX_CHARS",
    "_WHOLE_FILE_PHRASES",
    "_WHOLE_FILE_THRESHOLD",
    "_WIDER_INTENTS_RE",
    "_XML_FUNCTION_NAME_ALIASES",
    "_XML_FUNCTION_PARAM_RE",
    "_XML_FUNCTION_RE",
    "_XML_INVOKE_RE",
    "_XML_PARAM_RE",
]

_STALL_TIMEOUT_S = float(os.environ.get("MASTER_AI_STALL_TIMEOUT_S", "90"))

_TURN_DEPTH = 0  # >0 while a turn is actively running; nests for handle_loop_task

_ACTIVE_PROFILE_FILE = Path.home() / ".master_ai_active_profile"

KEYS_FILE = Path.home() / ".master_ai_keys"  # SHARED across profiles

OLLAMA_URL = "http://localhost:11434"

LOCAL_NUM_CTX = int(os.environ.get("MASTER_AI_LOCAL_NUM_CTX", "4096"))

LOCAL_REQUEST_TIMEOUT_OVERRIDE = None

PIPER_MODEL = Path.home() / "scripts/voices/en_US-lessac-medium.onnx"

LOG_FILE = Path.home() / "scripts/master.log"

WHISPER_MODEL = "base"

MODE_FILE = (
    Path.home() / ".master_ai_mode"
)  # persists last-selected mode across sessions

LAST_ROUTE = ""  # route used by the most recent handle() — for Review's "who" line

LAST_MODEL = ""  # model name used by the most recent handle() — for Review's "who" line

PENDING_PLAN_TEXT = ""

PENDING_PLAN_REQUEST = ""

PENDING_USER_NOTE = ""

_RELOAD_CARRY_FILE = Path.home() / ".master_ai_reload_carry"

PENDING_CONTINUATION = None

_THINKING_T0 = 0.0

_LAST_MEMORY_SLICE_HASH = ""

_LAST_MEMORY_SLICE_AT_S = 0.0

_LAST_DENIED_ACTION: dict[str, Any] = {}

_LAST_BLOCKED_ACTION: dict[
    str, Any
] = {}  # safeguard-blocked directive; consumed in process_reply to feed the BLOCKED back to the LLM next turn

_LAST_HOOK_BLOCK: dict[
    str, Any
] = {}  # P1.4: hook-blocked action (CREATE/EDIT); consumed in process_reply's action_failed branch to feed [HOOK BLOCKED] back

ACTIVE_PROJECT = ""

_SETTINGS = Path.home() / ".master_ai_settings"

TTS_ENABLED = "TTS_OFF" not in (_SETTINGS.read_text() if _SETTINGS.exists() else "")

CLOUD_MODEL_KEYS = {
    "opencode": "opencode",
    "nvidia": "nvidia",
    "nemotron": "openrouter",
    "hermes-405b": "openrouter",
    "openrouter": "openrouter",
    "kimi-k2": "opencode",
    "kimi-k2.6": "opencode",
    "kimi-k3": "opencode",
    "opencode-go": "opencode_go",
    "glm-5.3-flash": "opencode_go",
    "poolside-s": "poolside",
    "poolside-xs": "poolside",
}

CLOUD_MODEL_NAMES = frozenset(CLOUD_MODEL_KEYS)

PLAN_DEBATE_PLANNER_A = os.environ.get(
    "PLAN_DEBATE_PLANNER_A", "ollama-cloud::kimi-k2.7-code"
)

PLAN_DEBATE_PLANNER_B = os.environ.get(
    "PLAN_DEBATE_PLANNER_B", "nvidia/nemotron-3-ultra-550b-a55b:free"
)

PLAN_DEBATE_MERGER = os.environ.get("PLAN_DEBATE_MERGER", "minimax/minimax-m3:free")

PLAN_DEBATE_FALLBACK = os.environ.get(
    "PLAN_DEBATE_FALLBACK", "opencode::mimo-v2.5-free"
)

PLAN_DEBATE_MAX_ROUNDS = int(os.environ.get("PLAN_DEBATE_MAX_ROUNDS", "6"))

GLOBAL_HISTORY: list[Any] = []  # shared reference for signal handlers

CHARS_SINCE_REMIND = 0  # chars accumulated since last drift reminder

AUTO_SAVE_THRESHOLD = 10000  # update session file every ~10000 chars (was 3000)

AUTO_SAVE_EVERY_TURN = True

DRIFT_REMINDER_CHARS = 3000

SESSION_TS = int(time.time())  # fixed for entire session — overwrites same file

CHARS_PER_TOKEN = 3.6  # conservative English/code average; safety margin

CONTEXT_FILL_RATIO = 0.95  # Elijah: 95% of the model's real window

CONTEXT_WATERMARK_FLOOR = 60000  # never below the pre-fix local guard

_WATERMARK_HEADROOM = 0  # mutable headroom added when operator picks "keep going"

FREE_SUFFIX = ":free"  # OpenRouter marks free tiers with this suffix

PROVIDER_CONTEXT_TOKENS = {
    "ollama": 128000,
    "local": 128000,
    "opencode_go": 262144,
    "opencode-zen": 262144,
    "opencode": 262144,
    "poolside": 262144,
    "poolside-s": 262144,
    "poolside-xs": 262144,
    "laguna": 262144,
    "laguna-s": 262144,
    "laguna-xs": 262144,
    "laguna-s-2.1": 262144,
    "laguna-xs-2.1": 262144,
    "laguna-xs-2.1-free": 262144,
    "gemini": 1000000,
    "cerebras": 131072,
    "openrouter": 128000,
    "deepseek": 65536,
}

BEHAVIOR_FILE = Path.home() / ".sensei_behavior.md"

RESUME_FLAG = Path.home() / ".master_ai_resume"

RESUME_FLAG_MAX_AGE = 600  # seconds; stale resume flags must not revive old sessions

MAX_CONTINUATION_TURNS = 60  # operator-requested ceiling for long audit/task chains  # 2026-09-11: was hardcoded 60 — operator hit the cap on long audits

AUTO_NUDGE_MAX = 3

ACTIVE_PROJECT_FILE = Path.home() / ".master_ai_active_project"

ACTIVE_TASK_FILE = Path.home() / ".master_ai_active_task"

ACTIVE_MODEL_FILE = Path.home() / ".master_ai_active_model"

ACTIVE_TASK = ""

_CHAIN_SUDO_ACKS = 0

PROJECTS_MD_FILE = Path.home() / "scripts" / "PROJECTS.md"

THREAD_FILE = Path.home() / ".master_ai_thread"

_EMAIL_PROVIDERS = {
    "gmail": {
        "host": "smtp.gmail.com",
        "port": 465,
        "ssl": True,
        "key": "gmail_app_password",
        "domains": ("gmail.com", "googlemail.com"),
    },
    "aol": {
        "host": "smtp.aol.com",
        "port": 465,
        "ssl": True,
        "key": "aol_app_password",
        "domains": ("aol.com", "verizon.net", "yahoo.com"),
    },
    "outlook": {
        "host": "smtp-mail.outlook.com",
        "port": 587,
        "ssl": False,  # STARTTLS
        "key": "outlook_app_password",
        "domains": ("outlook.com", "hotmail.com", "live.com", "msn.com"),
    },
}

G = "\033[92m"  # bright green

C = "\033[96m"  # bright cyan

Y = "\033[33m"  # yellow

R = "\033[91m"  # bright red

M = "\033[95m"  # bright magenta

W = "\033[1m"  # bold (readable on any background)

D = "\033[0m"  # terminal default (black on light bg, white on dark)

X = "\033[0m"  # reset

BOLD = "\033[1m"

BC = "\033[1;34m"  # bold blue  — banner + INFO lines

BG = "\033[1;32m"  # bold green — banner accent + AI VOICE (conversational prose)

BW = "\033[97m"  # bright white — banner labels

BY = "\033[1;33m"  # bold yellow — PLAN / numbered steps / RUN: EDIT: CREATE:

BO = "\033[38;5;208m"  # orange — CAUTION / warnings / destructive-command previews

AMBER = "\033[38;2;199;118;26m"  # darker amber (#c7761a) — secondary yellow for footer hints / legend (distinct from BY/Y)

DIMB = "\033[2;34m"  # dim blue — SOURCES / URLs / reference footers

BM = "\033[1;35m"  # bold magenta — YOUR input (distinct from AI cyan)

BTN_G = "\033[42m\033[30m"  # green  bg + black text

BTN_Y = "\033[43m\033[30m"  # yellow bg + black text

BTN_R = "\033[41m\033[30m"  # red    bg + black text

BTN_C = "\033[46m\033[30m"  # cyan   bg + black text

ATTACHMENT_MAX_CHARS = 140000

_ATTACHMENT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".jsonc",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".conf",
    ".log",
    ".sql",
}

_ATTACHMENT_DOTFILE_SUFFIXES = {
    "",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".zprofile",
    ".zlogin",
    ".zlogout",
    ".vimrc",
    ".nanorc",
}

_DESKTOP_APP_ALIASES = {
    "libreoffice": ["libreoffice"],
    "libre office": ["libreoffice"],
    "writer": ["libreoffice", "--writer"],
    "libreoffice writer": ["libreoffice", "--writer"],
    "calc": ["libreoffice", "--calc"],
    "libreoffice calc": ["libreoffice", "--calc"],
    "impress": ["libreoffice", "--impress"],
    "libreoffice impress": ["libreoffice", "--impress"],
    "files": ["xdg-open", str(Path.home())],
    "file manager": ["xdg-open", str(Path.home())],
}

_DESKTOP_APP_COMMANDS = {
    "xdg-open",
    "gio",
    "libreoffice",
    "soffice",
    "google-chrome",
    "chrome",
    "chromium",
    "chromium-browser",
    "firefox",
    "nautilus",
    "discord",
    "gtk-launch",
}

_DESKTOP_DOC_SUFFIXES = {
    ".odt",
    ".ods",
    ".odp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pdf",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".txt",
    ".md",
}

_OPEN_ANYWHERE_RE = re.compile(
    r"\bopen(?:ing)?\s+(?:up\s+)?(?:the\s+|my\s+)?"
    r"([a-z0-9][a-z0-9 _-]{1,40}?)"
    r"(?=[.?!,]|\s+(?:up|now|please|for me|for you|on this|on my)\b|$)",
    re.IGNORECASE,
)

_APP_INSTALLED_RE = re.compile(
    r"^(?:is|do i have|did i (?:already )?(?:download|install)|check if)\s+"
    r"(?:i\s+(?:already\s+)?have\s+)?(.+?)\s+(?:already\s+)?"
    r"(?:installed|downloaded|on (?:this|my)\s+(?:computer|machine|pc|box|system))\??[\s.!?]*$",
    re.IGNORECASE,
)

_DOWNLOAD_INSTALL_RE = re.compile(
    r"^(?:download|install|get)\s+(?:me\s+)?(?:the\s+)?(?:a\s+)?(.+?)[\s.!?]*$",
    re.IGNORECASE,
)

_GOOGLE_WORKSPACE_BARE_NAV_RE = re.compile(
    r"^(?:go to|open|check|show me)\s+(?:my\s+)?google\s+(drive|gmail|mail|calendar)\s*[.!?]*$",
    re.IGNORECASE,
)

_GOOGLE_WORKSPACE_BARE_NAV_COMMAND = {
    "drive": ("drive.search", {"query": "", "max": 20}),
    "gmail": ("gmail.search", {"query": "", "max": 10}),
    "mail": ("gmail.search", {"query": "", "max": 10}),
    "calendar": ("calendar.list", {"max": 10}),
}

_SHOW_ACTIVITY_LIMIT = 20

_CLOUD_CIRCUITS: dict[str, float] = {}

CODE_WORDS = {
    "code",
    "python",
    "bash",
    "javascript",
    "js",
    "script",
    "debug",
    "function",
    "class",
    "error",
    "fix",
    "bug",
    "write",
    "program",
    "def",
    "import",
    "html",
    "css",
}

ALTER_WORDS = {
    "edit",
    "modify",
    "refactor",
    "patch",
    "rewrite",
    "replace",
    "rename",
    "install",
    "uninstall",
    "configure",
    "setup",
    "create",
    "delete",
    "remove",
    "build",
    "generate",
    "make",
    "update",
    "upgrade",
    "migrate",
}

VISION_WORDS = {
    "image",
    "photo",
    "picture",
    "see",
    "show",
    "look",
    "describe",
    "what is this",
    "analyze this",
    "read this",
    "whats in",
}

WEB_WORDS = {
    "latest",
    "today",
    "current",
    "news",
    "search",
    "find",
    "download",
    "who is",
    "what is happening",
    "price",
    "weather",
    "2024",
    "2025",
    "2026",
    "recently",
}

COMPLEX_WORDS = {
    "explain",
    "analyze",
    "compare",
    "difference",
    "pros",
    "cons",
    "plan",
    "strategy",
    "why",
    "how does",
    "what causes",
    "in depth",
    "detailed",
    "thorough",
    "research",
    "summarize",
    "write a report",
    "essay",
    "deep dive",
}

_MULTI_STEP_PHRASES = (
    # 2026-09-27: was only "and then" plus specific "then <verb>" combos
    # (return/give/summarize/report/list/print/save) -- missed ordinary
    # multi-step instructions using any other verb, e.g. "build X, then
    # wire it into Y, then test it" matched nothing at all. A bare " then "
    # is safe to add now that _looks_multi_step checks _looks_like_question
    # first -- a genuine question ("what happens then?") is already routed
    # away before phrase matching runs, so this can't reintroduce the
    # over-triggering that was just fixed.
    " then ",
    " and then ",
    " after that ",
    " next, ",
    " next step",
    " step by step",
    "step 1",
    "step 2",
    "step 3",
    "first, ",
    "first thing",
    "finally, ",
    " in order: ",
    " in sequence",
    " one by one",
    "1. ",
    "2. ",
    "3. ",
    "a, b, c,",
    "a, b, and c",
    "for each ",
    "for every ",
    "for all ",
    " for each of",
    " for all of",
    "all of the ",
    "all of these ",
    "all the ",
    "walk me through",
    "go through",
    "make sure to",
    "make sure all",
    "verify that",
    "verify each",
    "check that",
    "check each",
    "confirm that",
    "then return",
    "then give me",
    "then summarize",
    "then report",
    "then list",
    "then print",
    "then save",
)

_QUESTION_MARKERS = ("?",)

_QUESTION_LEAD_WORDS = (
    "how ",
    "what ",
    "why ",
    "when ",
    "where ",
    "who ",
    "which ",
    "can you explain",
    "could you explain",
    "do you know",
    "does it",
    "is it",
    "are there",
    "explain ",
)

_PLAN_APPROVAL_PHRASES = (
    "yes",
    "yep",
    "yeah",
    "yup",
    "go ahead",
    "go for it",
    "do it",
    "proceed",
    "sounds good",
    "lets build it",
    "let us build it",
    "build it",
    "lets do it",
    "let us do it",
    "confirmed",
    "approved",
    "sure",
    "okay",
    "ok",
)

REASONING_WORDS = {
    "think",
    "reason",
    "logic",
    "proof",
    "math",
    "calculate",
    "step by step",
    "walk me through",
    "figure out",
    "solve",
    "puzzle",
    "hypothesis",
}

SURVIVAL_WORDS = {
    "survival",
    "survive",
    "off-grid",
    "offgrid",
    "bushcraft",
    "apocalypse",
    "apocalyptic",
    "shelter",
    "tent",
    "fire",
    "forage",
    "trap",
    "snare",
    "hunt",
    "purify",
    "water filter",
    "rain catchment",
    "compost",
    "homestead",
    "permaculture",
    "solar",
    "battery",
    "generator",
    "hand pump",
    "well",
    "latrine",
    "outhouse",
    "preserve",
    "canning",
    "smoking",
    "curing",
    "dehydrate",
    "jerky",
    "root cellar",
    "tarp",
    "hut",
    "cabin",
    "log",
    "wattle",
    "daub",
    "earthbag",
    "cob",
    "adobe",
    "first aid",
    "splint",
    "wound",
    "remedy",
    "herb",
    "medicinal",
    "plant id",
    "scrap",
    "salvage",
    "repurpose",
    "fix broken",
    "rebuild",
    "from scratch",
    "grid down",
    "no power",
    "off the grid",
    "doomsday",
    "prepper",
    "prep",
}

TOOL_REQUIRED_PHRASES = (
    "refresh your memory",
    "refresh memory",
    "master update",
    "update master ai",
    "update master-ai",
    "update sensei",
    "sensei update",
    "update my project",
    "update the project",
    "update project",
    "save the conversation",
    "save this conversation",
    "save this chat",
    "write to ",
    "write a file",
    "edit the file",
    "edit this file",
    "create the file",
    "create a file",
    "create one complete",
    "create and run",
    "make a script",
    "write a script",
    "make a video",
    "create a video",
    "generate a video",
    "make a clip",
    "create a clip",
    "generate a clip",
    "make a movie",
    "create a movie",
    "generate a movie",
    "make a screen",
    "create a screen",
    "generate a screen",
    "make a credit screen",
    "create a credit screen",
    "matrix credit screen",
    "matrix credits",
    "credit roll",
    "complete bash script",
    "bash script at",
    "script at /",
    "script at ~",
    "chmod +x",
    "verify it exists",
    "delete the file",
    "run this",
    "run the command",
    "execute this",
    "execute the",
)

_CODE_SYNTHESIS_VERBS = {
    "make",
    "build",
    "create",
    "generate",
    "write",
    "code",
    "program",
    "draw",
    "animate",
    "render",
    "simulate",
    "design",
}

_CODE_SYNTHESIS_ARTIFACT_WORDS = {
    "animation",
    "effect",
    "screen",
    "screensaver",
    "credit",
    "credits",
    "roll",
    "game",
    "toy",
    "demo",
    "dashboard",
    "interface",
    "ui",
    "visual",
    "visualizer",
    "simulation",
    "simulator",
    "scene",
    "sprite",
    "terminal",
    "ascii",
    "curses",
    "tui",
    "cli",
    "browser",
    "web",
    "webpage",
    "page",
    "canvas",
    "html",
    "script",
    "tool",
    "app",
    "program",
    "video",
    "clip",
    "movie",
    "intro",
    "outro",
    "logo",
    "title",
    "particles",
}

_NON_CODE_SYNTHESIS_HINTS = {
    "joke",
    "story",
    "poem",
    "song",
    "recipe",
    "list",
    "plan",
    "summary",
    "report",
    "email",
    "message",
    "caption",
    "name",
    "names",
}

_TERMINAL_VISUAL_BARE_REQUESTS = {
    "matrix rain",
    "matrix-rain",
    "matrix rain please",
    "matrix animation",
    "matrix animation please",
    "matrix terminal",
    "matrix terminal please",
    "matrix screensaver",
    "matrix screensaver please",
    "terminal rain",
    "terminal animation",
    "terminal screensaver",
}

_TERMINAL_VISUAL_ACTION_RE = re.compile(
    r"\b(run|start|launch|open|show|play|do|make|create|generate|render|"
    r"animate|display|pull\s+up)\b"
)

PRODUCT_UPDATE_COMMANDS = {
    "update",
    "upgrade",
    "master update",
    "update master",
    "update master ai",
    "update master-ai",
    "sensei update",
    "update sensei",
}

ROUTER_METRICS_FILE = Path.home() / ".master_ai_router_metrics.jsonl"

ROUTER_METRICS_MAX_SCAN = 500

WEATHER_WORDS = {"weather", "forecast", "temperature", "humidity"}

_RECALL_TRIGGERS = (
    "remember",
    "recall",
    "what did we",
    "earlier you",
    "before we",
    "last time",
    "previously",
    "you said",
    "we talked about",
)

_PRONOUNS_NEED_ANTECEDENT = {"it", "this", "that", "them", "those", "these"}

_ACTION_VERBS = {
    "do",
    "run",
    "fix",
    "delete",
    "remove",
    "edit",
    "change",
    "update",
    "install",
    "start",
    "stop",
    "restart",
    "kill",
    "try",
}

_GREETINGS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "sup",
    "howdy",
    "hola",
    "thanks",
    "thank",
    "thx",
    "ty",
    "ok",
    "okay",
    "k",
    "cool",
    "nice",
    "great",
    "good",
    "yes",
    "yep",
    "yeah",
    "y",
    "no",
    "nope",
    "nah",
    "n",
    "bye",
    "goodbye",
    "cya",
    "later",
}

_ACKNOWLEDGMENT_RESPONSES = {
    "nice": "Okay.",
    "ok": "Okay.",
    "okay": "Okay.",
    "cool": "Okay.",
    "thanks": "You're welcome.",
    "thank you": "You're welcome.",
    "thx": "You're welcome.",
    "ty": "You're welcome.",
    "got it": "Okay.",
}

_FILE_INTENT_PATTERNS = [
    re.compile(
        r"^where(?:\s+is|\s+are|\s+was|\s+were|'s|s)\s+(?:the\s+|my\s+|a\s+|an\s+|some\s+)?(.+)$"
    ),
    re.compile(r"^find(?:\s+me)?\s+(?:the\s+|my\s+|a\s+|an\s+|some\s+)?(.+)$"),
    re.compile(r"^locate\s+(?:the\s+|my\s+|a\s+|an\s+)?(.+)$"),
    re.compile(r"^do\s+i\s+have\s+(?:a\s+|an\s+|any\s+)?(.+)$"),
    re.compile(r"^show\s+me\s+(?:the\s+|my\s+|a\s+|an\s+)?(.+?)(?:\s+please)?$"),
]

_FILE_LOCAL_CONTEXT_PHRASES = (
    "on my computer",
    "on this computer",
    "on my machine",
    "on this machine",
    "on my system",
    "on disk",
    "on the disk",
    "in my files",
    "in files",
    "in my folders",
    "in folders",
    "in my directory",
    "in my directories",
    "in my home",
    "under ~",
    "under /home",
    "in desktop",
    "in downloads",
    "in documents",
    "in scripts",
    "file path",
    "path to",
)

_FILEISH_WORD_HINTS = {
    "file",
    "files",
    "folder",
    "folders",
    "dir",
    "directory",
    "directories",
    "path",
    "paths",
    "readme",
    "license",
    "makefile",
    "dockerfile",
    "requirements",
    "pyproject",
    "package.json",
    "config",
    "settings",
    "log",
    "logs",
    "script",
    "scripts",
    "desktop",
    "downloads",
    "documents",
    "templates",
}

_AUTO_CONTEXT_FILE_ALIASES = {
    # Users ask for the "Codex md" handoff, but the repo's real handoff file
    # is still named CLAUDE.md for cross-agent continuity.
    "codex.md": "CLAUDE.md",
    "codes.md": "CLAUDE.md",
    "codex_memory.md": "CLAUDE.md",
    "codex-memory.md": "CLAUDE.md",
}

_DIRECTIVE_NAMES = ("RUN", "RUNTERM", "READ", "CREATE", "EDIT", "REMEMBER")

_APP_SHAPE_WORDS = {
    "app",
    "application",
    "script",
    "tool",
    "program",
    "software",
    "build",
    "make",
    "create",
    "develop",
    "generate",
    "write",
    "save",
    "install",
    "uninstall",
    "python",
    "html",
    "javascript",
    "tkinter",
    "browser",
    "file",
    "folder",
    "directory",
}

_IMAGE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[~/.\w\-]+\.(?:png|jpe?g|gif|webp|bmp|tiff?)\b",
    re.I,
)

_VISION_INTENT_RE = re.compile(
    r"\b(?:describe|caption|analyze|annotate|ocr|read|what(?:'s| is)\s+in|show\s+me|look\s+at)\s+"
    r"(?:this|that|the|my|a|an|these|those)?\s*"
    r"(?:image|photo|picture|screenshot|snapshot|thumbnail)s?\b",
    re.I,
)

_VISION_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|doesn'?t|didn'?t|never|no\s+need\s+to|without|skip(?:ping)?|won'?t|cannot|can'?t|isn'?t|aren'?t)\b",
    re.I,
)

_VISION_NEGATION_LOOKBACK = 40

_LOCAL_FIND_HINTS = {
    "file",
    "folder",
    "directory",
    "dir",
    "repo",
    "project",
    "script",
    "manual",
    "pdf",
    "doc",
    "docx",
    "txt",
    "md",
    "json",
    "yaml",
    "yml",
    "csv",
    "resume",
    "résumé",
    "cv",
    "certificate",
    "transcript",
    "notes",
}

_PROSE_TARGET_RE = re.compile(
    r"\b(it'?s|i'?m|i'?ve|that'?s|probably|maybe|which|because|actually)\b",
    re.IGNORECASE,
)

_CLOUD_STORAGE_SERVICE_NAMES = {
    "google drive",
    "my drive",
    "gdrive",
    "drive",
    "dropbox",
    "onedrive",
    "one drive",
    "icloud",
    "icloud drive",
    "box",
    "box.com",
}

_TIME_WORDS = frozenset(
    {
        "yesterday",
        "tonight",  # strong on their own
    }
)

_TIME_PHRASES = (
    "last night",
    "this morning",
    "who won",
    "who's winning",
    "whos winning",
    "what happened at",
    "what happened last",
    "what happened yesterday",
    "what happened today",
    "what happened tonight",
    "score of",
    "result of",
    "results of",
    "as of today",
    "as of now",
    "who is the president",
    "who is the ceo of",
    "stock price of",
    "current stock",
    "stock market today",
    "news today",
    "today's news",
    "todays news",
    "latest news",
    "breaking news",
    "headlines today",
    "playoff games",
    "playoff game tonight",
    "games tonight",
    "games today",
    "game tonight",
    "game today",
)

_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "placeholder.com",
    "yourdomain.com",
    "your-domain.com",
    "domain.com",
    "website.com",
    "mysite.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
}

_PLACEHOLDER_URL_RE = re.compile(
    r'https?://(?:[^\s<>"\')\]]+)',
    re.IGNORECASE,
)

_GEMINI_MODEL_CHAIN = (
    "gemini-2.5-flash",  # newest flash — usually has free quota
    "gemini-flash-latest",  # alias that Google points at current free tier
    "gemini-2.0-flash",  # prior-gen fallback
    "gemini-2.5-flash-lite",  # smaller / cheaper — last resort
)

_FEW_SHOT_SETTINGS_FILE = Path.home() / ".master_ai_settings"

_TURN_PRIVATE_REASONS: list[str] = []

_THINK_REJECT_CACHE: dict[str, bool] = {}

REPLY_SHAPES_SYSTEM_ADDITION = (
    "\n\n[REPLY SHAPES]\n"
    "Start your answer immediately — no preamble, no scratchpad line.\n"
    "Structure your answer in these shapes so the UI can color-code:\n"
    "  - PLAN lines: numbered steps ('1.', '2.') or directive lines (read/run/runterm/create/edit, each on its own line at column 0)\n"
    "  - CAUTION lines: start with '⚠' when something destructive or\n"
    "    risky is about to happen — rm, force-push, drop, systemctl stop\n"
    "  - SOURCES lines: when referencing URLs, end reply with a 'Sources:'\n"
    "    line followed by one URL per line\n"
    "  - Everything else is plain conversational prose (your voice)\n"
)

LOCAL_DIRECTIVE_HINT = (
    "[How to respond when the user wants a file created, edited, a command run,\n"
    "or browser work done.]\n"
    "Reason in ONE short prose sentence describing the choice. The sentence must contain\n"
    "NO colon-suffixed directive words at all — those are reserved keywords the parser\n"
    "matches verbatim. Then on the NEXT LINE, at column 0, emit the directive itself.\n"
    "Available directive keywords: read, run, runterm, create, edit, ask, done, remember, search,\n"
    "browser_click, browser_fill, browser_read, browser_nav, browser_screenshot — each followed by a colon,\n"
    "only on its own line, never inside a sentence.\n\n"
    "Available directive shapes (use each on its OWN line, never inline in prose):\n"
    "  - read followed by a colon and a filepath\n"
    "  - run followed by a colon and a bash command (captured output: ls, git, pytest, apt)\n"
    "  - runterm followed by a colon and a bash command (visual / animated / TTY scripts)\n"
    "  - create followed by a colon and a filepath, then a content block bounded by\n"
    "    triple-less-than CONTENT and triple-greater-than CONTENT markers\n"
    "  - edit followed by a colon and a filepath, then FIND and REPLACE blocks\n"
    "  - browser_click followed by a colon and a CSS selector\n"
    "  - browser_fill followed by a colon, a CSS selector, then ' :: ' (or ' => '),\n"
    "    then the value to type\n"
    '  - browser_read followed by a colon and a CSS selector (use "main" for the page)\n'
    "  - browser_nav followed by a colon and a URL\n"
    '  - browser_screenshot followed by a colon and "viewport" (default) or "fullpage"\n'
    "  - done followed by a colon and a one-line summary (ends the agent loop)\n\n"
    "Pick runterm when the script clears the screen, animates, reads keyboard, or needs\n"
    "a real TTY. Pick run for everything else. Use browser_* when work is on the active\n"
    "Chrome tab. For chat or explanation, reply as plain prose with no directive at all.\n\n"
    "Browser rules: when a [BROWSER PAGE CONTEXT] block is present it is ground truth;\n"
    "selectors must match elements in it, not invented ones. When a [PREVIOUS ROUND\n"
    "RESULTS] block is present it is what already happened — do not re-emit completed\n"
    'actions. If a previous result shows status "rejected" or "denied", pick a different\n'
    "selector or emit done with what was achieved. Emit done when the work is finished.\n\n"
    "Result honesty: never state, paraphrase, or imply a command's result before the\n"
    "dispatcher runs it. Reason about what you're checking, not what the output will be.\n"
    "Never write 'Result:' or 'Output:' from a guess. The actual machine output is\n"
    "authoritative once it arrives.\n\n"
    "User: "
)

_VOICE_FILE = Path.home() / "scripts/master_ai_voice.json"

MASTER_AI_IDENTITY_SYSTEM = (
    "You are Master AI — Elijah's collaborator on your-machine (Linux). "
    "You run as Sensei (tmux agent) or Pupil (browser UI), with Dojo (project picker), "
    "Belts (themes), voice servers (stt:5050 / tts), harvest (cache+few-shot), and "
    "doctor/health command — every surface, every command, every file IS you. "
    "When the user says 'you' / 'your app' / 'this app' / 'this project,' they mean "
    "Master AI itself. Read those prompts as self-referential — never advise yourself "
    "like a generic developer building from scratch.\n\n"
    "2026-09-07 standing rule, every reply, no exceptions: any answer longer than a "
    "couple sentences must end with a short 'Summary' section (2-5 sentences or bullets) "
    "restating the core point in plain language. Every sentence in the reply, summary "
    "included, must be a complete sentence ending in real terminal punctuation "
    "(. ! or ?) — never stop mid-clause, mid-word, or on a dangling conjunction. If you "
    "are running low on room to finish, cut detail from the middle, not the ending — the "
    "summary and its closing punctuation must always land.\n\n"
    "2026-09-27 standing rule: follow the rule above silently. Never narrate, quote, or "
    "reason out loud about these formatting instructions in your reply — no 'I need to "
    "make sure every sentence ends with punctuation' or 'let me draft this as a numbered "
    "list to comply' text. Output only the actual answer the user asked for."
)

_SYS_CAP_CACHE = {"ts": 0.0, "block": ""}

_SYS_CAP_TTL_S = 600.0

_GROQ_MAX_INPUT_CHARS = 22000

_OPENCODE_GO_MODELS_CACHE = Path.home() / ".master_ai_opencode_go_models_cache.json"

_OPENCODE_GO_MODELS_TTL = 24 * 3600

_OPENCODE_ZEN_MODELS_CACHE = Path.home() / ".master_ai_opencode_zen_models_cache.json"

_OPENCODE_ZEN_MODELS_TTL = 24 * 3600

_CLOUD_HARD_TIMEOUT = 150

_LOCAL_HARD_TIMEOUT = 600

_MAX_AUTO_CONTINUATIONS = 3

_FALLBACK_ORDER_FILE = Path.home() / ".master_ai_fallback_order.json"

_DEFAULT_FALLBACK_ORDER = [
    "opencode",
    "nemotron",
    "openrouter",
    "deepseek-r1",
    "hermes-405b",
    "nvidia",
    "nvidia-nano",
]

_VALID_FALLBACK_NAMES = {
    "opencode",
    "nvidia",
    "nvidia-nano",
    "hermes-405b",
    "gpt-oss-120b",
    "nemotron",
    "qwen3-coder",
    "deepseek-r1",
    "openrouter",
}

ASK_CLOUD_BARE_PROVIDERS = frozenset(
    {
        "opencode",
        "opencode-go",
        "glm-5.3-flash",
        "nvidia",
        "nvidia-nano",
        "hermes-405b",
        "gpt-oss-120b",
        "nemotron",
        "qwen3-coder",
        "deepseek-r1",
        "openrouter",
        "poolside-s",
        "poolside-xs",
    }
)

TTS_MAX_CHARS = 500  # truncate long replies so TTS doesn't hang on documents

ARIA_VOICE_CONFIG = Path.home() / "ai-controller" / "voices" / "aria" / "config.json"

_COMPACT_KEEP_RECENT = 12  # most recent raw messages stay verbatim, full detail

_ROUTE_HISTORY_BUDGETS = {
    "chat": 14000,  # cloud_fast/Groq — real request-size ceiling, raise carefully
    "tool": 18000,  # local with tool-required intent
    "code": 70000,  # CODE_WORDS / ALTER_WORDS local
    "reasoning": 220000,  # cloud_deep/DeepSeek — ~128K token window, was 10x undersized
    "vision": 16000,  # local llava
    "default": 70000,  # legacy local cap (pre-P1.2)
}

_NONLOCAL_ROUTE_TIERS = {
    "cloud_fast": "chat",
    "cloud_deep": "reasoning",
    "cloud": "reasoning",
    "cloud_vision": "vision",
    "vision": "vision",
    "web": "reasoning",
}

_SLICER_PRE_LINES = 50

_SLICER_POST_LINES = 100

_SLICER_MAX_CHARS = 8000

_WHOLE_FILE_THRESHOLD = 200  # files <= this many lines, inject whole

_WHOLE_FILE_MAX_CHARS = (
    64000  # escape-hatch cap (raised 2026-09-11 for framework audits)
)

_WHOLE_FILE_CLOUD_BIAS_AT = (
    15000  # inject_chars > this triggers cloud bias if available
)

_AUTO_CONTEXT_MAX_FILES = 2

_SLICER_MAX_SLICES_PER_FILE = 2

_SYMBOL_MIN_LENGTH = 4

_REF_DENSITY_TIGHT_BELOW = 3  # symbol appears <3 times → tighten

_REF_DENSITY_EXPAND_ABOVE = 15  # symbol appears >15 times → expand

_TIGHTER_INTENTS_RE = re.compile(
    r"\b(?:rename|where\s+is|find|locate|show\s+me\s+the\s+def(?:inition)?|"
    r"what\s+line|on\s+what\s+line)\b",
    re.I,
)

_WIDER_INTENTS_RE = re.compile(
    r"\b(?:fix|debug|understand|audit|trace|why\s+does|why\s+is|"
    r"root\s+cause|how\s+does|walk\s+through|explain\s+the\s+flow)\b",
    re.I,
)

_INSTRUCTION_VERB_BLACKLIST = frozenset(
    {
        "READ",
        "RUNTERM",
        "CREATE",
        "EDIT",
        "WRITE",
        "OPEN",
        "DELETE",
        "REMOVE",
        "DONE",
        "PLAN",
        "TODO",
        "FIXME",
        "NOTE",
        "WARN",
        "INFO",
    }
)

_SYMBOL_PATTERNS = [
    re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(\s*\)"),  # function_name()
    re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b"),  # ALL_CAPS_NAMES
    re.compile(r"\b([A-Z][a-zA-Z0-9]{3,})\b"),  # CamelCase
    re.compile(r"(?:def|class)\s+([a-z_][a-zA-Z0-9_]{3,})"),  # def foo / class Bar
    re.compile(r"`([a-zA-Z_][a-zA-Z0-9_]{3,})`"),  # `backtick`
    re.compile(r"\b([a-z][a-z0-9_]{3,}_[a-z0-9_]+)\b"),  # snake_case (must contain _)
]

_WHOLE_FILE_PHRASES = (
    "whole file",
    "entire file",
    "full file",
    "read all of",
    "full review",
    "complete file",
    "all of the file",
    # 2026-09-11: audit/review prompts imply whole-file intent
    "audit",
    "review this file",
    "walk through this file",
    "analyze this file",
)

_APPROVED_DEFAULT_TTL_S = 24 * 3600  # 24h

_APPROVED_GLOBAL_SCOPE = "*"  # cwd token meaning "any directory"

_PREFIX_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[@-_]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)

_ERROR_MARKERS = ("unavailable", "timed out", "no response", "error:", "failed")

_A_SAFE = [
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "      \\*/        ",
        "      /|\\        ",
        "     /   \\       ",
    ],
    [
        "   /  ___  \\     ",
        "  ║ =(o_o)= ║    ",
        "  ║   \\*/   ║    ",
        "   \\  /|\\  /     ",
        "      / \\        ",
    ],
    [
        "  ╔══(___) ══╗   ",
        "  ║  (o_o)  ║    ",
        "  ║   \\*/   ║    ",
        "  ╚═══/|\\═══╝    ",
        "     [GUARDED]   ",
    ],
]

_A_PLAN = [
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "      /|\\        ",
        "     / | \\       ",
        "    /  |  \\      ",
    ],
    [
        "      ___        ",
        "   ══(-_-)══     ",
        "    __(|)__      ",
        "   /  /|\\  \\     ",
        "  /  /   \\  \\    ",
    ],
    [
        "      ___        ",
        "   ══(~_~)══     ",
        "  /‾‾‾|‾‾‾\\     ",
        " |  z z z  |     ",
        "  \\_______/      ",
    ],
    [
        "      ___        ",
        "   ══(- -)══     ",
        "  /‾‾‾|‾‾‾\\     ",
        " |   ─OM─  |     ",
        "  \\_______/      ",
    ],
]

_A_AUTO = [
    [
        "  ___            ",
        "=(o▶o)=          ",
        "  )|( >>         ",
        " / \\ >>          ",
        "/   \\            ",
    ],
    [
        "      ___        ",
        "   >=(o▶o)=      ",
        "      )|( >>>    ",
        "     / \\         ",
        "    /   \\        ",
    ],
    [
        "           ___   ",
        "  >>> >>> =(o▶o)=",
        "           )|(⚡  ",
        "          / \\    ",
        "         /   \\   ",
    ],
]

_A_SHURIKEN = [
    ["   ✦  ─┼─  ✦    ", "      ─┼─        ", "   ✦  ─┼─  ✦    "],
    ["    ╲  ─╬─  ╱   ", "       ─╬─       ", "    ╱  ─╬─  ╲   "],
    ["   ✧  ═╬═  ✧    ", "      ═╬═        ", "   ✧  ═╬═  ✧    "],
    ["  ★★  ═╬═  ★★   ", "      ═╬═        ", "  ★★  ═╬═  ★★   "],
]

_A_BOW = [
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "      /|\\        ",
        "     /   \\       ",
    ],
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "     \\|/         ",
        "     / \\         ",
    ],
    [
        "   ___           ",
        "  (o_o)══        ",
        "  _\\|            ",
        "   / \\           ",
    ],
    [
        "  ___            ",
        " (^_^)══         ",
        "  _\\|            ",
        "   / \\           ",
    ],
]

_A_PUNCH = [
    ["   o             ", "  /|\\            ", "  / \\            "],
    ["   o             ", "  \\|~>           ", "  / \\            "],
    ["   o             ", "  \\| ~~> ✦       ", "  / \\            "],
]

_A_VANISH = [
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "      /|\\        ",
        "     /   \\       ",
    ],
    [
        "      ___        ",
        "   ══(o_o)══  ~  ",
        "      /|\\   ~~   ",
        "     /   \\ ~~~   ",
    ],
    [
        "      ___        ",
        "   ══(._.)══ ~~~ ",
        "      /|\\ ~~~~~  ",
        "     /   \\       ",
    ],
    [
        "       _         ",
        "    ══(.)══ ~~~  ",
        "       |  ~~~~~  ",
        "       |         ",
    ],
    [
        "                 ",
        "       ~ ~~~~    ",
        "      ~  ~~~~    ",
        "                 ",
    ],
]

_A_APPEAR = [
    [
        "                 ",
        "                 ",
        "    ~ ~~~~       ",
        "   ~  ~~~~       ",
        "                 ",
    ],
    [
        "       _         ",
        "    ══(.)══      ",
        "       |  ~~~~   ",
        "       |         ",
    ],
    [
        "      ___        ",
        "   ══(._.)══     ",
        "      /|\\        ",
        "     /   \\       ",
    ],
    [
        "      ___        ",
        "   ══(o_o)══     ",
        "      /|\\        ",
        "     /   \\       ",
    ],
]

MODE_CONTRACTS = {
    "plan": {
        "tagline": "concrete execution plan first — default, no execution",
        "contract": (
            "1. Ask for work, I inspect context mentally and draft a concrete execution plan.\n"
            "2. Good plans name likely files, exact changes, risks, and verification.\n"
            "3. Press 1 or Enter to execute the approved plan, 2 to edit, 3 to discard.\n"
            "4. I ask questions only when the answer blocks safe execution."
        ),
    },
    "review": {
        "tagline": "asks before every command (per-action confirm)",
        "contract": (
            "1. Every RUN: / EDIT: / CREATE: asks '1/2/3/4/5 — Yes/Always/No/Edit/Ask'.\n"
            "2. Shows who / what / why / where before the prompt.\n"
            "3. Destructive patterns (rm -rf, drop table, force-push) still pause.\n"
            "4. Sudo is always handed off to your terminal — never runs inline.\n"
            "5. Use this when you want to watch every step land."
        ),
    },
    "auto": {
        "tagline": "commands flow without asking (destructive still pauses)",
        "contract": (
            "1. Execute immediately — RUN: / EDIT: / CREATE: fire without asking.\n"
            "2. Minimize interruptions — no pauses for routine questions.\n"
            "3. Prefer action over planning — start working.\n"
            "4. Course corrections welcome — 'mode plan' or 'mode review' takes the wheel back.\n"
            "5. Destructive actions still pause — rm -rf, drop table, force-push,\n"
            "   package uninstall, ollama rm, chmod -R, systemctl stop.\n"
            "6. No data exfiltration — secrets/keys never sent to external services."
        ),
    },
}

MODE_HINT_TITLES = {
    "plan": "Plan Mode — concrete execution plan",
    "review": "Review Mode — confirm every command",
    "auto": "⚠  Auto Mode Active — you are allowing:",
}

_OPENROUTER_MODELS_CACHE = Path.home() / ".master_ai_openrouter_models_cache.json"

_OPENROUTER_MODELS_TTL = 24 * 3600

_REAL_CTX_FILE = Path.home() / ".master_ai_real_ctx.json"

_NVIDIA_MODELS_CACHE = Path.home() / ".master_ai_nvidia_models_cache.json"

_CEREBRAS_MODELS_CACHE = Path.home() / ".master_ai_cerebras_models_cache.json"

_PROVIDER_MODELS_TTL = 24 * 3600

_POOLSIDE_MODELS_CACHE = Path.home() / ".master_ai_poolside_models_cache.json"

_QWEN_MODELS_CACHE = Path.home() / ".master_ai_qwen_models_cache.json"

_GROQ_MODELS_CACHE = Path.home() / ".master_ai_groq_models_cache.json"

_OLLAMA_CLOUD_MODELS_CACHE = Path.home() / ".master_ai_ollama_cloud_models_cache.json"

_OLLAMA_LOCAL_CACHE = {"ts": 0.0, "models": []}

_OLLAMA_LOCAL_TTL = 30

_PROVIDER_PICKER_ORDER = (
    "local",
    "ollama-cloud",
    "openrouter",
    "nvidia",
    "cerebras",
    "groq",
    "qwen",
)

_POST_REPLY_GRACE = 10.0

_LAST_BUSY_CLEARED_TS = 0.0

_DIM = "\033[3m"  # italic only — visible on light bg

_THINK = "\033[3;38;5;240m"  # italic + darker grey, readable on light terminal

_THINK_TIPS = [
    ("thinking...", "checking memory for context"),
    ("thinking...", "routing through fallback chain"),
    ("type ahead", "I'll queue your next message"),
    ("slow?", "try 'model groq' next time"),
    ("cache", "response cache stats"),
    ("save session", "archive now + generate summary"),
    ("scrollback", "50k lines — drag to copy back"),
]

_HELP_HIDDEN_FILE = Path.home() / ".master_ai_help_hidden"

BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
]

_EMBEDDED_DIRECTIVE_RE = re.compile(
    r'(?<=[^\s\'"` ])(RUN|RUNTERM|READ|CREATE|EDIT|SEND_EMAIL|REMEMBER):',
    re.IGNORECASE,
)

_STRAY_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U0000fe0f"  # variation selector-16 (emoji presentation, e.g. ✌️)
    "\U0000200d"  # zero-width joiner (compound/skin-tone emoji sequences)
    "]"
)

_PROSE_LEAK_RE = re.compile(
    r"\b(let'?s|we can|i'll|i will|actually|which we saw|that is a|that's a|those are)\b",
    re.IGNORECASE,
)

_CLEANUP_PROTECTED_PATHS = (
    "~/Downloads",
    "$HOME/Downloads",
    "/home/user/Downloads",
    "~/Desktop",
    "$HOME/Desktop",
    "/home/user/Desktop",
    "~/Documents",
    "$HOME/Documents",
    "/home/user/Documents",
    "~/Pictures",
    "$HOME/Pictures",
    "/home/user/Pictures",
    "~/Videos",
    "$HOME/Videos",
    "/home/user/Videos",
    "~/Music",
    "$HOME/Music",
    "/home/user/Music",
    "~/scripts",
    "$HOME/scripts",
    "/home/user/scripts",
    "~/.ollama",
    "$HOME/.ollama",
    "/home/user/.ollama",
)

_CLEANUP_SAFE_DELETE_HINTS = (
    "/.cache/",
    "/Trash/",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules/.cache",
    "/tmp/",
    "/var/tmp/",
    "Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
)

_AGENT_POLICY_REQUEST_RULES = (
    (
        "credential theft",
        (
            "steal password",
            "steal passwords",
            "dump passwords",
            "dump browser passwords",
            "extract browser passwords",
            "exfiltrate credentials",
            "steal cookies",
            "session hijack",
            "browser cookie dump",
        ),
    ),
    (
        "phishing or fraud",
        (
            "phishing page",
            "phishing site",
            "credential harvesting",
            "harvest credentials",
            "fake login",
            "spoof login",
            "bank scam",
            "romance scam",
        ),
    ),
    (
        "malware or persistence",
        (
            "keylogger",
            "backdoor",
            "reverse shell",
            "persistence payload",
            "stealth persistence",
            "ransomware",
            "cryptominer",
            "botnet",
        ),
    ),
    (
        "unauthorized access",
        (
            "privilege escalation exploit",
            "exploit ssh",
            "brute force ssh",
            "bypass login",
            "break into",
            "hack into",
            "unauthorized access",
        ),
    ),
    (
        "scaled abuse",
        (
            "ddos",
            "denial of service",
            "spam thousands",
            "mass spam",
            "bulk account creation",
            "fake accounts",
            "credential stuffing",
        ),
    ),
    (
        "covert surveillance",
        (
            "spy on",
            "track someone",
            "monitor someone",
            "secretly record",
            "stalk",
            "stalking",
            "without them knowing",
        ),
    ),
)

_AGENT_POLICY_COMMAND_RULES = (
    (
        "credential theft",
        (
            "login data",
            "cookies",
            "key4.db",
            "signons.sqlite",
            ".aws/credentials",
            ".config/gcloud",
            ".ssh/id_rsa",
            ".ssh/id_ed25519",
        ),
    ),
    (
        "malware or persistence",
        (
            "nc -e",
            "ncat -e",
            "bash -i >&",
            "/dev/tcp/",
            "crontab",
            "/etc/cron",
            "/var/spool/cron",
            "authorized_keys",
            "systemctl enable --now",
            "nohup",
        ),
    ),
    (
        "scaled abuse",
        (
            "hping3",
            "slowloris",
            "masscan",
            "hydra ",
            "medusa ",
        ),
    ),
)

_CRON_INSTALL_REDIRECT_RE = re.compile(
    r">>?\s*['\"]?\S*(?:/etc/crontab|/etc/cron\.[a-z]+|/var/spool/cron)"
)

_CRONTAB_INVOCATION_RE = re.compile(
    r"(?:^|[;&|\n]|\$\(|`)\s*(?:sudo\s+|env\s+)*crontab\b([^;&|\n]*)"
)

_AGENT_POLICY_EXFIL_TOKENS = (
    "curl ",
    "wget ",
    "scp ",
    "rsync ",
    "nc ",
    "ncat ",
    "socat ",
    "ftp ",
)

_DESTRUCTIVE_PATTERNS = (
    # deletion / shredding
    "shred ",
    "unlink ",
    "rmdir ",
    "trash-put ",
    # git destructive
    "git reset --hard",
    "git push --force",
    "git push -f",
    "git clean -f",
    "git checkout --",
    "git branch -d",
    "git branch -D",
    # systemd state changes
    "systemctl stop",
    "systemctl disable",
    "systemctl mask",
    "systemctl --user stop",
    "systemctl --user disable",
    # database
    "drop table",
    "drop database",
    "truncate table",
    # mass perm/ownership changes
    "chmod -r",
    "chmod -rf",
    "chown -r",
    "chattr +i",
    # aggressive process kills
    "pkill -9",
    "killall -9",
    "kill -kill",
    "kill -9 -1",
    # filesystem low-level (BLOCKED_PATTERNS catches mkfs+dd, list for completeness)
    "mkswap",
    "fdisk ",
    "parted ",
    # package uninstall — banner promises these pause. sudo-apt already
    # hands off, but user-level pip/npm/snap/pipx can uninstall without
    # sudo and would otherwise flow through auto mode silently.
    "pip uninstall",
    "pip3 uninstall",
    "pipx uninstall",
    "npm uninstall",
    "npm rm ",
    "npm remove",
    "yarn remove",
    "pnpm remove",
    "pnpm uninstall",
    "snap remove",
    "flatpak uninstall",
    "flatpak remove",
    "apt remove",
    "apt purge",
    "apt autoremove",
    "gem uninstall",
    "cargo uninstall",
    "ollama rm ",  # don't auto-drop a model — user paid time to pull it
    # overwriting redirections against real files (heuristic — `> /tmp/` is fine)
    "> /etc/",
    "> /usr/",
    "> /var/",
    "> /boot/",
)

AUDIT_LOG = Path.home() / ".master_ai_audit.log"

AUDIT_LOG_JSONL = Path.home() / ".master_ai_audit_typed.jsonl"

_NOOP_TOKENS = {"", ":", "true", "false", "exit", "exit 0"}

_READ_ALLOWED_ROOTS = (
    Path.home(),
    Path("/tmp"),
    Path("/var/log"),
)

_READ_DENY_PATTERNS = (
    re.compile(r"(^|/)\.ssh(/|$)"),
    re.compile(r"(^|/)\.gnupg(/|$)"),
    re.compile(r"(^|/)\.aws/credentials"),
    re.compile(r"(^|/)\.master_ai_keys$"),
    re.compile(r"(^|/)\.master_ai_creator$"),
    re.compile(r"(^|/)\.netrc$"),
    re.compile(r"^/etc/(?:shadow|gshadow|sudoers(?:\.d)?)"),
    re.compile(r"^/root(?:/|$)"),
    re.compile(r"^/proc(?:/|$)"),
    re.compile(r"^/sys(?:/|$)"),
)

_INTERACTIVE_RUN_WORDS = {
    "less",
    "more",
    "man",
    "nano",
    "vim",
    "vi",
    "emacs",
    "top",
    "htop",
    "btop",
    "watch",
    "tail -f",
    "ssh",
    "mysql",
    "psql",
    "sqlite3",
}

_VISUAL_RUN_WORDS = {
    "rain",
    "matrix",
    "animation",
    "animate",
    "screensaver",
    "terminal-effect",
    "terminal_effect",
    "curses",
    "fullscreen",
}

_LAST_LIVE_TYPED_ACTIONS: list[dict] = []

_LIVE_TYPED_ACTIONS_CAP = 200

_TURN_EDITED_PATHS: set[str] = set()

_TURN_VERIFIED_SINCE_EDIT = True  # nothing edited yet -> nothing to prove

_VERIFY_STOP_ATTEMPTS = 0

_VERIFY_STOP_MAX_ATTEMPTS = 2  # mirrors Hermes's max_attempts=2

_NON_CODE_VERIFY_EXTENSIONS = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".log", ".csv", ".tsv", ".json"}
)

_VERIFY_COMMAND_MARKERS = (
    "pytest",
    "py_compile",
    "unittest",
    "npm test",
    "npm run test",
    "yarn test",
    "go test",
    "cargo test",
    "bash -n",
    "node --check",
    "eslint",
    "flake8",
    "ruff",
    "mypy",
    "shellcheck",
)

_CD_SEPARATORS = ("&&", "||", ";", "&", "|")

_SECURITY_AUDIT_NAME_ALIASES = {"whisper": "openai-whisper"}

_SENSEI_BRIDGE_URL = "http://127.0.0.1:8791"

_BROWSER_READONLY_KINDS = {
    "BROWSER_READ",
    "BROWSER_READ_PAGE",
    "BROWSER_OBSERVE",
    "BROWSER_SCREENSHOT",
    "BROWSER_WAIT",
    "BROWSER_SCROLL",
    "BROWSER_FIND",
    "BROWSER_EXTRACT_LIST",
    "BROWSER_DRIVE_INSPECT_FOLDER",
    "BROWSER_CONSOLE",
    "BROWSER_NETWORK",
}

_BROWSER_DIRECTIVE_RE = re.compile(r"^\s*(BROWSER_[A-Z_]+):\s*(.+)$")

_BROWSER_SEP_RE = re.compile(r"\s*(?:::|=>|:=)\s*")

_SKILL_SESSION_MARKER = "[SENSEI_SKILL_SESSION]"

_DIRECTIVE_KEYWORDS_RE = re.compile(
    # No leading \b: models observed cramming directives together with
    # zero separator at all once the <tool_call> tag between them is
    # stripped (e.g. "3000BROWSER_WAIT:") — \b doesn't fire between a
    # digit and a letter (both are \w), so a leading boundary check missed
    # this exact case. The trailing (?=\s|$) after the colon already
    # disambiguates real directives from prose sharing a substring.
    r"(RUN_SKILL|RUNTERM|RUN|READ|CREATE|EDIT|ASK|DONE|REMEMBER|SEARCH|"
    r"TASK_ADD|TASK_DONE|"
    r"SEND_EMAIL|REMOTE_MCP|SEND_TELEGRAM|BROWSER_[A-Z_]+):(?=\s|$)"
)

_TOOL_CALL_TAG_RE = re.compile(r"</?\s*tool_calls?\s*>", re.IGNORECASE)

_ARG_XML_TAG_RE = re.compile(r"</?\s*arg_(?:key|value)\b", re.IGNORECASE)

_THINK_TAG_RE = re.compile(r"</?\s*think\s*>", re.IGNORECASE)

_XML_INVOKE_RE = re.compile(
    # Native XML tool-call blocks some <tool_call>-agnostic models emit despite the
    # system prompt forbidding it (live 2026-09-10 on poolside/laguna-xs-2.1:free
    # and nvidia::deepseek-coder-6.7b: `<invoke name="RUN"><parameter
    # name="command" string="true">ls ~/scripts/</parameter></invoke>`).
    # Previously these were invisible to every directive parser: the reply
    # rendered as prose + raw XML, nothing dispatched, and the model — seeing
    # no tool output — re-emitted the same block forever (the "Sensei going
    # crazy" loop). Convert each block to the bare directive grammar the
    # dispatcher actually speaks, then let _normalize_directive_lines do the
    # rest (newline forcing, backtick parity).
    r'<invoke\s+name\s*=\s*"([A-Za-z_]+)"\s*>(.*?)</invoke\s*>',
    re.IGNORECASE | re.DOTALL,
)

_XML_PARAM_RE = re.compile(
    r'<parameter\s+name\s*=\s*"([A-Za-z_]+)"[^>]*>(.*?)</parameter\s*>',
    re.IGNORECASE | re.DOTALL,
)

_XML_FUNCTION_RE = re.compile(
    r"<function\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</function\s*>",
    re.IGNORECASE | re.DOTALL,
)

_XML_FUNCTION_PARAM_RE = re.compile(
    r"<parameter\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>(.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

_XML_FUNCTION_NAME_ALIASES = {
    "bash": "RUN",
    "shell": "RUN",
    "execute": "RUN",
    "exec": "RUN",
    "terminal": "RUN",
    "run_command": "RUN",
    "run_shell_command": "RUN",
}

_JSON_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.IGNORECASE | re.DOTALL
)

_MISTRAL_TOOL_CALLS_RE = re.compile(
    r"\[TOOL_CALLS\]\s*(\[.*?\])", re.IGNORECASE | re.DOTALL
)

_BARE_KEYWORD_LINE_RE = re.compile(
    r"^\s*(?:<\s*tool_calls?\s*>\s*)?"
    r"(RUN_SKILL|RUNTERM|RUN|READ|CREATE|EDIT|ASK|DONE|REMEMBER|SEARCH|"
    r"TASK_ADD|TASK_DONE|SEND_EMAIL|REMOTE_MCP|SEND_TELEGRAM|BROWSER_[A-Z_]+)"
    r"\s*$",
    re.IGNORECASE,
)

_BARE_KEYWORD_ARG_RE = re.compile(
    r"^\s*(?:</?\s*tool_calls?\s*>\s*|(?:Command)?:{1,2}\s*)(.+)$", re.IGNORECASE
)

_SHELL_SYNTAX_MARKER_RE = re.compile(r"~/|\.\./|/[a-zA-Z0-9_.]|&&|\|\||`|\$\(")

_MAX_LINE_REPEATS = 3

_GATE_KIND_BY_LIST = {
    "read_paths": "READ",
    "run_cmds": "RUN",
    "runterm_cmds": "RUNTERM",
    "send_email_specs": "SEND_EMAIL",
    "send_telegram_specs": "SEND_TELEGRAM",
}

_WHATSNEW_STATE = Path.home() / ".master_ai_whatsnew_state.json"

LOOP_MAX_CYCLES = 5

LOOP_MAX_SECONDS = 600  # 10 minutes wall-clock ceiling

_REASON_DEPTHS = frozenset({"fast", "standard", "deep", "max"})

_READ_TARGET_FILLER_WORDS = (
    " me ",
    " the ",
    " in ",
    " on ",
    " at ",
    " for ",
    " from ",
    " about ",
    " a ",
    " my ",
)

_MARKDOWN_SKILL_DIRS = (
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".master_ai_skills",
)
