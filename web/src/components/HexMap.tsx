"use client";

import "maplibre-gl/dist/maplibre-gl.css";

import { MapboxOverlay, type MapboxOverlayProps } from "@deck.gl/mapbox";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { ScatterplotLayer } from "@deck.gl/layers";
import type { PickingInfo } from "@deck.gl/core";
import { useEffect, useMemo, useState } from "react";
import { Map, useControl } from "react-map-gl/maplibre";

import { divColor, imbalance, seqColor, DEMAND_RAMP, SUPPLY_RAMP, type Layer, type RGB } from "@/lib/palette";
import type { Cell, Match } from "@/lib/types";

const STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
const PULSE_MS = 5000;
const CAB: RGB = [247, 181, 0];

function DeckOverlay(props: MapboxOverlayProps) {
  const overlay = useControl(() => new MapboxOverlay(props));
  overlay.setProps(props);
  return null;
}

export interface HoverInfo {
  id: string;
  x: number;
  y: number;
}

interface Props {
  cells: Cell[];
  layer: Layer;
  max: number;
  matches: Match[];
  selected: string | null;
  onSelect: (cell: Cell | null) => void;
  onHover: (h: HoverInfo | null) => void;
}

export default function HexMap({ cells, layer, max, matches, selected, onSelect, onHover }: Props) {
  const [beforeId, setBeforeId] = useState<string | undefined>();
  const [now, setNow] = useState(() => Date.now());

  // Animate only while the newest pulse is alive; each new match restarts the loop
  const latest = matches[0]?.at ?? 0;
  useEffect(() => {
    if (!latest) return;
    let raf = 0;
    const loop = () => {
      const t = Date.now();
      setNow(t);
      if (t - latest < PULSE_MS) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [latest]);

  const visible = useMemo(
    () => cells.filter(([, s, d]) => (layer === "supply" ? s > 0 : layer === "demand" ? d > 0 : s + d > 0)),
    [cells, layer],
  );

  const layers = useMemo(() => {
    const fill = (c: Cell): RGB =>
      layer === "supply" ? seqColor(SUPPLY_RAMP, c[1], max)
        : layer === "demand" ? seqColor(DEMAND_RAMP, c[2], max)
          : divColor(imbalance(c[1], c[2]));
    const sel = visible.find((c) => c[0] === selected) ?? (selected ? ([selected, 0, 0] as Cell) : null);
    // Interleaved mode: draw under the basemap's labels. MapboxOverlay reads `beforeId` off each layer,
    // but @deck.gl/mapbox doesn't export that prop type (LayerOverlayProps), so it goes in via a spread.
    const under: object = { beforeId };
    return [
      new H3HexagonLayer<Cell>({
        id: "hexes",
        data: visible,
        getHexagon: (c) => c[0],
        getFillColor: fill,
        coverage: 0.9,                 // the gap between cells is the "surface gap" between fills
        extruded: false,
        stroked: false,
        opacity: 0.88,
        pickable: true,
        autoHighlight: true,
        highlightColor: [247, 181, 0, 90],
        updateTriggers: { getFillColor: [layer, max] },
        transitions: { getFillColor: 350 },
        ...under,
      }),
      new H3HexagonLayer<Cell>({
        id: "selected",
        data: sel ? [sel] : [],
        getHexagon: (c) => c[0],
        filled: false,
        stroked: true,
        getLineColor: CAB,
        lineWidthUnits: "pixels",
        getLineWidth: 2.5,
        ...under,
      }),
      new ScatterplotLayer<Match>({
        id: "pulses",
        data: matches.filter((m) => now - m.at < PULSE_MS),
        getPosition: (m) => [m.pickup[1], m.pickup[0]],
        stroked: true,
        filled: true,
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        getRadius: (m) => 4 + 26 * Math.min(1, (now - m.at) / PULSE_MS) ** 0.6,
        getFillColor: (m) => [...CAB, Math.round(200 * (1 - (now - m.at) / PULSE_MS) ** 3)] as [number, number, number, number],
        getLineColor: (m) => [...CAB, Math.round(255 * (1 - (now - m.at) / PULSE_MS))] as [number, number, number, number],
        getLineWidth: 2,
        updateTriggers: { getRadius: now, getFillColor: now, getLineColor: now },
      }),
    ];
  }, [visible, layer, max, matches, now, selected, beforeId]);

  return (
    <Map
      initialViewState={{ latitude: 40.738, longitude: -73.955, zoom: window.innerWidth < 860 ? 10.4 : 11.3 }}
      mapStyle={STYLE}
      style={{ position: "absolute", inset: 0 }}
      attributionControl={{ compact: true }}
      onLoad={(e) => setBeforeId(e.target.getStyle().layers.find((l) => l.type === "symbol")?.id)}
    >
      <DeckOverlay
        interleaved
        layers={layers}
        onClick={(info: PickingInfo<Cell>) => onSelect(info.layer?.id === "hexes" && info.object ? info.object : null)}
        onHover={(info: PickingInfo<Cell>) =>
          onHover(info.layer?.id === "hexes" && info.object ? { id: info.object[0], x: info.x, y: info.y } : null)}
      />
    </Map>
  );
}
