# GridRoute

Real-time ride dispatch on Uber's H3 hexagonal grid. Moving drivers are indexed into H3 cells in Redis,
every multi-key write is an atomic Lua script, and riders are matched without ever double-booking a driver.
Learned ETAs and demand forecasting (LightGBM, trained on real NYC 2016 trips) come next.

Design and plan: [docs/implementation.md](docs/implementation.md).

## Run it

```bash
docker compose up -d --build --scale ingest=2 --scale matcher=2   # Redis, gateway :8000, workers, 5k drivers, riders,
                                                                  # Prometheus :9090, Grafana :3001
cd web && npm install && npm run dev                              # ops dashboard on :3000
```

Tests (need Redis: `docker compose up -d redis`):

```bash
pip install -e ".[dev]"
pytest
```

## Status

M1 (core engine) is nearly complete. These claims are tested in CI and hold in live runs:

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
| **R3** | **Batch** | **ETA model** | **4:19 ± 0:06** | 7:47 ± 0:05 | 13.45 ± 0.73 | **1.59 ± 0.06** | 171 ms | **1:22** |

What this says, honestly:

- **Batching is the big win.** Assigning each 2-second batch jointly (Hungarian) instead of first-come-first-served
  cuts mean pickup by 24–30 s (8–10%) and empty km by 9–10%, well outside the seed spread.
- **The learned ETA helps most inside a batch.** R3 is the best row on pickup time and empty km; greedy + ETA (R1)
  is within noise of R0, because with ~5 nearby candidates distance and drive time usually agree on the winner.
  Where they disagree it matters: the dashboard's *Why this driver* panel shows drivers 46 m away by straight
  line but 2 minutes away by Manhattan's one-way streets.
- **The ETA halves the pickup-estimate error** the rider is shown (1:22 vs 2:41 for the distance heuristic),
  even where it doesn't change the driver.
- **No matching policy fixes unmatched riders** (13–14% in every row). The failure is spatial: idle drivers wait
  where their last trip ended. That is what row R4 (demand forecasting + repositioning) is for.
- **Known limit:** the model was trained on cleaned taxi trips, which exclude legs under 300 m / 60 s, so it
  predicts at least ~2 min even for a 10-second pickup leg. Training on pickup-like legs is the next model.

Map data © OpenStreetMap contributors, basemap © CARTO.
