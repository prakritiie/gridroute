# GridRoute v2. Targets grow with each milestone (train, eval, publish, bench, replay come later).
PY ?= python
DRIVERS ?= 5000
INGEST ?= 2
MATCHERS ?= 2
RIDE_RATE ?= 5

.PHONY: install up down stack osrm gateway sim riders ingest matcher reaper aggregator web data clean test audit

install:
	$(PY) -m pip install -e ".[dev]"

up:            ## Redis only
	docker compose up -d --wait redis

stack:         ## full M1 loop in containers: gateway :8000, workers, simulator, riders
	SIM_DRIVERS=$(DRIVERS) RIDE_RATE=$(RIDE_RATE) docker compose up -d --build --scale ingest=$(INGEST) --scale matcher=$(MATCHERS)

down:
	docker compose --profile osrm down

osrm:          ## one-time: download NY extract, build the MLD graph, serve on :5001
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

test: up
	$(PY) -m pytest

audit:
	$(PY) -m tools.audit
