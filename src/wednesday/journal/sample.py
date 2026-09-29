"""The sample portfolio: a made-up XAUUSD account that shows what a filled journal looks like.

Deterministic (a fixed seed), so tests and the docs screenshot come out the same
every time. It makes the M1 bars along with the trades, since the drawdown is
rebuilt from M1 and nobody's stored history covers these dates. The bars live
under their own source key (``sample``), so the scanner, the Lab and real
journals never read them. Every deal price sits inside its minute's bar.

The numbers are synthetic: no account, strategy or broker produced them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SOURCE = "sample"  # the bar stream's source key, next to "mt5", "yahoo", ...
SYMBOL = "XAUUSD"
SEED = 22
START = "2026-03-02"  # a Monday
END = "2026-07-31"  # a Friday, inclusive
FIRST_PRICE = 2320.0
LOT = 100.0  # money per 1.00 move per lot (100 oz)
COMMISSION = 7.0  # per lot, round turn
SWAP = 18.0  # per lot per night held past 21:00
EDGE = 0.53  # how often the side agrees with the next hour's move
CASH = [  # (when, kind, amount)
    ("2026-03-02 00:30", "deposit", 5000.0),
    ("2026-04-14 09:05", "deposit", 2500.0),
    ("2026-06-29 17:40", "withdrawal", -1500.0),
]


def _bars(rng: np.random.Generator) -> pd.DataFrame:
    """Weekday M1 bars from START to END: a random walk, livelier in London and New York hours."""
    days = pd.bdate_range(START, END)
    t = days.as_unit("s").asi8[:, None] + np.arange(1440)[None, :] * 60
    t = t.ravel()
    minute = (t // 60) % 1440
    hour = minute / 60
    vol = 0.16 + 0.22 * ((hour >= 7) & (hour < 11)) + 0.30 * ((hour >= 12.5) & (hour < 16)) - 0.06 * (hour >= 21)
    steps = rng.normal(0.0015, 1.0, len(t)) * vol
    close = FIRST_PRICE + np.cumsum(steps)
    open_ = np.concatenate([[FIRST_PRICE], close[:-1]])
    wick = np.abs(rng.normal(0, 0.45, (2, len(t)))) * vol[None, :]
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    volume = np.round(rng.gamma(2.0, 40.0, len(t)) * vol * 5)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                      index=pd.DatetimeIndex(pd.to_datetime(t, unit="s")).as_unit("s"))
    return df.round({"open": 2, "high": 2, "low": 2, "close": 2}).pipe(_tidy)


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    """Keep high and low around open and close after rounding."""
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df


def _price(rng: np.random.Generator, low: float, high: float) -> float:
    return round(float(rng.uniform(low, high)), 2)


def generate(seed: int = SEED) -> dict:
    """{"trades", "cash", "bars"}: trades and cash rows as the journal stores them, and the M1 bars."""
    rng = np.random.default_rng(seed)
    bars = _bars(rng)
    t = bars.index.as_unit("s").asi8
    o, h, lo, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    cash = [{"id": f"c{i + 1}", "time": int(pd.Timestamp(when).timestamp()), "kind": kind, "amount": amount,
             "comment": "Sample " + kind} for i, (when, kind, amount) in enumerate(CASH)]
    flows = sorted((row["time"], row["amount"]) for row in cash)

    trades: list[dict] = []
    balance_at = lambda when: sum(a for w, a in flows if w <= when) + sum(  # noqa: E731
        x["profit"] + x["commission"] + x["swap"] for x in trades if x["close_time"] <= when)
    day_starts = np.nonzero(((t // 60) % 1440 == 0))[0]
    for d0 in day_starts:
        if t[d0] < flows[0][0]:
            continue
        n = rng.poisson(1.9)
        for m in sorted(rng.integers(60, 19 * 60, n)):
            i = int(d0 + m)
            hold = int(rng.choice([rng.integers(8, 60), rng.integers(60, 240), rng.integers(240, 720)], p=[0.45, 0.4, 0.15]))
            ahead = c[min(i + 60, len(c) - 1)] - c[i]
            up = (ahead >= 0) == (rng.random() < EDGE)
            stop = float(rng.choice([rng.uniform(3, 7), rng.uniform(7, 14), rng.uniform(18, 30)], p=[0.35, 0.5, 0.15]))
            target = stop * float(rng.uniform(0.7, 1.8))
            size_stop = stop
            if rng.random() < 0.12:  # held through a deep loss for a small win: the drawdown shows it, the statement doesn't
                stop, target, hold = float(rng.uniform(25, 40)), float(rng.uniform(3, 8)), int(rng.integers(240, 900))
                size_stop = float(rng.uniform(4, 7))  # sized as if the stop were a tight one
            entry = _price(rng, lo[i], h[i])
            sl, tp = (entry - stop, entry + target) if up else (entry + stop, entry - target)
            j, exit_ = min(i + hold, len(c) - 1), None
            for k in range(i + 1, min(i + hold, len(c) - 1) + 1):
                if t[k] - t[k - 1] > 60:  # the weekend: close on Friday's last bar
                    j = k - 1
                    break
                hit_sl = lo[k] <= sl if up else h[k] >= sl
                hit_tp = h[k] >= tp if up else lo[k] <= tp
                if hit_sl or hit_tp:
                    level = sl if hit_sl else tp  # both in one bar: the stop, to be safe
                    below = (up and hit_sl) or (not up and not hit_sl)  # the level is under the price
                    gapped = o[k] <= level if below else o[k] >= level
                    j, exit_ = k, round(float(o[k] if gapped else min(max(level, lo[k]), h[k])), 2)
                    break
            if exit_ is None:
                exit_ = _price(rng, lo[j], h[j])
            if j <= i:
                continue
            open_time = int(t[i] + rng.integers(0, 60))
            close_time = int(t[j] + rng.integers(0, 60))
            balance = balance_at(open_time)
            risk = float(rng.choice([0.006, 0.01, 0.015]))
            volume = float(np.clip(round(balance * risk / (size_stop * LOT), 2), 0.01, 5.0))
            move = (exit_ - entry) * (1 if up else -1)
            nights = int((close_time - 21 * 3600) // 86400 - (open_time - 21 * 3600) // 86400)
            pos = str(40_000_000 + len(trades) * 7 + 3)
            trades.append({
                "id": f"p{pos}", "position": pos, "symbol": SYMBOL, "side": "buy" if up else "sell", "volume": volume,
                "open_time": open_time, "open_price": entry, "close_time": close_time, "close_price": exit_,
                "profit": round(move * volume * LOT, 2), "commission": round(-COMMISSION * volume, 2),
                "swap": round(-SWAP * volume * nights, 2), "sl": round(sl, 2), "tp": round(tp, 2), "comment": None,
            })
    return {"trades": trades, "cash": cash, "bars": bars}
