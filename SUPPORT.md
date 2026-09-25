# Master AI Support

Last updated: 2026-04-25

## First Checks

Run these from a terminal:

```bash
cd ~/scripts
bash sensei_selftest.sh
python3 -m py_compile master_ai.py
```

Inside Sensei, run:

```text
doctor
router
harvest
```

## Common Fixes

- If Sensei feels stuck, type `kick`.
- If the browser UI looks stale, type `refresh`.
- If Ollama is missing, run `bash install.sh` again.
- If a model is missing, run `ollama pull qwen2.5:7b`, `ollama pull qwen2.5:3b`, or `ollama pull llava`.
- If a command is blocked, read the reason. Master AI blocks missing binaries, interactive commands in `RUN:`, destructive commands, and sudo/password workflows by design.

## What To Include In A Support Request

- Operating system and hardware RAM
- The output of `doctor`
- The last 20 lines of `~/scripts/master.crash.log`, if it exists
- The exact prompt that failed
- Whether the mode was Plan, Review, or Auto

Do not send passwords, API keys, private documents, or payment information.

## Supported Platforms

- Linux: full support
- WSL2 Ubuntu: supported, with systemd caveats
- macOS: partial support through Homebrew/Ollama
- Native Windows without WSL: not recommended
