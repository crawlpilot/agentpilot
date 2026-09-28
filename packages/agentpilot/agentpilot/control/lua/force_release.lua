-- Reaper-only reclaim of an ACTIVE lease nobody renewed in time -- keyed by
-- identity, not lease_id (the reaper's whole premise is "the owner is
-- presumed gone", so it can't prove ownership the way release_lease.lua
-- requires). Releases to IDLE, never destroys -- see Registry.force_release's
-- docstring for why.
--
-- The slot is kept for the same reason release_lease.lua keeps it: this hands
-- the identity back, it does not close the browser. What it must NOT do is
-- refresh the slot's deadline -- the owner is presumed gone, so there is
-- nothing left claiming this is alive, and letting the deadline run out is
-- exactly how a node stops counting a browser whose holder died. The reaper's
-- idle-TTL pass normally destroys it first; the deadline is the backstop for
-- when the reaper itself is the thing that died.
--
-- KEYS[1] = active:{identity-slug} hash
-- ARGV[1] = now (unix seconds, stored as released_at)
-- ARGV[2] = key_ttl_seconds

local current_lease = redis.call('HGET', KEYS[1], 'lease_id')
if current_lease and current_lease ~= '' then
  redis.call('DEL', 'lease_owner:' .. current_lease)
end

redis.call('HSET', KEYS[1], 'state', 'idle', 'lease_id', '', 'owner', '', 'released_at', ARGV[1])
redis.call('PEXPIRE', KEYS[1], math.floor(tonumber(ARGV[2]) * 1000))
return 'OK'
