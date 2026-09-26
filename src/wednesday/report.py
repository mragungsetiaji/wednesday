"""Plain-text rendering of scan results."""

from __future__ import annotations

from .bias import RISK_TEXT, TradeBias, setup_risk
from .levels import Level
from .scanner import ScanResult

SHORT = {"ob": "OB", "liquidity": "LIQ", "idm": "IDM"}


def _price(lv: Level, digits: int) -> str:
    if lv.top == lv.bottom:
        return f"{lv.top:.{digits}f}"
    return f"{lv.bottom:.{digits}f}-{lv.top:.{digits}f}"


def _fmt(lv: Level | None, price: float, digits: int) -> str:
    if lv is None:
        return "-"
    edge = lv.bottom if lv.bottom > price else lv.top
    touches = f" t{lv.touches}" if lv.detector == "ob" else ""
    return f"{lv.label} {_price(lv, digits)} ({edge - price:+.{digits}f}){touches}"


def _event(lv: Level, digits: int) -> str:
    if lv.detector == "ob":
        verb = "mitigated"
    else:
        verb = "grabbed" if lv.meta.get("grab") else "broken"
    return f"{lv.label} {_price(lv, digits)} {verb} @ {lv.ended_time:%H:%M}"


def format_scan(result: ScanResult, symbol: str = "XAUUSD", digits: int = 2, bias: TradeBias | None = None) -> str:
    price = result.price
    lines = [
        f"[{result.time:%Y-%m-%d %H:%M}] {symbol} price {price:.{digits}f}",
        f"{'TF':<5} {'DET':<4} {'#':>3}  {'NEAREST ABOVE':<34} {'NEAREST BELOW':<34} NOTES",
    ]
    for r in result.results:
        for i, (det, s) in enumerate(r.sets.items()):
            notes = []
            if s.inside:
                notes.append("inside " + ", ".join(f"{lv.label} {_price(lv, digits)}" for lv in s.inside))
            notes += [_event(lv, digits) for lv in s.recent]
            tf = ""
            if i == 0:
                arrow = {"bullish": "▲", "bearish": "▼"}[r.bias.direction] if r.bias else " "
                tf = f"{r.timeframe.name} {arrow}"
            lines.append(
                f"{tf:<5} {SHORT.get(det, det[:4]):<4} {len(s.active):>3}  "
                f"{_fmt(s.above, price, digits):<34} {_fmt(s.below, price, digits):<34} {'; '.join(notes) or '-'}"
            )
    if bias:
        lines.append(f"bias: {bias.direction.upper()}" + (f" ({bias.note})" if bias.note else ""))
    for side in ("sell", "buy"):
        label = RISK_TEXT.get(setup_risk(side, bias), "")
        for rank, (tf, lv, dist) in enumerate(result.setups(side, limit=3, bias=bias), 1):
            m = lv.meta
            cap = " capped" if m["sl_capped"] else ""
            swing = f" {m['swing']}" if m.get("swing") else ""
            lines.append(
                f"{side.upper()} LIMIT #{rank} {tf.name:<3} {m['priority']:<7}{swing} entry {m['entry']:.{digits}f} "
                f"SL {m['sl']:.{digits}f} (risk {m['risk']:.{digits}f}{cap}) "
                f"dist {dist:.{digits}f} t{lv.touches}" + (f"  {label}" if label else "")
            )
    for det in result.detectors:
        up, down = result.nearest(det, "above"), result.nearest(det, "below")
        parts = []
        if up:
            parts.append(f"above {up[0].name} {_fmt(up[1], price, digits)}")
        if down:
            parts.append(f"below {down[0].name} {_fmt(down[1], price, digits)}")
        if parts:
            lines.append(f"nearest {SHORT.get(det, det)}: " + " | ".join(parts))
    return "\n".join(lines)
