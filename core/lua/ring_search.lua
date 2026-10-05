-- ring_search.lua: nearest fresh drivers in one round trip, best-first over H3 cells (matcher).
-- KEYS: none (cell keys are derived; single-node Redis like the other scripts)
-- ARGV: need, min_score, prune (1/0), lat, lng, n_cells, cell_1, bound_1, cell_2, bound_2, ...
--   cells: every cell of rings 0..kmax, sorted by bound ascending (computed on the client)
--   bound: a lower bound on the distance from the query to ANY point of that cell
--          (centre distance minus the max res-9 circumradius in the service area)
-- Visits cells nearest-bound first; once `need` drivers are in hand, a cell whose bound is beyond the
-- current need-th distance can't contain anything better, and neither can any later cell, so it stops.
-- That is exact within the searched rings (stronger than the k+1 ring guard it replaced).
-- prune=0 stops as soon as `need` drivers are found, unchecked: kept only so a test can show the difference.
--
-- Bench history (bench/results): per-ring round trips -> ~2.7 ms p50 at 1k drivers; touching every driver
-- in the first two rings -> 17-21 ms at 100k (Manhattan density, ~150 drivers per res-9 cell).
-- Returns a flat list, nearest first: id_1, metres_1, id_2, metres_2, ...  (ties broken by id)
local need, min_score, prune = tonumber(ARGV[1]), ARGV[2], ARGV[3] == '1'
local lat0, lng0 = math.rad(tonumber(ARGV[4])), tonumber(ARGV[5])
local n_cells = tonumber(ARGV[6])
local cos0 = math.cos(lat0)

local best = {}            -- up to `need` entries {dist, id}, kept sorted ascending
local function better(x, y) if x[1] ~= y[1] then return x[1] < y[1] end return x[2] < y[2] end
local function offer(e)
  if #best == need and not better(e, best[need]) then return end
  local i = #best < need and #best + 1 or need
  best[i] = e
  while i > 1 and better(best[i], best[i - 1]) do
    best[i], best[i - 1] = best[i - 1], best[i]
    i = i - 1
  end
end

for c = 0, n_cells - 1 do
  local cell, bound = ARGV[7 + 2 * c], tonumber(ARGV[8 + 2 * c])
  if #best == need then
    if not prune or bound > best[need][1] then break end
  end
  for _, d in ipairs(redis.call('ZRANGEBYSCORE', 'cell:' .. cell, min_score, '+inf')) do
    local p = redis.call('HMGET', 'driver:' .. d, 'lat', 'lng')
    if p[1] then
      local lat1 = math.rad(tonumber(p[1]))
      local a = math.sin((lat1 - lat0) / 2) ^ 2
              + cos0 * math.cos(lat1) * math.sin(math.rad(tonumber(p[2]) - lng0) / 2) ^ 2
      offer({2 * 6371000 * math.asin(math.sqrt(a)), d})
    end
  end
end

local out = {}
for i = 1, #best do
  out[#out + 1] = best[i][2]
  out[#out + 1] = tostring(best[i][1])
end
return out
