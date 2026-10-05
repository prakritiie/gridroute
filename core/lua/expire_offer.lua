-- expire_offer.lua: an offer's lease ran out without an accept; put the ride back in the queue (reaper).
-- KEYS[1] = ride:{ride_id}   ARGV: ride_id, now_ms
-- Atomically: ride -> queued (attempts + 1, re-entered on stream:rides), driver -> available and back in its
-- cell, offer tracking removed. A no-op while the driver still holds the lock (lease not over yet) or once
-- the ride has moved on (accepted, re-queued by move_driver.lua, gone).
-- Returns 1 = re-queued, 0 = nothing to do.
local ride, now = ARGV[1], ARGV[2]
if redis.call('HGET', KEYS[1], 'status') ~= 'offered' then
  redis.call('ZREM', 'offers', ride)
  return 0
end
local d = redis.call('HGET', KEYS[1], 'driver')
if d and redis.call('GET', 'lock:driver:' .. d) == ride then return 0 end    -- still within the lease

local lat, lng, t_req = unpack(redis.call('HMGET', KEYS[1], 'lat', 'lng', 't_req'))
redis.call('HSET', KEYS[1], 'status', 'queued', 't_offer_expired', now)
redis.call('HDEL', KEYS[1], 'driver')
redis.call('HINCRBY', KEYS[1], 'attempts', 1)
redis.call('ZREM', 'offers', ride)
redis.call('XADD', 'stream:rides', 'MAXLEN', '~', 100000, '*',
           'ride_id', ride, 'lat', lat, 'lng', lng, 't', t_req or now, 'retry', 1)
redis.call('XADD', 'stream:events', 'MAXLEN', '~', 1000000, '*',
           'type', 'offer_expired', 'ride', ride, 'driver', d or '', 't', now)

if d then
  local dkey = 'driver:' .. d
  if redis.call('HGET', dkey, 'ride') == ride and redis.call('HGET', dkey, 'status') == 'assigned' then
    redis.call('HSET', dkey, 'status', 'available')
    redis.call('HDEL', dkey, 'ride')
    local cell = redis.call('HGET', dkey, 'cell')
    if cell then
      redis.call('ZADD', 'cell:' .. cell, tonumber(redis.call('HGET', dkey, 'ts') or '0'), d)
      redis.call('SADD', 'active_cells', cell)
    end
  end
end
return 1
