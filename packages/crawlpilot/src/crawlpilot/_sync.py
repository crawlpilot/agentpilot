"""Running an async client synchronously, once.

`Crawlpilot` is `AsyncCrawlpilot` with the `await` taken off, and a remote client
stands in exactly the same relation to its own async half. That wrapper is the
trickiest code in either package -- a private event loop on a worker thread, a
`run_coroutine_threadsafe` hop per call, and a shutdown that has to stop the loop
from the *outside* -- and it is the kind of code that is subtly wrong in the
second copy rather than obviously wrong.

So there is one copy. `LoopThread` owns the loop; `SyncProxy` forwards a live
object's coroutines onto it.

**Why a loop on a thread rather than `asyncio.run` per call.** The expensive
thing is the resource behind the client -- a browser process, or a pooled HTTP
connection. `asyncio.run` builds and tears down a loop per call, which drops
anything bound to it, so a second `scrape()` would relaunch the browser it just
closed.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any


class LoopThread:
    """A private event loop, running on a daemon thread for the client's life."""

    def __init__(self, name: str = "crawlpilot") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro: Any) -> Any:
        """Run a coroutine on the loop and block until it finishes."""

        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @property
    def closed(self) -> bool:
        return self._loop.is_closed()

    def close(self) -> None:
        """Stop the loop and join the thread. Idempotent.

        `call_soon_threadsafe` because `stop()` has to be requested from the
        thread that is *not* running the loop, and the join is bounded so a
        wedged task cannot hang interpreter shutdown.
        """

        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


class SyncProxy:
    """An async object with the `await` taken off.

    Every public coroutine of the wrapped object is forwarded to the loop by
    `__getattr__`, so this needs no per-method wrapper and cannot drift out of
    step with the class it wraps -- which matters more since the verb split:
    `SessionVerbs` has ~60 methods and gains more from `tools/catalog.py`, and
    hand-written forwarders would be a third place to keep in step.

    Non-callables (`session_id`, `tier`) pass through untouched.
    """

    def __init__(self, target: Any, call: Any) -> None:
        self._target = target
        self._call = call

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def sync(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            return self._call(result) if asyncio.iscoroutine(result) else result

        return sync


__all__ = ["LoopThread", "SyncProxy"]
