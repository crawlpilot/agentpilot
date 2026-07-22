"""`baas.auth.store.InMemoryApiKeyStore` -- the dev/test-only fallback path
(no Postgres required). See `tests/test_postgres_api_key_store.py` for the
same behavioral contract exercised against a real Postgres, including
cross-instance persistence, which `InMemoryApiKeyStore` deliberately can't
provide (see its docstring).
"""

from __future__ import annotations

from baas.auth.store import InMemoryApiKeyStore


async def test_create_then_resolve_round_trips() -> None:
    store = InMemoryApiKeyStore()
    record, plaintext = await store.create("acme", "prod-crawler")
    resolved = await store.resolve(plaintext)
    assert resolved is not None
    assert resolved.tenant == "acme"
    assert resolved.name == "prod-crawler"
    assert resolved.key_id == record.key_id


async def test_resolve_bumps_last_used_at() -> None:
    store = InMemoryApiKeyStore()
    _record, plaintext = await store.create("acme", "prod-crawler")
    first = await store.resolve(plaintext)
    assert first is not None
    assert first.last_used_at is not None


async def test_resolve_rejects_unknown_plaintext() -> None:
    store = InMemoryApiKeyStore()
    assert await store.resolve("bk_live_not-a-real-key") is None


async def test_plaintext_is_never_stored_verbatim() -> None:
    """The whole point of hashing: a key's plaintext must not appear anywhere
    resolvable except the one-time return value from `create()`."""

    store = InMemoryApiKeyStore()
    _record, plaintext = await store.create("acme", "prod-crawler")
    listed = await store.list("acme")
    assert all(plaintext not in str(vars(r)) for r in listed)


async def test_revoked_key_no_longer_resolves() -> None:
    store = InMemoryApiKeyStore()
    record, plaintext = await store.create("acme", "prod-crawler")
    await store.revoke(record.key_id)
    assert await store.resolve(plaintext) is None


async def test_list_is_scoped_to_tenant() -> None:
    store = InMemoryApiKeyStore()
    await store.create("acme", "key-a")
    await store.create("acme", "key-b")
    await store.create("globex", "key-c")
    acme_keys = await store.list("acme")
    assert {r.name for r in acme_keys} == {"key-a", "key-b"}


async def test_revoke_unknown_key_id_is_a_no_op() -> None:
    store = InMemoryApiKeyStore()
    await store.revoke("no-such-key-id")  # must not raise
