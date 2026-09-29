"""Price alerts on drawings: crossing a line or trendline, entering or leaving a rectangle."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.drawing_alerts import DrawingAlerts, bar_events, clean_alert, format_message, line_price, tick_event
from wednesday.drawings import clean_drawing
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store

ID = "0f6c1d2e-8b4a-4c7e-9a51-3d2f7e6b1c90"
T0 = pd.Timestamp("2026-03-02 10:00")
U0 = int(T0.timestamp())


def bars(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, start=T0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="1min")
    highs = highs or [c + 0.2 for c in closes]
    lows = lows or [c - 0.2 for c in closes]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=idx)


def hline(p: float) -> dict:
    return {"id": ID, "kind": "hline", "points": [{"t": U0, "p": p}]}


def alert(condition: str, mode: str = "once", **kw) -> dict:
    return {"condition": condition, "mode": mode, "note": None, "expires_at": None, "intrabar": False, "armed": True,
            "armed_bar": None, "fires": 0, **kw}


def test_a_line_crosses_on_the_bar_that_reaches_it_from_the_other_side():
    b = bars([2000, 2001, 2004, 2007, 2004], highs=[2000.2, 2001.2, 2005.5, 2007.2, 2004.2])
    up = bar_events(hline(2005), alert("cross_up"), b, prev_close=1999)
    # Minute 2's wick counts, and minute 3 again: it closed back below in between.
    assert [(pd.Timestamp(t, unit="s").minute, lvl, what) for t, lvl, what in up] == [(2, 2005, "crossed above"),
                                                                                    (3, 2005, "crossed above")]
    down = bar_events(hline(2005), alert("cross_down"), b, prev_close=1999)
    assert [pd.Timestamp(t, unit="s").minute for t, _, _ in down] == [4]  # from 2007 down to a 2003.8 low
    assert len(bar_events(hline(2005), alert("cross"), b, prev_close=1999)) == 3
    assert bar_events(hline(2005), alert("cross_up"), b, prev_close=None)[0][0] == up[0][0]  # first bar only sets the close


def test_a_trendline_uses_its_price_at_the_bar_time():
    line = {"id": ID, "kind": "trendline", "points": [{"t": U0 + 600, "p": 2010}, {"t": U0, "p": 2000}]}  # +1 per minute
    assert line_price(line, U0 - 60) is None and line_price(line, U0 + 1200) == pytest.approx(2020)  # extended right
    b = bars([1999, 2000, 2004, 2004], highs=[1999.2, 2000.2, 2004.2, 2004.2])  # line at 2000, 2001, 2002, 2003
    [(t, level, what)] = bar_events(line, alert("cross_up"), b, prev_close=1998)
    assert pd.Timestamp(t, unit="s").minute == 2 and level == pytest.approx(2002)
    # Before its first point a trendline has no price: nothing fires.
    assert bar_events(line, alert("cross_up"), bars([2001, 2010], start=T0 - pd.Timedelta(minutes=5)), 1990) == []


def test_a_rectangle_is_entered_and_left():
    box = {"id": ID, "kind": "rect", "points": [{"t": U0 + 60, "p": 2010}, {"t": U0 + 3600, "p": 2000}]}
    b = bars([2015, 2015, 2009, 2008, 2008, 1995], lows=[2014.8, 2014.8, 2008.8, 2007.8, 2007.8, 1994.8])
    [(t, level, what)] = bar_events(box, alert("enter"), b, prev_close=2015)
    assert pd.Timestamp(t, unit="s").minute == 2 and level == 2010 and what == "entered"
    [(t, level, what)] = bar_events(box, alert("exit"), b, prev_close=2015)
    assert pd.Timestamp(t, unit="s").minute == 5 and level == 2000 and what == "left"
    # Bars before the left edge don't count.
    early = bars([2015, 2008], lows=[2014.8, 2007.8], start=T0 - pd.Timedelta(minutes=2))
    assert bar_events(box, alert("enter"), early, prev_close=2015) == []


def test_ticks_fire_intrabar():
    assert tick_event(hline(2005), alert("cross_up"), 2004.9, 2005.1, U0) == (2005, "crossed above")
    assert tick_event(hline(2005), alert("cross_up"), 2005.1, 2004.9, U0) is None
    box = {"id": ID, "kind": "rect", "points": [{"t": U0, "p": 2010}, {"t": U0 + 60, "p": 2000}]}
    assert tick_event(box, alert("enter"), 2011, 2009, U0 + 30) == (2010, "entered")


def test_alert_validation():
    assert clean_alert("hline", {"condition": "cross"}, 5)["armed_bar"] == 5
    for kind, body in (("rect", {"condition": "cross"}), ("path", {"condition": "cross"}),
                       ("hline", {"condition": "cross", "mode": "twice"}),
                       ("hline", {"condition": "cross", "expires_at": "2020-01-01T00:00"}),
                       ("hline", {"condition": "cross", "note": "x" * 201})):
        with pytest.raises(ValueError):
            clean_alert(kind, body, None)


def test_the_message_states_facts():
    text = format_message("XAUUSD", hline(2476.3), alert("cross_up", note="Asia high"), 2476.3, "crossed above", 2476.9,
                          "2026-03-02 10:02", False)
    assert "XAUUSD crossed above 2,476.30</b>, your line 'Asia high'" in text and "closed M1 bar of 2026-03-02 10:02" in text
    assert "re-arm" in text
    assert "Intrabar" in format_message("XAUUSD", hline(2476.3), alert("cross", mode="every"), 2476.3, "crossed below",
                                        2476.1, None, True)


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'xau.db'}")


def save(store, drawing: dict, **alert_body) -> None:
    store.drawing_put({**clean_drawing(drawing["id"], {**drawing, "style": {"color": None, "width": 1}}),
                       "source": "s", "symbol": "X"})
    armed_bar = alert_body.pop("armed_bar", None)
    store.drawing_alert_put({"drawing_id": drawing["id"], "source": "s", "symbol": "X",
                             **clean_alert(drawing["kind"], alert_body, armed_bar)})


def run(da: DrawingAlerts, history: pd.DataFrame, sent: list) -> list[str]:
    """First check sets the baseline at the first bar; the second sees the rest as new."""
    da.check("s", "X", history.iloc[:1], sent.append)
    return da.check("s", "X", history, sent.append)


def test_once_fires_once_then_disarms_and_never_twice(store):
    save(store, hline(2005), condition="cross")
    history = bars([2000, 2006, 2004, 2006])  # crosses three times
    sent: list[str] = []
    keys = run(DrawingAlerts(store), history, sent)
    assert len(keys) == 1 and len(sent) == 1
    [a] = store.drawing_alerts("s", "X")
    assert not a["armed"] and a["fires"] == 1 and a["fired_price"] == 2006
    # Re-armed, a restart that sees the same bars again doesn't send the logged bar twice.
    store.drawing_alert_update(ID, {"armed": True})
    again = run(DrawingAlerts(store), history, sent)
    assert again and again[0] != keys[0] and len(sent) == 2  # the next crossing, not the logged one
    [row] = [r for r in store.recent_alerts(10) if r["key"] == keys[0]]
    assert row["status"] == "sent" and row["kind"] == "drawing" and "Crossed above your line" in row["summary"]


def test_every_fires_on_each_bar_and_only_bars_after_arming_count(store):
    save(store, hline(2005), condition="cross", mode="every", armed_bar=U0 + 60)  # armed after the second bar
    sent: list[str] = []
    keys = run(DrawingAlerts(store), bars([2000, 2006, 2004, 2006]), sent)
    assert len(keys) == 2 and len(sent) == 2  # minutes 2 and 3; minute 1 closed before it was armed
    assert store.drawing_alerts("s", "X")[0]["armed"]


def test_moving_the_drawing_moves_the_alert_and_expired_ones_stop(store):
    save(store, hline(2005), condition="cross_up")
    da = DrawingAlerts(store)
    da.check("s", "X", bars([2000]), None)
    store.drawing_put({**clean_drawing(ID, {**hline(2010), "style": {"color": None, "width": 1}}), "source": "s", "symbol": "X"})
    da.invalidate()
    keys = da.check("s", "X", bars([2000, 2006, 2011]), None)
    assert len(keys) == 1 and keys[0].endswith(f"bar{U0 + 120}")  # at 2010, not 2005
    rows = {r["key"]: r for r in store.recent_alerts(5)}
    assert rows[keys[0]]["status"] == "logged"  # no Telegram: logged, still fired
    # An expired alert disarms without firing.
    save(store, hline(2020), condition="cross_up", expires_at=(pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=1)).isoformat())
    store.drawing_alert_update(ID, {"expires_at": "2000-01-01T00:00:00+00:00"})
    da.invalidate()
    assert da.check("s", "X", bars([2000, 2011, 2025], start=T0 + pd.Timedelta(minutes=10)), None) == []
    assert not store.drawing_alerts("s", "X")[0]["armed"]


def test_intrabar_only_when_asked(store):
    save(store, hline(2005), condition="cross_up", intrabar=False)
    da = DrawingAlerts(store)
    da.check_tick("s", "X", 2004, T0, None)
    assert da.check_tick("s", "X", 2006, T0, None) == []
    save(store, hline(2005), condition="cross_up", intrabar=True)
    da.invalidate()
    da.check_tick("s", "X", 2004, T0, None)
    [key] = da.check_tick("s", "X", 2006, T0 + pd.Timedelta(seconds=20), None)
    assert "tick" in key and store.recent_alerts(1)[0]["timeframe"] == "tick"
    # The closed bar of that same minute doesn't send it again.
    store.drawing_alert_update(ID, {"mode": "every"})
    da.invalidate()
    da.check("s", "X", bars([2000], start=T0 - pd.Timedelta(minutes=1)), None)
    assert da.check("s", "X", bars([2000, 2006], start=T0 - pd.Timedelta(minutes=1)), None) == []


def test_drawing_alert_api(store, tmp_path):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    engine = Engine(SyntheticFeed(history=500, end=pd.Timestamp("2026-03-02 12:00")), cfg, "XAUUSD", store)
    client = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    assert client.put(f"/api/drawings/{ID}/alert", json={"condition": "cross"}).status_code == 404
    client.put(f"/api/drawings/{ID}", json={"kind": "hline", "points": [{"t": U0, "p": 2005}], "style": {"color": None, "width": 1}})
    assert client.put(f"/api/drawings/{ID}/alert", json={"condition": "enter"}).status_code == 422
    engine.step()
    saved = client.put(f"/api/drawings/{ID}/alert", json={"condition": "cross_up", "note": "Asia high"}).json()
    assert saved["armed"] and saved["armed_bar"] == int(engine.snapshot()[2].index[-1].timestamp())
    body = client.get("/api/drawings").json()
    assert body["alerts"][0]["drawing_id"] == ID and "source" not in body["alerts"][0]
    assert client.get("/api/drawings/alerts").json()["alerts"][0]["note"] == "Asia high"
    # Saving the moved drawing leaves the alert as it was.
    client.put(f"/api/drawings/{ID}", json={"kind": "hline", "points": [{"t": U0, "p": 2010}], "style": {"color": None, "width": 1}})
    assert client.get("/api/drawings/alerts").json()["alerts"][0]["armed"]
    assert client.delete(f"/api/drawings/{ID}/alert").status_code == 200
    assert client.delete(f"/api/drawings/{ID}/alert").status_code == 404
