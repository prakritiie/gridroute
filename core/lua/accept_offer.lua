-- accept_offer.lua: the driver accepts an offer before its lease runs out (gateway, on WS accept).
-- KEYS[1] = driver:{id}   KEYS[2] = lock:driver:{id}   KEYS[3] = ride:{ride_id}
-- ARGV: driver_id, ride_id, trip_lease_ms, now_ms
-- Returns 1 = accepted (lock extended to the trip lease), 0 = offer lapsed or isn't this driver's,
--         -1 = ride no longer offered (already accepted, re-queued, or gone).
local id, ride = ARGV[1], ARGV[2]
if redis.call('GET', KEYS[2]) ~= ride then return 0 end                 -- lease ran out, or another ride
if redis.call('HGET', KEYS[3], 'status') ~= 'offered' then return -1 end
if redis.call('HGET', KEYS[3], 'driver') ~= id then return 0 end

redis.call('PEXPIRE', KEYS[2], ARGV[3])
redis.call('HSET', KEYS[3], 'status', 'assigned', 't_assigned', ARGV[4])
redis.call('ZREM', 'offers', ride)
return 1
