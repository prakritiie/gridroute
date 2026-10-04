-- reap_cells.lua: evict silent drivers from a batch of cells (reaper, every 5 s).
-- KEYS[1] = active_cells   ARGV: cutoff_ms, cell1, cell2, ...
-- Removes members whose last ping is older than cutoff_ms; drops cells that end up empty from active_cells.
-- Atomic per batch, so a concurrent move_driver ZADD can't leave a non-empty cell outside active_cells.
-- Returns the number of drivers evicted.
local cutoff = '(' .. ARGV[1]
local evicted = 0
for i = 2, #ARGV do
  local key = 'cell:' .. ARGV[i]
  evicted = evicted + redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
  if redis.call('ZCARD', key) == 0 then
    redis.call('SREM', KEYS[1], ARGV[i])
  end
end
return evicted
