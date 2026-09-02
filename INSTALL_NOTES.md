# Master AI CLI — Offline-First Setup & Optional Cloud Escalation

## What this is

Sensei is a local-first AI agent runtime. It runs on your machine first and only reaches out to cloud providers when you explicitly configure keys. The installer is designed to work without any API keys.

## What you need

- Linux, macOS, or WSL2 on Windows
- Python 3
- ~16 GB RAM recommended (8 GB minimum with smaller models)
- Ollama (the installer can install it for you)
- Bash-compatible shell

## Offline-first install

```bash
# Download and extract the buyer bundle
mkdir -p ~/Downloads/master-ai && cd ~/Downloads/master-ai
tar -xzf master-ai-vYYYYMMDD.tar.gz

# Run the installer
bash master-ai-YYYYMMDD/install.sh
```

The installer:
- Copies runtime files to `~/scripts/`
- Installs `master` and `sensei` commands in `~/.local/bin/`
- Creates required runtime directories under `~/.master_ai_*`
- Seeds a default profile at `~/.master_ai_profiles/default/config.json`
- Checks sandbox dependencies (`systemd-run`, `unshare`, `prlimit`, `bash`, `python3`)
- Optionally installs Ollama and pulls default models
- Optionally enables user systemd services for 24/7 operation

No keys are required. If you skip the API-key step, Sensei falls back to local Ollama models.

## Optional cloud escalation

Cloud providers are **opt-in**. Add keys after install at any time:

```bash
# Edit directly (chmod 600)
~/.master_ai_keys
```

Supported providers (each independently gated by its own key):

| Prefix | Provider |
|---|---|
| `gsk_` | Groq |
| `sk-or-v1-` | OpenRouter |
| `AIzaSy` | Google Gemini |
| `sk-ant-` | Anthropic Claude |
| `sk-proj-` or `sk-` | OpenAI / DeepSeek |
| `hf_` | HuggingFace |
| `xai-` | xAI |
| `nvapi-` | NVIDIA |

Add a key during install by pasting it when prompted, or write the JSON file manually:

```json
{
  "openrouter": "sk-or-v1-...",
  "gemini": "AIzaSy..."
}
```

## Sandbox safety

On Linux/WSL, every `RUN` shell command is wrapped with:

```
systemd-run --user --scope -p TasksMax=200 -p MemoryMax=1G -- \
  unshare -U -m -p --mount-proc --map-root-user -f -- \
    prlimit --nofile=512 --as=1073741824 -- \
      bash -c <command>
```

This contains:
- Fork bombs via `TasksMax`
- Memory runaway via `MemoryMax`
- Secret paths hidden by bind-mount overlay (e.g. `~/.ssh`, `~/.master_ai_keys`)
- No real root inside the namespace even if the command tries

`RUNTERM` (interactive terminal) is not sandboxed because it opens a visible GUI terminal you watch and control.

## Reinstall / update

Re-run the installer from a new bundle:

```bash
bash ~/scripts/install.sh
```

Your profile, keys, skills, and chat history live in `~/.master_ai_*` and are preserved.

## Uninstall

No global changes are made outside `~/.local/bin`, `~/.config/systemd/user`, and `~/.master_ai_*`. Remove those directories and the `~/scripts/` bundle to clean up.

## Verification

After install, run:

```bash
bash ~/scripts/sensei_selftest.sh
python3 ~/scripts/test_typed_dispatch_e2e.py
python3 ~/scripts/test_sandbox_escape.py
```

Expected: `agent_standards_score()` at 100/100, 0 WARN, 0 FAIL.
