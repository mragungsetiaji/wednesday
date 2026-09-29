import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.distribution import COLUMNS, MIN_SAMPLES, distribution, tables, values
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.quarters import quarter_blocks
from wednesday.scanner import ScanConfig
from wednesday.server import create_app


def day_table(closes: list[float], start: str = "2025-01-06", live_last: bool = True) -> dict[str, pd.DataFrame]:
    """Trading days (weekdays) with the given closes; each opens at the previous close."""
    days = pd.bdate_range(start, periods=len(closes))
    opens = [closes[0]] + closes[:-1]
    df = pd.DataFrame({"day": days, "label": days.strftime("%a"), "start": days, "open": opens,
                       "high": np.maximum(opens, closes) + 1, "low": np.minimum(opens, closes) - 1,
                       "close": closes, "live": False})
    df.loc[len(df) - 1, "live"] = live_last
    empty = pd.DataFrame(columns=COLUMNS)
    return {"day": df[COLUMNS], "week": empty, "session": empty, "q90": empty}


def test_percentile_and_z_match_a_hand_calculation():
    rng = np.random.default_rng(3)
    closes = list(2000 * np.cumprod(1 + rng.normal(0, 0.01, 200)))
    closes.append(closes[-1] * 0.966)  # today: -3.4%
    t = day_table(closes)
    out = distribution(t, "day", "change", "all")
    moves = (np.array(closes[1:-1]) / np.array(closes[:-2]) - 1) * 100  # closed days' close-to-close
    x = -3.4
    assert out["current"]["forming"] and out["current"]["value"] == pytest.approx(x)
    assert out["samples"] == len(moves) == 199
    assert out["stats"]["mean"] == pytest.approx(moves.mean())
    assert out["stats"]["std"] == pytest.approx(moves.std(ddof=1))
    assert out["z"] == pytest.approx((x - moves.mean()) / moves.std(ddof=1))
    p = out["percentile"]
    assert p["side"] == "low"
    assert p["above"] == pytest.approx(np.mean(moves > x))
    assert p["tail"] == pytest.approx(np.mean(moves <= x))
    assert sum(out["histogram"]["counts"]) == 199
    edges = out["histogram"]["edges"]
    assert edges[0] <= min(moves.min(), x) and edges[-1] >= max(moves.max(), x)
    # Every "day like this" moved at least as far down, newest first, with the next day's move.
    like = out["like_this"]
    assert all(r["value"] <= x for r in like) and [r["day"] for r in like] == sorted((r["day"] for r in like), reverse=True)


def test_few_samples_hide_the_percentile():
    t = day_table([2000 + i for i in range(MIN_SAMPLES)])  # MIN_SAMPLES - 1 closed changes
    out = distribution(t, "day", "change", "all")
    assert out["samples"] == MIN_SAMPLES - 2  # the first day has no previous close
    assert out["percentile"] is None and out["z"] is None and out["stats"] is not None
    assert distribution(day_table([2000 + i for i in range(MIN_SAMPLES + 2)]), "day", "change", "all")["percentile"]


def test_lookback_and_weekday_filter():
    t = day_table([2000 + (i % 7) for i in range(600)])
    all_days = distribution(t, "day", "range_pct", "all")
    year = distribution(t, "day", "range_pct", "1y")
    assert year["samples"] < all_days["samples"] and year["coverage"]["from"] >= "2026"
    wed = distribution(t, "day", "range_pct", "all", same_weekday=True)
    cur = pd.Timestamp(wed["current"]["day"])
    assert wed["filters"]["weekday"] == cur.strftime("%a") and wed["samples"] == pytest.approx(all_days["samples"] / 5, abs=1)
    with pytest.raises(ValueError):
        distribution(t, "month", "change")
    with pytest.raises(ValueError):
        values(t, "day", "size")


def test_atr_uses_only_the_days_before():
    closes = [2000.0] * 30
    t = day_table(closes, live_last=False)
    t["day"].loc[29, ["high", "low"]] = [2050.0, 1950.0]  # a huge last day doesn't widen its own ATR
    v = values(t, "day", "range_atr")
    assert np.isnan(v["value"].iloc[13]) and v["value"].iloc[14] == pytest.approx(1.0)
    assert v["value"].iloc[29] == pytest.approx(100 / 2)


def m1(start: str, end: str) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="1min", inclusive="left")
    price = 2000 + np.arange(len(idx)) * 0.001
    return pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1.0}, index=idx)


@pytest.mark.parametrize("clock,before,after", [
    ("UTC", "2026-03-04 23:00", "2026-03-09 22:00"),  # 18:00 New York: 23:00 UTC in EST, 22:00 after the change
    ("NY+7", "2026-03-05 01:00", "2026-03-10 01:00"),  # a New York +7 clock doesn't move
])
def test_trading_days_start_at_18_00_new_york_through_dst(clock, before, after):
    bars = m1("2026-03-03 00:00", "2026-03-12 00:00")
    t = tables(quarter_blocks(bars, clock))
    starts = {r["day"].date().isoformat(): r["start"] for _, r in t["day"].iterrows()}
    assert starts["2026-03-05"] == pd.Timestamp(before)  # Thursday, EST
    assert starts["2026-03-10"] == pd.Timestamp(after)  # Tuesday, EDT (the change was Sunday 8 March)
    # Weeks gather Monday to Friday; sessions and 90-minute blocks carry their session.
    assert len(t["week"]) == 2 and t["week"]["label"].iloc[1] == "Week of 09 Mar"
    assert t["q90"]["label"].iloc[0].split(" ")[-1] in {"Q1", "Q2", "Q3", "Q4"}
    assert set(t["q90"]["label"].str.rsplit(" ", n=1).str[0]) <= {"Tokyo", "London", "NY AM", "NY PM"}


def test_daily_bars_extend_the_history_back():
    bars = m1("2026-03-09 00:00", "2026-03-12 00:00")
    d1_days = pd.bdate_range("2026-01-05", "2026-03-10")
    d1 = pd.DataFrame({"open": 1900.0, "high": 1910.0, "low": 1890.0, "close": 1905.0, "volume": 1.0}, index=d1_days)
    t = tables(quarter_blocks(bars, "UTC"), d1)
    assert t["day"]["day"].min() == pd.Timestamp("2026-01-05")
    assert t["day"]["day"].is_monotonic_increasing and not t["day"]["day"].duplicated().any()
    # The M1 days win where both have them.
    assert (t["day"].set_index("day").loc["2026-03-10", "open"]) > 1990


def test_distribution_endpoint(tmp_path):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    engine = Engine(SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00")),
                    cfg, "XAUUSD")
    api = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    assert api.get("/api/distribution").status_code == 503
    engine.step()
    body = api.get("/api/distribution?period=session&measure=range_pct&lookback=all&session=true").json()
    assert body["period"] == "session" and body["samples"] > 0 and body["current"]["label"] in {"Tokyo", "London", "NY AM", "NY PM"}
    assert body["filters"]["session"] == body["current"]["label"]
    assert api.get("/api/distribution?period=year").status_code == 422
