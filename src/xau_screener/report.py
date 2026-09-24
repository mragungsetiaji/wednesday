"""Plain-text rendering of scan results."""

from __future__ import annotations

from .orderblock import OrderBlock
from .scanner import ScanResult


def _fmt_ob(ob: OrderBlock | None, price: float, digits: int) -> str:
    if ob is None:
        return "-"
    tag = "BULL" if ob.kind == "bullish" else "BEAR"
    edge = ob.bottom if ob.bottom > price else ob.top
    dist = edge - price
    return f"{tag} {ob.bottom:.{digits}f}-{ob.top:.{digits}f} ({dist:+.{digits}f}) t{ob.touches}"


def format_scan(result: ScanResult, symbol: str = "XAUUSD", digits: int = 2) -> str:
    price = result.price
    lines = [
        f"[{result.time:%Y-%m-%d %H:%M}] {symbol} price {price:.{digits}f}",
        f"{'TF':<4} {'#OB':>4}  {'NEAREST ABOVE':<38} {'NEAREST BELOW':<38} INSIDE",
    ]
    for r in result.results:
        inside = ", ".join(
            f"{'BULL' if o.kind == 'bullish' else 'BEAR'} {o.bottom:.{digits}f}-{o.top:.{digits}f}" for o in r.inside
        ) or "-"
        lines.append(
            f"{r.timeframe.name:<4} {len(r.active):>4}  "
            f"{_fmt_ob(r.above, price, digits):<38} {_fmt_ob(r.below, price, digits):<38} {inside}"
        )
    up, down = result.nearest_above(), result.nearest_below()
    summary = []
    if up:
        summary.append(f"nearest above: {up[0].name} {_fmt_ob(up[1], price, digits)}")
    if down:
        summary.append(f"nearest below: {down[0].name} {_fmt_ob(down[1], price, digits)}")
    if summary:
        lines.append(" | ".join(summary))
    return "\n".join(lines)
