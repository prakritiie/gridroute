-- unmatch_ride.lua: give up on a ride after too many failed attempts (matcher).
-- KEYS[1] = ride:{id}   ARGV: now_ms
-- Only a still-queued ride becomes 'unmatched'; one that was assigned meanwhile is left alone.
-- Returns 1 = marked unmatched, 0 = no longer queued.
if redis.call('HGET', KEYS[1], 'status') ~= 'queued' then return 0 end
redis.call('HSET', KEYS[1], 'status', 'unmatched', 't_unmatched', ARGV[1])
return 1
