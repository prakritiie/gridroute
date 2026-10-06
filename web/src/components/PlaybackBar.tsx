"use client";

import { Pause, Play } from "lucide-react";

import type { PlaybackControls } from "@/lib/playback";

const SPEEDS = [30, 60, 120];

function mmss(s: number) {
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
}

/** Transport controls for the static demo: scenario, play/pause, scrubber, speed. */
export function PlaybackBar({ p }: { p: PlaybackControls }) {
  const on = p.scenarios.find((s) => s.key === p.scenario);
  return (
    <div className="pbar" role="group" aria-label="Replay controls">
      <div className="pbar__top">
        <span className="pbar__title">
          Recorded replay · {on ? new Date(`${on.day}T12:00:00`).toLocaleDateString("en-US", { weekday: "short", day: "numeric", month: "short" }) : ""}
          {" "}2016 · 20% of real requests, {on?.drivers?.toLocaleString() ?? "1,600"} drivers
        </span>
        <div className="seg seg--sm" role="radiogroup" aria-label="Repositioning">
          {p.scenarios.map((s) => (
            <button key={s.key} role="radio" aria-checked={p.scenario === s.key}
              className={p.scenario === s.key ? "is-active" : ""} onClick={() => p.setScenario(s.key)}>
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <div className="pbar__row">
        <button className="pbar__play" onClick={() => p.setPlaying(!p.playing)} aria-label={p.playing ? "Pause" : "Play"}>
          {p.playing ? <Pause size={16} fill="currentColor" /> : <Play size={16} fill="currentColor" />}
        </button>
        <span className="pbar__clock">{p.simClock}</span>
        <input className="pbar__range" type="range" min={0} max={Math.max(1, p.duration)} step={30} value={p.pos}
          onChange={(e) => p.seek(Number(e.target.value))} aria-label="Replay position"
          aria-valuetext={`${p.simClock} New York time`} />
        <span className="pbar__dur">{mmss(p.duration)}</span>
        <div className="seg seg--sm" role="radiogroup" aria-label="Playback speed">
          {SPEEDS.map((v) => (
            <button key={v} role="radio" aria-checked={p.speed === v} className={p.speed === v ? "is-active" : ""}
              onClick={() => p.setSpeed(v)}>{v}×</button>
          ))}
        </div>
      </div>
    </div>
  );
}
