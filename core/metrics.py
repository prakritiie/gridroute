"""Prometheus metrics (spec §15). Workers expose them on METRICS_PORT if set; the gateway serves GET /metrics."""
import os

from prometheus_client import Counter, Gauge, Histogram, start_http_server

MS_BUCKETS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)

# Ingest
pings_ingested = Counter("pings_ingested_total", "Pings applied to the cell index")
pings_stale = Counter("pings_stale_total", "Pings rejected by the seq check")
ingest_lag = Histogram("ingest_lag_ms", "Ping timestamp to applied", buckets=MS_BUCKETS)

# Matching
decision_latency = Histogram("decision_latency_ms", "Matcher time per batch", buckets=MS_BUCKETS)
rides_matched = Counter("rides_matched_total", "Rides assigned a driver")
rides_unmatched = Counter("rides_unmatched_total", "Rides given up after max attempts")
claim_conflicts = Counter("claim_conflicts_total", "claim_driver returned 0 (driver lost to another claim)")

# Supply + correctness (aggregator)
drivers_available = Gauge("drivers_available", "Fresh drivers in the cell index")
drivers_busy = Gauge("drivers_busy", "Drivers assigned to a ride")
double_bookings = Gauge("double_bookings", "C1 violations found by the live audit; must be 0")
stream_pending = Gauge("stream_pending", "Entries not yet processed (lag + pending) per stream", ["stream"])
drivers_evicted = Counter("drivers_evicted_total", "Silent drivers removed by the reaper")
offers_expired = Counter("offers_expired_total", "Offers that lapsed without an accept and were re-queued")

# Gateway
rides_requested = Counter("rides_requested_total", "POST /rides accepted")
pings_received = Counter("pings_received_total", "Valid pings enqueued by the gateway")
driver_sockets = Gauge("driver_sockets", "Open /ws/driver connections")
ops_sockets = Gauge("ops_sockets", "Open /ws/ops connections")


def serve_from_env() -> None:
    if port := int(os.environ.get("METRICS_PORT", "0")):
        start_http_server(port)
