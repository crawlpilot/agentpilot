"""Unit tests for `agentpilot.agent.reliability` -- error taxonomy, the
retry helper (idempotent paths only), and the typed circuit breaker."""

from __future__ import annotations

import pytest

from agentpilot.agent.reliability import (
    CircuitBreaker,
    CircuitBreakerTripped,
    ErrorClass,
    FailureKind,
    RetryStrategy,
    classify_error,
)
from agentpilot.spi.errors import CapacityExhausted, NavigationTimeout, StaleRefError


def test_classify_error_taxonomy() -> None:
    assert classify_error(TimeoutError()) is ErrorClass.TIMEOUT
    assert classify_error(ValueError("bad action")) is ErrorClass.VALIDATION
    assert classify_error(StaleRefError("e1", epoch_superseded=False)) is ErrorClass.TRANSIENT
    assert classify_error(NavigationTimeout("nav")) is ErrorClass.TRANSIENT
    assert classify_error(CapacityExhausted()) is ErrorClass.PERMANENT


async def test_retry_retries_transient_then_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise StaleRefError("e1", epoch_superseded=False)
        return "ok"

    result = await RetryStrategy(max_retries=3, base_delay_s=0.0).execute(flaky)
    assert result == "ok"
    assert calls == 3


async def test_retry_does_not_retry_validation() -> None:
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("unknown action")

    with pytest.raises(ValueError):
        await RetryStrategy(max_retries=3, base_delay_s=0.0).execute(bad)
    assert calls == 1  # validation errors fail fast, no retries


async def test_retry_exhausts_and_reraises() -> None:
    async def always() -> None:
        raise NavigationTimeout("nope")

    with pytest.raises(NavigationTimeout):
        await RetryStrategy(max_retries=2, base_delay_s=0.0).execute(always)


def test_circuit_breaker_typed_counters_and_trip() -> None:
    cb = CircuitBreaker(llm_threshold=3, validation_threshold=5, execution_threshold=2)
    assert cb.record_failure(FailureKind.LLM) == 1
    # A different kind has its own counter -- one LLM + one execution != 2 LLM.
    assert cb.record_failure(FailureKind.EXECUTION) == 1
    with pytest.raises(CircuitBreakerTripped) as exc:
        cb.record_failure(FailureKind.EXECUTION)  # execution threshold is 2
    assert exc.value.kind is FailureKind.EXECUTION
    assert exc.value.count == 2


def test_circuit_breaker_reset_clears_all() -> None:
    cb = CircuitBreaker(llm_threshold=3)
    cb.record_failure(FailureKind.LLM)
    cb.record_failure(FailureKind.LLM)
    cb.reset()
    # After reset the consecutive count restarts, so we don't trip at 3rd - 2.
    assert cb.record_failure(FailureKind.LLM) == 1
