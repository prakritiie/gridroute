# GridRoute

Real-time ride dispatch on Uber's H3 hexagonal grid. Moving drivers are indexed into H3 cells in Redis,
every multi-key write is an atomic Lua script, and riders are matched without ever double-booking a driver.
On top of that core: a learned ETA and a 15-minute demand forecast (LightGBM, trained on GPU on 67M real NYC
2016 trips), used to pick drivers and to send idle drivers to where riders are about to appear.

Design and plan: [docs/implementation.md](docs/implementation.md). Static demo (a recorded replay, no
backend): [docs/demo.md](docs/demo.md).

## Run it

```bash
docker compose up -d --build --scale ingest=2 --scale matcher=2   # Redis, gateway :8000, workers, 5k drivers, riders,
                                                                  # Prometheus :9090, Grafana :3001
cd web && npm install && npm run dev                              # ops dashboard on :3000
```

Set `GRIDROUTE_DATA_DIR` (in `.env` or the shell) to the folder `python -m data.download` fills; the policy
lab's runner mounts it to replay real trips.

## Explore it

The dashboard has three views:

- **Live.** The map shows supply, demand, imbalance or the 15-minute **forecast** per hex at three sizes,
  with the moves the repositioner makes. KPIs and a live audit (double-bookings, invariant violations) sit
  beside it. Click any match to see **why that driver** won: every candidate's straight-line distance, OSRM
  time, model ETA and cost.
- **Policy lab.** Pick a June 2016 day and window, greedy or batch matching, distance or ETA cost, and
  repositioning on or off. The runner pauses the demo, replays real demand through the live system and
  records it. Compare up to four runs side by side, with changes against a baseline, and export them to CSV.
- **Chaos.** Silence 10–60% of drivers in an area, burst demand ×10, or deliver pings out of order, then watch
  recovery: available drivers fall within ~30 s, stale pings are rejected, and the double-booking counter
  stays at 0.

Tests (need Redis: `docker compose up -d redis`):

```bash
pip install -e ".[dev]"
pytest
```

## Status

M1–M3 are done (core engine, real data and benchmarks, learned ETA and demand forecasting). M4 adds the
policy lab, chaos controls and the static demo. These claims are tested in CI and hold in live runs:

| Claim | Evidence |
|---|---|
| C1 No driver is ever double-booked | 16 matcher processes racing on 1,000+ real claim conflicts: 0 double-bookings. A naive check-then-set version double-books 146 of 50 drivers under the same harness. |
| C2 Location state stays consistent | Every fresh, available driver is in exactly one cell; audited live every 10 s |
| C3 Nearby lookup is correct | Ring search top-5 = brute force on 500/500 queries (285/500 wrong without the k+1 guard) |
| C4 The system heals itself | Silent drivers leave search in ≤ 30 s and the index in ≤ 35 s; the simulator survives gateway restarts |
| C5 The core is fast, measured fairly | [Benchmarks](docs/benchmarks.md) vs Redis GEO and PostGIS on real positions, 1k → 100k drivers. H3 is the fastest writer (24–38k position updates/s) and beats Redis GEO lookups at 100k (4.6 ms p50); PostGIS's KNN index wins pure lookup at high density (1.0 ms), and one Redis caps near 200 lookups/s at Manhattan density. The benchmark drove a best-first rewrite of the search (4.6× faster at 100k). |

## Learned ETA (M3)

LightGBM on `log1p(duration)`, trained on 1.2M Jan–Apr 2016 trips **on the GPU** (LightGBM's CUDA learner, built in
[ml/Dockerfile.gpu](ml/Dockerfile.gpu); RTX 3050, 13 min), early-stopped on May, tested on 300k June trips it
never saw. Features come from one shared function used by training and the matcher alike
([core/features.py](core/features.py)), with a parity test. Full card: [docs/model_card_eta.md](docs/model_card_eta.md).

| Mean absolute error (June) | B1 straight-line | B2 raw OSRM | B3 hex-hour speed | **Model** |
|---|---|---|---|---|
| All trips | 6.69 min | 7.37 min | 5.08 min | **3.08 min (−39% vs B3)** |
| < 3 km (pickup-like) | 3.61 min | 5.39 min | 2.94 min | **2.27 min (−23%)** |

RMSLE 0.301 (B3 0.416, raw OSRM 0.797). The model beats every baseline on every slice: distance, borough,
airports and time of day.

## Demand forecast (M3)

How many requests will each ~5 km² hex (H3 res 7) get in the next 15 minutes? One global LightGBM with a
Poisson objective over 84 hexes, trained Jan–Apr, early-stopped on May, tested on June. Features are lags
(last slot, last hour, same slot yesterday and last week, 4-week average), hour of week and holidays, built by
one shared function at training and serving time. [Model card](docs/model_card_demand.md).

| June 2016, WAPE per (hex, 15 min) | Seasonal naive | 4-week average | **Model** |
|---|---|---|---|
| All hexes | 14.8% | 11.5% | **8.4%** |
| Busy hexes (top 20%) | 13.3% | 10.2% | **7.2%** |

Live, the aggregator counts requests per hex on the sim clock, the forecaster predicts each new slot, and the
repositioner sends 20% of a hex's idle-driver surplus toward the neighbouring hex with the largest expected
shortfall, every 2 sim-minutes. The dashboard's **Forecast** layer shows the forecast and the moves.

## Results: does better prediction make better dispatch? (C7)

Real June 2016 demand replayed through the live system (Tue 7 Jun, 07:00–09:00, 20% of real requests, 1,600
drivers, 3 seeds per row). Pickup time is the simulated *true* drive time: OSRM road time × June congestion,
which the ETA model never trains on. Each row changes one thing. Full protocol and per-run detail:
[docs/results.md](docs/results.md).

| Row | Matching | Cost | Pickup mean | Pickup p90 | Unmatched % | Empty km / driver-h | Decision p99 | Pickup-estimate error |
|---|---|---|---|---|---|---|---|---|
| R0 | Greedy | Straight-line distance | 4:49 ± 0:07 | 8:02 ± 0:06 | 13.98 ± 1.50 | 1.77 ± 0.03 | 345 ms | n/a |
| R1 | Greedy | ETA model | 4:45 ± 0:05 | 8:06 ± 0:07 | 12.67 ± 0.94 | 1.75 ± 0.03 | 219 ms | 1:24 |
| R2 | Batch (Hungarian) | Straight-line distance | 4:25 ± 0:02 | 7:43 ± 0:04 | 14.12 ± 0.04 | 1.62 ± 0.02 | 102 ms | 2:41 |
| R3 | Batch | ETA model | 4:19 ± 0:06 | 7:47 ± 0:05 | 13.45 ± 0.73 | **1.59 ± 0.06** | 171 ms | **1:22** |
| **R4** | **Batch + repositioning** | **ETA model** | **3:59 ± 0:12** | **7:28 ± 0:19** | **7.40 ± 0.38** | 1.97 ± 0.05 | 213 ms* | 1:27 |

\* Two of three R4 seeds (216 and 210 ms); the third (1,120 ms) ran while the dashboard was being rebuilt on
the same laptop and is reported in [docs/results.md](docs/results.md) as recorded.

What this says, honestly:

- **Batching is the big win.** Assigning each 2-second batch jointly (Hungarian) instead of first-come-first-served
  cuts mean pickup by 24–30 s (8–10%) and empty km by 9–10%, well outside the seed spread.
- **The learned ETA helps most inside a batch.** R3 is the best row on pickup time and empty km; greedy + ETA (R1)
  is within noise of R0, because with ~5 nearby candidates distance and drive time usually agree on the winner.
  Where they disagree it matters: the dashboard's *Why this driver* panel shows drivers 46 m away by straight
  line but 2 minutes away by Manhattan's one-way streets.
- **The ETA halves the pickup-estimate error** the rider is shown (1:22 vs 2:41 for the distance heuristic),
  even where it doesn't change the driver.
- **No matching policy fixes unmatched riders** (13–14% in every row R0–R3). The failure is spatial: idle
  drivers wait where their last trip ended, while riders appear elsewhere.
- **Repositioning fixes most of it.** Moving idle drivers ahead of forecast demand (R4) nearly halves unmatched
  riders (13.45% → 7.40%, about 390 more rides served per run) and cuts mean pickup by another 20 s. The cost is
  empty driving: +0.38 km per driver-hour (+24%), almost all of it the repositioning drives themselves
  (0.40 km/driver-h, ~1,000 moves per run). The median number of idle drivers in slots where riders went
  unserved falls from ~610 to ~370.
- **Known limit:** the model was trained on cleaned taxi trips, which exclude legs under 300 m / 60 s, so it
  predicts at least ~2 min even for a 10-second pickup leg. Training on pickup-like legs is the next model.

Map data © OpenStreetMap contributors, basemap © CARTO.
