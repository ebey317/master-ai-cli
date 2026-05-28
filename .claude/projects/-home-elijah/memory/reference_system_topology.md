---
name: system-topology
description: "Madam-Mary full system map — services, files, models, IPTV, projects, workflow — auto-injected so no re-training needed after /clear"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 73d21f04-9d8b-4f3e-a4b4-d84ca22b1f97
---

**MACHINE: Madam-Mary (HP ProDesk, Ubuntu x86_64)**
User: Elijah Wilkins, Indianapolis. No mouse, no keyboard — voice input only. Never suggest mouse-only solutions. Never grey text in terminal.

**LAUNCH:**
- `bash ~/scripts/launch_master_ai.sh` — attach tmux session "master-ai"
- `bash ~/scripts/master.sh` — master menu (all services)
- Option 4=Sensei tmux AI, 5=Pupil browser UI, 1=Full startup

**SERVICES (always running):**
- Ollama: http://localhost:11434 (systemd, auto-starts)
- Pupil UI: http://localhost:8080/pupil.html (also http://100.101.249.96:8080/pupil.html via Tailscale)
- TTS: http://localhost:5050 (Piper, en_US-lessac-medium)
- Tailscale IP: 100.101.249.96 (remote/phone access)
- RustDesk ID: 1808427068
- Sunkissed: http://localhost:5173 (npm run dev)
- Jellyfin: running (media server, local library)

**LOCAL AI MODELS (The Trifecta):**
- master-ai:latest — qwen2.5:7b + Sensei SYSTEM prompt — daily driver (brain/coder)
- qwen2.5:3b — SPARK, instant, idle/quick answers only
- llava:latest — EYES, vision/image scan (~5s)
- Cloud: Groq, DeepSeek-R1, qwen3.5:cloud, Gemini 2.0 Flash, OpenRouter (keys in ~/.master_ai_keys)

**IPTV:**
- M3U list: ~/.iptv/channels.m3u
- Player: MPV (command line) or Hypnotix (GUI)
- ESPN stream: mpv command with XTREAM creds (stored in memory)
- Jellyfin = media server for local library content

**KEY FILES:**
- ~/scripts/master_ai.py — AI engine (STT/TTS/routing)
- ~/scripts/howwework.txt — full stack reference (READ FIRST)
- ~/scripts/sensei_mcp_server.py — Sensei MCP server
- ~/scripts/sensei_bridge.py — browser bridge
- ~/.master_ai_memory — persistent facts (all apps share)
- ~/.master_ai_keys — API keys JSON (chmod 600)
- ~/.master_ai_chats/ — ALL chat history
- ~/Desktop/AI_CONTEXT/ — session snapshots (context_latest.zip, every 5 min via cron)
- ~/MD/ → symlink to ~/.claude/projects/-home-elijah/memory/ (shared Codex+Claude markdown)

**PROJECTS:**
- ~/projects/fairchance/ — Fair Chance job autofill service
- ~/projects/reentry-desk/ — Reentry Desk MCP server (6 tools)
- ~/projects/sensei/ — Sensei browser extension
- ~/projects/claf/ — CLAF routing layer
- ~/scripts/sensei_extension/ — Chrome extension base (clone in progress)
- ~/Desktop/extension-hands/ — extension work area
- ~/Desktop/Sunkissed Security/ — privacy camera project (localhost:5173)

**MEMORY / SESSION FLOW:**
- `remember: <fact>` in Sensei saves to ~/.master_ai_memory (persists forever, all apps)
- Context auto-saves every 5 min: ~/Desktop/AI_CONTEXT/context_latest.zip
- After /clear: hook fires → injects all ⚠️/⚡ flagged memories automatically
- Claude Code hooks: ~/.claude/hooks/ (userpromptsubmit_inject.sh = session injector)

**RECOVERY:**
- Frozen Sensei: type `refresh` → `kick` → run ~/scripts/master_ai_refresh.sh
- Force kill: `pkill -KILL -f "python3.*master_ai.py"` (supervisor respawns in 3s)
- Full nuke: `tmux kill-session -t master-ai && bash ~/scripts/launch_master_ai.sh`
