import numpy as np
import pandas as pd
import pytest

from wednesday.quarters import clock_error, quarter_blocks, quarter_stats, quarters_payload, to_new_york
from wednesday.settings import DataSettings


def m1_utc(start, end, slope):
    idx = pd.date_range(start, end, freq="1min", inclusive="left")
    close = 2500 + slope(np.arange(len(idx)), idx)
    return pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1.0}, index=idx)


def test_clock_conversion():
    t = pd.DatetimeIndex([pd.Timestamp("2026-03-02 05:00"), pd.Timestamp("2026-07-01 04:00")])
    assert list(to_new_york(t, "UTC")) == [pd.Timestamp("2026-03-02 00:00"), pd.Timestamp("2026-07-01 00:00")]  # EST, EDT
    assert to_new_york(pd.DatetimeIndex(["2026-03-02 07:00"]), "NY+7")[0] == pd.Timestamp("2026-03-02 00:00")
    assert to_new_york(pd.DatetimeIndex(["2026-03-02 08:00"]), "UTC+3")[0] == pd.Timestamp("2026-03-02 00:00")
    assert to_new_york(pd.DatetimeIndex(["2026-03-02 05:00"]), "Europe/London")[0] == pd.Timestamp("2026-03-02 00:00")
    assert clock_error("NY+7") is None and clock_error("Asia/Jakarta") is None
    assert "Unknown clock" in clock_error("Mars/Base")


def test_blocks_follow_the_new_york_trading_day():
    # Sun 1 Mar 18:00 NY (23:00 UTC) to Wed 4 Mar 18:00 NY: the Monday, Tuesday and Wednesday trading days.
    def slope(i, idx):
        return np.where(idx < pd.Timestamp("2026-03-02 23:00"), i * 0.01, np.where(idx < pd.Timestamp("2026-03-03 23:00"), 50 - (i - 1440) * 0.01, 40 + (i - 2880) * 0.01))

    m1 = m1_utc("2026-02-28 12:00", "2026-03-04 23:00", slope)  # starts on Saturday: weekend bars are dropped
    blocks = quarter_blocks(m1, "UTC")
    week = blocks["week"]
    assert [b.label for b in week] == ["Mon", "Tue", "Wed"]
    assert week[0].start == pd.Timestamp("2026-03-01 23:00")  # Sunday 18:00 NY opens Monday
    assert [b.close > b.open for b in week] == [True, False, True]
    assert [b.label for b in blocks["session"][:4]] == ["Tokyo", "London", "NY AM", "NY PM"]
    assert blocks["session"][1].start == pd.Timestamp("2026-03-02 05:00")  # London opens at midnight NY
    assert len(blocks["session"]) == 12 and len(blocks["q90"]) == 48
    assert [b.label for b in blocks["q90"][:5]] == ["Q1", "Q2", "Q3", "Q4", "Q1"]
    assert not any(b.live for bs in blocks.values() for b in bs)

    stats = quarter_stats(blocks)
    mon, tue, wed = stats["week"][:3]
    assert (mon["count"], mon["green"], tue["green"], wed["green"]) == (1, 1, 0, 1)
    assert stats["week"][4] == {"label": "Fri", "count": 0, "green": 0, "avg_change": None}


def test_the_running_block_is_live_and_left_out_of_stats():
    m1 = m1_utc("2026-03-01 23:00", "2026-03-02 13:10", lambda i, idx: i * 0.01)  # NY 08:10 Monday
    blocks = quarter_blocks(m1, "UTC")
    assert [b.live for b in blocks["week"]] == [True]
    assert [b.label for b in blocks["session"]] == ["Tokyo", "London", "NY AM"] and blocks["session"][-1].live
    assert blocks["q90"][-1].label == "Q2" and blocks["q90"][-1].live  # 07:30-09:00 NY
    assert quarter_stats(blocks)["week"][0]["count"] == 0
    payload = quarters_payload(m1, "UTC")
    assert payload["rows"]["week"][0]["start_unix"] == int(pd.Timestamp("2026-03-01 23:00").timestamp())


def test_clock_setting_defaults_per_source():
    assert DataSettings(source="mt5").resolved_clock == "NY+7"
    assert DataSettings(source="yfinance").resolved_clock == "UTC"
    assert DataSettings(source="csv", csv_path="x.csv", clock="Asia/Jakarta").resolved_clock == "Asia/Jakarta"
    assert DataSettings(source="synthetic", clock="nope").validate()
