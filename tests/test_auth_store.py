"""`baas.auth.store` -- both backends against the shared `ApiKeyStoreProtocol`
shape. Redis-backed cases use `fakeredis`, same pattern as
`test_proxy_pinning.py`/`test_redis_registry.py`.
"""

from __future__ import annotations

import fakeredis
import pytest

from baas.auth.store import ApiKeyStoreProtocol, InMemoryApiKeyStore, RedisApiKeyStore


def _stores() -> list[ApiKeyStoreProtocol]:
    return [InMemoryApiKeyStore(), RedisApiKeyStore(fakeredis.aioredis.FakeRedis())]


@pytest.mark.parametrize("store", _stores())
async def test_create_then_resolve_round_trips(store: ApiKeyStoreProtocol) -> None:
    record, plaintext = await store.create("acme", "prod-crawler")
    resolved = await store.resolve(plaintext)
    assert resolved is not None
    assert resolved.tenant == "acme"
    assert resolved.name == "prod-crawler"
    assert resolved.key_id == record.key_id


@pytest.mark.parametrize("store", _stores())
async def test_resolve_bumps_last_used_at(store: ApiKeyStoreProtocol) -> None:
    _record, plaintext = await store.create("acme", "prod-crawler")
    first = await store.resolve(plaintext)
    assert first is not None
    assert first.last_used_at is not None


@pytest.mark.parametrize("store", _stores())
async def test_resolve_rejects_unknown_plaintext(store: ApiKeyStoreProtocol) -> None:
    assert await store.resolve("bk_live_not-a-real-key") is None


@pytest.mark.parametrize("store", _stores())
async def test_plaintext_is_never_stored_verbatim(store: ApiKeyStoreProtocol) -> None:
    """The whole point of hashing: a key's plaintext must not appear anywhere
    resolvable except the one-time return value from `create()`."""

    _record, plaintext = await store.create("acme", "prod-crawler")
    listed = await store.list("acme")
    assert all(plaintext not in str(vars(r)) for r in listed)


@pytest.mark.parametrize("store", _stores())
async def test_revoked_key_no_longer_resolves(store: ApiKeyStoreProtocol) -> None:
    record, plaintext = await store.create("acme", "prod-crawler")
    await store.revoke(record.key_id)
    assert await store.resolve(plaintext) is None


@pytest.mark.parametrize("store", _stores())
async def test_list_is_scoped_to_tenant(store: ApiKeyStoreProtocol) -> None:
    await store.create("acme", "key-a")
    await store.create("acme", "key-b")
    await store.create("globex", "key-c")
    acme_keys = await store.list("acme")
    assert {r.name for r in acme_keys} == {"key-a", "key-b"}


@pytest.mark.parametrize("store", _stores())
async def test_revoke_unknown_key_id_is_a_no_op(store: ApiKeyStoreProtocol) -> None:
    await store.revoke("no-such-key-id")  # must not raise


async def test_redis_store_survives_a_fresh_instance_same_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    _record, plaintext = await RedisApiKeyStore(redis).create("acme", "prod-crawler")
    # A brand-new store object (simulating a process restart) backed by the
    # *same* Redis must still resolve the key -- this is the entire reason
    # keys live in Redis rather than an in-memory dict in production.
    resolved = await RedisApiKeyStore(redis).resolve(plaintext)
    assert resolved is not None
    assert resolved.tenant == "acme"
