# Static demo site

A static build of the dashboard that plays a recorded replay. It needs no backend, so it can be hosted for
free on Vercel. A visitor sees the real dispatch system's output for Tuesday 7 June 2016, 07:00–09:00
(20% of real yellow-cab requests, 1,600 drivers). They can switch repositioning off and on, scrub the
timeline, change hex size and map layer, and compare every recorded run in the policy lab. Chaos, new
experiments and match explanations need the running system.

## 1. Record (needs the full stack and the data)

```bash
docker compose stop sim riders                      # the replay needs the city to itself
python -m sim.replay --policy batch-eta --reset --seed 2 --playback
python -m sim.replay --policy batch-eta --reposition --reset --seed 2 --playback
docker compose start sim riders
```

Each run writes `playback.json` next to its recording (one frame every 30 sim-seconds, plus every match,
forecast and repositioning cycle). Move these two runs out of `$GRIDROUTE_DATA_DIR/runs/` into
`$GRIDROUTE_DATA_DIR/demo/`. They are not protocol seeds, and `eval/report.py` reads every run in `runs/`.

## 2. Export

```bash
python -m tools.export_demo --off $GRIDROUTE_DATA_DIR/demo/<R3 run> --on $GRIDROUTE_DATA_DIR/demo/<R4 run>
```

This writes `web/public/demo/{index,off,on,runs}.json`, which are committed to the repo.

## 3. Build and check locally

```bash
cd web
NEXT_PUBLIC_DEMO=1 npm run build                    # static export into web/out/
npx serve out                                       # http://localhost:3000
```

## 4. Deploy on Vercel

One-time setup in the Vercel dashboard:

1. **Add New → Project → Import** the GitHub repository. If it is private, grant the Vercel GitHub app
   access to it.
2. **Root Directory:** `web`. **Framework Preset:** Next.js (detected).
3. **Environment Variables:** `NEXT_PUBLIC_DEMO` = `1`.
4. **Deploy.** Every push to `main` redeploys.

Or from a terminal: run `npx vercel login`, then `cd web && npx vercel --prod -e NEXT_PUBLIC_DEMO=1 -b NEXT_PUBLIC_DEMO=1`.
