# Madam-Mary Field Manual

**Operator:** Elijah (ebey317@gmail.com)
**Machine:** Madam-Mary · HP ProDesk · Ubuntu Linux x86_64
**Written:** 2026-05-22

Everything you need to keep the stack running, recover from breakage, and hand it off cold to a future operator (human or agent).

---

## 1. THE STACK AT A GLANCE

```
┌──────────────────────────────────────────────────────────────┐
│  CLAUDE CODE (Max OAuth, ~/.claude/.credentials.json)        │  ← talks to YOU
├──────────────────────────────────────────────────────────────┤
│  CLAF orchestrator  (~/projects/claf/orchestrator.py)        │
│  http://localhost:8000/v1/messages  (Anthropic-shape proxy)  │
│  ─ Local / Tap / Flash trickle routing                       │
│  ─ Throttle: 5 flash/hr, 15 tap/hr, 25K tokens/day           │
├──────────────────────────────────────────────────────────────┤
│  Cloud peers (read from keychain at startup):                │
│    t=1 ollama-cloud-coder   qwen3-coder:480b-cloud  FREE     │
│    t=2 groq                 llama-3.3-70b           FREE     │
│    t=3 cerebras             qwen-3-235b             credits  │
│    t=6 fireworks            deepseek-v4-pro         paid     │
│    t=7 openrouter           sonnet-4.6              paid     │
│    t=9 anthropic-haiku      haiku-4-5               paid     │
├──────────────────────────────────────────────────────────────┤
│  Local Ollama (:11434)  ── master-ai:latest, qwen2.5:3b/7b   │
│                           llava, fast-agent, qwen2.5vl       │
│                           qwen3-coder:480b-cloud (signed)    │
├──────────────────────────────────────────────────────────────┤
│  Sensei bridge (:8080)  ── Chrome-extension HTTP backend     │
│  Pupil / Sensei TUI      ~/scripts/master_ai.py              │
└──────────────────────────────────────────────────────────────┘

Sunkissed Soul (Base44 app, id 69bbc5d1e9e0ac17a3180439)
  → planned to talk to CLAF at :8000 via HubConfiguration.claf_url
```

## 2. ACCOUNT SEPARATION (LOAD-BEARING — DON'T BREAK THIS)

Two Anthropic-flavored billing surfaces. They must not cross.

| Account | Path | Used By | Cost Model |
|---|---|---|---|
| **Max subscription** | `~/.claude/.credentials.json` | Claude Code CLI / extension only | Flat $100/mo |
| **Console (Platform key)** | `~/Desktop/keychain/master_ai_keys` → `ANTHROPIC_CONSOLE_KEY` | CLAF cloud peer (Flash → Anthropic Haiku) | Per-token, $0 if you don't escalate |

### How the separation is enforced

1. The keychain stores the key under `ANTHROPIC_CONSOLE_KEY` (NOT `ANTHROPIC_API_KEY`). If something stray sources the keychain file, only `ANTHROPIC_CONSOLE_KEY` leaks into env — Claude Code reads `ANTHROPIC_API_KEY` so it stays clean.
2. `~/projects/claf/orchestrator.py:_normalize_bootstrap_key` has an alias that translates `ANTHROPIC_CONSOLE_KEY → ANTHROPIC_API_KEY` ONLY inside CLAF's own process env. Nothing downstream sees it.
3. `~/projects/claf/launch.sh:30` explicitly `unset ANTHROPIC_API_KEY` before `exec claude` — belt-and-suspenders.
4. `~/Downloads/claf_lockdown.sh` writer emits `ANTHROPIC_CONSOLE_KEY` (re-runs don't undo separation).

### Verify separation any time

```
keychain check
```

All four lines must read `OK`. If any read `FAIL` or `WARN`, fix before doing anything else with Anthropic.

## 3. THE KEYCHAIN

Single source of truth for all cloud API keys.

| Path | What |
|---|---|
| `~/Desktop/keychain/master_ai_keys` | The real file (chmod 600) |
| `~/.master_ai_keys` | Symlink → above (for back-compat with hardcoded paths) |

### Current contents (as of this manual)

```
ANTHROPIC_CONSOLE_KEY    sk-ant-…       108 chars   ✓ paid, working (Haiku only on Tier-1)
OPENROUTER_API_KEY       sk-or-v1-…      73 chars   ✓ pay-per-use, working
GROQ_API_KEY             gsk_…           56 chars   ✓ free tier, working
CEREBRAS_API_KEY         csk-…           52 chars   ✓ credits/paid, working
FIREWORKS_API_KEY        fw_…            25 chars   ✓ paid, working
FIRECRAWL_API_KEY        fc-…            35 chars   ✓ tool key (web scrape)
SERPER_API_KEY           94df…           40 chars   ✓ tool key (Google search API)
```

Removed by policy ("no more paid keys"): DeepSeek, OpenAI, Gemini.

### Keychain commands

```
keychain list             show registered names with masked values + perms
keychain probe            one /models call per key, no token spend
keychain probe groq       probe just one (shortcuts: anthropic|console, groq, gemini,
                          openrouter|or, cerebras, fireworks, openai|gpt, deepseek|ds)
keychain edit             open in $EDITOR (chmod 600 after save)
keychain backup           timestamped manual copy
keychain check            verify env separation (Max OAuth vs Console)
keychain path             show symlink + real target
```

### Adding a new key (provider that already has a CLAF peer)

```
keychain edit
  # append:  NEWPROVIDER_API_KEY=...
  # save+exit
keychain probe newprovider    # confirm it authenticates
restart CLAF (see §5)         # so the env reloads
```

### Adding a brand-new provider

1. Append to `~/Desktop/keychain/master_ai_keys`: `NEWPROVIDER_API_KEY=...`
2. Edit `~/projects/claf/claf_config.py:_cloud_peers()` — add a `Provider(...)` entry with `tier`, `name`, `pool="cloud"`, `kind` (openai_compat | anthropic | ollama), `model`, `url`, `env_key="NEWPROVIDER_API_KEY"`, `enabled=_env_present("NEWPROVIDER_API_KEY")`.
3. Edit `~/projects/claf/orchestrator.py:_KEY_MAP` — add `"newprovider": "NEWPROVIDER_API_KEY"`.
4. Edit `~/scripts/keychain.sh` probe specs (add the provider's `/v1/models` endpoint) and the shortcut case statement.
5. Restart CLAF.
6. `keychain probe newprovider` to verify.

### Rotating a key

```
keychain edit
  # change just the value on the line
  # save+exit
keychain probe <name>
restart CLAF
```

## 4. CLAF ORCHESTRATOR

### What it is

A FastAPI proxy on `:8000` that accepts Anthropic-format requests (Claude Code's native shape) and routes them locally or to cloud peers under a strict daily token budget.

### Files

```
~/projects/claf/
├── orchestrator.py        FastAPI app, request handler, throttle wiring
├── claf_config.py         Mode + provider registry + _select_mode() + TAP_TEMPLATES
├── claf_throttle.py       ThrottleState + reserve/commit/refund + snapshot
├── launch.sh              Wires Claude Code at it (unsets ANTHROPIC_API_KEY)
├── orchestrator.log       JSON-lines audit log (route_decision, trickle_*, etc.)
└── .backup.*              Date-stamped snapshots before each edit
```

### Three modes (claf_config.py MODE)

| Mode | What | Set via |
|---|---|---|
| `local` | Ollama only; no cloud peers in PROVIDERS | `CLAF_MODE=local` |
| `hybrid` (default) | Local for routine; cloud peers on hard tasks via _select_mode | `CLAF_MODE=hybrid` |
| `cloud` | Cloud peers only; local bypassed | `CLAF_MODE=cloud` |

### Three escalation tiers (orchestrator.py messages())

| Tier | Trigger | Cost |
|---|---|---|
| **Local** | Default; routine text, short payloads | $0 |
| **Tap** | Prompt mentions regex / sql / bash / debug + short; reserves 800 tokens | Cheap (snippet polish via tier-1 cloud peer) |
| **Flash** | `metadata.force_cloud=true` OR debug/refactor/architecture/race keywords + 5000-token reservation | One full cloud peer call |

`metadata.emergency=true` (alongside force_cloud) draws from the daily 3-emergency-flash pool — bypasses hourly Flash cap.

### Throttle budgets (~/projects/claf/claf_throttle.py)

```python
flash_budget_hourly = 5         # full cloud handoffs / hour
tap_budget_hourly = 15           # snippet polishes / hour
token_budget_daily = 25_000      # total tokens reserved / day
emergency_flash_daily = 3        # bypass cap
```

Math at the ceiling: 5 flashes × 5K = 25K (one hour can saturate the day) or 31 taps × 800 = ~25K (lots of polishes). Daily is the binding constraint.

Raise the ceiling: edit `THROTTLE.token_budget_daily` in `claf_throttle.py`, restart CLAF.

## 5. RECOVERY PROCEDURES

### CLAF restart (after editing config)

```
PID=$(pgrep -f "python3 orchestrator.py" | head -1) ; kill $PID 2>/dev/null
cd ~/projects/claf && nohup python3 orchestrator.py > /tmp/claf.log 2>&1 &
sleep 4 ; ss -tlpn | grep :8000 && echo OK
```

If port doesn't bind, `tail /tmp/claf.log` — usually a syntax error in claf_config.py.

### CLAF "still alive after kill" (we hit this)

```
for PID in $(pgrep -f "python3 orchestrator.py"); do kill $PID; done
sleep 3
ss -tlpn | grep :8000 || echo "port free"
# then start fresh
```

### Ollama models broken / quiet

```
sudo systemctl restart ollama
ollama list                                # see registered models
ollama run qwen2.5:7b "hi"                # smoke test local
ollama run qwen3-coder:480b-cloud "hi"    # smoke test cloud
ollama signin                              # if cloud says "not signed in"
```

### Sensei TUI (master_ai.py) frozen

```
inside Sensei:    refresh        # soft re-exec
inside Sensei:    kick           # exit 42, supervisor respawns
any shell:        ~/scripts/master_ai_kick.sh
any shell:        pkill -KILL -f "python3.*master_ai.py"
nuke + restart:   tmux kill-session -t master-ai && bash ~/scripts/launch_master_ai.sh
```

### "Where were we?" handoff to a new session

```
ls -t ~/Desktop/AI_CONTEXT/context_*.txt | head -1
```

That's the newest 5-min auto-saved snapshot. A fresh Claude Code session also reads:

- `~/CLAUDE.md` (startup routine)
- `~/.claude/projects/-home-elijah/memory/MEMORY.md` (pinned project + feedback memories)
- `~/Desktop/AI_CONTEXT/` (rolling snapshots)
- `~/scripts/howwework.txt` (full stack reference)

## 6. SUNKISSED SOUL (Base44 app)

App ID: `69bbc5d1e9e0ac17a3180439`
Editor: https://app.base44.com/apps/69bbc5d1e9e0ac17a3180439/editor/preview
Preview tokens rotate; use Base44 MCP to get a fresh one.

### Entities (20)

CustomModule, UserProfile, Content, DeviceProfile, CompanionProfile, SKSGoal, StoragePartition, Recipe, Remedy, ShareSettings, SyncDevice, Picture, ExternalStorage, Onboarding, SecurityEvent, SecurityAssessment, SecuritySettings, HubConfiguration, CameraDevice, User.

### Hub wiring (the live integration)

`HubConfiguration` entity should hold:

```
hub_name        "tavern" or "madam-mary"
ollama_url      http://localhost:11434  (NOT :5173 — that was wrong)
claf_url        http://localhost:8000   (new field per the audit)
keychain_status configured | missing | unknown   (replaces groq_api_key)
ollama_status   online | offline (live, driven by /api/tags ping every 30s)
models_available  [auto-populated from /api/tags response]
```

Never store API key values in entity data — they live in `~/Desktop/keychain/master_ai_keys` only.

### Send the app a build directive

```
mcp__claude_ai_Base44__edit_base44_app
  appId: 69bbc5d1e9e0ac17a3180439
  editPrompt: "your spec here"
```

Costs Base44 credits, not Claude tokens. Build runs on their backend.

## 7. POWER MOVES (MCP tools at your disposal)

### Browser (Sensei MCP — opens tabs in the visible MCP tab group)

```
mcp__sensei__browse(url)      navigate the current MCP tab
mcp__sensei__read()           read visible page
mcp__sensei__click(what)      click by label or selector
mcp__sensei__fill(where, text) type into a field
mcp__sensei__search(query)    Google search
```

### Voice + run

```
~/scripts/speak.sh "message"     speak (Piper TTS)
mcp__sensei__run(cmd)            run a shell command (with /home dir context)
```

### Google Drive (read existing docs, create new, search)

```
mcp__claude_ai_Google_Drive__search_files(query)
mcp__claude_ai_Google_Drive__read_file_content(fileId)
mcp__claude_ai_Google_Drive__create_file(content, title, mimeType)
mcp__claude_ai_Google_Drive__list_recent_files(orderBy)
```

### Gmail (search, draft, send via Google account)

```
mcp__claude_ai_Gmail__search_threads(query)
mcp__claude_ai_Gmail__create_draft(to, subject, body)
```

### Hugging Face (model discovery, dataset preview, doc search)

```
mcp__claude_ai_Hugging_Face__hub_repo_search(query, repo_types)
mcp__claude_ai_Hugging_Face__paper_search(query)
```

### Canva / Indeed / Todoist / ZipRecruiter / Base44 — connectors available, ask if needed

## 8. KEY DOCS ON DISK

| Path | What |
|---|---|
| `~/CLAUDE.md` | Startup routine + standing rules (read on every fresh session) |
| `~/scripts/howwework.txt` | Full stack + services reference |
| `~/scripts/ARCHITECTURE.md` | Design decisions (WHAT) |
| `~/scripts/DEV_PROCESS.md` | Process patterns (HOW) |
| `~/scripts/KEYCHAIN.md` | Keychain schema + rotation procedures |
| `~/projects/claf/CLAUDE.md` | CLAF-specific notes |
| `~/.claude/projects/-home-elijah/memory/MEMORY.md` | Pinned memories index |
| `~/Desktop/AI_CONTEXT/context_*.txt` | Rolling 5-min context snapshots |
| `~/Desktop/FIELD_MANUAL.md` | **This file** |

## 9. DAILY CHECKS (60 seconds)

```
keychain check                  # env separation clean?
keychain probe                  # all keys still authenticate?
curl -s http://localhost:8000/healthz | jq .ollama_reachable
curl -s http://localhost:8000/stats | jq .throttle    # budget burn so far today
ollama list | head              # cloud + local models OK
```

If any line is RED, fix that one before moving on.

## 10. THE THINGS THAT WILL TRIP YOU UP

1. **`pkill -f orchestrator.py` can match the bash command running it** and the kill cascade gets weird. Use explicit PIDs from `pgrep -f`.
2. **Two orchestrator processes can exist** if a restart didn't actually kill the old one. Always check `pgrep -af` shows exactly one before assuming a restart took effect.
3. **`stat` doesn't follow symlinks by default.** `~/.master_ai_keys` shows perm 777 (the symlink) — the real file is 600. Use `stat -L` or `readlink -f`.
4. **`ollama_installed: false` + `ollama_status: online`** is a real contradiction in the Sunkissed app. Don't trust either field alone; ping `/api/tags`.
5. **OpenRouter's `/v1/models` endpoint is public** — auth-required probes need `/auth/key` to actually test the key.
6. **Groq's API blocks no-User-Agent requests with Cloudflare 1010.** Always send a UA header.
7. **Anthropic Tier-1 caps Opus and Sonnet harshly** (429 on every call). Haiku has the most headroom; that's why CLAF's anthropic peer is pinned to `claude-haiku-4-5-20251001` until the tier raises.
8. **Ollama Cloud has paid models** (`qwen3.5:cloud`, `kimi-k2.5:cloud`) that 4xx as "subscription required" on the free tier. Only `qwen3-coder:480b-cloud` is free on `ebey317`'s account.
9. **Two device keys on one Ollama account** is normal — each `ollama signin` adds a key per machine. Both legit; don't revoke unless you don't recognize a device.
10. **Base44 entity data is client-readable.** Never store API keys, OAuth tokens, or secrets in entity fields. Always reference them from the local keychain via a `*_status` enum.

---

**End of manual.** Hand this file to any future operator or agent and they can pick up cold.
