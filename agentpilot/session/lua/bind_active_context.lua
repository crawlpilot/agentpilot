-- Second half of a *fresh* RedisRegistry.acquire(): records the ContextRef
-- that driver.open() just produced. A no-op-with-error if the lease was
-- reclaimed while open() was running (extremely rare -- e.g. the reaper's
-- lease-expiry sweep fired mid-launch) so a late write can never resurrect
-- a lease nobody holds anymore.
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = lease_id (must still be the current holder)
-- ARGV[2] = context_id
-- ARGV[3] = pid ("" if unknown)
-- ARGV[4] = node_id

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease ~= ARGV[1] then
  return redis.error_reply('LEASE_LOST')
end

redis.call('HSET', KEYS[1], 'context_id', ARGV[2], 'pid', ARGV[3], 'node_id', ARGV[4])
return 'OK'
