-- ACTIVE -> IDLE (never destroys -- same convention as the in-memory
-- Registry.release(); only the reaper's evict.lua/DEL destroys). A no-op
-- (returns 0) if this lease_id is no longer the current holder -- already
-- reclaimed by the reaper or already released, so there's nothing to undo.
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = lease_id
-- ARGV[2] = now (unix seconds, stored as released_at)

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease ~= ARGV[1] then
  return 0
end

redis.call('HSET', KEYS[1], 'state', 'idle', 'lease_id', '', 'owner', '', 'released_at', ARGV[2])
redis.call('DEL', 'lease_owner:' .. ARGV[1])
return 1
