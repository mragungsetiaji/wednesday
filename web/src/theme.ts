import { useEffect, useState } from "react";

/** Colors the canvas chart needs (CSS variables can't reach into the canvas). Mirrors styles.css. */
export interface ChartPalette {
  surface: string;
  text: string;
  textStrong: string;
  muted: string;
  grid: string;
  bull: string;
  bear: string;
  liquidity: string;
  idm: string; // neutral: IDM is told apart by its dotted line + label, not a 4th hue
  accent: string; // state only: events, selection
  draw: string; // the trader's own drawings: a hue no detector uses
  mode: "light" | "dark";
}

const DARK: ChartPalette = {
  surface: "#1a1a19",
  text: "#c3c2b7",
  textStrong: "#f4f3ee",
  muted: "#8a897f",
  grid: "#2a2a28",
  bull: "#199e70",
  bear: "#e66767",
  liquidity: "#3987e5",
  idm: "#c3c2b7",
  accent: "#d99a1e",
  draw: "#a78bfa",
  mode: "dark",
};

const LIGHT: ChartPalette = {
  surface: "#fcfcfb",
  text: "#52514e",
  textStrong: "#141413",
  muted: "#8a897f",
  grid: "#ecebe6",
  bull: "#1baf7a",
  bear: "#e34948",
  liquidity: "#2a78d6",
  idm: "#52514e",
  accent: "#8f5b00",
  draw: "#7c3aed",
  mode: "light",
};

const query = () => window.matchMedia("(prefers-color-scheme: light)");

export function useChartPalette(): ChartPalette {
  const [light, setLight] = useState(() => query().matches);
  useEffect(() => {
    const mq = query();
    const onChange = () => setLight(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return light ? LIGHT : DARK;
}

/** hex "#rrggbb" + alpha -> rgba() */
export function withAlpha(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
