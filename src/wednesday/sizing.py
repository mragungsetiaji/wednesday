"""Position size for each setup: the lot that risks the trader's chosen amount between entry and stop.

The balance and the symbol's contract spec come from the MT5 terminal when it is
connected (``MT5Feed.trading_spec``), otherwise from Settings > Risk. Lots always
round *down* to the broker's step, so the money at risk never exceeds the budget;
when even the minimum lot risks more, the setup says so instead of sizing it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal

RISK_KEY = "risk"
RISK_MODES = ("percent", "amount")


@dataclass
class RiskSettings:
    enabled: bool = False
    mode: str = "percent"  # percent: of the balance; amount: fixed money per trade
    value: float = 1.0
    use_mt5: bool = True  # balance and symbol spec from the terminal when MT5 is the source
    balance: float | None = None  # otherwise (or before MT5 connects)
    currency: str = "USD"
    contract_size: float = 100.0  # money per 1.00 price move per lot (XAUUSD: 100 oz, USD account)
    min_lot: float = 0.01
    lot_step: float = 0.01
    risk_off_multiplier: float = 0.5  # RISK OFF setups (against the bias) risk this share

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> RiskSettings:
        d = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        s = cls(**d)
        s.validate()
        return s

    def validate(self) -> None:
        if self.mode not in RISK_MODES:
            raise ValueError(f"mode must be one of {', '.join(RISK_MODES)}")
        if not self.value or self.value <= 0:
            raise ValueError("Risk per trade must be more than 0")
        if self.mode == "percent" and self.value > 100:
            raise ValueError("Risk per trade can't be more than 100%")
        if self.balance is not None and self.balance <= 0:
            raise ValueError("Balance must be more than 0")
        if self.contract_size <= 0 or self.min_lot <= 0 or self.lot_step <= 0:
            raise ValueError("Contract size, minimum lot and lot step must be more than 0")
        if not 0 <= self.risk_off_multiplier <= 1:
            raise ValueError("The RISK OFF multiplier must be between 0 and 1")
        self.currency = (self.currency or "USD").strip().upper()[:8]


@dataclass(frozen=True)
class Sizer:
    """The resolved inputs, and the size of a setup."""

    balance: float | None
    currency: str
    budget: float  # money at risk per trade (before the RISK OFF multiplier)
    per_point: float  # money per 1.00 price move per lot
    min_lot: float
    lot_step: float
    max_lot: float | None
    off_multiplier: float
    source: str  # "mt5" or "settings"

    def to_dict(self) -> dict:
        return asdict(self)

    def size(self, stop: float | None, risk: str | None = None) -> dict | None:
        """Lots for a setup ``stop`` (entry to stop, in price) away; ``risk`` is its RISK ON / OFF label."""
        if not stop or stop <= 0:
            return None
        budget = self.budget * (self.off_multiplier if risk == "off" else 1.0)
        per_lot = stop * self.per_point
        lots = floor_to_step(budget / per_lot, self.lot_step)
        if self.max_lot is not None:
            lots = min(lots, floor_to_step(self.max_lot, self.lot_step))
        min_risk = round(self.min_lot * per_lot, 2)
        if lots < self.min_lot:
            return {"lots": 0.0, "risk": 0.0, "reward_2r": 0.0, "budget": round(budget, 2),
                    "below_min": True, "min_lot_risk": min_risk}
        at_risk = lots * per_lot
        return {"lots": lots, "risk": round(at_risk, 2), "reward_2r": round(2 * at_risk, 2), "budget": round(budget, 2),
                "below_min": False, "min_lot_risk": min_risk}


def floor_to_step(value: float, step: float) -> float:
    """``value`` rounded down to a multiple of ``step``, without float dust (0.35 stays 0.35)."""
    d = Decimal(str(step))
    steps = math.floor(value / step + 1e-9)
    return float(steps * d)


def resolve(settings: RiskSettings, spec: dict | None) -> Sizer | None:
    """A sizer from the settings and, when ``use_mt5``, the terminal's ``spec``; None when off or no balance."""
    if not settings.enabled:
        return None
    live = spec if settings.use_mt5 and spec else None
    balance = (live or {}).get("balance") or settings.balance
    if settings.mode == "percent":
        if not balance:
            return None
        budget = balance * settings.value / 100
    else:
        budget = settings.value
    if live:
        return Sizer(balance, live.get("currency") or settings.currency, budget, live["per_point"], live["min_lot"],
                     live["lot_step"], live.get("max_lot"), settings.risk_off_multiplier, "mt5")
    return Sizer(balance, settings.currency, budget, settings.contract_size, settings.min_lot, settings.lot_step, None,
                 settings.risk_off_multiplier, "settings")


def pip_size(symbol: str, price: float | None, spec: dict | None = None) -> float:
    """One pip in price: ten of the symbol's points when the terminal reports them (0.1 on a
    2-digit gold), otherwise the usual size for the symbol (see :func:`journal.stats.pip_size`)."""
    point = (spec or {}).get("point")
    if point:
        return float(point) * 10
    from .journal.stats import pip_size as usual

    return usual(symbol, price or 0.0)


def fmt_money(value: float, currency: str) -> str:
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


def size_text(size: dict | None, currency: str) -> str | None:
    """One line for the ladder and alerts, e.g. ``0.35 lot · risk $105.00 · 2R $210.00``."""
    if not size:
        return None
    if size["below_min"]:
        return f"Min lot risks {fmt_money(size['min_lot_risk'], currency)}, over the {fmt_money(size['budget'], currency)} budget"
    return (f"{size['lots']:g} lot · risk {fmt_money(size['risk'], currency)} · "
            f"2R {fmt_money(size['reward_2r'], currency)}")
