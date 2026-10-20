# Terminal-Bench — Master AI (Option A: live /chat)

Adapter `terminal_bench/master_ai_adapter.py` drives the **live** Master AI `/chat` server on `127.0.0.1:8080` (same path Eval v2 validated at 96%). The harness runs on the host, so no Docker networking changes are needed.

## Prereqs

```bash
# host: Master AI HTTP server must be up
systemctl --user status stt_server  # or however you launch it on :8080
curl -s http://127.0.0.1:8080/health

# harness deps (once)
pip install terminal-bench
pip install -e ~/master-ai-cli  # so TB can import terminal_bench.master_ai_adapter
```

## Run

```bash
# single task smoke
tb run \
  --agent "terminal_bench.master_ai_adapter:MasterAIAgent" \
  --model master-ai \
  --task-id <id> \
  --n-concurrent 1

# full suite
tb run \
  --agent "terminal_bench.master_ai_adapter:MasterAIAgent" \
  --model master-ai \
  --dataset-name terminal-bench-core --dataset-version 0.1.1
```

## What the adapter does

1. `POST /chat {prompt, mode:auto, source:terminal_bench, round_budget}` to the live server
2. Executes each returned `actions[]` (CREATE/READ/EDIT/RUN/RUNTERM) **inside** the TB Docker container via `TmuxSession.container.exec_run`
3. `POST /chat/continue` with `action_results` until `done` or budget

## HEADLESS_SYSTEM_PROMPT fix

`headless_runner.py` now teaches all 6 directives (was 2). Required before any `HeadlessRunner` path can score honestly.

## Known gap vs Option B

`HeadlessRunner` direct path would avoid the HTTP hop and be trivial to containerize, but needs the same prompt fix (done) plus execution isolation. Keep as fallback if `:8080` is unreachable in CI.
