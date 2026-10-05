-- claim_driver.lua: check-and-claim a driver for a ride in one atomic step (matcher).
-- KEYS[1] = driver:{id}   KEYS[2] = lock:driver:{id}   KEYS[3] = ride:{ride_id}
-- ARGV: driver_id, ride_id, lease_ms, now_ms, max_age_ms, [state]
--   state 'offered' (M2 default): a short offer lease; the driver must accept (accept_offer.lua) before it
--     runs out, or the reaper puts the ride back in the queue. The ride is tracked in the `offers` zset.
--   state 'assigned': claim for the whole trip in one step (M1 behaviour, kept for tests and tools).
-- Returns 1 = claimed, 0 = driver unavailable (try next candidate), -1 = ride already handled.
local id, ride = ARGV[1], ARGV[2]
local now, max_age = tonumber(ARGV[4]), tonumber(ARGV[5])
local state = ARGV[6] or 'assigned'

if redis.call('HGET', KEYS[3], 'status') ~= 'queued' then return -1 end  -- ride already handled
if redis.call('HGET', KEYS[1], 'status') ~= 'available' then return 0 end
local ts = tonumber(redis.call('HGET', KEYS[1], 'ts') or '0')
if now - ts > max_age then return 0 end                                  -- silent driver
if not redis.call('SET', KEYS[2], ride, 'NX', 'PX', ARGV[3]) then return 0 end

local cell = redis.call('HGET', KEYS[1], 'cell')
redis.call('HSET', KEYS[1], 'status', 'assigned', 'ride', ride)
redis.call('ZREM', 'cell:' .. cell, id)
if state == 'offered' then
  redis.call('HSET', KEYS[3], 'status', 'offered', 'driver', id, 't_offered', now)
  redis.call('ZADD', 'offers', now + tonumber(ARGV[3]), ride)            -- score = when the offer lapses
else
  redis.call('HSET', KEYS[3], 'status', 'assigned', 'driver', id, 't_assigned', now)
end
redis.call('SADD', 'rides:open', ride)        -- index of rides holding a driver (the audit reads only these)
return 1
