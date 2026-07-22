-- Reserve-first half of RedisRegistry.acquire(). Atomically checks the
-- <=1-ACTIVE invariant and, if clear, immediately flips the identity to
-- ACTIVE under the new lease -- *before* the (possibly slow, seconds-long)
-- driver.open() runs in Python. This is deliberately two Redis round trips
-- (this script, then bind_active_context.lua once open() finishes) rather
-- than one lock held across the whole open() call: a Lua script can't await
-- a Python coroutine, and holding a *distributed* lock for a multi-second
-- browser launch would be its own outage risk (client dies mid-launch ->
-- lock never releases without a separate expiry mechanism anyway).
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = owner
-- ARGV[2] = ttl_seconds
-- ARGV[3] = lease_id
-- ARGV[4] = now (unix seconds)
-- ARGV[5..7] = tenant, domain, name (stored losslessly so callers never have
--              to reconstruct an IdentityKey by splitting the slug back apart)
--
-- Returns {reuse (0|1), context_id, pid, node_id}. `reuse=1` means a warm
-- context_id already exists (a prior release left it IDLE) -- the caller
-- must NOT call driver.open() again, exactly the P0->P1 SingletonLock bug.

local state = redis.call('HGET', KEYS[1], 'state')
if state == 'active' then
  return redis.error_reply('LEASE_CONFLICT')
end

local context_id = redis.call('HGET', KEYS[1], 'context_id')
local reuse = 0
if context_id and context_id ~= '' then
  reuse = 1
end

redis.call('HSET', KEYS[1],
  'state', 'active',
  'lease_id', ARGV[3],
  'owner', ARGV[1],
  'acquired_at', ARGV[4],
  'ttl_seconds', ARGV[2],
  'released_at', '',
  'tenant', ARGV[5],
  'domain', ARGV[6],
  'name', ARGV[7]
)
redis.call('SET', 'lease_owner:' .. ARGV[3], KEYS[1])

local pid = redis.call('HGET', KEYS[1], 'pid') or ''
local node_id = redis.call('HGET', KEYS[1], 'node_id') or ''
return {reuse, context_id or '', pid, node_id}
