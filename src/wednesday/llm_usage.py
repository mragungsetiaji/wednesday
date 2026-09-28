"""What LLM calls cost, and a monthly budget that stops them.

Every call records its feature (``brief``, or a plugin's: ``recap``, ``ask``,
``embeddings``...), provider, model and token counts. The counts come from the
provider's ``usage`` fields; only a reply without them is estimated (about four
characters a token) and marked so. Cost is tokens times the price table: a few
defaults, editable in Settings > LLM usage (saved in the ``llm_prices`` setting),
in US dollars per million tokens. A model without a price is logged with no cost.

The budget (``llm_budget`` setting) is a monthly limit in US dollars, per UTC
calendar month. At 80% the dashboard warns; at 100% :meth:`UsageLog.guard` refuses
scheduled jobs outright and manual runs unless the user confirmed. The guard runs
on the server, before the call.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

PRICES_KEY = "llm_prices"
BUDGET_KEY = "llm_budget"
WARN_AT = 0.8
CHARS_PER_TOKEN = 4

# USD per million tokens: input, output, and cached input read / written. Check them
# against the provider's pricing page; Settings > LLM usage edits and adds models.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 1.25},
    "gpt-5-mini": {"input": 0.25, "output": 2.0, "cache_read": 0.025, "cache_write": 0.25},
}
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


class BudgetExceeded(RuntimeError):
    """The month's LLM spend reached the budget."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


def clean_prices(body: dict) -> dict[str, dict[str, float]]:
    """A price table from the dashboard: model -> input/output (required) and cache prices."""
    out = {}
    for model, p in (body or {}).items():
        model = str(model).strip()
        if not model or not isinstance(p, dict):
            raise ValueError("Each price needs a model name")
        try:
            row = {k: float(p[k]) for k in ("input", "output")}
            for k in ("cache_read", "cache_write"):
                row[k] = float(p[k]) if p.get(k) not in (None, "") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{model}: input and output prices are numbers (USD per million tokens)") from exc
        if any(v is not None and v < 0 for v in row.values()):
            raise ValueError(f"{model}: prices can't be negative")
        if row["cache_read"] is None:
            row["cache_read"] = row["input"] * 0.1
        if row["cache_write"] is None:
            row["cache_write"] = row["input"] * 1.25
        out[model] = row
    return out


class UsageLog:
    def __init__(self, store=None):
        self.store = store
        self._memory: list[dict] = []  # without a database (--db none)
        self._lock = threading.Lock()

    # ---- settings -----------------------------------------------------------
    def prices(self) -> dict[str, dict[str, float]]:
        saved = self.store.get_setting(PRICES_KEY) if self.store else None
        return saved["models"] if saved and "models" in saved else dict(DEFAULT_PRICES)

    def set_prices(self, body: dict) -> dict:
        prices = clean_prices(body)
        if self.store:
            self.store.set_setting(PRICES_KEY, {"models": prices})
        return prices

    def price_of(self, model: str) -> dict[str, float] | None:
        """Exact model name, else the longest priced name it starts with (dated snapshots)."""
        prices = self.prices()
        if model in prices:
            return prices[model]
        match = max((m for m in prices if model.startswith(m)), key=len, default=None)
        return prices[match] if match else None

    def budget(self) -> float | None:
        saved = self.store.get_setting(BUDGET_KEY) if self.store else None
        value = (saved or {}).get("monthly_usd")
        return float(value) if value else None

    def set_budget(self, monthly_usd) -> float | None:
        value = None if monthly_usd in (None, "", 0) else float(monthly_usd)
        if value is not None and value < 0:
            raise ValueError("The budget can't be negative")
        if self.store:
            self.store.set_setting(BUDGET_KEY, {"monthly_usd": value})
        return value

    # ---- the log ------------------------------------------------------------
    def cost(self, model: str, usage: dict) -> float | None:
        p = self.price_of(model)
        if p is None:
            return None
        return sum(usage.get(f"{k}_tokens", 0) * p[k] for k in PRICE_FIELDS) / 1_000_000

    def record(self, feature: str, provider: str, model: str, usage: dict | None, *, text_in: str = "",
               text_out: str = "", scheduled: bool = False, when: datetime | None = None) -> dict:
        """Log one call. ``usage`` is the provider's token counts (``input_tokens``,
        ``output_tokens``, ``cache_read_tokens``, ``cache_write_tokens``); None estimates
        them from the text sent and received."""
        estimated = usage is None
        if estimated:
            usage = {"input_tokens": estimate_tokens(text_in), "output_tokens": estimate_tokens(text_out)}
        tokens = {f"{k}_tokens": int(usage.get(f"{k}_tokens") or 0) for k in PRICE_FIELDS}
        row = {"id": str(uuid.uuid4()), "created_at": (when or _now()).isoformat(), "feature": feature,
               "provider": provider, "model": model, **tokens, "cost": self.cost(model, tokens),
               "estimated": estimated, "scheduled": scheduled}
        if self.store:
            self.store.llm_usage_add(row)
        else:
            with self._lock:
                self._memory.append(row)
        return row

    def rows_since(self, since: datetime) -> list[dict]:
        if self.store:
            return self.store.llm_usage_since(since.isoformat())
        with self._lock:
            return [r for r in self._memory if r["created_at"] >= since.isoformat()]

    # ---- the budget -----------------------------------------------------------
    def state(self, now: datetime | None = None) -> dict:
        """This month's spend against the budget: level none (no budget), ok, warn (80%) or over."""
        rows = self.rows_since(month_start(now or _now()))
        spent = sum(r["cost"] or 0 for r in rows)
        limit = self.budget()
        share = spent / limit if limit else None
        level = "none" if not limit else "over" if share >= 1 else "warn" if share >= WARN_AT else "ok"
        return {"limit": limit, "spent": spent, "share": share, "level": level, "calls": len(rows),
                "unpriced_calls": sum(r["cost"] is None for r in rows)}

    def guard(self, feature: str, scheduled: bool = False, confirmed: bool = False) -> dict:
        """Call before an LLM request. Over the budget, a scheduled job never runs and a
        manual one only when the user confirmed it."""
        st = self.state()
        if st["level"] == "over" and (scheduled or not confirmed):
            what = "Scheduled LLM jobs are paused" if scheduled else "Confirm to run it anyway"
            raise BudgetExceeded(f"Over the monthly LLM budget (${st['spent']:.2f} of ${st['limit']:.2f}). {what}, "
                                 "or raise the budget in Settings > LLM usage.")
        return st

    def report(self, now: datetime | None = None, top: int = 10) -> dict:
        """This month by feature and by model, the last 30 days by day, and the costliest calls."""
        now = now or _now()
        start = month_start(now)
        since = min(start, (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0))
        rows = self.rows_since(since)
        month = [r for r in rows if r["created_at"] >= start.isoformat()]

        def group(key: str) -> list[dict]:
            out: dict[str, dict] = {}
            for r in month:
                g = out.setdefault(r[key], {"key": r[key], "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                            "cache_read_tokens": 0, "cost": 0.0, "unpriced": 0})
                g["calls"] += 1
                g["unpriced"] += r["cost"] is None
                for k in ("input_tokens", "output_tokens", "cache_read_tokens"):
                    g[k] += r[k]
                g["cost"] += r["cost"] or 0
            return sorted(out.values(), key=lambda g: -g["cost"])

        days: dict[str, float] = {}
        first_day = (now - timedelta(days=29)).date()
        for i in range(30):
            days[(first_day + timedelta(days=i)).isoformat()] = 0.0
        for r in rows:
            d = r["created_at"][:10]
            if d in days:
                days[d] += r["cost"] or 0
        costly = sorted(month, key=lambda r: -(r["cost"] or 0))[:top]
        return {"month": start.strftime("%Y-%m"), "budget": self.state(now), "by_feature": group("feature"),
                "by_model": group("model"), "daily": [[d, c] for d, c in days.items()], "top": costly,
                "prices": self.prices(), "warn_at": WARN_AT}
