import type { Sizing } from "./api";

/** ``value`` rounded down to a multiple of ``step`` without float dust (mirrors sizing.floor_to_step). */
function floorToStep(value: number, step: number): number {
  const decimals = (String(step).split(".")[1] ?? "").length;
  return Number((Math.floor(value / step + 1e-9) * step).toFixed(decimals));
}

/**
 * Lots for a stop `stop` away from the entry (in price), the way the server sizes setups
 * (see sizing.Sizer.size): the budget over the loss per lot, rounded down to the lot step.
 * Null when the stop is zero or even the minimum lot risks more than the budget.
 */
export function sizeFor(s: Sizing, stop: number): { lots: number; risk: number } | null {
  if (!(stop > 0)) return null;
  const perLot = stop * s.per_point;
  let lots = floorToStep(s.budget / perLot, s.lot_step);
  if (s.max_lot !== null) lots = Math.min(lots, floorToStep(s.max_lot, s.lot_step));
  if (lots < s.min_lot) return null;
  return { lots, risk: lots * perLot };
}
