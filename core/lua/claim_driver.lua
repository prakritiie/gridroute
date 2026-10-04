-- claim_driver.lua: check-and-claim a driver for a ride in one atomic step (matcher).
-- KEYS[1] = driver:{id}   KEYS[2] = lock:driver:{id}   KEYS[3] = ride:{ride_id}
-- ARGV: driver_id, ride_id, lease_ms, now_ms, max_age_ms
-- Returns 1 = claimed, 0 = driver unavailable (try next candidate), -1 = ride already handled.
local id, ride = ARGV[1], ARGV[2]
local now, max_age = tonumber(ARGV[4]), tonumber(ARGV[5])

if redis.call('HGET', KEYS[3], 'status') ~= 'queued' then return -1 end  -- ride already handled
if redis.call('HGET', KEYS[1], 'status') ~= 'available' then return 0 end
local ts = tonumber(redis.call('HGET', KEYS[1], 'ts') or '0')
if now - ts > max_age then return 0 end                                  -- silent driver
if not redis.call('SET', KEYS[2], ride, 'NX', 'PX', ARGV[3]) then return 0 end

local cell = redis.call('HGET', KEYS[1], 'cell')
redis.call('HSET', KEYS[1], 'status', 'assigned', 'ride', ride)
redis.call('ZREM', 'cell:' .. cell, id)
redis.call('HSET', KEYS[3], 'status', 'assigned', 'driver', id, 't_assigned', now)
return 1
