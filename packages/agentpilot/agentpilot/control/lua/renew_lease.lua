-- Sliding-window lease renewal -- same semantics as session.lease.renew()
-- (bumps acquired_at forward). Errors if this lease_id is no longer the
-- current holder, so a renew() racing a reaper reclaim fails loudly
-- instead of silently reviving a lease that's already gone.
--
-- Renewal also pushes the node slot's deadline out, which is what makes the
-- slot set self-cleaning: claiming and renewing are the same ZADD, so a
-- heartbeat is idempotent and carries no state, and the only thing that ever
-- reaches a deadline is a holder that stopped running. Nothing has to notice a
-- crash for its slot to stop counting.
--
-- KEYS[1] = active:{identity-slug} hash
-- KEYS[2] = node_slots:{node_id} sorted set
-- ARGV[1] = lease_id
-- ARGV[2] = now (unix seconds)
-- ARGV[3] = identity slug
-- ARGV[4] = slot_ttl_seconds
-- ARGV[5] = key_ttl_seconds

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease ~= ARGV[1] then
  return redis.error_reply('LEASE_NOT_FOUND')
end

local key_ttl_ms = math.floor(tonumber(ARGV[5]) * 1000)
redis.call('HSET', KEYS[1], 'acquired_at', ARGV[2])
redis.call('PEXPIRE', KEYS[1], key_ttl_ms)
redis.call('PEXPIRE', 'lease_owner:' .. ARGV[1], key_ttl_ms)
redis.call('ZADD', KEYS[2], tonumber(ARGV[2]) + tonumber(ARGV[4]), ARGV[3])
redis.call('PEXPIRE', KEYS[2], key_ttl_ms)
return 'OK'
