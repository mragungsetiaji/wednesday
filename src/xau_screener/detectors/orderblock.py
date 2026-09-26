"""Order blocks for a limit-entry strategy.

Each bullish break of structure (close above the latest unbroken swing high)
produces demand order blocks in the leg between the broken swing and the
breakout candle; bearish breaks are the mirror (supply).

* The order block candle is an **opposite-colour candle**: a red (bearish) candle
  for a buy OB, a green (bullish) candle for a sell OB.
* **Extreme OB** (high priority): the last red candle at or before the lowest low
  of the leg (buy), or the last green candle at or before its highest high (sell):
  the candle the move started from.
* **Middle OBs** (low priority): red candles later in the rally that are followed
  by an impulsive green candle closing above the red body (sell: mirror).
* **Limit plan**: buy limit at the top of the red body, stop at the bottom of the
  body; sell limit at the bottom of the green body, stop at the top. The stop is
  capped at ``max_sl`` price units from the entry.
* **Taken / invalid**: once a later wick trades through the whole body
  (``mitigation="wick"``, the default) or a candle closes beyond it
  (``mitigation="close"``). Touching only the entry keeps the OB valid; those
  candles are counted in ``touches``.

``zone`` picks what the level's top/bottom cover: the body (default, entry to
body stop) or the full candle range; entry and stop always come from the body.
"""

from __future__ import annotations

from ..levels import Level
from ..structure import Context
from .base import Detector

PRIORITY_RANK = {"extreme": 0, "middle": 1}


class OrderBlockDetector(Detector):
    name = "ob"
    title = "Order blocks"

    def detect(self, ctx: Context) -> list[Level]:
        p = self.params
        if len(ctx) < 2 * p.swing_length + 2:
            return []
        o, h, l, c = ctx.open, ctx.high, ctx.low, ctx.close
        red = c < o
        green = c > o

        # candle index -> (priority, direction, break index); extreme wins over middle.
        found: dict[int, tuple[str, str, int]] = {}

        def add(idx: int, priority: str, direction: str, brk_idx: int) -> None:
            prev = found.get(idx)
            if prev is None or PRIORITY_RANK[priority] < PRIORITY_RANK[prev[0]]:
                found[idx] = (priority, direction, brk_idx)

        for brk in ctx.structure(p.swing_length).breaks:
            start, end = brk.swing.index, brk.index
            if brk.direction == "bullish":
                ext = min(range(start, end), key=lambda k: (l[k], -k))  # lowest low of the leg
                opposite, impulsive = red, lambda k: green[k + 1] and c[k + 1] > o[k]
            else:
                ext = max(range(start, end), key=lambda k: (h[k], k))  # highest high of the leg
                opposite, impulsive = green, lambda k: red[k + 1] and c[k + 1] < o[k]

            ob = next((k for k in range(ext, start - 1, -1) if opposite[k]), None)
            if ob is not None:
                add(ob, "extreme", brk.direction, brk.index)
            for k in range(ext + 1, end):
                if opposite[k] and impulsive(k):
                    add(k, "middle", brk.direction, brk.index)

        levels = [self._level(ctx, idx, *info) for idx, info in sorted(found.items())]
        return levels

    def _level(self, ctx: Context, idx: int, priority: str, direction: str, brk_idx: int) -> Level:
        p = self.params
        o, h, l, c, times = ctx.open, ctx.high, ctx.low, ctx.close, ctx.times
        body_top, body_bottom = max(o[idx], c[idx]), min(o[idx], c[idx])
        if direction == "bullish":
            entry, stop = body_top, max(body_bottom, body_top - p.max_sl)
        else:
            entry, stop = body_bottom, min(body_top, body_bottom + p.max_sl)
        top, bottom = (body_top, body_bottom) if p.zone == "body" else (h[idx], l[idx])
        tag = "BULL OB" if direction == "bullish" else "BEAR OB"
        level = Level(
            self.name, direction, float(top), float(bottom), times[idx], times[brk_idx],
            tag if priority == "extreme" else f"{tag} (mid)",
            meta={
                "priority": priority,
                "entry": float(entry),
                "sl": float(stop),
                "risk": round(abs(entry - stop), 10),
                "body": round(body_top - body_bottom, 10),
                "sl_capped": bool(body_top - body_bottom > p.max_sl),
            },
        )

        for j in range(brk_idx + 1, len(c)):
            if direction == "bullish":
                taken = c[j] < body_bottom if p.mitigation == "close" else l[j] <= body_bottom
                touched = l[j] <= entry
            else:
                taken = c[j] > body_top if p.mitigation == "close" else h[j] >= body_top
                touched = h[j] >= entry
            if taken:
                level.ended_time = times[j]
                break
            if touched:
                level.touches += 1
        return level
