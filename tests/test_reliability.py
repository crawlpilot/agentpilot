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
from crawlpilot.spi.errors import CapacityExhausted, NavigationTimeout, StaleRefError


def test_classify_error_taxonomy() -> None:
    assert classify_error(TimeoutError()) is ErrorClass.TIMEOUT
    assert classify_error(ValueError("bad action")) is ErrorClass.VALIDATION
    assert classify_error(StaleRefError("e1")) is ErrorClass.TRANSIENT
    assert classify_error(NavigationTimeout("nav")) is ErrorClass.TRANSIENT
    assert classify_error(CapacityExhausted()) is ErrorClass.PERMANENT


async def test_retry_retries_transient_then_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise StaleRefError("e1")
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


def test_node_capacity_is_transient_while_the_base_stays_permanent() -> None:
    """A refusal that says "retry shortly" must not be classified never-retry.

    `CapacityExhausted` is PERMANENT for its two original raisers, and rightly:
    `placer.py`'s "no worker node has capacity" and the driver's "session
    already has N tabs open" are not fixed by making the same call again. Node
    admission is the opposite -- it refuses because N browsers are open at this
    instant, and the ordinary reason the last one exists is a build that is about
    to finish and hand the slot back.

    Reusing the base class inverted that, and the cost was measured: three
    Walgreens onboards failed inside 0.1s of being claimed, `this node already
    holds 4 browser contexts (max 4); retry shortly` recorded as the terminal
    error on runs that never opened a browser.

    `NodeAtCapacity` is a subclass, so `isinstance` order decides this -- the
    transient check has to come first or it inherits exactly what it exists to
    escape.
    """

    from crawlpilot.spi.errors import CapacityExhausted, NodeAtCapacity

    assert classify_error(NodeAtCapacity("full")) is ErrorClass.TRANSIENT
    assert classify_error(CapacityExhausted("no node")) is ErrorClass.PERMANENT

    strategy = RetryStrategy()
    assert strategy.should_retry(NodeAtCapacity("full"))
    assert not strategy.should_retry(CapacityExhausted("no node"))


def test_node_capacity_is_indistinguishable_from_its_base_on_the_wire() -> None:
    """The split is for in-process routing only. Every client-visible thing --
    status, code, Retry-After -- stays identical, which is what makes
    subclassing the right tool rather than a new sibling error."""

    from crawlpilot.spi.errors import CapacityExhausted, NodeAtCapacity

    assert issubclass(NodeAtCapacity, CapacityExhausted)
    assert NodeAtCapacity.code == CapacityExhausted.code
    assert NodeAtCapacity.http_status == CapacityExhausted.http_status == 503
    assert NodeAtCapacity.retry_after_seconds == CapacityExhausted.retry_after_seconds == 5
