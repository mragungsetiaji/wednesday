"""Turn an MT5 history report into trades and cash.

:func:`parse_report` reads the history report the MetaTrader 5 terminal saves
(History tab, right click, Report, HTML). Its *Positions* table gives the trades
and its *Deals* table the deposits and withdrawals. Wednesday doesn't talk to
the terminal itself; the trader exports the report and uploads it.

Times stay on the broker's server clock, as unix seconds.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser

# MT5 deal types of cash rows (ENUM_DEAL_TYPE)
BALANCE, CREDIT, BONUS = 2, 3, 6


class ReportError(ValueError):
    """A file that can't be read, with a message for the user."""


# A file the parser chokes on in another way (cut short, broken markup, a missing column); the
# Rust server (server-rs/src/journal/report.rs) answers these with the same message.
UNREADABLE = "Couldn't read that report. Save it again in the terminal: History tab, right click, Report, HTML"


def cash_kind(deal_type: int, amount: float) -> str:
    if deal_type == BALANCE:
        return "deposit" if amount >= 0 else "withdrawal"
    if deal_type in (CREDIT, BONUS):
        return "credit"
    return "other"  # charges, corrections, interest, fees billed as their own deal


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
    try:
        return _parse_report(data)
    except ReportError:
        raise
    except (UnicodeError, KeyError, IndexError, AssertionError, ValueError) as exc:
        raise ReportError(UNREADABLE) from exc


def _parse_report(data: bytes) -> dict:
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
