"""Timeframe definitions and M1 -> higher timeframe resampling."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

M1 = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class Timeframe:
    name: str
    minutes: int

    @property
    def delta(self) -> pd.Timedelta:
        return pd.Timedelta(minutes=self.minutes)

    @property
    def rule(self) -> str:
        return f"{self.minutes}min"


# Ordered from the highest timeframe to the lowest: this is the scan order.
TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe("4H", 240),
    Timeframe("1H", 60),
    Timeframe("30M", 30),
    Timeframe("15M", 15),
    Timeframe("5M", 5),
)

TIMEFRAMES_BY_NAME = {tf.name: tf for tf in TIMEFRAMES}


def parse_timeframes(names: str | list[str]) -> list[Timeframe]:
    """Parse "4H,1H,5M" into Timeframe objects, sorted high -> low."""
    if isinstance(names, str):
        names = [n for n in names.split(",") if n.strip()]
    result = []
    for raw in names:
        key = raw.strip().upper()
        if key not in TIMEFRAMES_BY_NAME:
            raise ValueError(f"Unknown timeframe {raw!r}; choose from {', '.join(TIMEFRAMES_BY_NAME)}")
        result.append(TIMEFRAMES_BY_NAME[key])
    return sorted(set(result), key=lambda tf: tf.minutes, reverse=True)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Return a clean OHLCV frame: lowercase columns, sorted unique DatetimeIndex."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "volume" not in out.columns:
        out["volume"] = 0.0
    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"OHLCV data is missing columns: {missing}")
    if not isinstance(out.index, pd.DatetimeIndex):
        raise TypeError("OHLCV data must be indexed by a DatetimeIndex")
    out = out[OHLCV_COLUMNS].astype(float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def resample_ohlcv(m1: pd.DataFrame, tf: Timeframe, drop_incomplete: bool = True) -> pd.DataFrame:
    """Aggregate closed M1 bars into ``tf`` candles.

    Bars are labelled by their open time and aligned to midnight of the feed's
    clock (so 4H candles open at 00, 04, 08, ... in the feed's timezone, which
    matches the broker's chart when the feed uses broker server time).

    With ``drop_incomplete`` the still-forming last candle is removed, so order
    blocks are only built from closed candles.
    """
    if m1.empty:
        return m1.iloc[0:0]
    agg = m1.resample(tf.rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    agg = agg.dropna(subset=["open", "close"])
    if drop_incomplete:
        # The M1 feed only contains closed bars, so data is known up to last_open + 1min.
        data_end = m1.index[-1] + M1
        agg = agg[agg.index + tf.delta <= data_end]
    return agg
