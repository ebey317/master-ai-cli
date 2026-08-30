"""setup_wizard.py — interactive first-run setup for Master AI.

On a fresh GitHub clone the user has no ~/.master_ai_keys, no local models, and
no brand.sh splash. This wizard:

  1. Shows a built-in ASCII splash / login header.
  2. Offers a temporary GitHub Models assistant to talk them through config.
  3. Collects local/cloud provider preferences and API keys.
  4. Writes ~/.master_ai_keys (chmod 600).
  5. Declares "GitHub AI disconnected" and exits setup.

After setup, normal Master AI startup continues (permissions wizard, banner,
interactive loop). The GitHub token used here is NOT retained for chat; it is
only used during this one setup session.

Env override:
    MCLI_SKIP_SETUP=1   — bypass the wizard entirely (useful for tests/CI).
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

KEYS_FILE = Path.home() / ".master_ai_keys"
PERMS_FILE = Path.home() / ".master_ai_permissions_done"
SETUP_DONE_FILE = Path.home() / ".master_ai_setup_done"

GITHUB_MODELS_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_MODELS_DEFAULT_MODEL = "gpt-4o"

COLORS = {
    "bold": "\033[1m",
    "green": "\033[1;32m",
    "cyan": "\033[1;36m",
    "yellow": "\033[33m",
    "red": "\033[91m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

C = COLORS

SPLASH = f"""{C['cyan']}
    ███╗   ███╗ █████╗ ███████╗████████╗███████╗██████╗     █████╗ ██╗
    ████╗ ████║██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗   ██╔══██╗██║
    ██╔████╔██║███████║███████╗   ██║   █████╗  ██████╔╝   ███████║██║
    ██║╚██╔╝██║██╔══██║╚════██║   ██║   ██╔══╝  ██╔══██╗   ██╔══██║██║
    ██║ ╚═╝ ██║██║  ██║███████║   ██║   ███████╗██║  ██║██╗██║  ██║██║
    ╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝
{C['reset']}
{C['bold']}  Master AI — local-first agent CLI with vision, voice, MCP, and hybrid routing.{C['reset']}
{C['dim']}  Fresh clone detected. Let's get you configured in under two minutes.{C['reset']}
"""

SYSTEM_PROMPT = """You are the Master AI setup assistant, running temporarily through GitHub Models.
Your job is to help the user configure their local-first AI agent.

Rules:
- Be concise. One short paragraph per turn.
- Ask clarifying questions when needed.
- Recommend a hybrid setup: local Ollama for privacy/free daily use, plus free-tier cloud keys for heavy reasoning, vision, or when Ollama is offline.
- Do not execute shell commands. Only give guidance and ask for keys.
- When the user is ready to save, guide them to type "save".
- When setup is complete, guide them to type "done".
- Never claim to be connected after setup ends.
"""

# Display metadata only — detection below is prefix-based, so pasting a key
# for any of these (or an unlisted provider) files it under the right name.
PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter (free + paid models, one key unlocks many)",
        "key_name": "openrouter",
        "url": "https://openrouter.ai/settings/keys",
    },
    "groq": {
        "label": "Groq (free tier — Llama 3.3 70B, fast)",
        "key_name": "groq",
        "url": "https://console.groq.com/keys",
    },
    "gemini": {
        "label": "Google Gemini (free tier — 2.0 Flash, vision + web)",
        "key_name": "gemini",
        "url": "https://aistudio.google.com/app/apikey",
    },
    "openai": {
        "label": "OpenAI (paid — gpt-4o)",
        "key_name": "openai",
        "url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic (paid — Claude)",
        "key_name": "anthropic",
        "url": "https://console.anthropic.com/settings/keys",
    },
    "cerebras": {
        "label": "Cerebras (free tier — Qwen3-235B preview)",
        "key_name": "cerebras",
        "url": "https://cloud.cerebras.ai/platform/settings",
    },
    "fireworks": {
        "label": "Fireworks (BYOK — DeepSeek V3.1)",
        "key_name": "fireworks",
        "url": "https://fireworks.ai/account/api-keys",
    },
    "deepseek": {
        "label": "DeepSeek (paid — R1 reasoning)",
        "key_name": "deepseek",
        "url": "https://platform.deepseek.com/api_keys",
    },
    "huggingface": {
        "label": "HuggingFace",
        "key_name": "huggingface",
        "url": "https://huggingface.co/settings/tokens",
    },
    "xai": {
        "label": "xAI (Grok)",
        "key_name": "xai",
        "url": "https://console.x.ai",
    },
    "nvidia": {
        "label": "NVIDIA NIM (Llama/Nemotron catalog)",
        "key_name": "nvidia",
        "url": "https://build.nvidia.com",
    },
}


def _detect_provider(key: str) -> str | None:
    """Guess the provider from an API key's prefix, same map install.sh uses."""
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-v1-"):
        return "openrouter"
    if key.startswith("sk-proj-"):
        return "openai"
    if key.startswith("hf_"):
        return "huggingface"
    if key.startswith("AIzaSy"):
        return "gemini"
    if key.startswith("xai-"):
        return "xai"
    if key.startswith("csk-"):
        return "cerebras"
    if key.startswith("fw_"):
        return "fireworks"
    if key.startswith("nvapi-"):
        return "nvidia"
    if key.startswith("sk-"):
        return "deepseek"
    return None


def _print(text: str = "") -> None:
    print(text)


def _input(prompt: str) -> str:
    return input(prompt).strip()


def _yes_no(prompt: str, default_no: bool = True) -> bool:
    suffix = " [y/N] " if default_no else " [Y/n] "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return not default_no
    return raw.startswith("y")


# ~/.master_ai_keys is normally a symlink to the canonical keychain
# (~/Desktop/Projects/keychain/master_ai_keys, see KEYCHAIN.md), which is
# KEY=VALUE, not JSON — the single source of truth other consumers (CLAF,
# keychain.sh) also read. ANTHROPIC_API_KEY is deliberately never mapped —
# only ANTHROPIC_CONSOLE_KEY is, per the Max-OAuth/Console separation rule.
_KV_KEY_MAP = {
    "OPENROUTER_API_KEY": "openrouter",
    "GROQ_API_KEY": "groq",
    "GEMINI_API_KEY": "gemini",
    "ANTHROPIC_CONSOLE_KEY": "anthropic",
    "CEREBRAS_API_KEY": "cerebras",
    "FIREWORKS_API_KEY": "fireworks",
    "OPENAI_API_KEY": "openai",
    "DEEPSEEK_API_KEY": "deepseek",
    "HUGGINGFACE_TOKEN": "huggingface",
    "HF_TOKEN": "huggingface",
    "NVIDIA_API_KEY": "nvidia",
}
_CANONICAL_NAME = {v: k for k, v in _KV_KEY_MAP.items() if k != "HF_TOKEN"}


def _parse_kv_keys(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name, val = name.strip(), val.strip()
        short = _KV_KEY_MAP.get(name)
        if short and val and short not in out:
            out[short] = val
    return out


def _write_keys(keys: dict) -> None:
    """Write keys back. If KEYS_FILE is a symlink into the canonical KV
    keychain, update it in place (KEY=VALUE, preserving other lines/
    comments) instead of replacing the symlink with a JSON blob — that
    would orphan it from every other consumer (CLAF, keychain.sh)."""
    target = KEYS_FILE.resolve() if KEYS_FILE.is_symlink() else KEYS_FILE
    existing_text = target.read_text() if target.exists() else ""
    is_kv = bool(existing_text.strip()) and not existing_text.lstrip().startswith("{")

    if is_kv or (target != KEYS_FILE and not existing_text.strip()):
        lines = existing_text.splitlines()
        seen = set()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name = stripped.split("=", 1)[0].strip()
            for short, val in keys.items():
                if _CANONICAL_NAME.get(short) == name:
                    lines[i] = f"{name}={val}"
                    seen.add(short)
        for short, val in keys.items():
            if short in seen:
                continue
            canonical = _CANONICAL_NAME.get(short)
            if canonical:
                lines.append(f"{canonical}={val}")
        target.write_text("\n".join(lines) + "\n")
    else:
        target.write_text(json.dumps(keys, indent=2) + "\n")
    os.chmod(target, 0o600)


def _load_keys() -> dict:
    if not KEYS_FILE.exists():
        return {}
    text = KEYS_FILE.read_text().strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return _parse_kv_keys(text)


def _ollama_present() -> bool:
    return shutil.which("ollama") is not None


def _ollama_has_models() -> bool:
    if not _ollama_present():
        return False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            data = json.loads(r.read())
            return len(data.get("models", [])) > 0
    except Exception:
        return False


def _github_models_chat(messages: list[dict], token: str) -> str | None:
    """Single-shot chat to GitHub Models. Returns assistant content or None."""
    payload = {
        "model": GITHUB_MODELS_DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GITHUB_MODELS_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        _print(f"{C['red']}✗ GitHub Models HTTP {e.code}: {body}{C['reset']}")
        return None
    except Exception as e:
        _print(f"{C['red']}✗ GitHub Models error: {e}{C['reset']}")
        return None


def _manual_key_collection(keys: dict) -> dict:
    _print(f"\n{C['bold']}Provider setup — paste any key, it's auto-detected.{C['reset']}")
    _print(f"{C['dim']}Recognizes: " + ", ".join(PROVIDERS) + f"{C['reset']}")
    _print(f"{C['dim']}Enter with nothing to finish.{C['reset']}\n")
    for pid in PROVIDERS:
        if keys.get(PROVIDERS[pid]["key_name"]):
            _print(f"{C['green']}  ✓ {pid} already configured{C['reset']}")
    while True:
        key = getpass.getpass("  Paste a key (hidden, Enter to finish): ").strip()
        if not key:
            break
        provider = _detect_provider(key)
        if not provider:
            _print(f"{C['yellow']}  ? couldn't identify this key's provider from its prefix — skipped{C['reset']}")
            continue
        keys[provider] = key
        label = PROVIDERS.get(provider, {}).get("label", provider)
        _print(f"{C['green']}  ✓ {provider} — {label}{C['reset']}")
    return keys


def _interactive_github_setup(token: str) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    keys = _load_keys()

    _print(f"{C['bold']}  Starting temporary GitHub Models setup chat.{C['reset']}")
    _print(f"{C['dim']}  Type your question, 'save' to write keys, or 'done' to finish.{C['reset']}\n")

    # First assistant turn
    intro = (
        "Hi! I'm your temporary setup assistant.\n\n"
        "Tell me what you want to use Master AI for, or ask me to walk you through:\n"
        "  1) Installing Ollama + a local model\n"
        "  2) Which free cloud keys are worth adding\n"
        "  3) How hybrid routing decides local vs cloud\n\n"
        "Type 'save' whenever you're ready to save your keys, or 'done' to finish setup."
    )
    _print(f"{C['green']}Assistant:{C['reset']} {intro}\n")

    while True:
        try:
            user_text = _input(f"{C['bold']}You:{C['reset']} ")
        except (EOFError, KeyboardInterrupt):
            _print(f"\n{C['yellow']}Setup interrupted. Exiting.{C['reset']}")
            sys.exit(130)

        lower = user_text.lower()
        if lower == "save":
            keys = _manual_key_collection(keys)
            _write_keys(keys)
            _print(f"{C['green']}✓ Saved to {KEYS_FILE} (chmod 600).{C['reset']}")
            continue
        if lower in ("done", "finish", "quit", "exit"):
            _print(f"\n{C['green']}✓ Setup complete. Disconnecting GitHub AI.{C['reset']}")
            break

        messages.append({"role": "user", "content": user_text})
        reply = _github_models_chat(messages, token)
        if reply is None:
            _print(f"{C['yellow']}GitHub Models is not answering. Falling back to manual key entry.{C['reset']}")
            keys = _manual_key_collection(keys)
            _write_keys(keys)
            break
        messages.append({"role": "assistant", "content": reply})
        _print(f"\n{C['green']}Assistant:{C['reset']} {reply}\n")

    _print(f"\n{C['cyan']}🔌 GitHub AI disconnected.{C['reset']}")
    SETUP_DONE_FILE.touch()
    return keys


def _run_manual_setup() -> dict:
    _print(f"\n{C['bold']}Manual setup mode.{C['reset']}")
    _print("You can re-run this anytime with: master-ai --setup\n")
    keys = _load_keys()
    keys = _manual_key_collection(keys)
    _write_keys(keys)
    SETUP_DONE_FILE.touch()
    _print(f"\n{C['green']}✓ Manual setup saved.{C['reset']} {C['cyan']}No GitHub AI was used.{C['reset']}")
    return keys


def first_run_needed() -> bool:
    """True when neither setup nor permissions have run and no keys exist."""
    if os.environ.get("MCLI_SKIP_SETUP") in ("1", "true", "yes"):
        return False
    if SETUP_DONE_FILE.exists() or PERMS_FILE.exists():
        return False
    if KEYS_FILE.exists():
        return False
    return True


def run_setup_if_first_run() -> None:
    """Entry point called by master_ai.py main()."""
    if not first_run_needed():
        return

    os.system("clear")
    _print(SPLASH)
    _print(f"{C['bold']}Welcome. This appears to be your first run from a fresh clone.{C['reset']}\n")

    if not _yes_no("Run interactive setup? (Recommended)", default_no=False):
        _print(f"{C['yellow']}Skipping setup. You can run it later with: master-ai --setup{C['reset']}\n")
        SETUP_DONE_FILE.touch()
        return

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        _print(f"\n{C['bold']}GitHub Models setup assistant uses a GitHub token.{C['reset']}")
        _print(f"{C['dim']}Create one at https://github.com/settings/tokens with 'read:packages' and models access.{C['reset']}")
        _print(f"{C['dim']}The token is hidden when typed and is only used during this setup session.{C['reset']}")
        token = getpass.getpass("Paste GitHub token (hidden): ").strip()

    if token:
        keys = _interactive_github_setup(token)
    else:
        _print(f"{C['yellow']}No GitHub token provided. Switching to manual setup.{C['reset']}")
        keys = _run_manual_setup()

    # Reload globals in master_ai if it has already imported
    try:
        import master_ai
        master_ai.KEYS = keys
    except Exception:
        pass


def run_setup_explicit() -> None:
    """Entry point for `master-ai --setup`."""
    os.system("clear")
    _print(SPLASH)
    _print(f"{C['bold']}  Re-running setup wizard.{C['reset']}\n")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    use_github = _yes_no("Use GitHub Models assistant for interactive setup?", default_no=False)
    if use_github:
        if not token:
            token = getpass.getpass("Paste GitHub token (hidden): ").strip()
        if token:
            _interactive_github_setup(token)
        else:
            _run_manual_setup()
    else:
        _run_manual_setup()

    try:
        import master_ai
        master_ai.KEYS = _load_keys()
    except Exception:
        pass
    _print(f"\n{C['green']}✓ Setup finished. Run `master-ai` to start.{C['reset']}")
