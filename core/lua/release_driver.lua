-- release_driver.lua: free a driver when its trip is done (on trip_done).
-- KEYS[1] = driver:{id}   KEYS[2] = lock:driver:{id}   KEYS[3] = ride:{ride_id}
-- ARGV: driver_id, ride_id, now_ms
-- Returns 1 = released, 0 = caller's ride does not own the lock.
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end   -- only the owning ride may release
redis.call('DEL', KEYS[2])
redis.call('HSET', KEYS[1], 'status', 'available')
redis.call('HDEL', KEYS[1], 'ride')
redis.call('HSET', KEYS[3], 'status', 'completed', 't_done', ARGV[3])
redis.call('SREM', 'rides:open', ARGV[2])

-- Re-index at the last known position right away (spec v2 waited for the next ping, which left a
-- fresh available driver outside every cell for up to 4 s: a C2 violation and a lost match window).
-- Score = last ping time, so a driver that went silent is still filtered by search and reaped as usual.
local cell = redis.call('HGET', KEYS[1], 'cell')
if cell then
  redis.call('ZADD', 'cell:' .. cell, tonumber(redis.call('HGET', KEYS[1], 'ts') or '0'), ARGV[1])
  redis.call('SADD', 'active_cells', cell)
end
return 1
