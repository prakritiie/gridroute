// Nearby lookup under load: constant arrival rate (open model), so a slow backend can't hide its latency
// by slowing the load down (coordinated omission). One backend per run: -e BACKEND=h3|geo|postgis.
import http from "k6/http";
import { check } from "k6";
import { SharedArray } from "k6/data";

const BASE = __ENV.BASE || "http://bench-api:8100";
const BACKEND = __ENV.BACKEND || "h3";
const RATE = parseInt(__ENV.RATE || "500", 10);
const DURATION = __ENV.DURATION || "20s";

// Real June 2016 pickup points, written by bench/k6run.py
const points = new SharedArray("points", () => JSON.parse(open("/scripts/points.json")));

export const options = {
  discardResponseBodies: false,
  scenarios: {
    near: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 50,
      maxVUs: 400,
    },
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(99)", "max"],
};

export default function () {
  const p = points[Math.floor(Math.random() * points.length)];
  const res = http.get(`${BASE}/near/${BACKEND}?lat=${p[0]}&lng=${p[1]}`, { tags: { backend: BACKEND } });
  check(res, { "200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  const out = {};
  out[`/scripts/out/${BACKEND}-${__ENV.SIZE || "n"}.json`] = JSON.stringify(data);
  return out;
}
