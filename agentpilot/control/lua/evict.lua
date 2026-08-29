-- Reaper-only destroy: removes the identity's entry entirely (distinct from
-- release_lease.lua/force_release.lua, which only ever move ACTIVE -> IDLE).
-- Returns the fields needed to reconstruct the evicted ContextRef so the
-- caller can hand it to driver.close(); an empty context_id means there was
-- nothing to evict.
--
-- KEYS[1] = active:{identity-slug} hash

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease and current_lease ~= '' then
  redis.call('DEL', 'lease_owner:' .. current_lease)
end

local context_id = redis.call('HGET', KEYS[1], 'context_id')
local pid = redis.call('HGET', KEYS[1], 'pid')
local node_id = redis.call('HGET', KEYS[1], 'node_id')
redis.call('DEL', KEYS[1])
return {context_id or '', pid or '', node_id or ''}
