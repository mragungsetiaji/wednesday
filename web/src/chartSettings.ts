import { useSharedPref } from "./prefs";

/** How every chart looks, from the chart settings dialog. A null colour follows the theme. */
export interface ChartSettings {
  bull: string | null;
  bear: string | null;
  grid: "both" | "horz" | "vert" | "none";
  priceLine: boolean; // the dashed line at the last price
  crosshair: "normal" | "magnet"; // magnet: the horizontal line sits on the candle's close
  rightOffset: number; // empty bars kept right of the last candle
}

export const DEFAULT_CHART_SETTINGS: ChartSettings = {
  bull: null,
  bear: null,
  grid: "both",
  priceLine: true,
  crosshair: "normal",
  rightOffset: 12,
};

/** The chart settings, the same on every chart and in every window. */
export function useChartSettings(): [ChartSettings, (s: ChartSettings) => void] {
  const [saved, set] = useSharedPref<Partial<ChartSettings>>("wed.chartSettings", {});
  return [{ ...DEFAULT_CHART_SETTINGS, ...saved }, set];
}
