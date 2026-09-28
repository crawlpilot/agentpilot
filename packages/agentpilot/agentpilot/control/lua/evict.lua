-- Reaper-only destroy: removes the identity's entry entirely (distinct from
-- release_lease.lua/force_release.lua, which only ever move ACTIVE -> IDLE).
-- Returns the fields needed to reconstruct the evicted ContextRef so the
-- caller can hand it to driver.close(); an empty context_id means there was
-- nothing to evict.
--
-- This is also the ONE place a node slot is surrendered. `release` moves a
-- context to IDLE and the browser keeps running and keeps its memory, so the
-- node is still carrying it and the budget must still say so; only a destroy
-- gives the memory back. Freeing the slot at release would let a node admit a
-- fifth browser while four were still resident, which is the same
-- over-admission the slot set exists to prevent, arrived at from the other
-- direction.
--
-- KEYS[1] = active:{identity-slug} hash
-- KEYS[2] = node_slots:{node_id} sorted set
-- ARGV[1] = identity slug (the slot's member name)

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease and current_lease ~= '' then
  redis.call('DEL', 'lease_owner:' .. current_lease)
end

local context_id = redis.call('HGET', KEYS[1], 'context_id')
local pid = redis.call('HGET', KEYS[1], 'pid')
local node_id = redis.call('HGET', KEYS[1], 'node_id')
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return {context_id or '', pid or '', node_id or ''}
