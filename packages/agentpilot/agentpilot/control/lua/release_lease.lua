-- ACTIVE -> IDLE (never destroys -- same convention as the in-memory
-- Registry.release(); only the reaper's evict.lua/DEL destroys). A no-op
-- (returns 0) if this lease_id is no longer the current holder -- already
-- reclaimed by the reaper or already released, so there's nothing to undo.
--
-- The node slot is deliberately NOT released here. The browser is still
-- running, still resident, and still the reason the next open would not fit;
-- it is the destroy in evict.lua that gives the memory back. The slot's
-- deadline is pushed out instead, because an IDLE context is a live one and
-- letting it expire out of the count would understate what the node holds.
--
-- KEYS[1] = active:{identity-slug} hash
-- KEYS[2] = node_slots:{node_id} sorted set
-- ARGV[1] = lease_id
-- ARGV[2] = now (unix seconds, stored as released_at)
-- ARGV[3] = identity slug
-- ARGV[4] = slot_ttl_seconds
-- ARGV[5] = key_ttl_seconds

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease ~= ARGV[1] then
  return 0
end

local key_ttl_ms = math.floor(tonumber(ARGV[5]) * 1000)
redis.call('HSET', KEYS[1], 'state', 'idle', 'lease_id', '', 'owner', '', 'released_at', ARGV[2])
redis.call('PEXPIRE', KEYS[1], key_ttl_ms)
redis.call('DEL', 'lease_owner:' .. ARGV[1])
redis.call('ZADD', KEYS[2], tonumber(ARGV[2]) + tonumber(ARGV[4]), ARGV[3])
redis.call('PEXPIRE', KEYS[2], key_ttl_ms)
return 1
