"use client";

import { useEffect, useRef, useState } from "react";
import type { ChaosCommand, Forecast, HexSnapshot, Kpis, Link, Match, Reposition } from "./types";

// 127.0.0.1, not localhost: on Windows, WSL can hold [::1]:8000 and a browser that tries IPv6 first hangs there.
export const GATEWAY_WS = process.env.NEXT_PUBLIC_GATEWAY_WS ?? "ws://127.0.0.1:8000/ws/ops";

const HISTORY = 300;       // 5 min of 1 Hz KPI frames for the sparklines
const MATCH_LOG = 40;

export interface OpsState {
  link: Link;
  everConnected: boolean;
  hexes: HexSnapshot | null;
  kpis: Kpis | null;
  history: Kpis[];
  matches: Match[];
  forecast: Forecast | null;
  reposition: Reposition | null;
  chaos: ChaosCommand | null;
}

/** Live feed from the gateway's /ws/ops. Reconnects with capped backoff; never throws into the UI. */
export function useOps(url: string = GATEWAY_WS): OpsState {
  const [state, setState] = useState<OpsState>({
    link: "connecting", everConnected: false, hexes: null, kpis: null, history: [], matches: [],
    forecast: null, reposition: null, chaos: null,
  });
  const retry = useRef(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(url);
      ws.onopen = () => {
        retry.current = 0;
        setState((s) => ({ ...s, link: "live", everConnected: true }));
      };
      ws.onmessage = (ev) => {
        let msg: { type: string; data: unknown };
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "hexes") {
          setState((s) => ({ ...s, hexes: msg.data as HexSnapshot }));
        } else if (msg.type === "kpis") {
          const k = msg.data as Kpis;
          setState((s) => ({ ...s, kpis: k, history: [...s.history.slice(-(HISTORY - 1)), k] }));
        } else if (msg.type === "match") {
          const m = { ...(msg.data as Omit<Match, "at">), at: Date.now() };
          setState((s) => ({ ...s, matches: [m, ...s.matches].slice(0, MATCH_LOG) }));
        } else if (msg.type === "forecast") {
          setState((s) => ({ ...s, forecast: msg.data as Forecast }));
        } else if (msg.type === "chaos") {
          setState((s) => ({ ...s, chaos: msg.data as ChaosCommand }));
        } else if (msg.type === "reposition") {
          setState((s) => ({ ...s, reposition: { ...(msg.data as Omit<Reposition, "at">), at: Date.now() } }));
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setState((s) => ({ ...s, link: "reconnecting" }));
        const delay = Math.min(8000, 500 * 2 ** retry.current++);
        timer = setTimeout(connect, delay);
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [url]);

  return state;
}

/** REST base of the same gateway (ws://host:8000/ws/ops -> http://host:8000). */
export const GATEWAY_HTTP = GATEWAY_WS.replace(/^ws/, "http").replace(/\/ws\/ops$/, "");
