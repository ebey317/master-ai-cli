#!/usr/bin/env python3
"""NVIDIA NIM OpenAI-compatible proxy with per-key rate limiting and rotation.

Rate model:
  - one key  -> 40 requests / minute  -> 1 key every 1.5 s
  - two keys -> 80 requests / minute  -> alternate keys every 0.75 s

The proxy keeps a rolling request log per key and refuses to use a key until
it is below its per-minute budget. If both keys are hot, requests wait in a
FIFO queue. On upstream error (429/5xx/400) the request is retried with the
next key, and that key is given a short cooldown.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque

import aiohttp
from aiohttp import web

UPSTREAM = "https://integrate.api.nvidia.com/v1"
REQUESTS_PER_MINUTE_PER_KEY = 40
COOLDOWN_SECONDS = 60.0 / REQUESTS_PER_MINUTE_PER_KEY  # 1.5
ERROR_COOLDOWN_SECONDS = 5.0
TIMEOUT = aiohttp.ClientTimeout(total=300, connect=60)
RETRY_CODES = {400, 429, 500, 502, 503, 504}


def _load_keys():
    keys = []
    for name in ("NVIDIA_API_KEY_2", "NVIDIA_API_KEY_GPTOSS", "NVIDIA_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            keys.append(val)
    extra = os.environ.get("NVIDIA_API_KEYS", "")
    if extra:
        keys.extend(k.strip() for k in extra.split(",") if k.strip())
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    if not out:
        raise RuntimeError("No NVIDIA_API_KEY_* configured")
    return out


class KeyState:
    def __init__(self, key: str, idx: int):
        self.key = key
        self.idx = idx
        self.history = deque()  # timestamps of requests in the last 60s
        self.error_cooldown_until = 0.0
        self.lock = asyncio.Lock()

    def _prune(self, now: float):
        while self.history and self.history[0] < now - 60:
            self.history.popleft()

    def can_use(self, now: float) -> bool:
        self._prune(now)
        if now < self.error_cooldown_until:
            return False
        return len(self.history) < REQUESTS_PER_MINUTE_PER_KEY

    def record_use(self, now: float):
        self.history.append(now)

    def record_error(self, now: float):
        self.error_cooldown_until = now + ERROR_COOLDOWN_SECONDS


class ProxyServer:
    def __init__(self, port: int):
        self.port = port
        raw_keys = _load_keys()
        self.keys = [KeyState(k, i) for i, k in enumerate(raw_keys)]
        self.queue = asyncio.Queue()
        self.total_requests = 0
        self.total_errors = 0
        self.started_at = time.time()
        self.app = self._build_app()
        self._queue_task = None

    def _build_app(self):
        async def proxy(request: web.Request):
            path = request.path
            if request.query_string:
                path += "?" + request.query_string
            body = await request.read()
            headers = {k: v for k, v in request.headers.items()}
            headers.pop("host", None)
            headers.pop("Host", None)
            for h in list(headers):
                lower = h.lower()
                if lower.startswith("x-stainless") or lower in {
                    "accept-encoding",
                    "content-length",
                    "transfer-encoding",
                }:
                    headers.pop(h, None)

            loop = asyncio.get_event_loop()
            future = loop.create_future()
            await self.queue.put((request.method, path, headers, body, future))
            try:
                status, resp_body, resp_headers = await asyncio.wait_for(
                    future, timeout=600
                )
            except asyncio.TimeoutError:
                return web.Response(
                    status=504,
                    text='{"error":"proxy queue timeout"}',
                    content_type="application/json",
                )
            return web.Response(
                status=status,
                body=resp_body,
                headers={
                    k: v
                    for k, v in resp_headers.items()
                    if k.lower()
                    not in {"transfer-encoding", "content-encoding", "content-length"}
                },
            )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy)
        return app

    async def _worker(self):
        # The client side (proxy()) waits on this same future via
        # asyncio.wait_for(..., timeout=600); when that timeout fires it
        # cancels the future and the client already gets its 504. If this
        # worker is still mid-request past that point, its later
        # future.set_result() raises InvalidStateError -- and since the
        # except branch below used to retry set_result() on that same
        # already-done future unconditionally, the second call raised
        # again with nothing left to catch it, killing this coroutine's
        # while loop for good (self._queue_task is never resupervised).
        # Every request after that point queued forever and 504'd with a
        # timeout that looked like an upstream outage but was actually a
        # dead worker. Guard both paths with future.done() so a
        # late/duplicate result is just dropped, and keep the whole
        # per-item body inside try/except so no single bad request can
        # ever take the worker loop down again.
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            while True:
                method, path, headers, body, future = await self.queue.get()
                try:
                    try:
                        status, resp_body, resp_headers = await self._try_request(
                            session, method, path, headers, body
                        )
                        if not future.done():
                            future.set_result((status, resp_body, resp_headers))
                    except Exception as e:
                        logging.exception("worker error")
                        if not future.done():
                            future.set_result(
                                (
                                    502,
                                    json.dumps(
                                        {"error": f"proxy worker error: {e}"}
                                    ).encode(),
                                    {"content-type": "application/json"},
                                )
                            )
                finally:
                    self.queue.task_done()

    async def _try_request(self, session, method, path, headers, body):
        url = f"{UPSTREAM}{path}"
        last_status = 502
        last_body = b'{"error":"all keys failed"}'
        last_headers = {"content-type": "application/json"}

        for attempt in range(len(self.keys)):
            now = time.time()
            key_state = await self._pick_key(now)
            if key_state is None:
                # Wait until a key is available
                wait_until = self._next_available_at(now)
                delay = max(0.0, wait_until - now)
                logging.info("all keys at rpm limit, waiting %.2f s", delay)
                await asyncio.sleep(delay)
                now = time.time()
                key_state = await self._pick_key(now)
                if key_state is None:
                    # fallback: use least-loaded key anyway
                    key_state = min(self.keys, key=lambda k: len(k.history))

            req_headers = dict(headers)
            req_headers["Authorization"] = f"Bearer {key_state.key}"
            key_state.record_use(time.time())
            self.total_requests += 1
            try:
                async with session.request(
                    method=method, url=url, headers=req_headers, data=body
                ) as resp:
                    status = resp.status
                    resp_body = await resp.read()
                    resp_headers = dict(resp.headers)
                    if status in RETRY_CODES and attempt < len(self.keys) - 1:
                        logging.warning(
                            "key %d got %d, marking cooldown and retrying",
                            key_state.idx + 1,
                            status,
                        )
                        key_state.record_error(time.time())
                        continue
                    return status, resp_body, resp_headers
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.total_errors += 1
                key_state.record_error(time.time())
                if attempt < len(self.keys) - 1:
                    logging.warning(
                        "key %d failed (%s), retrying",
                        key_state.idx + 1,
                        type(e).__name__,
                    )
                    continue
                last_status = 502
                last_body = json.dumps({"error": f"proxy error: {e}"}).encode()
        return last_status, last_body, last_headers

    async def _pick_key(self, now: float):
        # Pick the key with the fewest recent uses among those that are usable now.
        candidates = [k for k in self.keys if k.can_use(now)]
        if not candidates:
            return None
        return min(candidates, key=lambda k: (len(k.history), k.idx))

    def _next_available_at(self, now: float) -> float:
        earliest = float("inf")
        for k in self.keys:
            k._prune(now)
            if len(k.history) >= REQUESTS_PER_MINUTE_PER_KEY:
                t = k.history[0] + 60
            else:
                t = now
            if k.error_cooldown_until > t:
                t = k.error_cooldown_until
            if t < earliest:
                earliest = t
        return earliest

    async def _stats_handler(self, request: web.Request):
        now = time.time()
        for k in self.keys:
            k._prune(now)
        data = {
            "uptime": now - self.started_at,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "queue_size": self.queue.qsize(),
            "keys": [
                {
                    "index": k.idx,
                    "prefix": k.key[:11],
                    "rpm_used_last_60s": len(k.history),
                    "error_cooldown_remaining": max(0.0, k.error_cooldown_until - now),
                }
                for k in self.keys
            ],
        }
        return web.json_response(data)

    async def run(self):
        self.app.router.add_get("/__proxy_stats", self._stats_handler)
        self._queue_task = asyncio.create_task(self._worker())
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        print(f"[nv-proxy] http://127.0.0.1:{self.port} -> {UPSTREAM}")
        print(
            f"[nv-proxy] keys: {len(self.keys)} ({REQUESTS_PER_MINUTE_PER_KEY} rpm/key)"
        )
        print(f"[nv-proxy] stats: http://127.0.0.1:{self.port}/__proxy_stats")
        while True:
            await asyncio.sleep(3600)


def main():
    port = int(os.environ.get("NVIDIA_PROXY_PORT", "8788"))
    logging.basicConfig(
        level=logging.INFO, format="[nv-proxy] %(levelname)s %(message)s"
    )
    try:
        asyncio.run(ProxyServer(port).run())
    except KeyboardInterrupt:
        print("[nv-proxy] shutting down")


if __name__ == "__main__":
    main()
