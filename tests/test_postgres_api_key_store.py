"""`baas.auth.store.PostgresApiKeyStore` -- against a real local Postgres test
database. Skipped automatically when `BAAS_TEST_DATABASE_URL` isn't set or
isn't reachable, so `pytest tests/` requires no Postgres by default (same
idiom as `tests/test_seam_e2e.py`'s compose-reachability skip). Point this at
a scratch database that already has `alembic upgrade head` applied, e.g.:
  BAAS_TEST_DATABASE_URL=postgresql://baas:baas@localhost:5432/baas_test
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from baas.auth.store import PostgresApiKeyStore

_DATABASE_URL = os.environ.get("BAAS_TEST_DATABASE_URL")


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="set BAAS_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresApiKeyStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM api_keys WHERE tenant LIKE 'pgtest-%'")
    await s.close()


def _tenant() -> str:
    return f"pgtest-{uuid.uuid4().hex[:8]}"


async def test_create_then_resolve_round_trips(store: PostgresApiKeyStore) -> None:
    tenant = _tenant()
    record, plaintext = await store.create(tenant, "prod-crawler")
    resolved = await store.resolve(plaintext)
    assert resolved is not None
    assert resolved.tenant == tenant
    assert resolved.key_id == record.key_id


async def test_resolve_bumps_last_used_at(store: PostgresApiKeyStore) -> None:
    _record, plaintext = await store.create(_tenant(), "prod-crawler")
    first = await store.resolve(plaintext)
    assert first is not None
    assert first.last_used_at is not None


async def test_resolve_rejects_unknown_plaintext(store: PostgresApiKeyStore) -> None:
    assert await store.resolve("bk_live_not-a-real-key") is None


async def test_revoked_key_no_longer_resolves(store: PostgresApiKeyStore) -> None:
    record, plaintext = await store.create(_tenant(), "prod-crawler")
    await store.revoke(record.key_id)
    assert await store.resolve(plaintext) is None


async def test_list_is_scoped_to_tenant(store: PostgresApiKeyStore) -> None:
    tenant = _tenant()
    await store.create(tenant, "key-a")
    await store.create(tenant, "key-b")
    await store.create(_tenant(), "key-c")
    listed = await store.list(tenant)
    assert {r.name for r in listed} == {"key-a", "key-b"}


async def test_revoke_unknown_key_id_is_a_no_op(store: PostgresApiKeyStore) -> None:
    await store.revoke("no-such-key-id")  # must not raise


async def test_store_survives_a_fresh_instance_same_database() -> None:
    assert _DATABASE_URL is not None
    tenant = _tenant()
    first = await PostgresApiKeyStore.connect(_DATABASE_URL)
    _record, plaintext = await first.create(tenant, "prod-crawler")
    await first.close()
    # A brand-new store object (simulating a process restart) backed by the
    # *same* Postgres must still resolve the key -- this is the entire
    # reason keys live in Postgres rather than an in-memory dict.
    second = await PostgresApiKeyStore.connect(_DATABASE_URL)
    try:
        resolved = await second.resolve(plaintext)
        assert resolved is not None
        assert resolved.tenant == tenant
    finally:
        async with second._pool.connection() as conn:
            await conn.execute("DELETE FROM api_keys WHERE tenant = %s", (tenant,))
        await second.close()
