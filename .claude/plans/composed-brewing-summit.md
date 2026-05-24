# Plan: Permanent service resilience for CLAF + sensei-bridge

## Context

Both the CLAF orchestrator and the sensei bridge die intermittently with no auto-recovery:

- **sensei-bridge** is already under systemd (`sensei-bridge.service`, `Restart=on-failure`) — it survives crashes. ✓
- **CLAF orchestrator** has a systemd unit (`claf.service`) that exists but is **inactive/dead**. The process that's currently running was started manually via a bash command — if it crashes, nothing restarts it. ✗

Additional issues found during exploration:
- `claf.service` has stale model config: logs to `/tmp/claf_orchestrator.log` (not the path watch.py reads), and `CLAF_LOCAL_MODEL=qwen2.5:7b` via a drop-in override — but the active `.env` and running process use `fast-agent:latest`.
- `Restart=on-failure` misses clean exits (exit 0). Both services should use `Restart=always` so any unexpected stop triggers a restart.

The fix is surgical: correct `claf.service`, stop the manually-running orchestrator, hand it to systemd, and tighten both units.

---

## Changes

### 1. Fix `claf.service`

File: `/home/elijah/.config/systemd/user/claf.service`

Problems to fix:
- Log path → `/home/elijah/projects/claf/orchestrator.log` (what watch.py tails)
- `Restart=on-failure` → `Restart=always`
- Remove stale `PYTHONPATH` and `CLAF_LOCAL_MODEL` env vars from the unit (those live in `.env`, loaded at import)

New unit:
```ini
[Unit]
Description=CLAF orchestrator — hybrid local/cloud proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/elijah/projects/claf
EnvironmentFile=-/home/elijah/projects/claf/.env
ExecStart=/usr/bin/python3 /home/elijah/projects/claf/orchestrator.py
Restart=always
RestartSec=5
StandardOutput=append:/home/elijah/projects/claf/orchestrator.log
StandardError=append:/home/elijah/projects/claf/orchestrator.log

[Install]
WantedBy=default.target
```

### 2. Delete the drop-in override

File to remove: `/home/elijah/.config/systemd/user/claf.service.d/use-vl3b.conf`

It overrides `CLAF_LOCAL_MODEL=qwen2.5:7b`, which conflicts with `fast-agent:latest` from `.env`. With `EnvironmentFile` pointing at `.env`, the drop-in is harmful and unnecessary.

### 3. Tighten `sensei-bridge.service`

File: `/home/elijah/.config/systemd/user/sensei-bridge.service`

Change `Restart=on-failure` → `Restart=always`. Everything else stays.

### 4. Reload, stop manual process, start via systemd

```bash
systemctl --user daemon-reload
kill -9 <manual orchestrator PID>          # stop the rogue process
systemctl --user restart claf.service
systemctl --user status claf.service       # verify active (running)
systemctl --user status sensei-bridge.service
```

### 5. Verify watch.py sees the log

```bash
python3 ~/projects/claf/watch.py           # should show banner + live events
curl -s http://localhost:8000/healthz      # should return mode=hybrid
curl -s http://localhost:8080/health       # sensei bridge
```

---

## What this gives you

| Service | Before | After |
|---------|--------|-------|
| sensei-bridge | Restart=on-failure (misses clean exits) | Restart=always |
| CLAF orchestrator | Manual process, no auto-restart | systemd-managed, Restart=always |
| Log path | /tmp/claf_orchestrator.log (broken) | /home/elijah/projects/claf/orchestrator.log |
| Model config | Conflicting (qwen2.5:7b drop-in vs fast-agent in .env) | .env is sole source, drop-in deleted |

After this, both services restart within 5 seconds of any crash or clean exit — no manual intervention needed.

No watchdog agent required. systemd's built-in restart is the right tool here.

---

## Files modified

- `/home/elijah/.config/systemd/user/claf.service` — rewritten
- `/home/elijah/.config/systemd/user/sensei-bridge.service` — one line change
- `/home/elijah/.config/systemd/user/claf.service.d/use-vl3b.conf` — deleted
