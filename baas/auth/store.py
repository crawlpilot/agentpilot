"""API-key storage for tenant-facing auth: Postgres-backed (persists across
restarts, shared across gateway/monolith processes) when `BAAS_DATABASE_URL`
is set, in-memory otherwise (dev/test only) -- the same dual-backend shape as
`baas.session.registry.RegistryProtocol`. Only a key's sha256 digest is ever
persisted (`keygen.hash_key`); the plaintext is returned once, at creation,
and never again.

Postgres, not Redis: this is "user management" data (tenant, key hash, name,
revocation), not the ephemeral distributed-lock/routing state `baas.session`
and `baas.identity.proxy_pinning` use Redis for -- see `alembic/versions/
0001_create_api_keys.py` for the schema this store reads and writes.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from baas.auth.keygen import generate_api_key, hash_key
from baas.auth.models import ApiKeyRecord


@runtime_checkable
class ApiKeyStoreProtocol(Protocol):
    async def create(self, tenant: str, name: str) -> tuple[ApiKeyRecord, str]: ...

    async def resolve(self, plaintext: str) -> ApiKeyRecord | None: ...

    async def list(self, tenant: str) -> list[ApiKeyRecord]: ...

    async def revoke(self, key_id: str) -> None: ...


class InMemoryApiKeyStore:
    """Dev/test-only fallback -- mirrors `session.registry.Registry`'s role as
    the no-Postgres-available path. Never appropriate for a real multi-process
    deployment: keys created here aren't visible to any other process."""

    def __init__(self) -> None:
        self._by_digest: dict[str, ApiKeyRecord] = {}
        self._digest_by_id: dict[str, str] = {}

    async def create(self, tenant: str, name: str) -> tuple[ApiKeyRecord, str]:
        plaintext, prefix, digest = generate_api_key()
        record = ApiKeyRecord(
            key_id=str(uuid.uuid4()),
            tenant=tenant,
            name=name,
            prefix=prefix,
            created_at=datetime.now(UTC),
        )
        self._by_digest[digest] = record
        self._digest_by_id[record.key_id] = digest
        return record, plaintext

    async def resolve(self, plaintext: str) -> ApiKeyRecord | None:
        digest = hash_key(plaintext)
        record = self._by_digest.get(digest)
        if record is None or record.revoked_at is not None:
            return None
        record = replace(record, last_used_at=datetime.now(UTC))
        self._by_digest[digest] = record
        return record

    async def list(self, tenant: str) -> list[ApiKeyRecord]:
        records = [r for r in self._by_digest.values() if r.tenant == tenant]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def revoke(self, key_id: str) -> None:
        digest = self._digest_by_id.get(key_id)
        if digest is None:
            return
        record = self._by_digest.get(digest)
        if record is not None:
            self._by_digest[digest] = replace(record, revoked_at=datetime.now(UTC))


class PostgresApiKeyStore:
    """Postgres-backed `ApiKeyStoreProtocol` implementation -- hand-written
    SQL against the `api_keys` table (`alembic/versions/0001_create_api_keys
    .py`), matching this codebase's no-ORM house style (the same idea as
    `baas.session.redis_registry`'s raw Lua scripts, just SQL instead of
    Lua). Only a key's sha256 digest is ever persisted, same contract as
    `InMemoryApiKeyStore`.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresApiKeyStore:
        """Async factory, not a plain constructor: unlike `redis.asyncio
        .Redis.from_url()` (lazy, non-blocking), a Postgres pool needs an
        explicit `await pool.open()` to warm its connections, which can only
        run inside a coroutine -- see `baas.gateway.wiring.Wiring
        ._connect_api_keys()`, itself awaited from `get_wiring()`."""

        pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        await pool.open(wait=True, timeout=10.0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def create(self, tenant: str, name: str) -> tuple[ApiKeyRecord, str]:
        plaintext, prefix, digest = generate_api_key()
        key_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO api_keys (key_id, tenant, name, prefix, digest, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (key_id, tenant, name, prefix, digest, created_at),
            )
        record = ApiKeyRecord(
            key_id=key_id, tenant=tenant, name=name, prefix=prefix, created_at=created_at
        )
        return record, plaintext

    async def resolve(self, plaintext: str) -> ApiKeyRecord | None:
        digest = hash_key(plaintext)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT key_id, tenant, name, prefix, created_at, last_used_at, revoked_at "
                    "FROM api_keys WHERE digest = %s",
                    (digest,),
                )
                row = await cur.fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            now = datetime.now(UTC)
            await conn.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE digest = %s", (now, digest)
            )
        return ApiKeyRecord(
            key_id=row["key_id"],
            tenant=row["tenant"],
            name=row["name"],
            prefix=row["prefix"],
            created_at=row["created_at"],
            last_used_at=now,
            revoked_at=None,
        )

    async def list(self, tenant: str) -> list[ApiKeyRecord]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT key_id, tenant, name, prefix, created_at, last_used_at, revoked_at "
                    "FROM api_keys WHERE tenant = %s ORDER BY created_at DESC",
                    (tenant,),
                )
                rows = await cur.fetchall()
        return [ApiKeyRecord(**row) for row in rows]

    async def revoke(self, key_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE api_keys SET revoked_at = %s WHERE key_id = %s AND revoked_at IS NULL",
                (datetime.now(UTC), key_id),
            )
