"""Inducement (IDM).

After a break of structure, the first pullback builds a minor (internal) swing
that early traders lean on. Its liquidity is the **inducement**: price usually
sweeps it before reaching the real point of interest (e.g. the order block).

* Bullish BOS -> bullish IDM = first internal swing low that forms after the
  break (a level below price; sweeping it is the bullish pullback trigger).
* Bearish BOS -> bearish IDM = first internal swing high after the break.

Internal swings use ``idm_length`` bars each side (smaller than the external
``swing_length``). Only the most recent IDM per direction is kept; it ends when a
wick trades through it (``meta["grab"]`` as for liquidity).
"""

from __future__ import annotations

from ..levels import Level
from ..structure import Context
from .base import Detector


class InducementDetector(Detector):
    name = "idm"
    title = "Inducement (IDM)"

    def detect(self, ctx: Context) -> list[Level]:
        h, l, c, times = ctx.high, ctx.low, ctx.close, ctx.times
        breaks = ctx.structure(self.params.swing_length).breaks
        internal = ctx.structure(self.params.idm_length).swings

        latest: dict[str, Level] = {}
        for brk in breaks:
            want = "low" if brk.direction == "bullish" else "high"
            sw = next((s for s in internal if s.kind == want and s.index > brk.index), None)
            if sw is None:
                continue
            level = Level(self.name, brk.direction, sw.price, sw.price, times[sw.index], times[sw.confirmed],
                          "IDM" + (" ▲" if brk.direction == "bullish" else " ▼"),
                          meta={"bos_time": times[brk.index].isoformat()})
            prev = latest.get(brk.direction)
            if prev is None or level.time >= prev.time:
                latest[brk.direction] = level

        for level in latest.values():
            start = times.get_loc(level.time) + 1
            for j in range(start, len(c)):
                through = l[j] < level.bottom if level.kind == "bullish" else h[j] > level.top
                if through:
                    level.ended_time = times[j]
                    level.meta["grab"] = bool(c[j] >= level.bottom if level.kind == "bullish" else c[j] <= level.top)
                    break
        return list(latest.values())
