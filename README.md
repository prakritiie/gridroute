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

Map data © OpenStreetMap contributors, basemap © CARTO.
