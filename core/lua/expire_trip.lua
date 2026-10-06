-- expire_trip.lua: close an accepted trip whose driver no longer holds it (reaper sweep of rides:open).
-- KEYS[1] = ride:{ride_id}   ARGV: ride_id, now_ms
-- move_driver.lua closes such a ride when the driver pings again. This covers a driver that never does:
-- its hash expired (silent > 1 h: phone died mid-trip; found live after a 16 h laptop sleep left 1,633 rides
-- 'assigned' to drivers that had long since come back as fresh, available ones).
-- Returns 1 = closed as expired, 0 = still held (lock matches), not open, or not an accepted trip.
local ride, now = ARGV[1], ARGV[2]
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'assigned' then
  if status ~= 'offered' then redis.call('SREM', 'rides:open', ride) end   -- index drift: tidy it
  return 0
end
local d = redis.call('HGET', KEYS[1], 'driver')
if d and redis.call('GET', 'lock:driver:' .. d) == ride then return 0 end

redis.call('HSET', KEYS[1], 'status', 'expired', 't_expired', now)
redis.call('SREM', 'rides:open', ride)
redis.call('XADD', 'stream:events', 'MAXLEN', '~', 1000000, '*',
           'type', 'lease_expired', 'ride', ride, 'driver', d or '', 't', now)
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
