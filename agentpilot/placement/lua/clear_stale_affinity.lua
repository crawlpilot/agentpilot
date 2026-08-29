-- Compare-and-delete: only clears an identity's affinity pointer if it still
-- points at the node the caller believes is dead. Without this check, the
-- node-reaper could erase a *fresh* affinity a healthy re-placement already
-- wrote for the same identity between the dead node's last heartbeat and
-- the reaper noticing -- a bare DEL would silently break that new placement.
--
-- KEYS[1] = affinity:{identity-slug}
-- ARGV[1] = expected (dead) node_id
--
-- Returns 1 if cleared, 0 if left alone (already pointing elsewhere, or gone).

local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
