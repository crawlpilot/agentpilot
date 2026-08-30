"""Typed hook chains with the dispatch rules written down and enforced.

Two shapes, and the distinction is the whole design:

- **Filter-shaped** (`classify`, `resolve`, `will_navigate`, `will_parse`):
  handlers run in order and the **first non-`None` result wins**; `None` means
  "no opinion, defer". This is exactly the convention the pre-existing
  `block_detect._SITE_CHECKERS` chain already used, ported from Pulsar's
  `ChainedHtmlIntegrityChecker`, so porting those checkers is mechanical.
- **Notification-shaped** (`navigated`, `document_steady`, `extracted`): **all
  matching handlers run, in order**. `extracted` additionally chains its value,
  so each handler enriches what the previous produced.

Every call is individually isolated: an exception is logged with the offending
extension's name and treated as `None`. A broken extension degrades one site; it
must never kill a crawl. Same for a hook that overruns its deadline.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import structlog

from crawlpilot import metrics

log = structlog.get_logger(__name__)

DEFAULT_HOOK_TIMEOUT_S = 5.0
"""Per-handler deadline. Generous enough for a `resolve` that clicks through a
wall, short enough that a hung extension cannot stall a crawl indefinitely."""


@dataclass
class _Handler[T]:
    extension: str
    fn: Callable[..., Any]


@dataclass
class HookChain[T]:
    """An ordered chain of handlers for one hook."""

    name: str
    handlers: list[_Handler[T]] = field(default_factory=list)
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_S

    def add_last(self, fn: Callable[..., Any], *, extension: str = "?") -> None:
        self.handlers.append(_Handler(extension, fn))

    def add_first(self, fn: Callable[..., Any], *, extension: str = "?") -> None:
        self.handlers.insert(0, _Handler(extension, fn))

    def __len__(self) -> int:
        return len(self.handlers)

    # ------------------------------------------------------------ dispatch

    def _observe(self, handler: _Handler[T], outcome: str) -> None:
        metrics.incr(
            "extension_hook_calls_total",
            extension=handler.extension,
            hook=self.name,
            outcome=outcome,
        )

    def first_result(self, *args: Any, **kwargs: Any) -> T | None:
        """Filter-shaped, synchronous. First non-`None` wins."""

        for handler in self.handlers:
            try:
                result = handler.fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- isolation is the point
                self._observe(handler, "error")
                log.warning(
                    "extension.hook_failed",
                    extension=handler.extension,
                    hook=self.name,
                    error=str(exc),
                )
                continue
            if result is not None:
                self._observe(handler, "handled")
                return cast("T", result)
            self._observe(handler, "deferred")
        return None

    async def first_result_async(self, *args: Any, **kwargs: Any) -> T | None:
        """Filter-shaped, asynchronous, with a per-handler deadline."""

        for handler in self.handlers:
            try:
                result = await self._call(handler, *args, **kwargs)
            except TimeoutError:
                self._observe(handler, "timeout")
                log.warning(
                    "extension.hook_timeout",
                    extension=handler.extension,
                    hook=self.name,
                    timeout_s=self.timeout_s,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                self._observe(handler, "error")
                log.warning(
                    "extension.hook_failed",
                    extension=handler.extension,
                    hook=self.name,
                    error=str(exc),
                )
                continue
            if result is not None:
                self._observe(handler, "handled")
                return cast("T", result)
            self._observe(handler, "deferred")
        return None

    async def run_all(self, *args: Any, **kwargs: Any) -> None:
        """Notification-shaped: every handler runs, failures are isolated."""

        for handler in self.handlers:
            try:
                await self._call(handler, *args, **kwargs)
                self._observe(handler, "handled")
            except TimeoutError:
                self._observe(handler, "timeout")
                log.warning(
                    "extension.hook_timeout",
                    extension=handler.extension,
                    hook=self.name,
                    timeout_s=self.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                self._observe(handler, "error")
                log.warning(
                    "extension.hook_failed",
                    extension=handler.extension,
                    hook=self.name,
                    error=str(exc),
                )

    async def chain(self, value: T, *args: Any, **kwargs: Any) -> T:
        """Notification-shaped but value-carrying: each handler may return a
        replacement for `value`, and `None` means "leave it alone"."""

        for handler in self.handlers:
            try:
                result = await self._call(handler, value, *args, **kwargs)
            except TimeoutError:
                self._observe(handler, "timeout")
                continue
            except Exception as exc:  # noqa: BLE001
                self._observe(handler, "error")
                log.warning(
                    "extension.hook_failed",
                    extension=handler.extension,
                    hook=self.name,
                    error=str(exc),
                )
                continue
            self._observe(handler, "handled")
            if result is not None:
                value = cast("T", result)
        return value

    async def _call(self, handler: _Handler[T], *args: Any, **kwargs: Any) -> Any:
        result = handler.fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=self.timeout_s)
        return result
