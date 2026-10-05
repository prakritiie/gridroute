# GridRoute v2. Targets grow with each milestone (train, eval, publish, bench, replay come later).
PY ?= python
DRIVERS ?= 5000
INGEST ?= 2
MATCHERS ?= 2
RIDE_RATE ?= 5

.PHONY: install up down stack osrm gateway sim riders ingest matcher reaper aggregator web data clean congestion replay report features train evaluate bench test audit

install:
	$(PY) -m pip install -e ".[dev]"

up:            ## Redis only
	docker compose up -d --wait redis

stack:         ## everything: gateway :8000, workers, simulator, riders, Prometheus :9090, Grafana :3001
	SIM_DRIVERS=$(DRIVERS) RIDE_RATE=$(RIDE_RATE) docker compose up -d --build --scale ingest=$(INGEST) --scale matcher=$(MATCHERS)

down:
	docker compose --profile osrm down

osrm:          ## one-time: download the NYC extract, build the MLD graph (~1 min), serve on :5001
	docker compose --profile osrm up -d osrm

gateway:       ## host processes below need `make up` (Redis) and run one per terminal
	$(PY) -m uvicorn gateway.app:app --port 8000

sim:
	$(PY) -m sim.main --drivers $(DRIVERS)

riders:
	$(PY) -m sim.riders --rate $(RIDE_RATE)

ingest:
	$(PY) -m workers.ingest

matcher:
	$(PY) -m workers.matcher

reaper:
	$(PY) -m workers.reaper

aggregator:
	$(PY) -m workers.aggregator

web:           ## ops dashboard on :3000 (needs the gateway on :8000)
	cd web && npm run dev

data:          ## Jan-Jun 2016 trips -> $$GRIDROUTE_DATA_DIR/raw (resumable, ~3.5 h), then clean + data card
	$(PY) -m data.download
	$(PY) -m data.clean
	$(PY) -m data.card

clean:         ## re-run the cleaning rules (data/clean.sql) and regenerate docs/data_card.md
	$(PY) -m data.clean
	$(PY) -m data.card

congestion:    ## June congestion factors = the simulator's ground-truth travel times (needs OSRM)
	$(PY) -m data.congestion --sample 500000

SEED ?= 1
replay:        ## R0 protocol: Tue 7 Jun 2016 07-09, 20% demand, 1,600 drivers, x10 (stop the demo sim/riders first)
	$(PY) -m sim.replay --seed $(SEED) --reset

report:        ## results table from every recorded run -> docs/results.md
	$(PY) -m eval.report --write docs/results.md

features:      ## OSRM time + distance for the fixed train/validate/test samples (~30-45 min, needs OSRM)
	$(PY) -m ml.precompute

train:         ## ETA model on the GPU (CUDA LightGBM in ml/Dockerfile.gpu); falls back to CPU if no GPU
	docker compose --profile ml run --rm train python3 -m ml.eta_train

evaluate:      ## June metrics vs B1-B3 by slice -> models/eta/vN/{metrics.json,card.md}, docs/model_card_eta.md
	$(PY) -m ml.evaluate

bench:         ## H3 vs Redis GEO vs PostGIS at 1k-100k drivers -> docs/benchmarks.md (needs the trip data)
	docker compose --profile bench up -d --build postgis bench-api
	$(PY) -m bench.micro
	$(PY) -m bench.k6run
	$(PY) -m bench.report
	docker compose --profile bench stop postgis bench-api

test: up
	$(PY) -m pytest

audit:
	$(PY) -m tools.audit
