"""Turn MT5 history into trades and cash.

Two ways in:

* :func:`from_deals`: deals as the terminal's ``history_deals_get`` returns them
  (plus ``positions_get`` for what is still open).
* :func:`parse_report`: the history report the terminal saves (History tab, right
  click, Report, HTML). Its *Positions* table gives the trades and its *Deals*
  table the deposits and withdrawals.

Times stay on the broker's server clock, as unix seconds.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser

# MT5 deal types and entries (ENUM_DEAL_TYPE, ENUM_DEAL_ENTRY)
BUY, SELL, BALANCE, CREDIT, BONUS = 0, 1, 2, 3, 6
IN, OUT, INOUT, OUT_BY = 0, 1, 2, 3


class ReportError(ValueError):
    """A file or terminal answer that can't be read, with a message for the user."""


def _get(deal, name, default=None):
    return deal.get(name, default) if isinstance(deal, dict) else getattr(deal, name, default)


def cash_kind(deal_type: int, amount: float) -> str:
    if deal_type == BALANCE:
        return "deposit" if amount >= 0 else "withdrawal"
    if deal_type in (CREDIT, BONUS):
        return "credit"
    return "other"  # charges, corrections, interest, fees billed as their own deal


def from_deals(deals, positions=()) -> tuple[list[dict], list[dict]]:
    """(trades, cash) from MT5 deal records (namedtuples or dicts).

    A position is split into one trade per closing deal, with the average entry
    price and the entry commission shared by volume, so partial closes keep the
    floating P/L right. What is still open becomes a trade without a close time.
    """
    cash: list[dict] = []
    by_position: dict[str, list] = defaultdict(list)
    for d in deals:
        kind = int(_get(d, "type"))
        if kind in (BUY, SELL):
            by_position[str(_get(d, "position_id"))].append(d)
        elif kind not in (13, 14):  # buy/sell canceled
            amount = float(_get(d, "profit", 0)) + float(_get(d, "commission", 0)) + float(_get(d, "fee", 0) or 0) \
                + float(_get(d, "swap", 0))
            cash.append({"id": str(_get(d, "ticket")), "time": int(_get(d, "time")), "kind": cash_kind(kind, amount),
                         "amount": amount, "comment": _get(d, "comment") or None})

    open_now = {str(_get(p, "ticket")): p for p in positions}
    trades: list[dict] = []
    for pos, legs in by_position.items():
        legs.sort(key=lambda d: (int(_get(d, "time_msc", 0) or 0) or int(_get(d, "time")) * 1000, int(_get(d, "ticket"))))
        side, symbol = None, str(_get(legs[0], "symbol"))
        vol = avg = 0.0
        open_time = None
        entry_cost = entry_volume = 0.0  # commission paid on the way in, shared among the closes

        def enter(t, price, volume, cost):
            nonlocal vol, avg, open_time, entry_cost, entry_volume
            if vol == 0:
                open_time, entry_cost, entry_volume = t, 0.0, 0.0
            avg = (avg * vol + price * volume) / (vol + volume)
            vol += volume
            entry_cost += cost
            entry_volume += volume

        for d in legs:
            t, price, volume = int(_get(d, "time")), float(_get(d, "price")), float(_get(d, "volume"))
            cost = float(_get(d, "commission", 0)) + float(_get(d, "fee", 0) or 0)
            entry = int(_get(d, "entry"))
            deal_side = "buy" if int(_get(d, "type")) == BUY else "sell"
            if entry == IN or side is None:
                side = side or deal_side
                enter(t, price, volume, cost)
                continue
            closed = min(volume, vol) if entry == INOUT else volume
            share = closed / entry_volume if entry_volume else 0.0
            trades.append({
                "id": f"p{pos}:{_get(d, 'ticket')}", "position": pos, "symbol": symbol, "side": side,
                "volume": closed, "open_time": open_time, "open_price": avg, "close_time": t, "close_price": price,
                "profit": float(_get(d, "profit", 0)), "commission": cost * (closed / volume if volume else 1)
                + entry_cost * share, "swap": float(_get(d, "swap", 0)), "sl": None, "tp": None,
                "comment": _get(d, "comment") or None,
            })
            entry_cost -= entry_cost * share
            entry_volume -= closed
            vol = max(vol - closed, 0.0)
            if vol <= 1e-9:
                vol, avg = 0.0, 0.0
                if entry == INOUT and volume > closed:  # reversed: the rest opens the other way
                    side = deal_side
                    enter(t, price, volume - closed, cost * (volume - closed) / volume)
        if vol > 1e-9:
            p = open_now.get(pos)
            trades.append({
                "id": f"p{pos}", "position": pos, "symbol": symbol, "side": side, "volume": vol,
                "open_time": open_time, "open_price": avg, "close_time": None,
                "close_price": float(_get(p, "price_current")) if p is not None else None,
                "profit": float(_get(p, "profit", 0)) if p is not None else 0.0, "commission": entry_cost,
                "swap": float(_get(p, "swap", 0)) if p is not None else 0.0,
                "sl": (float(_get(p, "sl")) or None) if p is not None else None,
                "tp": (float(_get(p, "tp")) or None) if p is not None else None,
                "comment": None,
            })
    trades.sort(key=lambda r: (r["open_time"], r["id"]))
    cash.sort(key=lambda r: (r["time"], r["id"]))
    return trades, cash


# ---- the terminal's HTML report ----------------------------------------------------------------

SECTIONS = {"positions", "orders", "deals", "open positions", "working orders", "results", "summary"}


class _Rows(HTMLParser):
    """Every table row as a list of cell texts, colspans expanded, hidden cells dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1
        self._hidden = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._hidden = "hidden" in (a.get("class") or "").split()
            try:
                self._span = max(1, int(a.get("colspan") or 1))
            except ValueError:
                self._span = 1

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            if not self._hidden:
                text = " ".join("".join(self._cell).split())
                self._row.extend([text] + [""] * (self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if len(data) > 1 and data[1:2] == b"\x00":
        return data.decode("utf-16-le")
    return data.decode("utf-8-sig", errors="replace")


def _num(text: str) -> float:
    t = text.replace("\xa0", "").replace(" ", "").replace(",", "")
    if not t:
        return 0.0
    try:
        return float(t)
    except ValueError as exc:
        raise ReportError(f"Couldn't read the number {text!r} in the report") from exc


def _time(text: str) -> int:
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ReportError(f"Couldn't read the time {text!r} in the report")


def _header(row: list[str]) -> dict[str, int]:
    """Column name -> index; a repeated name gets a "close " prefix (Positions has Time and Price twice)."""
    cols: dict[str, int] = {}
    for i, name in enumerate(row):
        key = name.strip().lower().replace(" / ", "/")
        if not key:
            continue
        cols["close " + key if key in cols else key] = i
    return cols


def parse_report(data: bytes) -> dict:
    """``{"account": {...}, "trades": [...], "cash": [...]}`` from an MT5 history report (HTML)."""
    text = _decode(data)
    if "<table" not in text.lower():
        raise ReportError("That isn't an MT5 report. In the terminal: History tab, right click, Report, HTML")
    parser = _Rows()
    parser.feed(text)
    rows = parser.rows

    account: dict = {}
    for row in rows[:20]:
        cells = [c for c in row if c]
        for i, c in enumerate(cells[:-1]):
            label = c.rstrip(":").lower()
            if label == "account":
                m = re.match(r"\s*(\d+)(?:\s*\(([^)]*)\))?", cells[i + 1])
                if m:
                    account["login"] = m.group(1)
                    parts = [p.strip() for p in (m.group(2) or "").split(",")]
                    if parts and re.fullmatch(r"[A-Z]{3}", parts[0] or ""):
                        account["currency"] = parts[0]
                    if len(parts) > 1 and parts[1]:
                        account["server"] = parts[1]
            elif label == "company":
                account["company"] = cells[i + 1]
            elif label == "name":
                account["name"] = cells[i + 1]

    sections: dict[str, list[dict]] = {}
    current, cols = None, None
    for row in rows:
        cells = [c for c in row if c]
        if len(cells) == 1 and cells[0].lower() in SECTIONS:
            current, cols = cells[0].lower(), None
            continue
        if current is None:
            continue
        if cols is None:
            cols = _header(row)
            continue
        if len(cells) < 4:  # totals line under a table
            continue
        sections.setdefault(current, []).append({k: row[i] if i < len(row) else "" for k, i in cols.items()})

    positions, deals = sections.get("positions", []), sections.get("deals", [])
    if not positions and not deals:
        raise ReportError("No Positions or Deals table in the report. Save it from the History tab as HTML")

    trades = []
    for p in positions:
        side = p.get("type", "").lower()
        if side not in ("buy", "sell") or not p.get("close time"):
            continue
        commission = _num(p.get("commission", "")) + _num(p.get("fee", ""))
        trades.append({
            "id": f"p{p.get('position')}", "position": p.get("position", ""), "symbol": p.get("symbol", ""),
            "side": side, "volume": _num(p.get("volume", "").split("/")[0]),
            "open_time": _time(p["time"]), "open_price": _num(p.get("price", "")),
            "close_time": _time(p["close time"]), "close_price": _num(p.get("close price", "")),
            "profit": _num(p.get("profit", "")), "commission": commission, "swap": _num(p.get("swap", "")),
            "sl": _num(p.get("s/l", "")) or None, "tp": _num(p.get("t/p", "")) or None, "comment": None,
        })

    cash = []
    names = {"balance": BALANCE, "credit": CREDIT, "bonus": BONUS}
    for d in deals:
        kind = d.get("type", "").lower()
        if kind in ("buy", "sell") or not kind:
            continue
        amount = _num(d.get("profit", "")) + _num(d.get("commission", "")) + _num(d.get("fee", "")) + _num(d.get("swap", ""))
        cash.append({"id": d.get("deal") or f"{d.get('time')}:{amount}", "time": _time(d["time"]),
                     "kind": cash_kind(names.get(kind, -1), amount), "amount": amount,
                     "comment": d.get("comment") or None})

    if not trades and not cash:
        raise ReportError("The report has no closed trades or deposits")
    trades.sort(key=lambda r: (r["open_time"], r["id"]))
    cash.sort(key=lambda r: (r["time"], r["id"]))
    return {"account": account, "trades": trades, "cash": cash}
