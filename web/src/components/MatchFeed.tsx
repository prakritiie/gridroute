"use client";

import { clock, dur, num, shortId } from "@/lib/format";
import type { Kpis, Match } from "@/lib/types";

export function MatchFeed({ matches, kpis }: { matches: Match[]; kpis: Kpis | null }) {
  return (
    <aside className="feed" aria-label="Live matches">
      <div className="section__head feed__head">
        <h2>Live matches</h2>
        <span className="muted">{kpis ? `${num(kpis.matched_per_min)} / min` : ""}</span>
      </div>
      {matches.length === 0 ? (
        <div className="empty">
          <p>No matches yet</p>
          <span className="muted">New assignments appear here the moment a driver is claimed.</span>
        </div>
      ) : (
        <ol className="feed__list">
          {matches.map((m, i) => (
            <li key={`${m.ride_id}-${m.at}`} className={`feed__row ${i === 0 ? "is-new" : ""}`}>
              <div className="feed__line">
                <code className="feed__ride">{shortId(m.ride_id)}</code>
                <span className="feed__eta">{dur(m.eta_s)}</span>
              </div>
              <div className="feed__line feed__meta">
                <span>Driver {m.driver_id.toUpperCase()}</span>
                <time>{clock(m.at)}</time>
              </div>
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}
