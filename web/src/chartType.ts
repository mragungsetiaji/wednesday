import { useSharedPref } from "./prefs";

export { heikin, heikinBar } from "./heikin";

/**
 * How the candles are drawn. Heikin Ashi only changes the bars on screen: the scan, the levels,
 * the drawings and the positions all keep using the real candles.
 */
export type ChartType = "candles" | "hollow" | "heikin";

export const CHART_TYPES: { id: ChartType; label: string; icon: "candlestick" | "hollowCandle" | "heikinAshi" }[] = [
  { id: "candles", label: "Candles", icon: "candlestick" },
  { id: "hollow", label: "Hollow candles", icon: "hollowCandle" },
  { id: "heikin", label: "Heikin Ashi", icon: "heikinAshi" },
];

/** The chart type every chart shows, in every window. */
export function useChartType(): [ChartType, (t: ChartType) => void] {
  return useSharedPref<ChartType>("wed.chartType", "candles");
}
