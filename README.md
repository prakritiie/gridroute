# GridRoute

Real-time ride dispatch on Uber's H3 hexagonal grid. Moving drivers are indexed into H3 cells in Redis,
every multi-key write is an atomic Lua script, and riders are matched without ever double-booking a driver.
Learned ETAs and demand forecasting (LightGBM, trained on real NYC 2016 trips) come next.

Design and plan: [docs/implementation.md](docs/implementation.md).

## Run it

```bash
docker compose up -d --build --scale ingest=2 --scale matcher=2   # Redis, gateway :8000, workers, 5k drivers, riders
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

## Results so far

Real June 2016 demand replayed through the live system (Tue 7 Jun, 07:00–09:00, 20% of real requests,
1,600 drivers, 3 seeds). Pickup time is the simulated *true* drive time: OSRM road time × June congestion,
which the ETA model never trains on. Full protocol and per-run detail: [docs/results.md](docs/results.md).

| Row | Policy | Pickup mean | Pickup p90 | Unmatched % | Empty km / driver-h | Decision p99 |
|---|---|---|---|---|---|---|
| R0 | Greedy, straight-line distance | 4:53 ± 0:04 | 8:09 ± 0:03 | 12.89 ± 0.44 | 1.81 ± 0.01 | 210 ± 33 ms |

The baseline's main failure is spatial, not capacity: in the 5-minute slots where 10% or more of riders gave
up, a median of ~570 of 1,600 drivers were idle, waiting where their last trip ended, more than a 3-ring
search (~1 km) away. Rows R1–R4 (learned ETAs, batch assignment, repositioning) are measured against this.

Map data © OpenStreetMap contributors, basemap © CARTO.
