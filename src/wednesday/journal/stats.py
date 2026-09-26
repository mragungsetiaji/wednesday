"""Account numbers, with the drawdown rebuilt from the price.

* **Gain** is time-weighted: each closed trade grows the account by its result
  over the balance it was taken on, and those growths are chained. A deposit
  isn't gain and a withdrawal isn't loss.
* **Absolute gain** is the trading profit over what was deposited.
* **Drawdown** is the deepest fall of the equity (in the same time-weighted terms)
  from its high. Equity is rebuilt minute by minute: every M1 bar a trade was open,
  its floating result at that bar's worst price (the low for a buy, the high for
  a sell). The entry and exit minutes count from the deal price onwards, since
  the bar also holds prices from before the entry or after the exit.
* **Verification**: every deal price is checked against the M1 bar it was made in.
  The broker's deal clock and the price feed's clock can differ by whole hours;
  when the offset isn't set, the one where most deals match is used.

A trade is *verified* when there are bars for its whole life and its deal
prices match them. Unverified trades count at their closed result only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TOLERANCE = 0.0003  # a deal price may sit this share of the price outside its bar (spread, bid vs ask)
EDGE = 300  # seconds of missing bars allowed at either end of a trade
OFFSET_HOURS = range(-14, 15)
SAMPLE = 400  # trades used to find the clock offset


@dataclass
class Bars:
    """M1 bars of one symbol as arrays, on the price feed's clock."""

    t: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    @classmethod
    def of(cls, df: pd.DataFrame) -> Bars:
        t = pd.DatetimeIndex(df.index).as_unit("s").asi8.astype(np.int64)
        return cls(t, *(df[c].to_numpy(float) for c in ("open", "high", "low", "close")))

    def at(self, when: int) -> int | None:
        """Index of the bar containing ``when``, if there is one."""
        i = int(np.searchsorted(self.t, when, "right")) - 1
        return i if i >= 0 and when - self.t[i] < 60 else None


def price_ok(bars: Bars, when: int, price: float) -> bool | None:
    i = bars.at(when)
    if i is None:
        return None
    tol = price * TOLERANCE
    return bool(bars.low[i] - tol <= price <= bars.high[i] + tol)


def _ends(tr: dict) -> list[tuple[int, float]]:
    ends = [(tr["open_time"], tr["open_price"])]
    if tr["close_time"] is not None and tr["close_price"] is not None:
        ends.append((tr["close_time"], tr["close_price"]))
    return ends


def match_rate(trades: list[dict], bars: dict[str, Bars], offset: int) -> tuple[int, int]:
    """(deal prices checked, prices inside their bar) with the deal clock shifted by ``offset`` seconds."""
    checked = ok = 0
    for tr in trades:
        b = bars.get(tr["symbol"])
        if b is None:
            continue
        for when, price in _ends(tr):
            res = price_ok(b, when - offset, price)
            if res is not None:
                checked += 1
                ok += res
    return checked, ok


def detect_offset(trades: list[dict], bars: dict[str, Bars]) -> int | None:
    """The deal-clock minus price-clock offset (whole hours, in seconds) that most deal prices agree with."""
    usable = [t for t in trades if t["symbol"] in bars]
    if not usable:
        return None
    step = max(1, len(usable) // SAMPLE)
    sample = usable[::step]
    best, best_score = None, (0.0, 0)
    for h in sorted(OFFSET_HOURS, key=abs):  # ties go to the smaller shift
        checked, ok = match_rate(sample, bars, h * 3600)
        score = (ok / checked if checked else 0.0, checked)
        if checked >= 2 and score > best_score:
            best, best_score = h * 3600, score
    return best if best is not None and best_score[0] >= 0.6 else None


def multipliers(trades: list[dict]) -> dict[str, float]:
    """Account currency per 1.0 price move per lot, per symbol, read off the trades' own profits."""
    seen: dict[str, list[float]] = {}
    for tr in trades:
        if tr["close_price"] is None:
            continue
        move = (tr["close_price"] - tr["open_price"]) * (1 if tr["side"] == "buy" else -1) * tr["volume"]
        if abs(move) > 1e-9 and tr["profit"]:
            m = tr["profit"] / move
            if m > 0:
                seen.setdefault(tr["symbol"], []).append(m)
    return {s: float(np.median(v)) for s, v in seen.items()}


def _floating(tr: dict, b: Bars, offset: int, mult: float, last: int | None):
    """Per bar: (times on the deal clock, worst floating, floating at the close). None when bars are missing."""
    t0 = tr["open_time"] - offset
    t1 = (tr["close_time"] - offset) if tr["close_time"] is not None else last
    if t1 is None or len(b.t) == 0:
        return None
    i0 = int(np.searchsorted(b.t, t0, "right")) - 1
    i1 = int(np.searchsorted(b.t, t1, "right")) - 1
    if i0 < 0 or t0 - b.t[i0] >= EDGE or i1 < i0 or t1 - b.t[i1] >= EDGE:
        return None
    buy = tr["side"] == "buy"
    worst = (b.low if buy else b.high)[i0 : i1 + 1].copy()
    best = (b.high if buy else b.low)[i0 : i1 + 1].copy()
    mark = b.close[i0 : i1 + 1].copy()
    pick_worse, pick_better = (np.minimum, np.maximum) if buy else (np.maximum, np.minimum)
    entry, exit_ = tr["open_price"], tr["close_price"] if tr["close_time"] is not None else None
    # the entry minute: from the deal price to the bar's close
    worst[0], best[0] = pick_worse(entry, mark[0]), pick_better(entry, mark[0])
    if exit_ is not None:
        start = entry if i1 == i0 else b.open[i1]
        worst[-1], best[-1], mark[-1] = pick_worse(start, exit_), pick_better(start, exit_), exit_
        if i1 == i0:
            worst[-1], best[-1] = pick_worse(worst[-1], entry), pick_better(best[-1], entry)
    sign = 1.0 if buy else -1.0
    k = sign * tr["volume"] * mult
    return (b.t[i0 : i1 + 1] + offset, (worst - entry) * k, (mark - entry) * k, (best - entry) * k)


def _hourly(t: np.ndarray, v: np.ndarray, how: str) -> list[list[float]]:
    if len(t) == 0:
        return []
    s = pd.Series(v).groupby(t // 3600 * 3600).agg(how)
    return [[int(k), round(float(x), 4)] for k, x in s.items()]


def analyse(trades: list[dict], cash: list[dict], bars: dict[str, pd.DataFrame],
            offset: int | None = None) -> dict:
    """Everything the journal page shows. ``bars`` maps a trade symbol to its M1 bars (price clock)."""
    arrays = {s: Bars.of(df) for s, df in bars.items() if df is not None and len(df)}
    detected = offset is None
    if detected:
        offset = detect_offset(trades, arrays)
    mult = multipliers(trades)
    for s in arrays:  # gold without a closed trade to learn from: 100 oz a lot
        if s not in mult and any(k in s.upper() for k in ("XAU", "GOLD")):
            mult[s] = 100.0

    # ---- per trade: price check and floating P/L ----
    rows, pieces = [], []
    total_time = verified_time = 0
    for tr in trades:
        net = tr["profit"] + tr["commission"] + tr["swap"]
        b = arrays.get(tr["symbol"])
        checks = [price_ok(b, w - offset, p) for w, p in _ends(tr)] if b is not None and offset is not None else []
        ok = None if not checks or all(c is None for c in checks) else all(c is not False for c in checks)
        fl = None
        if b is not None and offset is not None and ok is not False and tr["symbol"] in mult:
            fl = _floating(tr, b, offset, mult[tr["symbol"]], int(b.t[-1]) if tr["close_time"] is None else None)
        end = tr["close_time"] if tr["close_time"] is not None else (int(b.t[-1]) + offset if b is not None and offset is not None else tr["open_time"])
        span = max(end - tr["open_time"], 60)
        total_time += span
        row = {**tr, "net": net, "price_ok": ok, "verified": fl is not None, "mae": None, "mfe": None, "floating": None}
        if fl is not None:
            verified_time += span
            t, worst, mark, best = fl
            row["mae"], row["mfe"] = round(float(min(worst.min(), 0.0)), 2), round(float(max(best.max(), 0.0)), 2)
            if tr["close_time"] is None:
                row["floating"] = round(float(mark[-1]), 2)
                row["close_price"] = float(b.close[-1])
            pieces.append((t, worst, mark, tr["open_time"], tr["close_time"]))
        elif tr["close_time"] is None:
            row["floating"] = tr["profit"] + tr["swap"]
        rows.append(row)

    closed = [r for r in rows if r["close_time"] is not None]
    open_ = [r for r in rows if r["close_time"] is None]

    # ---- realised timeline: cash and closed trades ----
    events = sorted([(c["time"], 0, c["amount"], c["kind"]) for c in cash]
                    + [(r["close_time"], 1, r["net"], "trade") for r in closed])
    ev_t = np.array([e[0] for e in events], dtype=np.int64)
    bal_after, fac_after = np.zeros(len(events)), np.ones(len(events))
    balance, factor = 0.0, 1.0
    for i, (_, _, amount, kind) in enumerate(events):
        if kind == "trade" and balance > 0:
            factor *= max(1 + amount / balance, 0.0)
        balance += amount
        bal_after[i], fac_after[i] = balance, factor

    # ---- equity at every minute a verified trade was open ----
    if pieces:
        frame = pd.DataFrame({"t": np.concatenate([p[0] for p in pieces]),
                              "w": np.concatenate([p[1] for p in pieces]),
                              "m": np.concatenate([p[2] for p in pieces])}).groupby("t").sum()
        ft, fw, fm = frame.index.to_numpy(np.int64), frame["w"].to_numpy(), frame["m"].to_numpy()
    else:
        ft, fw, fm = np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
    k = np.searchsorted(ev_t, ft, "left")  # events strictly before the bar
    fbal = np.where(k > 0, bal_after[np.maximum(k - 1, 0)], 0.0)
    ffac = np.where(k > 0, fac_after[np.maximum(k - 1, 0)], 1.0)
    safe = np.where(fbal > 0, fbal, 1.0)
    idx_w = np.where(fbal > 0, ffac * np.maximum(1 + fw / safe, 0.0), ffac)
    idx_m = np.where(fbal > 0, ffac * np.maximum(1 + fm / safe, 0.0), ffac)

    # at each cash or close event: the floating result of the other trades still open then
    ev_float = np.zeros(len(events))
    for t, _, mark, opened, closes in pieces:
        live = (ev_t >= opened) & (ev_t < (closes if closes is not None else np.iinfo(np.int64).max))
        if live.any():
            j = np.searchsorted(t, ev_t[live], "right") - 1
            ev_float[live] += np.where(j >= 0, mark[np.maximum(j, 0)], 0.0)
    safe_ev = np.where(bal_after > 0, bal_after, 1.0)
    ev_idx = np.where(bal_after > 0, fac_after * np.maximum(1 + ev_float / safe_ev, 0.0), fac_after)

    pt = np.concatenate([ev_t, ft])
    order = np.argsort(pt, kind="stable")
    pt, pw, pm = pt[order], np.concatenate([ev_idx, idx_w])[order], np.concatenate([ev_idx, idx_m])[order]
    peq = np.concatenate([bal_after + ev_float, fbal + fw])[order]
    if len(pt):
        peak = np.maximum.accumulate(np.concatenate([[1.0], pm[:-1]]))
        peak = np.maximum(peak, 1e-12)
        dd = np.clip(1 - pw / peak, 0, 1)
        worst_i = int(np.argmax(dd))
        max_dd, dd_at = float(dd[worst_i]), int(pt[worst_i])
    else:
        dd, max_dd, dd_at = np.zeros(0), 0.0, None

    # ---- summary ----
    deposits = sum(c["amount"] for c in cash if c["kind"] == "deposit")
    withdrawals = -sum(c["amount"] for c in cash if c["kind"] == "withdrawal")
    profit = sum(r["net"] for r in closed)
    floating = sum(r["floating"] or 0.0 for r in open_)
    gain = factor - 1
    start = min([c["time"] for c in cash] + [r["open_time"] for r in rows], default=None)
    last = max([c["time"] for c in cash] + [r["close_time"] for r in closed] + [r["open_time"] for r in open_], default=None)
    days = max(((last or 0) - (start or 0)) / 86400, 1.0)
    wins = [r["net"] for r in closed if r["net"] > 0]
    losses = [r["net"] for r in closed if r["net"] < 0]
    verified = sum(r["verified"] for r in rows)
    checked, matched = match_rate(trades, arrays, offset) if offset is not None else (0, 0)

    def pct(x):
        return round(x * 100, 2)

    return {
        "summary": {
            "gain": pct(gain), "abs_gain": pct(profit / deposits) if deposits else None,
            "daily": pct((1 + gain) ** (1 / days) - 1) if gain > -1 else None,
            "monthly": pct((1 + gain) ** (30.4375 / days) - 1) if gain > -1 else None,
            "drawdown": pct(max_dd), "drawdown_at": dd_at,
            "balance": round(balance, 2), "equity": round(balance + floating, 2), "floating": round(floating, 2),
            "profit": round(profit, 2), "deposits": round(deposits, 2), "withdrawals": round(withdrawals, 2),
            "credit": round(sum(c["amount"] for c in cash if c["kind"] == "credit"), 2),
            "other": round(sum(c["amount"] for c in cash if c["kind"] == "other"), 2),
            "max_floating_loss": round(float(min(fw.min(), 0.0)), 2) if len(fw) else None,
            "start": start, "last": last,
        },
        "trading": {
            "trades": len(closed), "open": len(open_), "won": len(wins), "lost": len(losses),
            "win_rate": pct(len(wins) / len(closed)) if closed else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "best": round(max(wins), 2) if wins else None, "worst": round(min(losses), 2) if losses else None,
            "lots": round(sum(r["volume"] for r in closed), 2),
            "commission": round(sum(r["commission"] for r in rows), 2), "swap": round(sum(r["swap"] for r in rows), 2),
            "avg_hold": int(np.mean([r["close_time"] - r["open_time"] for r in closed])) if closed else None,
        },
        "verification": {
            "basis": "ohlc" if verified else "closed",
            "trades": len(rows), "verified": int(verified),
            "coverage": pct(verified_time / total_time) if total_time else None,
            "prices_checked": checked, "prices_ok": matched,
            "offset_hours": offset / 3600 if offset is not None else None, "offset_detected": detected,
            "no_prices": sorted({r["symbol"] for r in rows} - arrays.keys()),
            "mismatched": [r["id"] for r in rows if r["price_ok"] is False][:50],
        },
        "series": {
            "growth": [[int(t), round((f - 1) * 100, 4)] for t, f in zip(ev_t, fac_after)],
            "balance": [[int(t), round(float(b), 2)] for t, b in zip(ev_t, bal_after)],
            "equity": _hourly(pt, peq, "min"),
            "drawdown": _hourly(pt, -dd * 100, "min"),
        },
        "trades": rows,
    }
