"""The trader's directional bias, and the label it puts on each setup.

The bias is set by hand (from news, DXY, yields, the LLM brief...). It is not a
score and doesn't filter anything; it only says where to focus:

* bearish: sells are ``RISK ON``, preferably at a lower-high order block;
  buys stay on the list as ``RISK OFF`` (momentum, smaller risk).
* bullish: the mirror, buys at a higher low first.
* neutral: no trade, every setup is labelled ``NO TRADE``.

A bias can expire at the New York close (17:00 NY) today or on Friday, so a
stale view doesn't carry into the next session unnoticed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BIAS_KEY = "bias"
DIRECTIONS = ("bullish", "bearish", "neutral")
EXPIRY = ("day", "week", "none")
NEW_YORK = ZoneInfo("America/New_York")
NY_CLOSE_HOUR = 17


def expiry_time(expiry: str, now: datetime) -> datetime | None:
    """When a bias set at ``now`` (aware) runs out: the next 17:00 NY close, or Friday's."""
    if expiry == "none":
        return None
    ny = now.astimezone(NEW_YORK)
    close = ny.replace(hour=NY_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if ny >= close:
        close += timedelta(days=1)
    if expiry == "week":
        close += timedelta(days=(4 - close.weekday()) % 7)  # Friday
    while close.weekday() >= 5:  # a close on the weekend rolls to Monday
        close += timedelta(days=1)
    return close.astimezone(timezone.utc)


@dataclass
class TradeBias:
    direction: str  # bullish | bearish | neutral
    note: str = ""
    expiry: str = "day"
    set_at: str | None = None  # ISO UTC
    expires_at: str | None = None  # ISO UTC, None = until changed

    def validate(self) -> list[str]:
        errors = []
        if self.direction not in DIRECTIONS:
            errors.append(f"Unknown bias {self.direction!r}")
        if self.expiry not in EXPIRY:
            errors.append(f"Unknown expiry {self.expiry!r}")
        if len(self.note) > 500:
            errors.append("Keep the note under 500 characters")
        return errors

    def stamped(self, now: datetime | None = None) -> TradeBias:
        now = now or datetime.now(timezone.utc)
        until = expiry_time(self.expiry, now)
        return TradeBias(self.direction, self.note.strip(), self.expiry, now.isoformat(),
                         until.isoformat() if until else None)

    def expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(self.expires_at)

    def to_dict(self, now: datetime | None = None) -> dict:
        return {**asdict(self), "expired": self.expired(now)}

    @classmethod
    def from_dict(cls, d: dict | None) -> TradeBias | None:
        if not d or not d.get("direction"):
            return None
        return cls(d["direction"], d.get("note") or "", d.get("expiry") or "day", d.get("set_at"), d.get("expires_at"))


def active(bias: TradeBias | None, now: datetime | None = None) -> TradeBias | None:
    """The bias if it is set and not expired."""
    return bias if bias and not bias.expired(now) else None


def setup_risk(side: str, bias: TradeBias | None) -> str | None:
    """``on`` (with the bias), ``off`` (against it), ``no_trade`` (neutral), None (no bias)."""
    if bias is None:
        return None
    if bias.direction == "neutral":
        return "no_trade"
    with_bias = (bias.direction == "bearish") == (side == "sell")
    return "on" if with_bias else "off"


RISK_TEXT = {"on": "RISK ON", "off": "RISK OFF", "no_trade": "NO TRADE"}
