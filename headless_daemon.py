#!/usr/bin/env python3
"""Headless job-queue daemon for master-ai-cli (ROADMAP Phase 3.5).

Accepts headless jobs asynchronously over a minimal stdlib HTTP API,
returns a job ID immediately, runs each job as one
`headless_runner.py --headless --task-file <file> --json` subprocess,
tracks pending/running/done/failed, and (optionally) POSTs the result to
a caller-supplied callback_url when the job finishes.

Deliberately independent of headless_runner's model wiring: the daemon
only spawns/tracks/reports the subprocess — whatever the runner answers
is captured and reported verbatim.

House style follows master_ai_scheduler.py (restored 2026-09-01):
plain stdlib, argparse CLI with subcommands, flat JSON state in $HOME,
pid/stop files, clear docstrings, log() helper. The HTTP layer follows
stt_server.py's ThreadingHTTPServer pattern (stdlib http.server; that
service itself is untouched and unrelated).

Storage / files:
  ~/.master_ai_jobs.json                  job table (state + results)
  ~/.master_ai_headless_daemon.pid        pid file (start/stop/status)
  ~/.master_ai_headless_daemon.stop       cooperative stop file (reserved)
  ~/.master_ai_logs/headless_daemon.log   daemon log
  ~/.master_ai_logs/job_<id>.log          per-job lifecycle log (id, preview,
                                          start/end, exit code — the minimum
                                          required entry, plus extras)
  ~/.master_ai_logs/job_<id>.task/.out/.err  task text / stdout / stderr

HTTP API (default 127.0.0.1:8793, override with --port or
HEADLESS_DAEMON_PORT):
  POST /jobs        {"task": "..."} | {"task_file": "..."} | {"callback_url": ...}
                    -> 202 {"job_id": "...", "status": "pending"}
  GET  /jobs        -> list of job summaries (newest first)
  GET  /jobs/<id>   -> full job record (result included when done)
  GET  /health      -> {"status": "ok", "pid": N} liveness probe
  POST /shutdown    -> graceful stop (used by the CLI `stop` action)

CLI (start/stop pattern matches master_ai_scheduler.py):
  python3 headless_daemon.py start [--port 8793]   # foreground
  python3 headless_daemon.py stop
  python3 headless_daemon.py status
  python3 headless_daemon.py submit "say hello" [--callback-url URL]
  python3 headless_daemon.py submit --task-file f.txt [--callback-url URL]
  python3 headless_daemon.py job <id>
  python3 headless_daemon.py list
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event

# ─── paths & constants ──────────────────────────────────────────────

JOBS_FILE = Path.home() / ".master_ai_jobs.json"
LOGS_DIR = Path.home() / ".master_ai_logs"
DAEMON_LOG = LOGS_DIR / "headless_daemon.log"
PID_FILE = Path.home() / ".master_ai_headless_daemon.pid"
STOP_FILE = Path.home() / ".master_ai_headless_daemon.stop"
RUNNER = Path(__file__).resolve().parent / "headless_runner.py"

DEFAULT_PORT = 8793
JOB_TIMEOUT_S = 900          # hard cap per job subprocess (15 min)
MAX_CONCURRENT_JOBS = 2
MAX_BODY_BYTES = 1_000_000
SHUTDOWN = Event()

VALID_TRANSITIONS = {
    "pending": {"running", "failed"},
    "running": {"done", "failed"},
    "done": set(),
    "failed": set(),
}


def log(msg: str) -> None:
    ts = datetime.now().isoformat()
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(DAEMON_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── job store ──────────────────────────────────────────────────────

_LOCK = threading.Lock()


def load_jobs() -> dict:
    if not JOBS_FILE.exists():
        return {"version": 1, "jobs": {}}
    try:
        data = json.loads(JOBS_FILE.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            return {"version": 1, "jobs": {}}
        return data
    except Exception as e:
        log(f"JOBS_LOAD_ERROR: {e}")
        return {"version": 1, "jobs": {}}


def save_jobs(jobs: dict) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(JOBS_FILE)


def get_job(job_id: str):
    return load_jobs()["jobs"].get(job_id)


def _job_log(job_id: str, msg: str) -> None:
    """Per-job lifecycle log — the minimum logging requirement (id,
    task preview, start/end times, exit code) lives here."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / f"job_{job_id}.log", "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass


def update_job(job_id: str, new_status: str = None, **fields) -> dict:
    """Update a job record atomically. Status changes are
    transition-checked (pending→running→done|failed) and timestamped."""
    with _LOCK:
        jobs = load_jobs()
        rec = jobs["jobs"].get(job_id)
        if rec is None:
            return {}
        if new_status is not None:
            old = rec.get("status")
            if new_status not in VALID_TRANSITIONS.get(old, set()):
                raise ValueError(
                    f"illegal status transition {old} -> {new_status} for {job_id}")
            rec["status"] = new_status
            stamp = {"running": "started_at", "done": "finished_at",
                     "failed": "finished_at"}.get(new_status)
            if stamp:
                rec[stamp] = datetime.now().isoformat()
            log(f"JOB {job_id}: {old} -> {new_status}")
        rec.update(fields)
        rec["updated"] = datetime.now().isoformat()
        save_jobs(jobs)
        return rec


# ─── job execution ──────────────────────────────────────────────────

def _write_task_file(job_id: str, task_text: str) -> Path:
    """Persist the task text; doubles as the runner's --task-file and the
    audit copy."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    p = LOGS_DIR / f"job_{job_id}.task"
    p.write_text(task_text)
    os.chmod(p, 0o600)
    return p


def _post_callback(url: str, payload: dict) -> None:
    """Best-effort webhook; failures are logged, never raised."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
        log(f"CALLBACK {url} -> HTTP {code}")
        _post_callback.last = code  # type: ignore[attr-defined]
    except Exception as e:
        log(f"CALLBACK_ERROR {url}: {e}")
        _post_callback.last = -1  # type: ignore[attr-defined]


_post_callback.last = None


def _run_job(job_id: str, task_text: str, callback_url: str = "") -> None:
    """Worker body: spawn the headless_runner subprocess, capture
    stdout/stderr/exit code, transition status, fire webhook."""
    task_path = _write_task_file(job_id, task_text)
    _job_log(job_id, f"start task={task_text[:120]!r}")

    try:
        update_job(job_id, new_status="running")
    except Exception as e:
        log(f"JOB {job_id}: could not mark running: {e}")
        return

    cmd = [sys.executable, str(RUNNER),
           "--headless", "--task-file", str(task_path), "--json"]
    started = time.monotonic()
    stdout, stderr, code = "", "", -1
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=JOB_TIMEOUT_S)
        stdout = (proc.stdout or "")[-200_000:]
        stderr = (proc.stderr or "")[-50_000:]
        code = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = ((e.stdout or b"").decode(errors="replace")
                  if isinstance(e.stdout, bytes) else (e.stdout or ""))[-200_000:]
        stderr = (((e.stderr or b"").decode(errors="replace")
                   if isinstance(e.stderr, bytes) else (e.stderr or ""))
                  + f"\n[job timeout after {JOB_TIMEOUT_S}s]")[-2000:]
        code = 124
        _job_log(job_id, f"timeout after {JOB_TIMEOUT_S}s")
    except Exception as e:
        try:
            update_job(job_id, new_status="failed", error=f"spawn failed: {e}",
                       exit_code=-1)
        except Exception:
            pass
        log(f"JOB {job_id}: spawn failed: {e}")
        _job_log(job_id, f"spawn-failed: {e}")
        return

    duration = round(time.monotonic() - started, 2)
    ok = (code == 0)

    # raw stream capture for full-fidelity reads later
    try:
        (LOGS_DIR / f"job_{job_id}.out").write_text(stdout)
        (LOGS_DIR / f"job_{job_id}.err").write_text(stderr)
    except OSError:
        pass

    parsed = None
    try:
        parsed = json.loads(stdout)
    except Exception:
        parsed = None

    fields = {
        "exit_code": code,
        "duration_s": duration,
        "result": {"stdout": stdout, "stderr": stderr, "exit_code": code},
        "status": "done" if ok else "failed",
    }
    if not ok:
        fields["error"] = (stderr.strip() or f"exit code {code}")[:500]
    try:
        update_job(job_id, new_status=fields.pop("status"), **fields)
    except Exception as e:
        log(f"JOB {job_id}: status update failed: {e}")

    _job_log(job_id, f"end exit={code} duration={duration}s out_chars={len(stdout)}")
    if ok and parsed is not None:
        _job_log(job_id, f"result_preview={json.dumps(parsed)[:200]}")

    if callback_url:
        payload = {
            "job_id": job_id,
            "status": "done" if ok else "failed",
            "exit_code": code,
            "duration_s": duration,
            "result": parsed if (ok and parsed is not None) else stdout[-4000:],
            "stderr": stderr[-2000:],
        }
        threading.Thread(target=_post_callback, args=(callback_url, payload),
                         daemon=True).start()


def submit_job(task: str = "", callback_url: str = "") -> dict:
    """Create a pending job record and queue it. Returns immediately —
    the caller never blocks on the task itself."""
    task = (task or "").strip()
    if not task:
        raise ValueError("task text is empty")
    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    rec = {
        "job_id": job_id,
        "status": "pending",
        "task_preview": task[:200],
        "task_chars": len(task),
        "callback_url": callback_url or "",
        "callback_status": None,
        "submitted_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "duration_s": None,
        "result": None,
        "error": None,
        "updated": datetime.now().isoformat(),
    }
    with _LOCK:
        jobs = load_jobs()
        jobs["jobs"][job_id] = rec
        save_jobs(jobs)
    log(f"JOB {job_id}: submitted ({len(task)} chars)"
        + (f" callback={callback_url}" if callback_url else ""))
    _job_log(job_id, f"submitted task={task[:120]!r}")
    _EXECUTOR.submit(_run_job, job_id, task, callback_url)
    return rec


_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                               thread_name_prefix="headless-job")


def reconcile_stale_jobs() -> int:
    """Startup reconciliation: jobs found in pending/running at boot can't
    be alive — their worker threads died with the previous daemon process.
    Mark them failed (error: interrupted by daemon restart). Found live
    2026-09-01: the daemon was killed mid-run during parallel-session
    testing and two jobs stayed 'running' forever. Returns count fixed."""
    fixed = 0
    with _LOCK:
        jobs = load_jobs()
        changed = False
        for jid, rec in jobs["jobs"].items():
            if rec.get("status") in ("pending", "running"):
                rec["status"] = "failed"
                rec["error"] = "interrupted: daemon stopped or restarted mid-job"
                rec["exit_code"] = -1
                rec["finished_at"] = datetime.now().isoformat()
                rec["updated"] = rec["finished_at"]
                _job_log(jid, "startup-reconciliation: marked failed (daemon was not running)")
                log(f"JOB {jid}: stale '{rec.get('status')}' -> failed (reconciliation)")
                fixed += 1
                changed = True
        if changed:
            save_jobs(jobs)
    return fixed


# ─── HTTP API (stt_server.py ThreadingHTTPServer pattern) ───────────

class JobHTTPHandler(BaseHTTPRequestHandler):
    server_version = "SenseiHeadlessDaemon/1.0"

    def log_message(self, fmt, *args):  # quiet: we do our own logging
        pass

    # ── helpers ──
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # ── routes ──
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._send(200, {"status": "ok", "pid": os.getpid()})
            return
        if path == "/jobs":
            jobs = load_jobs()["jobs"]
            summaries = sorted(jobs.values(),
                               key=lambda r: r.get("submitted_at", ""),
                               reverse=True)
            self._send(200, {"jobs": [
                {k: r.get(k) for k in ("job_id", "status", "task_preview",
                                       "exit_code", "submitted_at",
                                       "finished_at")}
                for r in summaries]})
            return
        if path.startswith("/jobs/"):
            job_id = path[len("/jobs/"):].strip("/")
            rec = get_job(job_id)
            if rec is None:
                self._send(404, {"error": f"unknown job: {job_id}"})
            else:
                self._send(200, rec)
            return
        self._send(404, {"error": f"no route: {path}"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/shutdown":
            self._send(200, {"status": "shutting_down"})
            threading.Thread(target=_graceful_shutdown, daemon=True).start()
            return
        if path != "/jobs":
            self._send(404, {"error": f"no route: {path}"})
            return
        data = self._read_body_json()
        if data is None:
            self._send(400, {"error": "request body must be a JSON object"})
            return
        task = str(data.get("task") or "").strip()
        task_file = str(data.get("task_file") or "").strip()
        callback_url = str(data.get("callback_url") or "").strip()
        if not task and not task_file:
            self._send(400, {"error": "job needs 'task' text or 'task_file'"})
            return
        if task_file:
            p = Path(task_file).expanduser()
            if not p.exists():
                self._send(400, {"error": f"task_file not found: {task_file}"})
                return
            try:
                task = p.read_text(errors="replace")
            except OSError as e:
                self._send(400, {"error": f"task_file unreadable: {e}"})
                return
        try:
            rec = submit_job(task=task, callback_url=callback_url)
        except Exception as e:
            self._send(500, {"error": f"submit failed: {e}"})
            return
        self._send(202, {"job_id": rec["job_id"], "status": rec["status"],
                         "callback_url": rec["callback_url"]})


_SERVER = None


def _graceful_shutdown() -> None:
    SHUTDOWN.set()
    try:
        if _SERVER is not None:
            _SERVER.shutdown()
    except Exception:
        pass


# ─── daemon lifecycle (scheduler house pattern) ─────────────────────

def start_daemon(port: int = DEFAULT_PORT):
    """Foreground serve loop. Returns when stopped."""
    pid_path = PID_FILE
    STOP_FILE.unlink(missing_ok=True)
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)
            log(f"daemon already running (pid {old_pid})")
            print(f"daemon already running (pid {old_pid})")
            return None
        except (ProcessLookupError, ValueError, OSError):
            pid_path.unlink(missing_ok=True)
    pid_path.write_text(str(os.getpid()))

    reconciled = reconcile_stale_jobs()
    if reconciled:
        log(f"reconciled {reconciled} stale job(s) at startup")

    global _SERVER
    port = int(os.environ.get("HEADLESS_DAEMON_PORT", port))
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), JobHTTPHandler)
        _SERVER = httpd
    except OSError as e:
        log(f"cannot bind 127.0.0.1:{port}: {e}")
        print(f"cannot bind 127.0.0.1:{port}: {e}", file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return None
    log(f"headless daemon started (pid {os.getpid()}, port {port})")
    print(f"headless daemon listening on 127.0.0.1:{port}")

    def _on_sigterm(_s, _f):
        threading.Thread(target=_graceful_shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        SHUTDOWN.set()
        _EXECUTOR.shutdown(wait=False, cancel_futures=True)
        try:
            httpd.server_close()
        except Exception:
            pass
        log("headless daemon stopped")
        pid_path.unlink(missing_ok=True)
    return True


def stop_daemon(port: int = DEFAULT_PORT) -> bool:
    pid_path = PID_FILE
    port = int(os.environ.get("HEADLESS_DAEMON_PORT", port))
    # 1) graceful: POST /shutdown, wait for pid file to clear
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        for _ in range(30):
            if not pid_path.exists():
                break
            time.sleep(0.2)
        if not pid_path.exists():
            print("daemon stopped (graceful)")
            return True
    except Exception:
        pass
    # 2) fallback: kill by pid file
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGKILL)
            print(f"daemon killed (pid {pid})")
        except ProcessLookupError:
            print("daemon already gone")
        except Exception as e:
            log(f"stop_daemon kill error: {e}")
        pid_path.unlink(missing_ok=True)
        return True
    print("daemon not running")
    return False


def daemon_status(port: int = DEFAULT_PORT) -> str:
    pid_path = PID_FILE
    if not pid_path.exists():
        return "stopped"
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
    except Exception:
        pid_path.unlink(missing_ok=True)
        return "stopped (stale pid)"
    try:
        port = int(os.environ.get("HEADLESS_DAEMON_PORT", port))
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            body = json.loads(resp.read().decode())
        return f"running (pid {pid}, health ok, jobs served)"
    except Exception as e:
        return f"running (pid {pid}) but /health failed: {e}"


# ─── CLI client helpers (test the API without writing a client) ─────

def _api(method: str, url: str, body: dict = None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"}
                                 if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}
    except Exception as e:
        return -1, {"error": str(e)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="headless_daemon",
        description="Sensei headless job-queue daemon (ROADMAP Phase 3.5)")
    ap.add_argument("action",
                    choices=["start", "stop", "status", "submit", "job", "list"])
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("HEADLESS_DAEMON_PORT", DEFAULT_PORT)))
    ap.add_argument("task", nargs="?", help="task text for `submit`")
    ap.add_argument("--task-file", help="read task text from a file (submit)")
    ap.add_argument("--callback-url", default="",
                    help="webhook URL to POST when the job finishes")
    ap.add_argument("--id", help="job id for the `job` action")
    args = ap.parse_args(argv)

    base = f"http://127.0.0.1:{args.port}"

    if args.action == "start":
        start_daemon(port=args.port)
        return 0

    if args.action == "stop":
        stop_daemon(port=args.port)
        return 0

    if args.action == "status":
        print(daemon_status(port=args.port))
        return 0

    if args.action == "submit":
        # goes through the HTTP API so the DAEMON runs the job
        task = args.task or ""
        if args.task_file:
            task = Path(args.task_file).expanduser().read_text(errors="replace")
        if not task.strip():
            print("error: provide a task string or --task-file", file=sys.stderr)
            return 1
        code, body = _api("POST", f"http://127.0.0.1:{args.port}/jobs",
                          {"task": task, "callback_url": args.callback_url})
        if code != 202:
            print(f"submit failed (HTTP {code}, daemon not running?): {body}",
                  file=sys.stderr)
            return 1
        print(json.dumps(body, indent=2))
        return 0

    if args.action == "job":
        if not args.id:
            print("usage: headless_daemon.py job <id>", file=sys.stderr)
            return 1
        code, body = _api("GET", f"http://127.0.0.1:{args.port}/jobs/{args.id}")
        if code != 200:
            print(f"query failed (HTTP {code}): {body}", file=sys.stderr)
            return 1
        print(json.dumps(body, indent=2))
        return 0

    if args.action == "list":
        code, body = _api("GET", f"http://127.0.0.1:{args.port}/jobs")
        if code != 200:
            print(f"daemon error (HTTP {code}): {body}", file=sys.stderr)
            return 1
        jobs = body.get("jobs", [])
        if not jobs:
            print("no jobs")
        for r in jobs:
            print(f"{r['job_id']:<26} {r['status']:<8} "
                  f"exit={r['exit_code']!s:<5} {r['task_preview'][:60]}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())