"""One event loop for the whole process, on a background thread.

WHY THIS EXISTS. The graph nodes are synchronous; MCP tools are async-only.
The obvious bridge is `asyncio.run(...)` at each boundary, and that is what
this code did first. It fails in a way that takes a while to see:

    asyncio.run() creates a loop, runs the coroutine, and CLOSES the loop.

Anything async that outlives the call - an MCP session, a stdio subprocess
transport, an httpx connection pool - was bound to a loop that no longer
exists. Tools fetched under one `asyncio.run` and invoked under the next raise
`RuntimeError: Event loop is closed`, from inside a tool call, where it looks
like the tool failed rather than like the bridge failed. In a pipeline that
replans around failed steps, that reads as a flaky tool.

So there is exactly one loop per process, it stays open, and every async call
goes through it. Resources created on it remain valid for every later call.

Using it from inside an already-running loop (FastAPI) is safe: the work is
submitted to the background loop and waited on, rather than nested.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def loop() -> asyncio.AbstractEventLoop:
    """The process's event loop, started on first use."""
    global _loop, _thread

    with _lock:
        if _loop is not None and not _loop.is_closed():
            return _loop

        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_run_forever, args=(_loop,), name="agentcore-aio", daemon=True
        )
        _thread.start()
        return _loop


def _run_forever(target: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(target)
    # An exception handler that stays quiet about teardown races. Anything a
    # caller needs to know arrives through `run()`'s future, not through here.
    target.set_exception_handler(lambda _loop, _context: None)
    target.run_forever()


def run(coro: Coroutine[Any, Any, T], *, timeout: float | None = 300) -> T:
    """Run a coroutine on the shared loop and return its result.

    `timeout` is a backstop, not a policy: a hung MCP server should surface as
    a failed step rather than a pipeline that never returns. A live demo that
    stops responding is worse than one that reports an error.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop())
    return future.result(timeout)


@atexit.register
def _shutdown() -> None:
    """Stop the loop on exit so the interpreter is not held open.

    Best effort, and deliberately silent. On Windows the Proactor loop owns
    overlapped I/O handles for any stdio subprocess an MCP server is running
    in, and tearing those down at interpreter exit races the subprocess: it
    prints `OSError: [WinError 6] The handle is invalid` from inside asyncio's
    own callback. The work is already finished and the process is leaving, so
    that traceback is noise - but it is alarming noise at the end of a demo,
    which is the worst possible moment for it.
    """
    global _loop

    if _loop is None or _loop.is_closed():
        return

    try:
        for task in asyncio.all_tasks(_loop):
            _loop.call_soon_threadsafe(task.cancel)
        _loop.call_soon_threadsafe(_loop.stop)
        if _thread is not None:
            _thread.join(timeout=5)
    except Exception:  # noqa: BLE001 - nothing useful can be done while exiting
        pass
    finally:
        _loop = None
