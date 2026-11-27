import { useSharedPref } from "./prefs.ts";

/** How every chart looks, from the chart settings dialog. A null colour follows the theme. */
export interface ChartSettings {
  bull: string | null; // the up candles' body
  bear: string | null; // the down candles' body
  body: boolean; // fill the bodies
  borders: boolean; // outline the bodies
  borderUp: string | null; // null: the body's colour
  borderDown: string | null;
  wick: boolean;
  wickUp: string | null; // null: the body's colour
  wickDown: string | null;
  prevClose: boolean; // colour a candle up or down by its close against the previous close, not its open
  grid: "both" | "horz" | "vert" | "none";
  priceLine: boolean; // the dashed line at the last price
  crosshair: "normal" | "magnet"; // magnet: the horizontal line sits on the candle's close
  rightOffset: number; // empty bars kept right of the last candle
  quarterColors: "direction" | "quarter"; // the quarterly pane's blocks: green / red by close, or a colour per Q
  quarters: [string, string, string, string]; // Q1 to Q4, when coloured by quarter
}

export const DEFAULT_CHART_SETTINGS: ChartSettings = {
  bull: null,
  bear: null,
  body: true,
  borders: false,
  borderUp: null,
  borderDown: null,
  wick: true,
  wickUp: null,
  wickDown: null,
  prevClose: false,
  grid: "both",
  priceLine: true,
  crosshair: "normal",
  rightOffset: 12,
  quarterColors: "direction",
  quarters: ["#4cbedf", "#e8a144", "#ca8cff", "#f387c1"],
};

/** The chart settings, the same on every chart and in every window. */
export function useChartSettings(): [ChartSettings, (s: ChartSettings) => void] {
  const [saved, set] = useSharedPref<Partial<ChartSettings>>("wed.chartSettings", {});
  return [{ ...DEFAULT_CHART_SETTINGS, ...saved }, set];
}
