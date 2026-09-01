#!/usr/bin/env python3
"""Independent master-ai-cli scheduler daemon.

Runs outside Hermes. Stores schedules in ~/.master_ai_schedules.json and
executes master-ai slash commands on time/cadence. Logs to
~/.master_ai_scheduler.log.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread

SCHEDULES_FILE = Path.home() / ".master_ai_schedules.json"
LOG_FILE = Path.home() / ".master_ai_scheduler.log"
SHUTDOWN = Event()


def log(msg: str):
    ts = datetime.now().isoformat()
    line = f"{ts} {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_schedules():
    if not SCHEDULES_FILE.exists():
        return []
    try:
        data = json.loads(SCHEDULES_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"load_schedules error: {e}")
        return []


def save_schedules(schedules):
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=2))


def _next_time(when: str, cadence: str, now: datetime = None):
    """Next occurrence strictly after `now`.

    2026-09-01: the fire decision used to call this with `now` as the
    reference point and then check `now >= result` -- but this function by
    construction always returns something strictly after whatever
    reference it's given, so that comparison could never be true. The
    trigger was structurally unreachable; caught via an actual end-to-end
    test (job never fired), not just code inspection. The fix isn't a new
    function -- it's calling this with the right reference point: the
    schedule's last_run (or creation time if it's never fired), not `now`.
    Once `now` catches up to that boundary, it fires; see _scheduler_loop.
    """
    now = now or datetime.now()
    m = re.match(r"(\d{1,2}):(\d{2})", when)
    hour = int(m.group(1)) if m else 0
    minute = int(m.group(2)) if m else 0
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        cad = (cadence or "daily").lower()
        if cad == "hourly":
            nxt += timedelta(hours=1)
        elif cad == "weekly":
            nxt += timedelta(days=7)
        elif cad == "monthly":
            # naive month step
            month = nxt.month + 1
            year = nxt.year
            if month > 12:
                month = 1
                year += 1
            nxt = nxt.replace(year=year, month=month)
        else:
            nxt += timedelta(days=1)
    return nxt


def _run_command(command: str):
    """Execute a scheduled command directly through the shell.

    2026-09-01: this used to shell out to `master_ai.py --run <cmd>`, a flag
    that never existed (main() has no argv handling for --run). Two days
    after this daemon was built, headless mode was reworked to --task/
    --headless (headless_runner.py, commit 11ebf5d) and this call site was
    never updated to match -- so scheduled jobs likely never actually ran
    even before the feature was removed as "unused" a week later.
    headless_runner's model-reply path is also a placeholder stub with no
    real LLM wired in, so it can't dispatch master-ai's internal TUI
    commands (doctor, etc.) either way. Running the scheduled string as a
    real shell command matches how Hermes's own cron (~/.hermes/cron/
    jobs.json) executes scripted jobs and is the only path that's actually
    functional today. Strip a leading slash for compatibility with schedules
    saved in the old "/rag rebuild"-style format.
    """
    cmd = command.lstrip("/")
    proc = subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        log(f"command failed ({proc.returncode}): {cmd}\n{proc.stderr[:500]}")
    else:
        out = proc.stdout.strip()
        log(f"ran: {cmd} -> {out[:200]}")


def _scheduler_loop():
    log("scheduler started")
    while not SHUTDOWN.is_set() and not _stop_file().exists():
        schedules = load_schedules()
        now = datetime.now()
        for s in schedules:
            if not s.get("enabled", True):
                continue
            last = s.get("last_run")
            anchor = datetime.fromisoformat(last) if last else datetime.fromisoformat(
                s.get("created") or now.isoformat()
            )
            boundary = _next_time(s.get("when", "00:00"), s.get("cadence", "daily"), anchor)
            if now >= boundary:
                log(f"triggering schedule {s.get('id')}: {s.get('command')}")
                Thread(target=_run_command, args=(s["command"],), daemon=True).start()
                s["last_run"] = now.isoformat()
        save_schedules(schedules)
        SHUTDOWN.wait(30)
    log("scheduler stopped")


def _pid_file():
    return Path.home() / ".master_ai_scheduler.pid"

def _stop_file():
    return Path.home() / ".master_ai_scheduler.stop"

def start_daemon():
    if SHUTDOWN.is_set():
        return None
    pid_path = _pid_file()
    stop_path = _stop_file()
    stop_path.unlink(missing_ok=True)
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)
            log(f"scheduler already running (pid {old_pid})")
            return None
        except (ProcessLookupError, ValueError, OSError):
            pid_path.unlink(missing_ok=True)
    t = Thread(target=_scheduler_loop, daemon=True)
    t.start()
    pid_path.write_text(str(os.getpid()))
    log(f"scheduler daemon started (pid {os.getpid()})")
    return t

def stop_daemon():
    SHUTDOWN.set()
    _stop_file().touch()
    pid_path = _pid_file()
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 9)
        except Exception as e:
            log(f"stop_daemon kill error: {e}")
        pid_path.unlink(missing_ok=True)
    return True

def daemon_status():
    pid_path = _pid_file()
    if not pid_path.exists():
        return "stopped"
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return f"running (pid {pid})"
    except Exception:
        pid_path.unlink(missing_ok=True)
        return "stopped (stale pid)"


def add_schedule(command: str, when: str = "00:00", cadence: str = "daily", sid: str = None):
    schedules = load_schedules()
    sid = sid or f"sched_{int(time.time())}"
    schedules.append({
        "id": sid,
        "command": command,
        "when": when,
        "cadence": cadence,
        "enabled": True,
        "created": datetime.now().isoformat(),
    })
    save_schedules(schedules)
    return sid


def remove_schedule(sid: str):
    schedules = load_schedules()
    before = len(schedules)
    schedules = [s for s in schedules if s.get("id") != sid]
    save_schedules(schedules)
    return before - len(schedules)


def list_schedules():
    return load_schedules()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "add", "remove", "list", "stop"])
    ap.add_argument("--command")
    ap.add_argument("--when", default="00:00")
    ap.add_argument("--cadence", default="daily", choices=["hourly", "daily", "weekly", "monthly"])
    ap.add_argument("--id")
    args = ap.parse_args()

    if args.action == "start":
        start_daemon()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            SHUTDOWN.set()
    elif args.action == "add":
        if not args.command:
            print("--command required")
            sys.exit(1)
        sid = add_schedule(args.command, args.when, args.cadence, args.id)
        print(sid)
    elif args.action == "remove":
        removed = remove_schedule(args.id)
        print(f"removed {removed} schedule(s)")
    elif args.action == "list":
        for s in list_schedules():
            print(f"{s['id']}  {s.get('when','--')} {s.get('cadence','daily'):8}  {s['command']}  enabled={s.get('enabled',True)}")
    elif args.action == "stop":
        stop_daemon()
        print("scheduler stopped")
