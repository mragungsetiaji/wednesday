"""The common output of every detector: a price zone (or a line when top == bottom)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime


def unix(t: datetime | None) -> int | None:
    """Seconds since 1970 with a naive feed time read as UTC, as the charts show it."""
    return calendar.timegm(t.utctimetuple()) if t is not None else None


@dataclass
class Level:
    detector: str  # registry name of the detector that produced it, e.g. "ob", "liquidity", "idm"
    kind: str  # detector specific: "bullish"/"bearish" for OB and IDM, "bsl"/"ssl" for liquidity
    top: float
    bottom: float
    time: datetime  # open time of the origin candle (OB candle, swing candle)
    confirmed_time: datetime  # when the level became known (break candle, swing confirmation)
    label: str = ""  # short human name, e.g. "BULL OB", "EQH x3"
    touches: int = 0  # candles that traded into the level without ending it
    ended_time: datetime | None = None  # mitigated / swept
    meta: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.ended_time is None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def to_dict(self) -> dict:
        def ts(t):
            return t.isoformat() if t is not None else None

        return {
            "detector": self.detector,
            "kind": self.kind,
            "label": self.label,
            "top": self.top,
            "bottom": self.bottom,
            "time": ts(self.time),
            "time_unix": unix(self.time),
            "confirmed_time": ts(self.confirmed_time),
            "touches": self.touches,
            "ended_time": ts(self.ended_time),
            "ended_time_unix": unix(self.ended_time),
            "meta": self.meta,
        }
