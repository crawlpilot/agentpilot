-- Reserve-only half of gateway placement: chooses a node for a NEW session
-- via affinity-first, then least-loaded, with admission control -- but does
-- NOT write session:{id} (that id doesn't exist yet at decision time; it's
-- minted by the worker, only known after its HTTP response comes back --
-- see placement/placer.py's commit_route()). Mirrors acquire_lease.lua's
-- role: reserve fast in Lua, let the caller do the slow multi-second worker
-- HTTP call in Python afterward -- Lua can't await a coroutine.
--
-- KEYS[1] = live_nodes (SET of node_id)
-- KEYS[2] = active:{identity-slug} hash (read-only here -- existence/state
--           check only, for the relocation-conflict rule)
-- ARGV[1] = identity slug (tenant/domain/name, IdentityKey.slug())
-- ARGV[2] = affinity ttl seconds
--
-- Returns {node_id, outcome} on success, outcome one of 'affinity_hit' |
-- 'relocated' | 'least_loaded' (purely descriptive, for the caller's
-- placement_decisions_total metric -- no behavioral meaning).
-- Errors: 'IDENTITY_ACTIVE_ELSEWHERE' if an affinity target is full/dead and
--         the identity already holds an ACTIVE lease elsewhere (relocating
--         would orphan a context a live client is mid-session on); 'NO_
--         CAPACITY' if no live node has room.

local affinity_key = 'affinity:' .. ARGV[1]
local affinity_node = redis.call('GET', affinity_key)

local function has_capacity(node_id)
  local cap = redis.call('HMGET', 'capacity:' .. node_id, 'active', 'max_contexts')
  local active, max_contexts = cap[1], cap[2]
  if not active or not max_contexts then
    -- No live heartbeat -- live_nodes membership alone is never trusted,
    -- capacity:{id}'s TTL is the actual liveness signal.
    return false
  end
  return tonumber(active) < tonumber(max_contexts)
end

local function reserve(node_id, outcome)
  -- Optimistic bookkeeping only -- the node's own next 2s heartbeat
  -- overwrites this with ground truth regardless, so this increment is
  -- never the sole admission gate, just a same-second nudge against a
  -- burst of concurrent placements picking the same momentarily-idle node.
  redis.call('HINCRBY', 'capacity:' .. node_id, 'active', 1)
  redis.call('SET', affinity_key, node_id, 'EX', ARGV[2])
  return {node_id, outcome}
end

if affinity_node and affinity_node ~= '' and has_capacity(affinity_node) then
  return reserve(affinity_node, 'affinity_hit')
end

if affinity_node and affinity_node ~= '' then
  local state = redis.call('HGET', KEYS[2], 'state')
  if state == 'active' then
    return redis.error_reply('IDENTITY_ACTIVE_ELSEWHERE')
  end
end

-- No affinity, or affinity target unusable and safe to relocate:
-- least-loaded by active/max_contexts ratio among live, capacitied nodes.
local candidates = redis.call('SMEMBERS', KEYS[1])
local best_node = nil
local best_ratio = nil
for _, node_id in ipairs(candidates) do
  local cap = redis.call('HMGET', 'capacity:' .. node_id, 'active', 'max_contexts')
  local active, max_contexts = cap[1], cap[2]
  if active and max_contexts then
    local max_n = tonumber(max_contexts)
    if max_n > 0 then
      local ratio = tonumber(active) / max_n
      if best_ratio == nil or ratio < best_ratio then
        best_node = node_id
        best_ratio = ratio
      end
    end
  end
end

if best_node == nil then
  return redis.error_reply('NO_CAPACITY')
end

local outcome = (affinity_node and affinity_node ~= '') and 'relocated' or 'least_loaded'
return reserve(best_node, outcome)
