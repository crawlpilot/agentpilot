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
-- NODE ADMISSION LIVES HERE, and that placement is the point. It used to be a
-- Python call one step earlier: read the count, return, then open. The
-- registry's asyncio.Lock is per-IDENTITY, so two *different* identities
-- serialize against nothing and both pass the same check. The log recorded the
-- result directly -- `admission.refused_max_contexts live=5 max_contexts=4` --
-- a limit of four, breached, by the thing enforcing it. A count is only worth
-- what it is worth at the moment of the claim, so the count and the claim have
-- to be the same step, and this script is the only step that already is.
--
-- The slot set is scored by EXPIRY, the shape firecrawl's concurrency limiter
-- uses (`zadd(key, now + timeout, id)`): a holder that dies stops being counted
-- the moment its score passes `now`, with no reaper in the path and no cleanup
-- to forget. The previous count came from scanning `active:*`, and no script
-- here set an expiry on anything -- so eighteen `node_sessions:*` keys survived
-- across two live nodes, sixteen belonging to containers that no longer
-- existed. A count taken over keys that nothing removes only ever grows.
--
-- KEYS[1] = active:{identity-slug} hash
-- KEYS[2] = node_slots:{node_id} sorted set, member = slug, score = deadline
-- ARGV[1] = owner
-- ARGV[2] = ttl_seconds
-- ARGV[3] = lease_id
-- ARGV[4] = now (unix seconds)
-- ARGV[5..7] = tenant, domain, name (stored losslessly so callers never have
--              to reconstruct an IdentityKey by splitting the slug back apart)
-- ARGV[8]  = identity slug (the slot's member name)
-- ARGV[9]  = max_contexts for this node
-- ARGV[10] = slot_ttl_seconds -- a crash backstop, refreshed by every heartbeat
-- ARGV[11] = key_ttl_seconds -- expiry for the bookkeeping keys themselves
--
-- Returns {reuse (0|1), context_id, pid, node_id}. `reuse=1` means a warm
-- context_id already exists (a prior release left it IDLE) -- the caller
-- must NOT call driver.open() again, exactly the P0->P1 SingletonLock bug.

local now = tonumber(ARGV[4])
local slot_ttl = tonumber(ARGV[10])
local key_ttl_ms = math.floor(tonumber(ARGV[11]) * 1000)

local state = redis.call('HGET', KEYS[1], 'state')
if state == 'active' then
  return redis.error_reply('LEASE_CONFLICT')
end

local context_id = redis.call('HGET', KEYS[1], 'context_id')
local reuse = 0
if context_id and context_id ~= '' then
  reuse = 1
end

-- Admission, before anything is mutated. Refusing after the HSET would leave
-- the identity reserved ACTIVE by a lease nobody holds, which is the state the
-- Python version had to unwind by hand in an `except` clause.
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if reuse == 0 and redis.call('ZSCORE', KEYS[2], ARGV[8]) == false then
  -- Only a genuinely NEW browser is ever refused. A warm context and a slot
  -- this node already holds both mean the memory is spent either way, and
  -- declining would stop the node doing the work that frees slots -- the
  -- deadlock that turns a busy minute into a stalled queue.
  if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[9]) then
    return redis.error_reply('CAPACITY_EXHAUSTED')
  end
end
redis.call('ZADD', KEYS[2], now + slot_ttl, ARGV[8])
redis.call('PEXPIRE', KEYS[2], key_ttl_ms)

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
redis.call('PEXPIRE', KEYS[1], key_ttl_ms)
redis.call('SET', 'lease_owner:' .. ARGV[3], KEYS[1], 'PX', key_ttl_ms)

local pid = redis.call('HGET', KEYS[1], 'pid') or ''
local node_id = redis.call('HGET', KEYS[1], 'node_id') or ''
return {reuse, context_id or '', pid, node_id}
