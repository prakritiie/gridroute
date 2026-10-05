"""Benchmark API: the three nearby-lookup backends behind one identical HTTP shape, for k6 (spec arrow 5).

    GET /near/{h3|geo|postgis}?lat=..&lng=..   -> {"drivers": [...]}

Identical FastAPI + JSON overhead on every route, so differences between routes are the backend's.
Data is loaded beforehand by `python -m bench.k6run` (same real June positions as bench/micro.py).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from bench.backends import BACKENDS

app = FastAPI(title="GridRoute nearby-lookup bench")
_backends = {name: cls() for name, cls in BACKENDS.items()}


@app.get("/near/{backend}")
def near(backend: str, lat: float, lng: float) -> dict:
    b = _backends.get(backend)
    if b is None:
        raise HTTPException(404, "unknown backend")
    return {"drivers": b.near(lat, lng)}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
