import { useEffect, useState } from "react";

/** Colors the canvas chart needs (CSS variables can't reach into the canvas). Mirrors styles.css. */
export interface ChartPalette {
  surface: string;
  text: string;
  muted: string;
  grid: string;
  bull: string;
  bear: string;
  mode: "light" | "dark";
}

const DARK: ChartPalette = {
  surface: "#1a1a19",
  text: "#c3c2b7",
  muted: "#8a897f",
  grid: "#2a2a28",
  bull: "#199e70",
  bear: "#e66767",
  mode: "dark",
};

const LIGHT: ChartPalette = {
  surface: "#fcfcfb",
  text: "#52514e",
  muted: "#8a897f",
  grid: "#ecebe6",
  bull: "#1baf7a",
  bear: "#e34948",
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
