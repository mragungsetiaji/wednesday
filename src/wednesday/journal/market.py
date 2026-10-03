"""The market as it stood when a journal entry was made (#14), frozen into the entry so a later
review sees what the trader saw, not what the scan says now."""

from __future__ import annotations

from datetime import datetime, timezone

TOP_SETUPS = 3
NEXT_NEWS = 3


def _setup(s: dict) -> dict:
    meta = s.get("meta") or {}
    return {"timeframe": s.get("timeframe"), "entry": meta.get("entry", s.get("top")), "sl": meta.get("sl"),
            "risk": s.get("risk"), "priority": meta.get("priority"), "swing": meta.get("swing"),
            "distance": s.get("distance")}


def freeze(scan: dict | None, bias: dict | None, news: list[dict], symbol: str, source: str,
           price: float | None = None, now: datetime | None = None) -> dict:
    """A small copy of the scan (price, bias, the nearest setups each side, each timeframe's last break)
    and the next high-impact releases. Never refers back to the live scan."""
    now = now or datetime.now(timezone.utc)
    scan = scan or {}
    setups = scan.get("setups") or {}
    return {
        "at": now.isoformat(),
        "symbol": symbol,
        "source": source,
        "bar_time": scan.get("time"),
        "price": price if price is not None else scan.get("price"),
        "bias": {k: bias[k] for k in ("direction", "note", "expires_at") if k in bias} if bias else None,
        "setups": {side: [_setup(s) for s in (setups.get(side) or [])[:TOP_SETUPS]] for side in ("sell", "buy")},
        "structure": [{"timeframe": t["timeframe"], "direction": t["bias"]["direction"], "event": t["bias"]["event"],
                       "level": t["bias"]["level"]} for t in scan.get("timeframes") or [] if t.get("bias")],
        "news": [{"time": e["time"], "currency": e.get("currency"), "title": e.get("title"), "impact": e.get("impact")}
                 for e in sorted(news, key=lambda e: e["time"]) if e["time"] >= now.isoformat()][:NEXT_NEWS],
    }
