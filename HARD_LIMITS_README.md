# Master AI — Hard Limits and Locked Rules

This file is the plain-English contract for what Sensei may do by itself, what
requires confirmation, and what always stops for a human handoff.

## Locked Rules

These rules are not tuning preferences. They are product safety limits.

1. Sensei never runs `sudo`.
2. Sensei never asks for, stores, pipes, echoes, or reuses a password.
3. Sensei never auto-confirms a root action.
4. Sensei may write a helper script in user space, but root execution of that
   script is still a human handoff.
5. Sensei must show the exact command before any sudo handoff.
6. You run sudo commands in a separate terminal.
7. After you return and press Enter or type `ok`, Sensei treats that as "the
   human completed the handoff" and may continue with non-sudo checks.
8. Any sudo command outside `SUDO_MAP.md` is a product bug until reviewed and
   documented.
9. Destructive non-sudo commands still pause for confirmation.
10. Auto mode means "flow through safe user-level work"; it does not bypass
    the sudo wall.

## Auto Mode and Sudo Handoffs

Yes, Sensei can work in Auto mode until it hits a sudo boundary.

The intended flow is:

1. Sensei runs normal user-level work automatically.
2. If root is needed, Sensei prints the exact command or script handoff.
3. Sensei pauses and waits.
4. You copy the command into a separate terminal and run it yourself.
5. You return to Sensei and press Enter or type `ok`.
6. Sensei continues with non-sudo verification, such as `systemctl show`,
   `curl`, `ss`, `cat /proc/cmdline`, or file checks.
7. If that sudo handoff was part of a pinned task and the chain reaches the end,
   Sensei may mark the pinned task done.

That is allowed because the password never enters Sensei and the root action is
still performed by you.

What is not allowed:

```bash
echo "$PASSWORD" | sudo -S ...
sudo -v
sudo bash script.sh
```

Those are not allowed inside Sensei. If a command starts with `sudo`, Sensei
must hand it to you.

## Sudo Script Pattern

For larger root changes, the safest pattern is:

1. Sensei creates or updates a normal file under `~/scripts/`.
2. Sensei shows the command:

```bash
sudo bash ~/scripts/name_of_script.sh
```

3. You run that command in another terminal.
4. Sensei runs a no-password verification command afterward.

The script itself should be short, idempotent, and explain what it changes.
If it edits system files, it should make a backup first.

## Reasoning Levels

Reasoning quality and execution permission are separate controls.

Execution modes:

- `mode plan` — think and plan only.
- `mode review` — ask before each user-level action.
- `mode auto` — run safe user-level work without repeated prompts.

Reasoning lanes:

- `think:` — local multi-pass reasoning loop.
- `think deep:` — deeper local reasoning loop.
- `tight:` — best available careful reasoning: DeepSeek-R1 when configured,
  local deep reasoning fallback otherwise.
- `deep:` — cloud/deep reasoning route when available.

Use `review` when you do not know the limits yet. Use `auto` only when the next
actions are obvious, reversible, and inside user space.

## Model Limits

The benchmark is a ceiling test, not a promise that every model can finish every
job.

- `qwen2.5:3b` is for quick answers and simple tasks.
- `qwen2.5:7b` or `master-ai` is the daily-driver work tier.
- `qwen2.5-coder:7b` is preferred for code and shell tasks when available.
- `qwen2.5:14b` is useful for harder local reasoning only on machines with
  enough RAM.
- Cloud deep reasoning can help with difficult judgment, but cloud cannot touch
  your filesystem or perform local verification.

If a task can change files, services, firewall rules, boot flags, packages, or
system state, start in `mode review`.

## Current Sudo Allowlist

The only documented sudo workflows live in `SUDO_MAP.md`:

- S01: cap Ollama to one loaded model.
- S02: enable user lingering.
- S03: open known Master AI LAN ports when `ufw` is active.
- S04: install/tune earlyoom and swappiness.
- S05: apply Intel i915 graphics safety boot flags.

Everything else should be handled as a new review item before it becomes a
supported workflow.
