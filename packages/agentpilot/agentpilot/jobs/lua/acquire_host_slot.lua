-- Per-host politeness gate, shared across every worker process.
--
-- KEYS[1] = the host's "next allowed start" key
-- ARGV[1] = now, in milliseconds
-- ARGV[2] = the minimum interval between starts on this host, in milliseconds
-- ARGV[3] = key TTL in seconds (so an idle host's key disappears on its own)
--
-- Returns the number of milliseconds the caller must wait before starting. 0
-- means go now.
--
-- The whole point of doing this in Lua rather than in Python is that the read
-- and the write have to be one step. Two workers that both GET the same
-- "next allowed" value and then both SET it have each concluded they may start
-- now, and the host sees two requests where the crawl-delay said one -- which
-- is precisely the politeness violation this exists to prevent, and it gets
-- worse, not better, as more workers are added.

local next_allowed = tonumber(redis.call('GET', KEYS[1]))
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local start_at
if next_allowed == nil or next_allowed <= now then
  start_at = now
else
  start_at = next_allowed
end

-- Claim this slot by moving the host's next allowed start past it. The caller
-- then sleeps until `start_at`; the reservation stands whether or not it does,
-- which is the conservative direction to fail in.
redis.call('SET', KEYS[1], start_at + interval, 'EX', ttl)

return start_at - now
