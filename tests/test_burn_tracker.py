"""`agentpilot.identity.burn_tracker.BurnTracker` -- weighted per-identity
warning accounting against `fakeredis`, same pattern as `test_proxy_pinning`."""

from __future__ import annotations

import fakeredis
import pytest

from agentpilot.identity.burn_tracker import (
    MAX_WARNINGS,
    MINOR_WARNING_FACTOR,
    BurnTracker,
)
from agentpilot.spi.identity import IdentityKey

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="alice")


@pytest.fixture
def tracker() -> BurnTracker:
    return BurnTracker(fakeredis.aioredis.FakeRedis())


async def test_blocks_accumulate_by_weight(tracker: BurnTracker) -> None:
    assert await tracker.warnings(IDENTITY) == 0
    assert await tracker.record_block(IDENTITY, 2) == 2
    assert await tracker.record_block(IDENTITY, 3) == 5
    assert await tracker.is_burned(IDENTITY) is False


async def test_forbidden_weight_is_instant_burn(tracker: BurnTracker) -> None:
    await tracker.record_block(IDENTITY, MAX_WARNINGS)  # a hard FORBIDDEN
    assert await tracker.is_burned(IDENTITY) is True


async def test_success_decrements_and_floors_at_zero(tracker: BurnTracker) -> None:
    await tracker.record_block(IDENTITY, 2)
    assert await tracker.record_success(IDENTITY) == 1
    assert await tracker.record_success(IDENTITY) == 0
    # Further successes never drive it negative.
    assert await tracker.record_success(IDENTITY) == 0


async def test_zero_weight_block_is_a_noop(tracker: BurnTracker) -> None:
    assert await tracker.record_block(IDENTITY, 0) == 0
    assert await tracker.warnings(IDENTITY) == 0


async def test_minor_warnings_convert_to_one_real_warning(tracker: BurnTracker) -> None:
    # The first FACTOR-1 minor blocks don't touch the real counter...
    for _ in range(MINOR_WARNING_FACTOR - 1):
        assert await tracker.record_minor_block(IDENTITY) == 0
    # ...the FACTOR-th converts to exactly one real warning and resets the minor.
    assert await tracker.record_minor_block(IDENTITY) == 1
    # And the cycle restarts from zero minor warnings.
    assert await tracker.record_minor_block(IDENTITY) == 1
    assert await tracker.warnings(IDENTITY) == 1


async def test_reset_clears_the_counter(tracker: BurnTracker) -> None:
    await tracker.record_block(IDENTITY, MAX_WARNINGS)
    await tracker.record_minor_block(IDENTITY)
    assert await tracker.is_burned(IDENTITY) is True
    await tracker.reset(IDENTITY)
    assert await tracker.warnings(IDENTITY) == 0
    assert await tracker.is_burned(IDENTITY) is False
    # Minor counter is cleared too: a fresh cycle needs the full factor again.
    for _ in range(MINOR_WARNING_FACTOR - 1):
        assert await tracker.record_minor_block(IDENTITY) == 0


async def test_survives_a_fresh_tracker_instance_same_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await BurnTracker(redis).record_block(IDENTITY, 4)
    assert await BurnTracker(redis).warnings(IDENTITY) == 4
