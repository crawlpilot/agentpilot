"""Reliability primitives for the agent loop: an error taxonomy, a
retry-with-backoff helper, and a typed circuit breaker. Ports the shape of
Browser4's `inference/detail/` (RetryStrategy, CircuitBreaker,
PerceptiveAgentError) onto crawlpilot's `spi.errors`.

Design rule: retries are only ever applied to *idempotent* operations -- page
observation (a read-only snapshot) and the LLM call. Action dispatch is never
auto-retried, because a half-applied batch of clicks/fills is not safely
repeatable; a failed dispatch is surfaced to the LLM and counted by the
circuit breaker instead.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from enum import Enum

from agentpilot.spi.errors import (
    CapacityExhausted,
    ContextCrashed,
    NavigationTimeout,
    StaleRefError,
    TabNotFound,
)


class ErrorClass(Enum):
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    PERMANENT = "permanent"


# Retried on the idempotent paths; VALIDATION/PERMANENT fail fast.
_RETRYABLE = frozenset({ErrorClass.TRANSIENT, ErrorClass.TIMEOUT})

_TRANSIENT_TYPES = (StaleRefError, NavigationTimeout)
_PERMANENT_TYPES = (CapacityExhausted, TabNotFound, ContextCrashed)


def classify_error(exc: BaseException) -> ErrorClass:
    """Map an exception to a coarse class. Used by both the retry helper
    (should we retry?) and the circuit breaker (which counter to bump)."""

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorClass.TIMEOUT
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        # `parse_agent_output` raises ValueError on an unknown action; a bad
        # JSON/schema decode raises these too -- an output-shape problem, not a
        # transient fault, so it must not be retried.
        return ErrorClass.VALIDATION
    if isinstance(exc, _TRANSIENT_TYPES):
        return ErrorClass.TRANSIENT
    if isinstance(exc, _PERMANENT_TYPES):
        return ErrorClass.PERMANENT
    # Network-ish errors from the LLM HTTP client (httpx) surface by name.
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ErrorClass.TIMEOUT
    if "connect" in name:
        return ErrorClass.TRANSIENT
    # Unknown: give it the benefit of the doubt as transient (one retry), which
    # is cheap and matches Browser4's default classification.
    return ErrorClass.TRANSIENT


class RetryStrategy:
    """Exponential backoff with multiplicative jitter for *idempotent* awaits.
    Mirrors Browser4's `RetryStrategy` (base 1s / cap 30s there); the defaults
    here are smaller because an agent step is interactive, not a batch fetch."""

    def __init__(
        self, *, max_retries: int = 2, base_delay_s: float = 0.5, max_delay_s: float = 8.0
    ) -> None:
        self._max_retries = max_retries
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s

    def should_retry(self, exc: BaseException) -> bool:
        return classify_error(exc) in _RETRYABLE

    def delay_for(self, attempt: int) -> float:
        # Multiplicative jitter keeps the delay monotonic in `attempt`.
        base = self._base_delay_s * (2.0**attempt)
        jittered = base * (1.0 + random.uniform(0.0, 0.3))
        return min(jittered, self._max_delay_s)

    async def execute[T](self, action: Callable[[], Awaitable[T]]) -> T:
        """Run `action`, retrying transient/timeout failures with backoff.
        `action` must be a fresh-awaitable factory (called once per attempt)."""

        attempt = 0
        while True:
            try:
                return await action()
            except Exception as exc:  # noqa: BLE001 -- reclassified below
                if not self.should_retry(exc) or attempt >= self._max_retries:
                    raise
                await asyncio.sleep(self.delay_for(attempt))
                attempt += 1


class FailureKind(Enum):
    LLM = "llm"
    VALIDATION = "validation"
    EXECUTION = "execution"


class CircuitBreakerTripped(Exception):
    """Raised by `CircuitBreaker.record_failure` when a per-kind threshold of
    *consecutive* failures is crossed -- the loop catches it and stops."""

    def __init__(self, kind: FailureKind, count: int, threshold: int) -> None:
        super().__init__(
            f"circuit breaker tripped: {count} consecutive {kind.value} failures "
            f"(threshold {threshold})"
        )
        self.kind = kind
        self.count = count
        self.threshold = threshold


class CircuitBreaker:
    """Typed consecutive-failure counters, one per `FailureKind`, each with its
    own threshold -- a generalization of the loop's old single
    `consecutive_failures` counter (mirrors Browser4's `CircuitBreaker`, whose
    defaults are LLM 5 / validation 8 / execution 3). `reset()` clears all
    counters on any successful step, preserving "consecutive" semantics."""

    def __init__(
        self, *, llm_threshold: int = 5, validation_threshold: int = 8, execution_threshold: int = 5
    ) -> None:
        self._thresholds = {
            FailureKind.LLM: llm_threshold,
            FailureKind.VALIDATION: validation_threshold,
            FailureKind.EXECUTION: execution_threshold,
        }
        self._counts = {kind: 0 for kind in FailureKind}

    def record_failure(self, kind: FailureKind) -> int:
        count = self._counts[kind] + 1
        self._counts[kind] = count
        threshold = self._thresholds[kind]
        if count >= threshold:
            raise CircuitBreakerTripped(kind, count, threshold)
        return count

    def reset(self) -> None:
        for kind in FailureKind:
            self._counts[kind] = 0

    def counts(self) -> dict[FailureKind, int]:
        return dict(self._counts)
