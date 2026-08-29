-- Sliding-window lease renewal -- same semantics as session.lease.renew()
-- (bumps acquired_at forward). Errors if this lease_id is no longer the
-- current holder, so a renew() racing a reaper reclaim fails loudly
-- instead of silently reviving a lease that's already gone.
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = lease_id
-- ARGV[2] = now (unix seconds)

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease ~= ARGV[1] then
  return redis.error_reply('LEASE_NOT_FOUND')
end

redis.call('HSET', KEYS[1], 'acquired_at', ARGV[2])
return 'OK'
