"""What happened to an order block traded with the screener's limit plan.

Buy: limit at the top of the body, stop at the bottom (capped at ``max_sl``);
sell: the mirror. The target is ``rr`` times the risk. The order goes in when
the block is known (``start``) and lives for ``horizon`` minutes of M1 bars.

* ``untouched``: price never came back to the entry.
* ``win`` / ``loss``: target or stop first after the fill. On the fill minute
  only the stop counts (a bar can't say whether its high or low came first,
  so the stop is assumed).
* ``open``: filled, neither hit before the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TradePlan:
    rr: float = 2.0
    horizon_minutes: int = 3 * 24 * 60
    max_sl: float = 3.0


def plan_levels(direction: str, top: float, bottom: float, max_sl: float) -> tuple[float, float] | None:
    """(entry, stop) from an order block body; None for a zero-size body."""
    if top <= bottom:
        return None
    if direction == "bullish":
        return top, max(bottom, top - max_sl)
    return bottom, min(top, bottom + max_sl)


def simulate(m1: pd.DataFrame, direction: str, entry: float, stop: float, start: pd.Timestamp,
             plan: TradePlan) -> tuple[str, float | None]:
    """(outcome, result in R): see the module docstring."""
    risk = abs(entry - stop)
    if risk <= 0:
        return "untouched", None
    i0 = m1.index.searchsorted(start)
    i1 = m1.index.searchsorted(start + pd.Timedelta(minutes=plan.horizon_minutes))
    lows = m1["low"].to_numpy()[i0:i1]
    highs = m1["high"].to_numpy()[i0:i1]
    if not len(lows):
        return "untouched", None
    bull = direction == "bullish"
    filled = np.flatnonzero(lows <= entry if bull else highs >= entry)
    if not len(filled):
        return "untouched", None
    f = filled[0]
    target = entry + plan.rr * risk if bull else entry - plan.rr * risk
    stop_hit = np.flatnonzero(lows[f:] <= stop if bull else highs[f:] >= stop)
    target_hit = np.flatnonzero(highs[f + 1:] >= target if bull else lows[f + 1:] <= target) + 1
    first_stop = stop_hit[0] if len(stop_hit) else None
    first_target = target_hit[0] if len(target_hit) else None
    if first_stop is None and first_target is None:
        return "open", None
    if first_target is None or (first_stop is not None and first_stop <= first_target):
        return "loss", -1.0
    return "win", plan.rr
