"""Order blocks (Smart Money Concepts style).

On a bullish break of structure (close above the latest unbroken swing high),
the **bullish order block** is the candle with the lowest low between that
swing high and the breakout candle: the last sell-off before the impulsive
move up (demand). Bearish is the mirror (supply). An order block is
**mitigated** once price closes through it (``mitigation="wick"``: any wick).
"""

from __future__ import annotations

from ..levels import Level
from ..structure import Context
from .base import Detector


class OrderBlockDetector(Detector):
    name = "ob"
    title = "Order blocks"

    def detect(self, ctx: Context) -> list[Level]:
        p = self.params
        if len(ctx) < 2 * p.swing_length + 2:
            return []
        o, h, l, c, times = ctx.open, ctx.high, ctx.low, ctx.close, ctx.times

        def bounds(i: int) -> tuple[float, float]:
            if p.zone == "body":
                return max(o[i], c[i]), min(o[i], c[i])
            return h[i], l[i]

        blocks: list[tuple[int, Level]] = []
        for brk in ctx.structure(p.swing_length).breaks:
            leg = range(brk.swing.index, brk.index)
            if brk.direction == "bullish":
                ob_idx = min(leg, key=lambda k: (l[k], -k))
            else:
                ob_idx = max(leg, key=lambda k: (h[k], k))
            top, bottom = bounds(ob_idx)
            kind = brk.direction
            label = "BULL OB" if kind == "bullish" else "BEAR OB"
            blocks.append((brk.index, Level(self.name, kind, float(top), float(bottom),
                                            times[ob_idx], times[brk.index], label)))

        for break_idx, ob in blocks:
            for j in range(break_idx + 1, len(c)):
                if ob.kind == "bullish":
                    breach = (c[j] if p.mitigation == "close" else l[j]) < ob.bottom
                    touched = l[j] <= ob.top
                else:
                    breach = (c[j] if p.mitigation == "close" else h[j]) > ob.top
                    touched = h[j] >= ob.bottom
                if breach:
                    ob.ended_time = times[j]
                    break
                if touched:
                    ob.touches += 1
        return [ob for _, ob in blocks]
