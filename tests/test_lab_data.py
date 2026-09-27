"""More M1 history (issue #12): file import in several formats, clock conversion, MT5 backfill, coverage."""

import sys
import types

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.feeds import MT5Feed
from wednesday.lab.bars import Backfill, BarsFileError, import_bars, parse_bars
from wednesday.lab.service import Lab
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store

from test_mt5_and_cli import FakeMT5

KEY = ("csv", "XAUUSD")


def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'd.db'}")


def test_mt5_export_utf16_tab():
    text = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
            "2024.01.02\t01:00:00\t2063.1\t2064.0\t2062.5\t2063.8\t120\t0\t20\n"
            "2024.01.02\t01:01:00\t2063.8\t2064.2\t2063.0\t2063.2\t98\t0\t20\n")
    parsed = parse_bars(text.encode("utf-16"))
    assert parsed.format == "mt5" and parsed.clock is None  # the broker's own clock
    assert parsed.bars.index[0] == pd.Timestamp("2024-01-02 01:00") and parsed.bars["volume"].iloc[0] == 120


def test_dukascopy_is_utc_and_day_first():
    text = ("Gmt time,Open,High,Low,Close,Volume\n"
            "02.01.2024 00:00:00.000,2063.1,2064.0,2062.5,2063.8,0.5\n"
            "02.01.2024 00:01:00.000,2063.8,2064.2,2063.0,2063.2,0.7\n")
    parsed = parse_bars(text.encode())
    assert parsed.format == "dukascopy" and parsed.clock == "UTC"
    assert parsed.bars.index[0] == pd.Timestamp("2024-01-02 00:00")  # 2 January, not 1 February


@pytest.mark.parametrize("text", [
    "20240102 180000;2063.1;2064.0;2062.5;2063.8;0\n20240102 180100;2063.8;2064.2;2063.0;2063.2;0\n",
    "2024.01.02,18:00,2063.1,2064.0,2062.5,2063.8,0\n2024.01.02,18:01,2063.8,2064.2,2063.0,2063.2,0\n",
])
def test_histdata_layouts_are_est(text):
    parsed = parse_bars(text.encode())
    assert parsed.format == "histdata" and parsed.clock == "UTC-5"
    assert parsed.bars.index[1] == pd.Timestamp("2024-01-02 18:01") and parsed.bars["close"].iloc[1] == 2063.2


def test_generic_csv_with_offset_becomes_utc():
    text = "time,open,high,low,close\n2024-01-02T03:00:00+03:00,1,2,0.5,1.5\n2024-01-02T03:01:00+03:00,1,2,0.5,1.5\n"
    assert parse_bars(text.encode()).bars.index[0] == pd.Timestamp("2024-01-02 00:00")


@pytest.mark.parametrize("text, reason", [
    ("time,open,high,low,close\n2024-01-02 00:00,1,2,0,1\n2024-01-02 00:05,1,2,0,1\n2024-01-02 00:10,1,2,0,1\n", "1-minute"),
    ("time,open,high\n2024-01-02 00:00,1,2\n2024-01-02 00:01,1,2\n", "Missing columns"),
    ("", "empty"),
])
def test_bad_files_say_why(text, reason):
    with pytest.raises(BarsFileError, match=reason):
        parse_bars(text.encode())


def minute_file(start: str, n: int, clock_note: str = "") -> bytes:
    idx = pd.date_range(start, periods=n, freq="1min")
    df = pd.DataFrame({"time": idx.strftime("%Y-%m-%d %H:%M:%S"), "open": 2000.0, "high": 2001.0,
                       "low": 1999.0, "close": np.arange(n, dtype=float), "volume": 1.0})
    return df.to_csv(index=False).encode()


def test_import_twice_adds_nothing_the_second_time(tmp_path):
    s = store(tmp_path)
    parsed = parse_bars(minute_file("2024-01-02 00:00", 500))
    first = import_bars(s, KEY, parsed, "UTC", "UTC")
    assert first["added"] == 500 and first["stored"] == 500
    again = import_bars(s, KEY, parse_bars(minute_file("2024-01-02 00:00", 600)), "UTC", "UTC")
    assert again["added"] == 100 and again["stored"] == 600  # only the new tail


def test_utc_file_into_an_ny_plus_7_feed(tmp_path):
    s = store(tmp_path)
    # 12:00 UTC is 07:00 New York in winter (UTC-5), 08:00 in summer (UTC-4); NY+7 adds 7 hours.
    winter = import_bars(s, KEY, parse_bars(minute_file("2024-01-15 12:00", 2)), "UTC", "NY+7")
    summer = import_bars(s, KEY, parse_bars(minute_file("2024-07-15 12:00", 2)), "UTC", "NY+7")
    assert winter["first"] == "2024-01-15T14:00:00" and summer["first"] == "2024-07-15T15:00:00"
    # HistData's EST (UTC-5) into UTC.
    est = import_bars(s, ("csv", "OTHER"), parse_bars(minute_file("2024-07-15 12:00", 2)), "UTC-5", "UTC")
    assert est["first"] == "2024-07-15T17:00:00"
    with pytest.raises(BarsFileError, match="Unknown clock"):
        import_bars(s, KEY, parse_bars(minute_file("2024-01-15 12:00", 2)), "Mars/Base", "UTC")


def test_lab_history_spans_the_import_and_respects_its_size(tmp_path):
    s = store(tmp_path)
    import_bars(s, KEY, parse_bars(minute_file("2023-01-02 00:00", 30_000)), "UTC", "UTC")
    lab = Lab(s, tmp_path / "models", DetectorParams())
    m1 = lab.history(*KEY, None)
    assert len(m1) == 30_000 and m1.index[0] == pd.Timestamp("2023-01-02 00:00")
    lab.set_history_bars(10_000)
    assert len(lab.history(*KEY, None)) == 10_000  # the latest 10k
    with pytest.raises(ValueError):
        lab.set_history_bars(10)
    assert Lab(s, tmp_path / "models", DetectorParams()).history_bars == 10_000  # saved
    days = s.bar_days(*KEY)
    assert days[0] == (int(pd.Timestamp("2023-01-02").timestamp()), 1440) and sum(n for _, n in days) == 30_000


class RangeMT5(FakeMT5):
    """A terminal with M1 history from ``oldest`` to ``newest``."""

    def __init__(self, oldest, newest):
        super().__init__()
        self.oldest, self.newest, self.calls = oldest, newest, []

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        assert timeframe == self.TIMEFRAME_M1 and isinstance(date_from, int)
        self.calls.append((date_from, date_to))
        lo, hi = max(date_from, self.oldest), min(date_to, self.newest)
        times = np.arange(lo - lo % 60 + (60 if lo % 60 else 0), hi + 1, 60)
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                 ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")]
        rates = np.zeros(max(len(times), 0), dtype=dtype)
        rates["time"] = times
        rates["open"] = rates["high"] = rates["low"] = rates["close"] = 2650.0
        return rates


def unix(s):
    return int(pd.Timestamp(s).timestamp())


def test_backfill_walks_back_to_the_start_in_chunks(tmp_path, monkeypatch):
    mt5 = RangeMT5(unix("2025-12-01"), unix("2026-01-31"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    feed = MT5Feed("XAUUSD")
    feed.connect()
    s = store(tmp_path)
    s.save_bars("mt5", "XAUUSD", feed.fetch_m1_range(pd.Timestamp("2026-01-30"), pd.Timestamp("2026-01-31")))
    engine = types.SimpleNamespace(feed=feed, buffer=types.SimpleNamespace(key=("mt5", "XAUUSD")),
                                   snapshot=lambda: (1, None, None), call=lambda fn, timeout=60: fn(feed))
    done = []
    job = Backfill()
    job.start(engine, s, pd.Timestamp("2026-01-10"), on_done=lambda: done.append(1))
    job._thread.join(10)
    n, first, _ = s.bar_bounds("mt5", "XAUUSD")
    assert job.state["error"] is None and done
    assert first == unix("2026-01-10") and n == 21 * 1440 + 1  # 10 Jan .. 31 Jan 00:00
    assert job.state["added"] == 20 * 1440
    assert len(mt5.calls) >= 4  # 5-day chunks, not one huge call

    # Past the terminal's oldest bar: stops with a note instead of looping.
    job.start(engine, s, pd.Timestamp("2025-06-01"))
    job._thread.join(10)
    assert "no M1 bars before" in job.state["note"] and s.bar_bounds("mt5", "XAUUSD")[1] == unix("2025-12-01")


def test_backfill_needs_mt5(tmp_path):
    from wednesday.feeds import SyntheticFeed
    engine = types.SimpleNamespace(feed=SyntheticFeed(), buffer=types.SimpleNamespace(key=None))
    with pytest.raises(RuntimeError, match="MT5"):
        Backfill().start(engine, store(tmp_path), pd.Timestamp("2025-01-01"))


def test_data_endpoints(tmp_path):
    s = store(tmp_path)
    csv = tmp_path / "feed.csv"
    csv.write_bytes(minute_file("2026-03-02 00:00", 300))
    cfg = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="csv", csv_path=str(csv)), s,
                      lab=Lab(s, tmp_path / "models", cfg.params))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    body = api.get("/api/lab/data").json()
    assert body["can_import"] is True and body["can_backfill"] is False and body["stored"]["bars"] == 0

    staged = api.post("/api/lab/data/import", content=minute_file("2026-01-05 00:00", 2000)).json()
    assert staged["format"] == "csv" and staged["bars"] == 2000 and len(staged["sample"]) == 5
    body = api.post(f"/api/lab/data/import/{staged['token']}", json={"clock": "UTC"}).json()
    assert body["imported"]["added"] == 2000 and body["stored"]["bars"] == 2000
    assert [n for _, n in body["days"]] == [1440, 560]
    assert api.post(f"/api/lab/data/import/{staged['token']}", json={}).status_code == 422  # used up
    assert api.post("/api/lab/data/import", content=b"nonsense").status_code == 422

    assert api.post("/api/lab/data/backfill", json={"start": "2025-01-01"}).status_code == 409  # not MT5
    assert api.post("/api/lab/data/backfill", json={"start": "soon"}).status_code == 422
    assert api.put("/api/lab/data", json={"history_bars": 50_000}).json()["history_bars"] == 50_000
    assert api.put("/api/lab/data", json={"history_bars": 5}).status_code == 422


def test_demo_data_cannot_import(tmp_path):
    s = store(tmp_path)
    cfg = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), s, lab=Lab(s, tmp_path / "models", cfg.params))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/lab/data").json()["can_import"] is False
    assert api.post("/api/lab/data/import", content=minute_file("2026-01-05 00:00", 5)).status_code == 409
