"""More M1 history (issue #12): file import in several formats, clock conversion, coverage (/api/history)."""


import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.history_import import BarsFileError, import_bars, parse_bars
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store

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


def test_coverage_per_day(tmp_path):
    s = store(tmp_path)
    import_bars(s, KEY, parse_bars(minute_file("2023-01-02 00:00", 30_000)), "UTC", "UTC")
    assert s.bar_bounds(*KEY)[0] == 30_000
    days = s.bar_days(*KEY)
    assert days[0] == (int(pd.Timestamp("2023-01-02").timestamp()), 1440) and sum(n for _, n in days) == 30_000


def test_history_endpoints(tmp_path):
    s = store(tmp_path)
    csv = tmp_path / "feed.csv"
    csv.write_bytes(minute_file("2026-03-02 00:00", 300))
    cfg = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="csv", csv_path=str(csv)), s)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    body = api.get("/api/history").json()
    assert body["can_import"] is True and body["stored"]["bars"] == 0 and "history_bars" not in body

    staged = api.post("/api/history/import", content=minute_file("2026-01-05 00:00", 2000)).json()
    assert staged["format"] == "csv" and staged["bars"] == 2000 and len(staged["sample"]) == 5
    body = api.post(f"/api/history/import/{staged['token']}", json={"clock": "UTC"}).json()
    assert body["imported"]["added"] == 2000 and body["stored"]["bars"] == 2000
    assert [n for _, n in body["days"]] == [1440, 560]
    assert api.post(f"/api/history/import/{staged['token']}", json={}).status_code == 422  # used up
    assert api.post("/api/history/import", content=b"nonsense").status_code == 422


def test_demo_data_cannot_import(tmp_path):
    s = store(tmp_path)
    cfg = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), s)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/history").json()["can_import"] is False
    assert api.post("/api/history/import", content=minute_file("2026-01-05 00:00", 5)).status_code == 409


def test_import_needs_a_database(tmp_path):
    csv = tmp_path / "feed.csv"
    csv.write_bytes(minute_file("2026-03-02 00:00", 300))
    cfg = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="csv", csv_path=str(csv)), None)
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    assert api.get("/api/history").json()["can_import"] is False
    res = api.post("/api/history/import", content=minute_file("2026-01-05 00:00", 5))
    assert res.status_code == 409 and "database" in res.json()["detail"]
