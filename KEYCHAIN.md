# Cloud API Keychain — official registry

## Canonical location

```
~/Desktop/keychain/master_ai_keys     chmod 600  (real file)
~/.master_ai_keys                     chmod 600  (symlink → above)
~/Desktop/keychain/                   chmod 700  (parent dir, owner-only)
```

The real file lives in `~/Desktop/keychain/` so it's visible to the operator at a glance. A symlink at `~/.master_ai_keys` preserves every consumer that was hardcoded to the old path (CLAF orchestrator, claf_lockdown.sh, keychain.sh, sensei_bridge, etc.) — they all keep working without code changes.

This is the single source of truth for all cloud-provider API keys on this machine. Every consumer MUST read keys from this file (or the symlink), never from a separate per-app config.

**Do not put `~/Desktop/keychain/` in any folder that syncs to cloud storage** (Dropbox, OneDrive, Google Drive, iCloud, Syncthing). Plain Linux Desktop on Madam-Mary is not synced, so the current setup is safe. If you ever migrate, re-verify before pasting any key.

## File schema

Plain `KEY=VALUE` lines. Lines starting with `#` are comments. Blank lines allowed. JSON object format is also accepted (CLAF orchestrator's loader supports both — see `~/projects/claf/orchestrator.py:_load_keys_json_or_kv`).

Mandatory header comment (kept by `~/Downloads/claf_lockdown.sh` writer):

```
# CLAF peer API keys — projected into env by orchestrator.py
# Format: KEY=VALUE per line. Loader fallback supports this.
# Rewritten by claf_lockdown.sh <timestamp>
```

## Variable names — STRICT convention

| Source name in keychain    | What it is                                          | Projected to env as       | Used by                                |
|-----------------------------|-----------------------------------------------------|---------------------------|----------------------------------------|
| `ANTHROPIC_CONSOLE_KEY`     | Anthropic Platform/Console (per-token billing)      | `ANTHROPIC_API_KEY` *     | CLAF Flash → Anthropic peer            |
| `OPENROUTER_API_KEY`        | OpenRouter (BYOK gateway to many models)            | same                      | CLAF tier-5 cloud peer                 |
| `GROQ_API_KEY`              | Groq (fast free tier, llama-3.3-70b-versatile)      | same                      | CLAF tier-1 cloud peer; Tap polish     |
| `GEMINI_API_KEY`            | Google Gemini (gemini-2.5-flash)                    | same                      | CLAF tier-2 cloud peer                 |
| `CEREBRAS_API_KEY`          | Cerebras (fast inference)                           | same                      | CLAF tier-3 cloud peer (optional)      |
| `FIREWORKS_API_KEY`         | Fireworks (hosted open models)                      | same                      | CLAF tier-4 cloud peer (optional)      |

\* **Critical separation.** The Anthropic key is stored under `ANTHROPIC_CONSOLE_KEY` (not `ANTHROPIC_API_KEY`) so an accidental `source ~/.master_ai_keys` in a shell cannot leak the platform key into Claude Code's env. Claude Code uses the **Max OAuth subscription** at `~/.claude/.credentials.json` and MUST NOT see the Console key. CLAF's orchestrator translates `ANTHROPIC_CONSOLE_KEY → ANTHROPIC_API_KEY` only inside its own process env, via the alias in `orchestrator.py:_normalize_bootstrap_key`.

## What lives WHERE — the two-account picture

```
~/.claude/.credentials.json     Max subscription (OAuth)        owns Claude Code runtime
~/.master_ai_keys               Console + 3rd-party API keys    owns CLAF cloud peers, Tap polish, future MCP servers
```

These never share a key name in env. If you ever see `ANTHROPIC_API_KEY` in your shell's `env` output, something has crossed — investigate immediately.

## Verifying separation

```
# Should print NOTHING in any interactive shell:
env | grep -i anthropic

# CLAF process can have it (it's projected there at bootstrap):
PID=$(pgrep -f "python3 orchestrator.py" | head -1)
xargs -0 -L1 -a /proc/$PID/environ | grep -i anthropic

# launch.sh strips the var before Claude Code starts (defense-in-depth):
grep -n 'unset ANTHROPIC_API_KEY' ~/projects/claf/launch.sh
```

## Adding a new key

```
# 1. Edit the keychain (don't echo the raw key in a logged terminal):
nano ~/.master_ai_keys
# 2. Append in the canonical format:
# NEWPROVIDER_API_KEY=...
# 3. chmod 600 if it isn't already:
chmod 600 ~/.master_ai_keys
# 4. If CLAF needs to know about this provider, add a Provider() entry in
#    ~/projects/claf/claf_config.py _cloud_peers() with env_key="NEWPROVIDER_API_KEY".
# 5. Restart CLAF: kill the orchestrator PID; python3 ~/projects/claf/orchestrator.py
# 6. Verify: curl http://localhost:8000/healthz | jq '.config.cloud_peers_enabled'
```

## Rotating a key

The key is per-line. Replace the value, restart CLAF, done.

```
sed -i 's|^GROQ_API_KEY=.*|GROQ_API_KEY=NEW_VALUE_HERE|' ~/.master_ai_keys
```

(Then restart CLAF as above.)

## Tooling

`~/scripts/keychain.sh` — small viewer/validator.

```
keychain list             # show registered names with masked values
keychain probe            # one tiny API call per enabled key, report OK/AUTH/RATE/NOFUNDS
keychain probe groq       # probe just one provider
keychain edit             # open the keychain in $EDITOR
keychain backup           # timestamped copy
```

## What does NOT belong here

- Claude Code's Max OAuth state. Lives at `~/.claude/.credentials.json`. Managed by `claude` CLI.
- Per-app session tokens (Chrome extension token, sensei_bridge token, etc.). Those live in their own dotfiles like `~/.master_ai_extension_token`.
- Secrets unrelated to LLM cloud peers (cloud provider IAM keys, deploy tokens, etc.). Use the appropriate provider-native location.

## Backups

`claf_lockdown.sh` keeps a timestamped backup at `$BACKUP_DIR/master_ai_keys.bak` on each rewrite. Additional ad-hoc backups land at `~/.master_ai_keys.bak.<timestamp>_<reason>`.

```
ls -la ~/.master_ai_keys.bak.* 2>/dev/null
```
