"""API-key storage for tenant-facing auth: Redis-backed (persists across
restarts, shared across gateway/worker processes) when `BAAS_REDIS_URL` is
set, in-memory otherwise (dev/test only) -- the same dual-backend shape as
`baas.session.registry.RegistryProtocol`. Only a key's sha256 digest is ever
persisted (`keygen.hash_key`); the plaintext is returned once, at creation,
and never again.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

from baas.auth.keygen import generate_api_key, hash_key
from baas.auth.models import ApiKeyRecord

_KEY_PREFIX = "apikey:"
_TENANT_INDEX_PREFIX = "apikey_by_tenant:"
_ID_INDEX_PREFIX = "apikey_id:"


@runtime_checkable
class ApiKeyStoreProtocol(Protocol):
    async def create(self, tenant: str, name: str) -> tuple[ApiKeyRecord, str]: ...

    async def resolve(self, plaintext: str) -> ApiKeyRecord | None: ...

    async def list(self, tenant: str) -> list[ApiKeyRecord]: ...

    async def revoke(self, key_id: str) -> None: ...


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def _record_from_fields(
    key_id: str,
    tenant: str,
    name: str,
    prefix: str,
    created_at: str,
    last_used_at: str,
    revoked_at: str,
) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=key_id,
        tenant=tenant,
        name=name,
        prefix=prefix,
        created_at=datetime.fromtimestamp(float(created_at or 0), UTC),
        last_used_at=datetime.fromtimestamp(float(last_used_at), UTC) if last_used_at else None,
        revoked_at=datetime.fromtimestamp(float(revoked_at), UTC) if revoked_at else None,
    )


class InMemoryApiKeyStore:
    """Dev/test-only fallback -- mirrors `session.registry.Registry`'s role as
    the no-Redis-available path. Never appropriate for a real multi-process
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


class RedisApiKeyStore:
    """Redis layout: `apikey:{sha256hex}` hash of the record fields,
    `apikey_by_tenant:{tenant}` set of digests (for listing), and
    `apikey_id:{key_id}` -> digest (for revoke-by-id, since the hash is
    keyed by digest, not the caller-facing `key_id`)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, digest: str) -> str:
        return f"{_KEY_PREFIX}{digest}"

    def _tenant_index(self, tenant: str) -> str:
        return f"{_TENANT_INDEX_PREFIX}{tenant}"

    def _id_index(self, key_id: str) -> str:
        return f"{_ID_INDEX_PREFIX}{key_id}"

    async def create(self, tenant: str, name: str) -> tuple[ApiKeyRecord, str]:
        plaintext, prefix, digest = generate_api_key()
        key_id = str(uuid.uuid4())
        created_at = time.time()
        await self._redis.hset(
            self._key(digest),
            mapping={
                "key_id": key_id,
                "tenant": tenant,
                "name": name,
                "prefix": prefix,
                "created_at": created_at,
                "last_used_at": "",
                "revoked_at": "",
            },
        )
        await self._redis.sadd(self._tenant_index(tenant), digest)
        await self._redis.set(self._id_index(key_id), digest)
        record = _record_from_fields(key_id, tenant, name, prefix, str(created_at), "", "")
        return record, plaintext

    async def _read(self, digest: str) -> ApiKeyRecord | None:
        raw = await self._redis.hgetall(self._key(digest))
        if not raw:
            return None
        return _record_from_fields(
            _decode(raw.get(b"key_id")),
            _decode(raw.get(b"tenant")),
            _decode(raw.get(b"name")),
            _decode(raw.get(b"prefix")),
            _decode(raw.get(b"created_at")),
            _decode(raw.get(b"last_used_at")),
            _decode(raw.get(b"revoked_at")),
        )

    async def resolve(self, plaintext: str) -> ApiKeyRecord | None:
        digest = hash_key(plaintext)
        record = await self._read(digest)
        if record is None or record.revoked_at is not None:
            return None
        now = time.time()
        await self._redis.hset(self._key(digest), "last_used_at", now)
        return replace(record, last_used_at=datetime.fromtimestamp(now, UTC))

    async def list(self, tenant: str) -> list[ApiKeyRecord]:
        digests = await self._redis.smembers(self._tenant_index(tenant))
        records = [r for d in digests if (r := await self._read(_decode(d))) is not None]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def revoke(self, key_id: str) -> None:
        raw_digest = await self._redis.get(self._id_index(key_id))
        if raw_digest is None:
            return
        await self._redis.hset(self._key(_decode(raw_digest)), "revoked_at", time.time())
