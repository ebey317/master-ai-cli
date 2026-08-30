"""Sensei full-screen TUI shell (v1.8).

Stationary bottom "input station":
  ┌─ ✏ label ──────────────────────────────┐
  │ 🥷  cursor lives here, text wraps      │
  │ /help · /model · /mode plan · /chats   │  (blue legend)
  │ 💭 rotating tip while idle             │
  └─────────────────────────────────────────┘

Everything else — AI replies, status line, scrollable history — overlays /
moves above this fixed box. The input area never shifts, so the cursor is
ALWAYS right after the ninja.

Slash-command namespace (v1.9):
  / = open the command palette
  /<name> = execute a built-in command
  All commands live under one prefix. No punctuation buckets.

Public API (used by master_ai.py):
    app = SenseiApp()
    app.set_label("thread-name")
    app.set_status("MODE:AUTO  and  MODEL:AUTO  and  MEM:42")
    app.write(text_with_ansi)          # append to output region
    app.run(on_submit=handler)         # blocks until user exits
    app.run_in_terminal(lambda: ...)   # suspend for classic input()
    app.scroll("up" | "down" | "top" | "bottom")
    app.exit()                         # clean shutdown
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer, Float, FloatContainer, HSplit, Window, WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea, RadioList, Box, Dialog


HISTORY_FILE = str(Path.home() / ".master_ai_history")

COMMAND_MENU_HINTS = {
    "hub": "open full command menu",
    "help": "quick reference",
    "controls": "terminal shortcuts and copy/paste rules",
    "tips": "show practical command tips",
    "model": "pick active model",
    "model auto": "return model routing to automatic",
    "model local": "select Sensei primary",
    "model qwen": "select Master AI alias",
    "model qwen2.5:3b": "select fast local model",
    "model llava": "select local vision model",
    "model groq": "select fast key-backed model",
    "model deepseek-r1": "select reasoning key-backed model",
    "model qwen3-coder": "select code key-backed model",
    "model gemini": "select research key-backed model",
    "model stats": "show individual model usage",
    "mode plan": "draft plans only",
    "mode review": "confirm each action",
    "mode auto": "run non-destructive work",
    "mode local": "force local/offline routing",
    "mode connected": "cloud-first when keys exist",
    "mode": "show current execution mode",
    "memory": "show saved facts",
    "remember:": "type your text after the colon",
    "forget:": "type keyword after the colon",
    "image:": "type an image prompt after the colon",
    "image status": "fetch/show an image job result",
    "image latest": "show latest generated image",
    "task": "task command help",
    "task add": "type task text after this",
    "task done": "mark a task complete",
    "task clear": "clear tasks",
    "tasks": "show active tasks",
    "save session": "save current chat",
    "load summary": "load compact context",
    "load session": "load saved chat",
    "transcript": "save transcript",
    "log": "show log path",
    "preview": "open latest preview",
    "clear": "clear screen",
    "clear history": "clear input history",
    "clear cache": "clear answer cache",
    "clear approved": "clear approved commands",
    "clear chats": "clear saved chats",
    "chats": "browse saved chats",
    "doctor": "system health",
    "clean": "open Sensei Clean dashboard",
    "clean ui": "open Sensei Clean dashboard",
    "update": "update Master AI safely",
    "master update": "update Master AI safely",
    "refresh": "soft reload",
    "restart": "restart Sensei",
    "up": "scroll chat up",
    "down": "scroll chat down",
    "top": "jump to oldest chat",
    "bottom": "jump to latest chat",
    "last": "show last reply",
    "mouse remote": "phone scrolling mode",
    "mouse local": "terminal copy mode",
    "mouse status": "show mouse mode",
    "few_shot on": "enable harvest few-shot injection",
    "few_shot off": "disable harvest few-shot injection",
    "few_shot status": "show few-shot toggle state",
    "few_shot": "show few-shot toggle state",
    "privacy status": "show privacy state for this turn",
    "privacy approve send": "approve one cloud send when turn is private",
    "projects": "project picker",
    "apps": "app tools",
    "autotips": "automatic tips",
    "slideshow": "open slideshow",
    "tour": "open walkthrough",
    "keys": "API key setup",
    "approved": "approved command list",
    "cache": "cache status",
    "perms": "permissions status",
    "tutorial": "replay walkthrough",
    "hints on": "enable contextual hints",
    "hints off": "disable contextual hints",
    "tts on": "enable voice replies",
    "tts off": "disable voice replies",
    "tts": "voice status",
    "hints": "hint status",
    "project": "current project",
    "search": "web search route",
    "max:": "max reasoning with mandatory self-critique",
    "agent:": "plan/execute/critique task loop",
    "dl": "download helper",
    "gdrive": "Google Drive helper",
    "git": "git command help",
    "git status": "show repo status",
    "git diff": "show repo diff",
    "git log": "show git log",
    "git commit": "commit workflow",
    "go": "accept pending plan",
    "cancel": "cancel pending plan",
    "accessibility": "input settings",
    "x": "exit / stop",
    "e": "edit thread label",
    "resize": "fit tmux pane",
    "only": "make Sensei the only tmux pane",
    # P1.5 + P1.7 new commands (2026-05-11)
    "stats": "observability rollup: routes, models, blocked, harvest",
    "agents": "show registered subagents",
    "agents list": "show registered subagents",
    "agents inspect": "type a subagent name after this",
    "agents run": "type a subagent name + task after this",
    # P1.4 hooks REPL (2026-05-11)
    "hooks": "show registered hooks (built-in + user)",
    "hooks list": "show registered hooks",
    "hooks enable": "type a hook id to enable it",
    "hooks disable": "type a hook id to disable it",
    "hooks reload": "reload user hooks from ~/.master_ai_hooks.json",
    # P1.3 reason depths
    "reason:": "deep reasoning lane (DeepSeek-R1 or local loop)",
    "reason fast:": "local fast reasoning (planner→solver→finalizer)",
    "reason standard:": "cloud DeepSeek-R1, else local standard loop",
    "reason deep:": "DeepSeek-R1, else local deep loop (default)",
    "reason max:": "local max loop (mandatory second-critic pass)",
}

# Single slash-command namespace. Every built-in command is reachable via
# /<name>. The slash palette opens with just "/" and filters as you type.
# Payload commands (the ones that need an argument) keep a trailing space or
# colon so the cursor lands ready for input.
PAYLOAD_COMMANDS = {
    "remember:", "forget:", "task add", "git commit",
    "image:", "image status", "image latest", "max:", "agent:",
    "reason:", "reason fast:", "reason standard:", "reason deep:", "reason max:",
    "agents run", "agents inspect",
    "hooks enable", "hooks disable",
}

COMMAND_MENU_GROUPS = {
    "/": [
        # general
        "hub", "help", "controls", "tips", "commands",
        "update", "master update", "refresh", "restart", "kick",
        "save session", "load summary", "load session", "transcript", "log",
        "preview", "clear", "clear history", "clear cache",
        "clear approved", "clear chats", "chats", "doctor", "clean",
        "projects", "apps", "autotips", "slideshow", "tour",
        "keys", "approved", "cache", "perms", "tutorial", "project",
        # settings / model
        "mode plan", "mode review", "mode auto", "mode local", "mode connected", "mode",
        "model", "model auto", "model local", "model master-ai", "model qwen2.5:3b", "model llava",
        "model groq", "model deepseek-r1", "model qwen3-coder", "model gemini",
        "model stats", "model qwen3.5:397b-cloud", "model kimi-k2.5:cloud",
        "tts on", "tts off", "tts",
        "hints on", "hints off", "hints",
        "mouse remote", "mouse local", "mouse status",
        "few_shot on", "few_shot off", "few_shot status",
        "privacy status", "privacy approve send",
        "accessibility",
        # navigation / status
        "up", "down", "top", "bottom", "last",
        # payload / tools
        "remember:", "forget:", "task add", "task done", "task clear", "tasks", "task",
        "git commit", "git status", "git diff", "git log", "git",
        "image:", "image status", "image latest", "max:", "agent:",
        "reason:", "reason fast:", "reason standard:", "reason deep:", "reason max:",
        "search", "read", "dl", "gdrive", "mesh",
        "agents", "agents list", "agents inspect", "agents run",
        "hooks", "hooks list", "hooks enable", "hooks disable", "hooks reload",
        "stats", "router", "router stats",
        # plan approval
        "go", "cancel", "e", "only",
        # exit
        "x",
    ],
}

COMPLETER_WORDS: List[str] = []
for _group in COMMAND_MENU_GROUPS.values():
    for _cmd in _group:
        if _cmd not in COMPLETER_WORDS:
            COMPLETER_WORDS.append(_cmd)

# Any command that legitimately needs an argument before it can run is a
# payload command; selecting it from the slash palette leaves the cursor at
# the end ready for typing instead of submitting immediately.
def _is_payload_command(command: str) -> bool:
    if command in PAYLOAD_COMMANDS:
        return True
    if command.endswith(":"):
        return True
    # subcommands that are known to need an argument
    return command in ("task add", "task done", "agents run", "agents inspect",
                       "hooks enable", "hooks disable", "git commit", "image status",
                       "search", "read", "dl", "mesh ask")

def _menu_prefix(text: str) -> Optional[str]:
    text = (text or "")
    return text[:1] if text[:1] in COMMAND_MENU_GROUPS else None

def _menu_command_matches(text: str) -> List[str]:
    prefix = _menu_prefix(text)
    if not prefix:
        return []
    query = text[1:].strip().lower()
    commands = COMMAND_MENU_GROUPS.get(prefix, [])
    query = (query or "").strip().lower()
    if not query:
        return list(commands)

    ranked = []
    for idx, command in enumerate(commands):
        cmd = command.lower()
        hint = COMMAND_MENU_HINTS.get(command, "").lower()
        cmd_words = re.split(r"[\s:/-]+", cmd)
        hint_words = re.split(r"[\s:/-]+", hint)
        if cmd.startswith(query):
            score = 0
        elif any(w.startswith(query) for w in cmd_words if w):
            score = 1
        elif any(w.startswith(query) for w in hint_words if w):
            score = 2
        elif query in cmd:
            score = 3
        elif query in hint:
            score = 4
        else:
            continue
        ranked.append((score, idx, command))
    return [command for _, _, command in sorted(ranked)]

class SlashCommandCompleter(Completer):
    """Popup command palette triggered by the single '/' prefix.

    Typing '/' opens the palette. Typing after the slash filters commands by
    text or hint. The inserted text replaces the slash so the existing command
    dispatcher in master_ai.py receives the same words as manual typing.

    2026-08-30: "/model" used to be special-cased here as a type-to-filter
    live dropdown. Operator, explicitly: "I'm not typing... remove it from
    my software." /model is now intercepted before it ever reaches this
    completer (see _submit in _build_keys()) and opens a pure arrow-key
    modal picker (_open_model_picker) instead — provider list, then that
    provider's full model list, no typing at any step. This class no
    longer needs a model_catalog_fn at all; it only serves the static
    command palette now.
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text or not _menu_prefix(text):
            return
        narrow = _term_size().columns < 100
        for command in _menu_command_matches(text):
            hint = "" if narrow else COMMAND_MENU_HINTS.get(command, "")
            yield Completion(
                command,
                start_position=-len(text),
                display=command,
                display_meta=hint,
            )

LEGEND_WORDS = [
    "/help", "/model", "/mode plan", "/chats", "/tts", "/slash",
    "e=edit label",
]

IDLE_TIPS = [
    "type '/' for the command palette",
    "'/help' for quick reference",
    "'/mode plan' brainstorms + drafts plans (default — no execution)",
    "'/mode review' confirms every command one at a time",
    "'/mode auto' runs commands without asking — destructive still pauses",
    "'fast: your prompt' routes through Groq (fast cloud, needs key)",
    "'deep: your prompt' routes to DeepSeek-R1 (deep reasoning)",
    "'max: your prompt' runs the strongest planner/critic reasoning loop",
    "'agent: your task' runs plan → execute → critique cycles",
    "'local: your prompt' forces local model explicitly",
    "'mode connected' switches the whole session to cloud-first",
    "on a pending plan — press 1 or Enter to accept, 4 to keep talking",
    "'/copy chat' saves the full session to a markdown file",
    "'/clear cache' if Sensei is serving the same cached answer",
    "'/doctor' shows URLs, services, mode, mouse, and current task",
    "'/chats' to browse saved sessions",
    "PageUp / PageDown scroll output by a full visible page",
    "'/up' / '/down' scroll output; '/top' / '/bottom' jumps",
    "Shift+Tab cycles plan → review → auto when input is empty",
    "'/mouse remote' for phone scrolling; '/mouse local' for drag-copy",
    "'/refresh' soft-reloads the engine; '/kick' forces a supervisor respawn",
    "'/e' edits this thread's label",
    "'/model' switches the active AI model",
    "'/tts on' speaks replies out loud (Piper voice)",
    "'/remember: <fact>' saves a fact across all sessions",
    "'/' alone opens the slash palette — one prefix for everything",
]


def _term_size():
    return shutil.get_terminal_size((80, 24))

def _fit_text(text: str, width: int) -> str:
    """Clamp one-line chrome text so frame titles/status never spill."""
    text = str(text or "").replace("\n", " ")
    width = max(1, int(width or 1))
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3].rstrip() + "..."


class _TUIStdout:
    """Replacement for sys.stdout that writes to the TUI's output buffer.
    Every write is defensively try/excepted — a raise from here would take
    down the worker thread and silently exit the app."""
    def __init__(self, app: "SenseiApp", original):
        self._app = app
        self._original = original
        self._buf = ""

    def write(self, s):
        try:
            if not s:
                return 0
            self._buf += s
            if "\n" in self._buf or len(self._buf) >= 256:
                self._app.write(self._buf)
                self._buf = ""
            return len(s)
        except Exception:
            try: self._original.write(s)
            except Exception: pass
            return len(s) if s else 0

    def flush(self):
        try:
            if self._buf:
                self._app.write(self._buf)
                self._buf = ""
        except Exception:
            pass

    def isatty(self):
        return True

    def fileno(self):
        try: return self._original.fileno()
        except Exception: return -1


class SafeFormattedTextControl(FormattedTextControl):
    """FormattedTextControl that never throws IndexError from get_line.

    Reason: prompt_toolkit's render loop calls get_line(i) where i can
    come from the cursor position, ScrollState, or wrap calculations.
    If a background thread mutates the output text between frame-start
    and line-fetch, `fragment_lines[i]` can trip `list index out of
    range` (controls.py:413) and crash the render. We wrap the returned
    UIContent's get_line callable so out-of-range indices return an
    empty fragment list (rendered as a blank line) instead of throwing.
    The next frame will re-measure with the current content.
    """

    def create_content(self, width, height):
        content = super().create_content(width, height)
        # prompt_toolkit caches UIContent between frames with the same
        # (width, height). Without this guard, every frame re-wraps the
        # previous wrapper, building a chain that blows the recursion
        # limit after ~1000 frames. Tag the wrapper so we only wrap once.
        if getattr(content.get_line, "_sensei_safe_wrapped", False):
            return content
        original_get_line = content.get_line
        def safe_get_line(i):
            try:
                return original_get_line(i)
            except IndexError:
                return []
        safe_get_line._sensei_safe_wrapped = True
        content.get_line = safe_get_line
        return content


class SenseiApp:
    def __init__(self, model_catalog_fn=None) -> None:
        # model_catalog_fn(query) -> [(pin_value, display, hint), ...]. Lets
        # "/model <query>" show a live picker across every provider key
        # that's actually configured (plus local Ollama) instead of the
        # ~20 static "model *" hint entries. Injected from master_ai.py
        # (keeps HTTP/caching logic there, out of this UI-only module).
        # Per Elijah 2026-08-20: "it should open up a model catalog for
        # every API key I have logged... scroll up and down and press
        # enter... just like every other CLI."
        self._model_catalog_fn = model_catalog_fn
        self._label = ""
        self._status = ""
        self._output_chunks: List[str] = []
        self._output_lock = threading.Lock()
        # Output render cache keyed by a monotonic write-version.
        # prompt_toolkit calls the text getter multiple times per frame;
        # if output mutates between calls, line fetch can race and throw.
        # Re-render only when output actually changed.
        self._output_render_cache = None
        self._output_render_cache_version = -1
        self._output_render_lines = 0
        self._output_version = 0
        self._tip_cycle = itertools.cycle(IDLE_TIPS)
        self._tip = next(self._tip_cycle)
        self._tip_last = time.time()
        self._tip_interval = 30.0  # rotate every 30s (was 5s)

        # Thinking-mode state: when the AI is generating, we rotate a different
        # set of narrative lines in the tip slot every 1.8s.
        self._thinking = False
        self._thinking_cycle = itertools.cycle([
            "Grinding...", "Pushing through...", "In deep meditation...",
            "Leveling up...", "Getting to the goal...", "Ninja-ing...",
            "Doing what ninjas do...",
        ])
        self._thinking_line = "Grinding..."
        self._thinking_last = 0.0
        self._thinking_interval = 1.8
        self._thinking_started = 0.0
        self._scroll_offset = 0  # lines scrolled up from bottom

        # Three thought-states: IDLE (rotating hints), THINKING (AI working),
        # HANDOFF (Plan→Review transition). Handoff suppresses idle + thinking
        # so the dispatch moment isn't drowned out by other thoughts.
        # Elijah 2026-04-21: "animate the handoff plan with thoughts and turn
        # off all the other thoughts during the handoff plan. three different
        # modes for the thoughts."
        self._plan_pending = False
        self._handoff_active = False
        self._handoff_cycle = itertools.cycle([
            "⚡ handing off to Review...",
            "🥋 → 🔴 mode flipping...",
            "plan accepted — Review taking over...",
            "executing plan step by step...",
            "🥋 Review mode active...",
        ])
        self._handoff_line = "⚡ handing off to Review..."
        self._handoff_last = 0.0
        self._handoff_interval = 1.0

        self._input = TextArea(
            prompt="🥷  ",
            multiline=True,
            wrap_lines=True,
            scrollbar=False,
            # Keep the station compact enough for 70x24 terminals while
            # still growing for pasted prompts.
            height=Dimension(min=1, max=5, preferred=2),
            history=FileHistory(HISTORY_FILE),
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            focusable=True,
            # User-typed text stays the terminal's default color — no blue.
            style="class:textinput",
        )

        # SafeFormattedTextControl catches IndexError inside get_line so
        # a mid-frame race (output thread appending while renderer is
        # measuring) never crashes the render loop. Last-frame content
        # simply blanks the affected line; next frame re-measures fresh.
        self._output_control = SafeFormattedTextControl(
            text=self._render_output,
            focusable=False,
            show_cursor=False,
            get_cursor_position=self._get_output_cursor,
        )
        self._output_window = Window(
            content=self._output_control,
            wrap_lines=True,
            always_hide_cursor=True,
            # class:chat is defined as `noinherit` in _build_style so the
            # Frame's mode-accent color does NOT bleed into chat content.
            # Elijah's rule 2026-04-21: chrome follows mode, TEXT stays on
            # stable semantic colors (blue=file/info, yellow=plan steps,
            # green=voice, red=warning, etc.) so his eye trains on meaning
            # instead of being re-tinted every mode change.
            style="class:chat",
            # display_arrows=False — ▲/▼ arrows at top+bottom of the track
            # read as a little pennant flag on screen, which distracts from
            # the chat content. Keep the track (useful scroll indicator),
            # drop the arrows. Typed `up`/`down`/`top`/`bottom` still scroll.
            right_margins=[ScrollbarMargin(display_arrows=False)],
        )
        # Route mouse-wheel events on the chat window into OUR scroll_offset.
        # Window's default handlers bump self.vertical_scroll, but our
        # rendering re-anchors to the invisible cursor each frame, so the
        # wheel would otherwise snap back every repaint. Overriding these
        # two methods keeps wheel and page-up/down on the same offset.
        def _wheel_up():
            self._scroll_offset += 5
            try: self._app.invalidate()
            except Exception: pass

        def _wheel_down():
            self._scroll_offset = max(0, self._scroll_offset - 5)
            try: self._app.invalidate()
            except Exception: pass

        self._output_window._scroll_up = _wheel_up
        self._output_window._scroll_down = _wheel_down
        # Framed chat region — wraps the output window in a bordered box so
        # text stays inside the frame on scroll instead of bleeding past the
        # edges. Title is mode-tinted (class:frame picks up the mode accent
        # color). 2026-04-20 per Elijah: "keep everything in frame instead
        # of bleeding out… we get that whole slideshow that comes down."
        self._output_frame = Frame(
            self._output_window,
            title=" 🥋  chat ",
            style="class:frame",
        )

        self._status_control = FormattedTextControl(text=self._render_status)
        self._status_window = Window(
            content=self._status_control,
            height=1,
            align=WindowAlign.RIGHT,
            style="class:status",
        )

        self._legend_control = FormattedTextControl(text=self._render_legend)
        self._legend_window = Window(
            content=self._legend_control, height=1, style="class:legend",
        )

        self._tip_control = FormattedTextControl(text=self._render_tip)
        self._tip_window = Window(
            content=self._tip_control, height=1, style="class:tip",
        )

        # Tip/thinking slot ABOVE the ninja — rotating idle hints (💭) or
        # thinking animation (🥷 [thinking] ...) depending on state.
        # Restored 2026-04-20 per Elijah: "make sure the thoughts for
        # idle and thinking are on" — 1 row cost, real feedback benefit.
        input_stack = HSplit([
            ConditionalContainer(self._tip_window, filter=Condition(self._show_tip_row)),
            self._input,
            self._legend_window,
        ])

        self._frame = Frame(input_stack, title=self._render_label,
                            style="class:frame")

        # Persistent "MASTER AI" header — single blue line pinned at the top
        # so the brand is always visible even when chat output scrolls.
        self._header_control = FormattedTextControl(
            text=self._render_header,
        )
        self._header_window = Window(
            content=self._header_control, height=1,
            align=WindowAlign.CENTER, style="class:header",
        )

        root = HSplit([
            self._header_window,
            self._status_window,
            self._output_frame,
            self._frame,
        ])
        # Modal /model picker — pure arrow-key + Enter navigation, no typing
        # at any step (see _open_model_picker). Floats full-screen above
        # everything else when active; a placeholder Window the rest of the
        # time so it costs nothing when hidden.
        self._picker_visible = False
        self._picker_radio = None       # currently-shown RadioList
        self._picker_on_select = None   # callback(value) fired on Enter
        self._picker_container = ConditionalContainer(
            Window(width=0, height=0),
            filter=Condition(lambda: self._picker_visible),
        )

        root = FloatContainer(
            content=root,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, display_arrows=True),
                ),
                Float(
                    left=0, top=0, right=0, bottom=0,
                    content=self._picker_container,
                ),
            ],
        )

        self._on_submit: Optional[Callable[[str], None]] = None

        # Mode-aware palette — set_mode() rebuilds the Style with one of
        # these accent colors so the header, frame, status, and legend
        # all visually signal the current mode at a glance. Default
        # 'plan' at startup — chat-only brainstorm mode, no execution.
        self._mode = "plan"
        self._app = Application(
            layout=Layout(root, focused_element=self._input),
            key_bindings=self._build_keys(),
            full_screen=True,
            # Mouse OFF by default — keeps the input box truly locked at
            # bottom and lets gnome-terminal handle native click-drag copy.
            # Opt-in with SENSEI_MOUSE=1 if you want wheel scroll inside the app.
            mouse_support=os.environ.get("SENSEI_MOUSE", "0") == "1",
            refresh_interval=0.25,
            style=self._build_style("plan"),
            # Force 24-bit truecolor so hex values like #ef4444 / #dc143c
            # render exactly instead of being quantized to the nearest of
            # 256 palette colors. Elijah 2026-04-20: "didnt render like
            # yours" — the issue was prompt_toolkit defaulting to 256
            # mode even though COLORTERM=truecolor.
            color_depth=ColorDepth.DEPTH_24_BIT,
            # Faster UI cadence so scrolling/typing feel native.
            min_redraw_interval=0.02,
            max_render_postpone_time=0.03,
        )

    # ── mode-aware theming ────────────────────────────────────
    # Accent color per mode — traffic-light scheme, dimmed so nothing
    # blares on the monitor. Header gets solid bg; frame/status/legend
    # get the color as foreground. Tip and thinking stay on their own
    # warm colors so they're always readable on any accent.
    # Dimmed down 2026-04-19 — original #c62828 was too bright on RustDesk-over-phone.
    _MODE_ACCENT = {
        # Stoplight remapped 2026-04-21 by semantic, not by old habit:
        #   Plan = full STOP (no execution ever) → RED
        #   Review = CAUTION (approve each step)  → AMBER
        #   Auto = GO (runs freely)               → GREEN
        # Plan's red is #cc0000 — the "true red without glare or tint" that
        # Elijah locked 2026-04-20 after the full tuning walk (#8b1a1a →
        # #b91c1c → #ef4444 → #dc2626 → #ef4444 → #dc143c → #ef4444 →
        # #c0392b → #ef4444 → #cc0000). Do NOT re-tune without his ask.
        "plan":   "#cc0000",  # true red — STOP: drafting only, no execution. Approved hex 2026-04-20.
        "review": "#c7761a",  # amber — CAUTION: per-command confirm, press 1/Enter to approve each
        "auto":   "#1a7a3a",  # forest green — GO: runs without asking (destructive still pauses)
    }

    def _build_style(self, mode: str) -> Style:
        accent = self._MODE_ACCENT.get(mode, "#2266cc")
        # Chrome follows the mode accent — frame borders, header, and
        # label all shift color when the mode changes. Plan=muted red,
        # Review=amber, Auto=green. The stoplight signal spans the full
        # chrome so the mode is visible at a glance, not just in status.
        return Style.from_dict({
            "status":      f"{accent} bold",
            "frame":       f"{accent} bold",
            "frame.label": f"{accent} bold",
            "legend":      f"{accent}",
            "sep":         "#999999",
            "tip":         "#1a7a3a italic bold",
            "thinking":    "#c7761a bold",
            "textinput":   "#ffffff noinherit",
            "header":      f"{accent} bold",
            # Chat content — noinherit so the Frame's mode color doesn't
            # bleed onto text. Semantic ANSI codes from _paint_line render
            # true (blue=file/info, yellow=plan, green=voice, red=warning).
            "chat":        "noinherit",
            # Scrollbar (2026-05-11) — was unset, falling to prompt_toolkit
            # defaults which render as reverse-video against the frame
            # (effectively invisible, ungrabbable). Thumb gets the mode
            # accent so the scrollbar reads as chrome and follows the
            # stoplight — Plan red / Review amber / Auto green. Track stays
            # neutral gray so the thumb has contrast. Mouse drag works when
            # SENSEI_MOUSE=1 (Elijah's remote/phone profile).
            "scrollbar":            "#666666 noinherit",
            "scrollbar.background": "#444444 noinherit",
            "scrollbar.button":     f"bg:{accent} {accent} bold noinherit",
            "scrollbar.arrow":      f"{accent} bold noinherit",
        })

    def set_mode(self, mode: str) -> None:
        """Swap the accent color when MODE changes. Called by master_ai.py
        on `mode plan|review|auto`. Silent no-op for unknown modes."""
        if mode not in self._MODE_ACCENT:
            return
        self._mode = mode
        try:
            self._app.style = self._build_style(mode)
            self._app.invalidate()
        except Exception:
            pass

    # ── rendering callbacks ────────────────────────────────────

    def _show_tip_row(self):
        """Hide idle tips on small terminals so chat/input keep usable space."""
        rows = _term_size().lines
        return rows >= 28 or self._thinking or self._handoff_active

    def _render_output(self):
        """Return the FULL output as ANSI — scroll is handled by positioning
        an invisible cursor that Window tracks (see _get_output_cursor).

        Cache is keyed to output write-version, not time. This avoids
        reparsing the full chat history on every idle repaint while still
        keeping render snapshots consistent across line-measure calls.
        """
        with self._output_lock:
            version = self._output_version
            if (
                self._output_render_cache is not None
                and self._output_render_cache_version == version
            ):
                return self._output_render_cache
            text = "".join(self._output_chunks)
        rendered = ANSI(text)
        self._output_render_cache = rendered
        self._output_render_cache_version = version
        self._output_render_lines = max(0, text.count("\n"))
        return rendered

    def _get_output_cursor(self):
        """Invisible cursor that Window auto-scrolls to keep visible.
        - scroll_offset == 0 → cursor at bottom of buffer → Window shows latest
        - scroll_offset  > 0 → cursor N lines above bottom → Window shows N back

        Uses the line count cached by _render_output so cursor y can
        never exceed what the control's fragment_lines actually contains.
        Prevents `list index out of range` in FormattedTextControl when
        output is appended between render-cycle getter calls.
        """
        # Ensure cache is populated for this frame. If _render_output
        # hasn't been called yet this tick (cold start), populate now.
        if self._output_render_cache is None:
            self._render_output()
        total = self._output_render_lines
        y = max(0, total - self._scroll_offset)
        return Point(x=0, y=y)

    def _render_status(self):
        width = max(10, _term_size().columns - 2)
        status = self._status or ""
        if width < 82:
            # Narrow form keeps the word "and" (no symbols) — Elijah
            # 2026-04-29: "the punctuation needs words not symbols".
            # Just collapse the double-space padding so it fits.
            status = status.replace("  and  ", " and ")
            # Keep the actual selected model visible on narrow terminals.
            status = status.replace("MODEL:AUTO+CLOUD", "MODEL:AUTO")
        return FormattedText([("class:status", f" {_fit_text(status, width - 2)} ")])

    def _render_header(self):
        width = max(10, _term_size().columns)
        clock = time.strftime("%m/%d/%Y %I:%M:%S %p")
        title = (
            f" MASTER AI - SENSEI  {clock} "
            if width < 76 else
            f" 🥷  MASTER  AI  —  SENSEI  {clock} "
        )
        return FormattedText([("class:header", _fit_text(title, width))])

    def _render_label(self):
        width = max(10, _term_size().columns - 6)
        lbl = f" ✏ {self._label} " if self._label else " ✏ "
        lbl = _fit_text(lbl, min(width, 48 if width >= 80 else width))
        return FormattedText([("class:frame.label", lbl)])

    def _render_label_with_tip(self):
        """Frame title: label + rotating tip, inline with the ninja's frame.
        One line, always visible, doesn't scroll away."""
        lbl = f" ✏ {self._label} " if self._label else " ✏ "
        # Freeze rotation while user is typing
        typing = bool(self._input.text)
        if not typing:
            now = time.time()
            if now - self._tip_last >= self._tip_interval:
                self._tip = next(self._tip_cycle)
                self._tip_last = now
        return FormattedText([
            ("class:frame.label", lbl),
            ("class:tip", f"  💭 {self._tip} "),
        ])

    def _render_legend(self):
        # Single slash-command namespace. The legend advertises the one
        # prefix that opens the command palette, plus the edit-label shortcut.
        current_mode = getattr(self, "_mode", "plan").upper()
        return FormattedText([
            ("class:legend", f"MODE:{current_mode}"),
            ("class:sep", "  and  "),
            ("class:legend", "slash"),
        ])

    def _render_tip(self):
        """Tip line has three states:
          - typing    → empty (disappears completely)
          - thinking  → '🥷 [thinking] <rotating>' every 1.8s
          - idle      → '💭 <rotating hint>' every 30s
        """
        typing = bool(self._input.text)
        if typing:
            return FormattedText([])
        now = time.time()
        if self._thinking:
            if now - self._thinking_last >= self._thinking_interval:
                self._thinking_line = next(self._thinking_cycle)
                self._thinking_last = now
            elapsed = ""
            if self._thinking_started:
                total = max(0, int(now - self._thinking_started))
                mm, ss = divmod(total, 60)
                elapsed = f" [{mm}:{ss:02d}]"
            line = _fit_text(f"🥷 [thinking]{elapsed} {self._thinking_line}", _term_size().columns - 4)
            return FormattedText([
                ("class:thinking", line),
            ])
        if now - self._tip_last >= self._tip_interval:
            self._tip = next(self._tip_cycle)
            self._tip_last = now
        line = _fit_text(f"💭 {self._tip}", _term_size().columns - 4)
        return FormattedText([("class:tip", line)])

    def _active_comma_completion(self) -> Optional[str]:
        """Return the selected punctuation-menu command, or the first match."""
        buf = self._input.buffer
        state = getattr(buf, "complete_state", None)
        if not state or not getattr(state, "completions", None):
            return self._menu_query_match(buf.document.text_before_cursor)
        original = getattr(state, "original_document", None)
        source = original.text_before_cursor if original else buf.document.text_before_cursor
        if "\n" in source or not _menu_prefix(source):
            return None
        index = state.complete_index if state.complete_index is not None else 0
        try:
            return state.completions[index].text
        except Exception:
            return None

    def _menu_query_match(self, text: str) -> Optional[str]:
        """Fallback when Enter lands before prompt_toolkit opens completions."""
        if "\n" in text or not _menu_prefix(text):
            return None
        matches = _menu_command_matches(text)
        return matches[0] if matches else None

    def _insert_payload_command(self, command: str) -> bool:
        """Payload commands wait for user text after the command is inserted."""
        if not _is_payload_command(command):
            return False
        suffix = "" if command.endswith(":") else " "
        self._input.buffer.document = Document(command + suffix, len(command + suffix))
        try: self._app.invalidate()
        except Exception: pass
        return True

    # ── key bindings ───────────────────────────────────────────

    def _build_keys(self) -> KeyBindings:
        kb = KeyBindings()

        # Modal picker (/model): app-level bindings so Enter/Escape stay
        # active for as long as self._picker_visible is True, regardless
        # of which widget technically has focus inside the picker.
        # RadioList's own control already handles up/down for moving the
        # highlight and toggling current_value — this just reads that
        # value on Enter and closes the picker. Both firing on one Enter
        # is harmless: RadioList's handler only updates current_value,
        # which is exactly what gets read here.
        @kb.add("enter", filter=Condition(lambda: self._picker_visible))
        def _picker_confirm(event):
            radio = self._picker_radio
            on_select = self._picker_on_select
            self._close_picker()
            if radio is not None and on_select is not None:
                on_select(radio.current_value)

        @kb.add("escape", filter=Condition(lambda: self._picker_visible))
        @kb.add("c-c", filter=Condition(lambda: self._picker_visible))
        def _picker_cancel(event):
            self._close_picker()

        @kb.add("enter", filter=has_focus(self._input))
        def _submit(event):
            # /model (no trailing text) opens the arrow-key modal picker —
            # provider list, then that provider's full model list, no
            # typing at either step. Operator, explicit: "I'm not typing.
            # I'm going to do slash model... arrow and enter key."
            _bare = self._input.text.strip().lower()
            if _bare in ("/model", "model"):
                self._input.text = ""
                self._open_model_picker()
                return

            menu_command = self._active_comma_completion()
            if menu_command:
                # Same interception, reached via the completion dropdown
                # (arrow/Tab to highlight "model", then Enter) instead of
                # typing the full word out — must not fall through to
                # normal dispatch, or this bypasses the picker exactly
                # like typing "/model" and pressing Enter directly does.
                if menu_command.strip().lower() == "model":
                    self._input.buffer.cancel_completion()
                    self._input.text = ""
                    self._open_model_picker()
                    return
                self._input.buffer.cancel_completion()
                if self._insert_payload_command(menu_command):
                    return
                text = menu_command
                self._input.text = ""
                self._scroll_offset = 0
                self.write(f"\n\033[1m> {text}\033[0m\n")
                if self._on_submit:
                    t = threading.Thread(
                        target=self._safe_dispatch, args=(text,), daemon=True,
                    )
                    t.start()
                return

            # Multi-line paste fix (2026-04-24): pasted text contains \n
            # characters that arrive as rapid-fire Enter events. Real
            # human Enters are always >50ms apart. If this Enter is within
            # 50ms of the previous one, it's part of a paste — insert as
            # newline instead of submitting. Without this, pasted prompts
            # get chopped into per-line submissions and Plan mode never
            # sees the full request (the slideshow-prompt-fragmented bug).
            import time as _t
            _now = _t.monotonic()
            _last = getattr(self, '_last_enter_time', 0.0)
            self._last_enter_time = _now
            if (_now - _last) < 0.05:
                self._input.buffer.insert_text("\n")
                return
            text = self._input.text.rstrip("\n")
            self._input.text = ""
            self._scroll_offset = 0
            # Echo the user's message into the chat scrollback so they can
            # scroll up later and reference what they asked.
            if text:
                self.write(f"\n\033[1m> {text}\033[0m\n")
            if self._on_submit:
                # run handler in worker so the app keeps repainting
                t = threading.Thread(
                    target=self._safe_dispatch, args=(text,), daemon=True,
                )
                t.start()

        @kb.add("escape", "enter", filter=has_focus(self._input))
        def _newline(event):
            self._input.buffer.insert_text("\n")

        @kb.add("down", filter=has_focus(self._input))
        def _completion_down(event):
            if self._active_comma_completion():
                self._input.buffer.complete_next()
            else:
                event.current_buffer.history_forward()

        @kb.add("up", filter=has_focus(self._input))
        def _completion_up(event):
            if self._active_comma_completion():
                self._input.buffer.complete_previous()
            else:
                event.current_buffer.history_backward()

        @kb.add("c-c")
        def _sigint(event):
            if self._on_submit:
                threading.Thread(
                    target=self._safe_dispatch, args=("x",), daemon=True,
                ).start()
            else:
                event.app.exit()

        # Bracketed paste — PROPER fix for multi-line paste-split bug
        # (2026-04-24). When the terminal sends bracketed-paste sequences
        # (ESC[200~ ... ESC[201~), prompt_toolkit fires a single
        # Keys.BracketedPaste event with the full pasted text in
        # event.data. Insert it directly into the buffer — newlines stay
        # as text characters, NOT as separate Enter key events. This
        # supersedes the time-heuristic in _submit which only catches
        # consecutive newlines but misses the first one in any burst.
        # The slideshow-prompt-split bug Elijah hit — this is the cure.
        @kb.add(Keys.BracketedPaste)
        def _paste(event):
            event.current_buffer.insert_text(event.data)

        @kb.add("s-tab", filter=has_focus(self._input))
        def _shift_tab(event):
            """Shift+Tab cycles ONLY plan → review → auto → plan.

            Matches Claude Code's 3-state Shift-Tab toggle. Universal CLI
            convention: Shift+Tab is the mode/agency-level switch, not a
            generic settings menu.

            - Completion menu open → step backward through choices (terminal
              standard reverse-tab behavior).
            - Input has text → start completion against the current text
              (don't drop the user's in-progress prompt to cycle mode).
            - Input empty → cycle mode: type the next `mode <name>` command
              and submit it. The existing command parser in master_ai.py
              handles save_mode() + _SENSEI_APP.set_mode() + chrome repaint,
              so we don't duplicate that logic here.
            """
            buf = self._input.buffer
            state = getattr(buf, "complete_state", None)
            if state and getattr(state, "completions", None):
                buf.complete_previous()
                return
            if buf.text:
                # Non-empty input — preserve reverse-completion behavior.
                buf.start_completion(select_first=False)
                return
            current = getattr(self, "_mode", "plan") or "plan"
            cycle = ("plan", "review", "auto")
            try:
                idx = cycle.index(current)
            except ValueError:
                idx = -1
            nxt = cycle[(idx + 1) % len(cycle)]
            # Mirror the Enter handler's submit path (line 854 _submit) — the
            # buffer's accept_handler isn't wired, so validate_and_handle()
            # is a no-op here. We have to dispatch through self._on_submit
            # directly, same as a real Enter press would.
            text = f"mode {nxt}"
            self._input.text = ""
            self._scroll_offset = 0
            self.write(f"\n\033[1m> {text}\033[0m\n")
            if self._on_submit:
                t = threading.Thread(
                    target=self._safe_dispatch, args=(text,), daemon=True,
                )
                t.start()

        # Single scroll binding — Shift+Up / Shift+Down. Elijah's pick
        # 2026-04-19: "only want one" + "hold is scroll." Shift is a real
        # modifier, so terminal key-repeat fires continuous Shift+Up
        # events while held → smooth scroll. Tap = one event = 3 lines.
        # Plain Up/Down stay reserved for input history.
        @kb.add("s-up")
        @kb.add("pageup")
        def _scroll_up(event):
            self._scroll_offset += max(8, _term_size().lines - 8)
            event.app.invalidate()

        @kb.add("s-down")
        @kb.add("pagedown")
        def _scroll_down(event):
            self._scroll_offset = max(0, self._scroll_offset - max(8, _term_size().lines - 8))
            event.app.invalidate()

        @kb.add("home")
        def _scroll_top(event):
            self._scroll_offset = 10_000
            event.app.invalidate()

        @kb.add("end")
        def _scroll_bottom(event):
            self._scroll_offset = 0
            event.app.invalidate()

        return kb

    def _safe_dispatch(self, text: str) -> None:
        try:
            if self._on_submit:
                self._on_submit(text)
        except SystemExit:
            try: self._app.exit()
            except Exception: pass
        except Exception as e:
            self.write(f"\n[tui handler error: {e}]\n")

    # ── public API ─────────────────────────────────────────────

    def start_thinking(self) -> None:
        """Switch the tip line into rotating [thinking] mode."""
        self._thinking = True
        self._thinking_started = time.time()
        self._thinking_last = 0.0  # force immediate refresh
        try: self._app.invalidate()
        except Exception: pass

    def stop_thinking(self) -> None:
        """Return the tip line to idle mode."""
        self._thinking = False
        self._thinking_started = 0.0
        self._tip_last = 0.0  # force idle tip to refresh
        try: self._app.invalidate()
        except Exception: pass

    def set_label(self, label: str) -> None:
        self._label = (label or "").strip()
        try: self._app.invalidate()
        except Exception: pass

    def set_status(self, text: str) -> None:
        self._status = text or ""
        try: self._app.invalidate()
        except Exception: pass

    def write(self, text: str) -> None:
        if not text:
            return
        text = str(text)
        # Chat-app behavior: follow the bottom only when the user is already
        # there. If they've scrolled up to read older output, preserve that
        # viewport by moving the scroll offset forward with the new lines.
        add_lines = max(1, text.count("\n"))
        with self._output_lock:
            if self._scroll_offset > 0:
                self._scroll_offset += add_lines
            self._output_chunks.append(str(text))
            self._output_version += 1
        try: self._app.invalidate()
        except Exception: pass

    def clear_output(self) -> None:
        with self._output_lock:
            self._output_chunks.clear()
            self._scroll_offset = 0
            self._output_version += 1
        try: self._app.invalidate()
        except Exception: pass

    def run(self, on_submit: Callable[[str], None]) -> None:
        """Blocks until the app exits. Caller is responsible for any stdout
        shimming — see _TUIStdout class in this module for a ready helper."""
        self._on_submit = on_submit
        self._app.run()

    def run_in_terminal(self, fn: Callable[[], object]):
        """Suspend the TUI, run a classic stdin/stdout block, then redraw."""
        return self._app.run_in_terminal(fn, in_executor=False)

    def _close_picker(self):
        """Hide the modal /model picker and return focus/keys to the main input."""
        self._picker_visible = False
        self._picker_container.content = Window(width=0, height=0)
        self._picker_radio = None
        self._picker_on_select = None
        try:
            self._app.layout.focus(self._input)
        except Exception:
            pass
        try:
            self._app.invalidate()
        except Exception:
            pass

    def _show_picker(self, values, title, on_select):
        """Show one step of the modal picker: a RadioList of (label, value)
        pairs. Arrow keys move the highlight (RadioList's own bindings),
        Enter confirms (app-level binding in _build_keys(), filtered on
        self._picker_visible — reads self._picker_radio), Escape/Ctrl-C
        cancels. No typing at any step."""
        radio = RadioList(values=values)
        dialog = Dialog(
            title=title,
            body=HSplit([Box(radio, padding=0)]),
            buttons=[],
            width=Dimension(preferred=74),
            with_background=True,
        )
        self._picker_radio = radio
        self._picker_on_select = on_select
        self._picker_container.content = dialog
        self._picker_visible = True
        try:
            self._app.layout.focus(radio)
        except Exception:
            pass
        try:
            self._app.invalidate()
        except Exception:
            pass

    def _open_model_picker(self):
        """Open the two-step arrow-key /model picker: provider list, then
        that provider's full model catalog. Everything runs inside this
        one long-lived Application — no nested Application, no
        run_in_terminal (that method doesn't exist on prompt_toolkit's
        Application in the installed version here — 3.0.52 — calling it
        used to surface as a "Press ENTER to continue..." loop from
        prompt_toolkit's own internals, before this was rewritten)."""
        if not self._model_catalog_fn:
            return
        try:
            providers = self._model_catalog_fn("", mode="providers")
        except Exception:
            providers = []
        if not providers:
            self.write(f"\n{Y}No provider keys configured — nothing to pick from.{X}\n")
            return

        def _after_model(model_id):
            if not model_id:
                return
            text = f"model {model_id}"
            self.write(f"\n\033[1m> {text}\033[0m\n")
            if self._on_submit:
                t = threading.Thread(target=self._safe_dispatch, args=(text,), daemon=True)
                t.start()

        def _after_provider(provider):
            if not provider:
                return
            try:
                models = self._model_catalog_fn(provider) or []
            except Exception:
                models = []
            if not models:
                self._close_picker()
                self.write(f"\n{Y}No models found for provider '{provider}'.{X}\n")
                return
            model_values = [(pin, f"{disp}  {hint}" if hint else disp) for pin, disp, hint in models]
            self._show_picker(model_values, f" {provider.upper()} Models ", _after_model)

        provider_values = [(key, f"{label}  {hint}" if hint else label) for key, label, hint in providers]
        self._show_picker(provider_values, " Select Provider ", _after_provider)

    def scroll(self, direction: str, n: int = 10) -> None:
        if direction == "up":
            self._scroll_offset += n
        elif direction == "down":
            self._scroll_offset = max(0, self._scroll_offset - n)
        elif direction == "top":
            self._scroll_offset = 10_000
        elif direction == "bottom":
            self._scroll_offset = 0
        try: self._app.invalidate()
        except Exception: pass

    def exit(self) -> None:
        try: self._app.exit()
        except Exception: pass

    def enable_number_confirm(self, check_fn: Callable[[], bool],
                               submit_fn: Callable[[str], None]) -> None:
        """Make number keys (1-5) auto-submit during confirm prompts.

        check_fn() — return True when a confirm prompt is awaiting input.
        submit_fn(digit_str) — called with "1" / "2" / "3" / "4" / "5"
            when the user presses that key while a confirm is awaiting.

        Registered with a Condition filter so number keys ONLY auto-submit
        when (a) a confirm is awaiting AND (b) the input field is empty
        (so the user can still type "type 1 2 3" as a normal message).

        Elijah 2026-04-21: "if I press the number, make sure it automatically
        enters. I don't wanna press one enter." Numbers without Enter is the
        target UX on his phone keyboard — Enter is a two-tap motion.
        """
        kb = self._app.key_bindings

        def _filter():
            try:
                return bool(check_fn()) and not self._input.text
            except Exception:
                return False

        cond = Condition(_filter)

        def _make_handler(digit: str):
            def _h(event):
                try:
                    submit_fn(digit)
                except Exception:
                    pass
            return _h

        for d in ("1", "2", "3", "4", "5"):
            kb.add(d, filter=cond)(_make_handler(d))


__all__ = [
    "SenseiApp", "COMPLETER_WORDS", "LEGEND_WORDS", "IDLE_TIPS",
    "TUIStdout",
]


# Public alias for the stdout-shim helper (master_ai.py imports this)
TUIStdout = _TUIStdout
