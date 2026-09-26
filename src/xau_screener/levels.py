"""The common output of every detector: a price zone (or a line when top == bottom)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Level:
    detector: str  # registry name of the detector that produced it, e.g. "ob", "liquidity", "idm"
    kind: str  # detector specific: "bullish"/"bearish" for OB and IDM, "bsl"/"ssl" for liquidity
    top: float
    bottom: float
    time: pd.Timestamp  # open time of the origin candle (OB candle, swing candle)
    confirmed_time: pd.Timestamp  # when the level became known (break candle, swing confirmation)
    label: str = ""  # short human name, e.g. "BULL OB", "EQH x3"
    touches: int = 0  # candles that traded into the level without ending it
    ended_time: pd.Timestamp | None = None  # mitigated / swept
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

        # Feed times are naive (broker server time); unix values treat them as UTC so charts show them as-is.
        def unix(t):
            return int(t.timestamp()) if t is not None else None

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
