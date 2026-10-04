-- move_driver.lua: apply one GPS ping atomically (ingest worker).
-- KEYS[1] = driver:{id}   KEYS[2] = lock:driver:{id}
-- ARGV: driver_id, new_cell, lat, lng, seq, ts_ms
-- Returns 1 = applied, 0 = stale / out-of-order ping (ignored).
local id, new_cell = ARGV[1], ARGV[2]
local seq, ts = tonumber(ARGV[5]), tonumber(ARGV[6])

local last = tonumber(redis.call('HGET', KEYS[1], 'seq') or '-1')
if seq <= last then return 0 end                      -- stale / out-of-order ping

local old_cell = redis.call('HGET', KEYS[1], 'cell')
local status = redis.call('HGET', KEYS[1], 'status') or 'available'
if status == 'assigned' and redis.call('EXISTS', KEYS[2]) == 0 then
  status = 'available'                                 -- lease expired: trip_done was lost
  redis.call('HDEL', KEYS[1], 'ride')
end

redis.call('HSET', KEYS[1], 'lat', ARGV[3], 'lng', ARGV[4],
           'cell', new_cell, 'seq', seq, 'ts', ts, 'status', status)
redis.call('PEXPIRE', KEYS[1], 3600000)

if status == 'available' then
  if old_cell and old_cell ~= new_cell then
    redis.call('ZREM', 'cell:' .. old_cell, id)
  end
  redis.call('ZADD', 'cell:' .. new_cell, ts, id)     -- score = freshness
  redis.call('SADD', 'active_cells', new_cell)
end
return 1
