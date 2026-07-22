-- Reaper-only reclaim of an ACTIVE lease nobody renewed in time -- keyed by
-- identity, not lease_id (the reaper's whole premise is "the owner is
-- presumed gone", so it can't prove ownership the way release_lease.lua
-- requires). Releases to IDLE, never destroys -- see Registry.force_release's
-- docstring for why.
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = now (unix seconds, stored as released_at)

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease and current_lease ~= '' then
  redis.call('DEL', 'lease_owner:' .. current_lease)
end

redis.call('HSET', KEYS[1], 'state', 'idle', 'lease_id', '', 'owner', '', 'released_at', ARGV[1])
return 'OK'
