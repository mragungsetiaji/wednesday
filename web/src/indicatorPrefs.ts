import { useSharedPref } from "./prefs";
import type { Indicator } from "./indicators";

/** The indicators every chart shows, in every window of this browser. */
export function useIndicators(): [Indicator[], (v: Indicator[]) => void] {
  return useSharedPref<Indicator[]>("wed.indicators", []);
}
