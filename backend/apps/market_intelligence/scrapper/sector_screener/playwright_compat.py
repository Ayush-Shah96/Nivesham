from __future__ import annotations

"""Windows-safe runner for async collectors that use Playwright.

Uvicorn on Windows may run the application on SelectorEventLoop, which cannot
create subprocesses. Playwright's async transport starts its driver with
asyncio.create_subprocess_exec and therefore raises NotImplementedError when
called from that loop. Run the *whole source collector coroutine* on a dedicated
ProactorEventLoop in a worker thread instead of changing the process-wide event
loop policy (which can break Uvicorn reload/server subprocess handling).
"""

import asyncio
import logging
import os
import sys
from functools import partial
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _run_in_compatible_loop(
    factory: Callable[..., Coroutine[Any, Any, T]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> T:
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(factory(*args, **kwargs))
    finally:
        # Do not leave Playwright response-listener tasks or async generators
        # behind when a source page closes or raises during capture.
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            logger.debug("PLAYWRIGHT_LOOP_PENDING_TASK_CLEANUP_FAILED", exc_info=True)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            logger.debug("PLAYWRIGHT_LOOP_ASYNCGEN_CLEANUP_FAILED", exc_info=True)
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            logger.debug("PLAYWRIGHT_LOOP_EXECUTOR_CLEANUP_FAILED", exc_info=True)
        loop.close()
        asyncio.set_event_loop(None)


async def run_async_collector(
    factory: Callable[..., Coroutine[Any, Any, T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a source collector on a subprocess-capable loop on Windows.

    On Linux/macOS the caller's loop is used normally. On Windows, a dedicated
    Proactor loop in a thread is used. If the caller is cancelled while browser
    work is active, wait for the bounded collector to close its browser before
    propagating cancellation; this prevents orphan Chromium processes and
    overlapping retries against the same source.
    """
    if os.name != "nt":
        return await factory(*args, **kwargs)

    outer_loop = asyncio.get_running_loop()
    runner = partial(_run_in_compatible_loop, factory, args, kwargs)
    future = outer_loop.run_in_executor(None, runner)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # Do not wait indefinitely for a Playwright thread after the API timeout.
        # The source collector itself is bounded per submission, and its refresh
        # lease prevents a superseded collector from publishing stale data.
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("PLAYWRIGHT_COLLECTOR_CANCELLED_THREAD_STILL_RUNNING")
        except Exception:
            logger.debug("PLAYWRIGHT_COLLECTOR_FINISHED_WITH_ERROR_DURING_CANCEL", exc_info=True)
        raise
