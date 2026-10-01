"""
Example showing how a Sensei tool script should use the context-preserving runner.
Replace ad-hoc `asyncio.run` + ThreadPoolExecutor patterns with this helper.
"""
from __future__ import annotations

import asyncio
from scripts.async_utils import run_async_preserving_context


async def _fetch_secrets_and_do_work() -> str:
    # This coroutine runs inside the side thread but *with the caller's contextvars*.
    # Calls to get_secret(), profile-scoped config, etc. will see the correct scope.
    from sensei.secrets import get_secret  # hypothetical import
    token = get_secret("API_TOKEN")  # profile-scoped secret lookup
    url = get_secret("API_URL")
    # ... do async work ...
    return f"work done with {token[:4]}..."


def sync_entry_point() -> str:
    """
    Synchronous entry point called from the agent loop (which has a running event loop).
    Uses the helper to preserve profile scope across the thread hop.
    """
    return run_async_preserving_context(_fetch_secrets_and_do_work(), timeout=30)


# Legacy pattern to REPLACE:
#
# def _run_async_legacy(coro):
#     try:
#         loop = asyncio.get_running_loop()
#     except RuntimeError:
#         loop = None
#     if loop and loop.is_running():
#         import concurrent.futures
#         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
#             return pool.submit(asyncio.run, coro).result()  # ❌ loses contextvars
#     return asyncio.run(coro)
