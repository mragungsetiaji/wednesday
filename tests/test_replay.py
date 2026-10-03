"""Bar replay (#23): nothing past the replay clock, and the scan matches a cut-off live scan."""

import pandas as pd
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.replay import cut, view
from wednesday.scanner import ScanConfig, scan
from wednesday.server import create_app
from wednesday.timeframes import TIMEFRAMES_BY_NAME

CFG = ScanConfig(lookback=60, params=DetectorParams(swing_length=3))
H4 = TIMEFRAMES_BY_NAME["4H"]


def history():
    return SyntheticFeed(seed=11, history=CFG.required_m1_bars() * 2, end=pd.Timestamp("2026-03-06 12:00"))._bars


def test_replay_never_shows_past_the_clock():
    m1 = history()
    at = int((m1.index[-1] - pd.Timedelta(hours=30, minutes=17)).timestamp())  # mid 4H candle
    v = view(m1, CFG, at, H4, 50)
    clock = pd.Timestamp(at, unit="s")
    known = cut(m1, at)
    assert known.index[-1] + pd.Timedelta(minutes=1) <= clock
    assert v["bar_time"] == int(known.index[-1].timestamp())
    last = v["candles"].iloc[-1]  # the 4H candle still forming at the clock, from its minutes so far
    so_far = known[known.index >= v["candles"].index[-1]]
    assert last["high"] == so_far["high"].max() and last["close"] == so_far["close"].iloc[-1]
    assert (v["candles"].index <= clock).all()
    assert all(lv["time"] <= clock.isoformat() for lv in v["levels"])


def test_replay_scan_matches_a_scan_cut_at_that_time():
    m1 = history()
    at = int((m1.index[-1] - pd.Timedelta(days=1, minutes=3)).timestamp())
    expected = scan(cut(m1, at).tail(CFG.required_m1_bars()), CFG).to_dict(None)
    assert view(m1, CFG, at, H4, 50)["scan"] == expected


def test_replay_endpoint(tmp_path):
    feed = SyntheticFeed(seed=11, history=CFG.required_m1_bars() * 2, end=pd.Timestamp("2026-03-06 12:00"))
    engine = Engine(feed, CFG, "XAUUSD")
    engine.step()
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    rng = client.get("/api/replay/range").json()
    assert rng["first"] < rng["last"]
    at = rng["last"] - 6 * 3600 + 25  # not on a whole minute: rounded down
    body = client.get(f"/api/replay?at={at}&tf=1H&limit=30").json()
    assert body["at"] == at - 25 and body["timeframe"] == "1H"
    assert body["candles"][-1]["time"] <= body["at"] and len(body["candles"]) == 30
    assert body["scan"]["timeframes"] and body["bar_time"] < body["at"]
    assert client.get(f"/api/replay?at={rng['first'] - 10 * 86400}&tf=1H").status_code == 422
    assert client.get(f"/api/replay?at={at}&tf=2H").status_code == 404
