"""Buy-side and sell-side liquidity (BSL / SSL).

Stops rest above swing highs (**BSL**) and below swing lows (**SSL**). Every
confirmed swing that price has not traded through yet is an active liquidity
level. Active swings within ``eq_tolerance x ATR(14)`` of each other are merged
into one **equal highs (EQH) / equal lows (EQL)** pool, a stronger magnet.

A level ends when a wick trades through it. ``meta["grab"]`` tells whether that
candle closed back inside (a liquidity grab / sweep) or beyond (a break).
"""

from __future__ import annotations

from ..levels import Level
from ..structure import Context, Swing
from .base import Detector


class LiquidityDetector(Detector):
    name = "liquidity"
    title = "Liquidity (BSL / SSL)"

    def detect(self, ctx: Context) -> list[Level]:
        h, l, c, times = ctx.high, ctx.low, ctx.close, ctx.times
        ended: list[Level] = []
        active: dict[str, list[tuple[Swing, Level]]] = {"bsl": [], "ssl": []}

        for sw in ctx.structure(self.params.swing_length).swings:
            kind = "bsl" if sw.kind == "high" else "ssl"
            level = Level(self.name, kind, sw.price, sw.price, times[sw.index], times[sw.confirmed],
                          "BSL" if kind == "bsl" else "SSL")
            # Bars up to the pivot's confirmation cannot exceed it by definition of a pivot.
            for j in range(sw.index + 1, len(c)):
                through = h[j] > sw.price if kind == "bsl" else l[j] < sw.price
                if through:
                    level.ended_time = times[j]
                    level.meta["grab"] = bool(c[j] <= sw.price if kind == "bsl" else c[j] >= sw.price)
                    break
            if level.active:
                active[kind].append((sw, level))
            else:
                ended.append(level)

        pools = self._merge_equal(active["bsl"], ctx) + self._merge_equal(active["ssl"], ctx)
        return ended + pools

    def _merge_equal(self, items: list[tuple[Swing, Level]], ctx: Context) -> list[Level]:
        """Group active swings whose prices are within the tolerance into one pool."""
        if not items:
            return []
        items = sorted(items, key=lambda it: it[0].price)
        groups: list[list[tuple[Swing, Level]]] = [[items[0]]]
        for sw, lvl in items[1:]:
            prev_sw = groups[-1][-1][0]
            tol = self.params.eq_tolerance * float(ctx.atr[max(sw.index, prev_sw.index)])
            if sw.price - prev_sw.price <= tol:
                groups[-1].append((sw, lvl))
            else:
                groups.append([(sw, lvl)])

        out = []
        for group in groups:
            if len(group) == 1:
                out.append(group[0][1])
                continue
            swings = [sw for sw, _ in group]
            kind = group[0][1].kind
            first = min(swings, key=lambda s: s.index)
            last_confirmed = max(swings, key=lambda s: s.confirmed)
            label = f"{'EQH' if kind == 'bsl' else 'EQL'} x{len(group)}"
            out.append(Level(self.name, kind, max(s.price for s in swings), min(s.price for s in swings),
                             ctx.times[first.index], ctx.times[last_confirmed.confirmed], label,
                             meta={"equal": len(group)}))
        return out
