from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday import news
from wednesday.brief import BriefRunner
from wednesday.engine import Runtime
from wednesday.news import Calendar, parse_events
from wednesday.news_stats import (CalendarImportError, NewsReactions, brief_line, number, parse_csv, reaction,
                                  store_rows, surprise)
from wednesday.quarters import utc_to_feed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME


def bars_around(t0: pd.Timestamp, path: dict[int, float], before: int = 240, after: int = 90) -> pd.DataFrame:
    """Flat M1 bars at 2000 with a wiggle, then the close following ``path`` (minute after t0 -> price)."""
    idx = pd.date_range(t0 - pd.Timedelta(minutes=before), t0 + pd.Timedelta(minutes=after - 1), freq="1min")
    mins = ((idx - t0) / pd.Timedelta(minutes=1)).astype(int)
    keys = sorted(path)
    close = np.array([2000.0 if m < 0 else float(np.interp(m, keys, [path[k] for k in keys])) for m in mins])
    close = close + np.where(mins < -1, np.sin(np.arange(len(idx))) * 0.5, 0)  # the last close before is 2000
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.2, "low": np.minimum(open_, close) - 0.2,
                         "close": close, "volume": 1.0}, index=idx)


T0 = pd.Timestamp("2026-03-04 13:30")


def test_figures_and_surprise():
    assert number("0.3%") == 0.3 and number("-12K") == -12_000 and number("1.2M") == 1_200_000
    assert number("<0.1%") == 0.1 and number("−0.2%") == -0.2 and number("") is None and number("n/a") is None
    assert surprise("0.4%", "0.3%") == "above" and surprise("150K", "180K") == "below"
    assert surprise("0.3%", "0.3%") == "inline" and surprise(None, "0.3%") is None


def test_reaction_measures_the_moves_after_the_release():
    # Up 6 in five minutes, 8 at fifteen, then back below the start by the hour.
    m1 = bars_around(T0, {0: 2000, 4: 2006, 14: 2008, 40: 1999, 59: 1997})
    got = reaction(m1, T0)
    assert got["moves"] == {5: pytest.approx(6.0, abs=0.01), 15: pytest.approx(8.0, abs=0.01),
                            60: pytest.approx(-3.0, abs=0.01)}
    assert got["reversed"] is True
    assert got["range15"] == pytest.approx(2008.2 - 1999.8, abs=0.01)
    assert got["atr"] and 0.5 < got["atr"] < 5
    # Bars missing after the release, or none just before it: left out.
    assert reaction(m1[m1.index < T0 + pd.Timedelta(minutes=30)], T0) is None
    assert reaction(m1[(m1.index < T0 - pd.Timedelta(minutes=20)) | (m1.index >= T0)], T0) is None


def test_stats_per_event_type_on_the_feed_clock(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'n.db'}")
    clock = "NY+7"
    frames, rows = [], []
    for i, (day, actual, move) in enumerate([("2026-01-13", "0.4%", 5.0), ("2026-02-11", "0.2%", -4.0),
                                              ("2026-03-11", "0.3%", 3.0), ("2026-04-10", "0.5%", 6.0)]):
        utc = pd.Timestamp(f"{day} 08:30", tz="America/New_York").tz_convert("UTC")  # across the DST change
        t0 = utc_to_feed(utc, clock)
        if i < 3:  # no bars for the April release: counted as stored, not used
            frames.append(bars_around(t0, {0: 2000, 4: 2000 + move, 59: 2000 + move / 2}))
        rows.append({"time": utc.isoformat(), "currency": "USD", "title": "CPI m/m", "impact": "High",
                     "actual": actual, "forecast": "0.3%", "previous": "0.2%"})
    store.calendar_put(store_rows(rows, source="csv"))
    m1 = pd.concat(frames)

    def bars(start, end):
        return m1[(m1.index >= start) & (m1.index <= end)]

    stats = NewsReactions(store).stats(bars, clock, ["USD"], ["High"], now=datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
    s = stats["usd cpi m/m"]
    assert s["count"] == 3 and s["stored"] == 4 and s["summary"] == f"CPI m/m: median 15m range {s['range15']:.2f} (last 3)"
    assert s["move"]["5"] == pytest.approx(4.0, abs=0.05) and s["reversed"] == 0 and s["up15"] == 2
    assert s["surprise"]["above"] == {"count": 1, "move15": pytest.approx(4.55, abs=0.01)}  # 5 at 4m, halving by 59m
    assert s["surprise"]["below"]["count"] == 1
    assert "actual above forecast 1x" in brief_line(s)
    # Read on the wrong clock, the "release" lands in the quiet hours before it.
    wrong = NewsReactions(store).stats(bars, "UTC", ["USD"], ["High"], now=datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
    assert wrong["usd cpi m/m"]["move"]["5"] < 1


def test_csv_import():
    data = (b"Title,Country,Date,Time,Impact,Actual,Forecast,Previous\n"
            b"Non-Farm Employment Change,USD,01-09-2026,8:30am,High,256K,164K,212K\n"
            b"Bank Holiday,USD,01-19-2026,All Day,Holiday,,,\n")
    [row] = parse_csv(data, "America/New_York")
    assert row["time"] == int(datetime(2026, 1, 9, 13, 30, tzinfo=timezone.utc).timestamp())
    assert (row["title"], row["actual"], row["forecast"], row["source"]) == ("Non-Farm Employment Change", "256K", "164K", "csv")
    [iso] = parse_csv(b"datetime,currency,event\n2026-01-09T08:30:00-05:00,usd,NFP\n")
    assert iso["time"] == row["time"] and iso["currency"] == "USD" and iso["impact"] is None
    with pytest.raises(CalendarImportError, match="header"):
        parse_csv(b"a,b\n1,2\n")
    with pytest.raises(CalendarImportError, match="time zone"):
        parse_csv(data, "Asia/Jakarta")
    with pytest.raises(CalendarImportError, match="Row 2"):
        parse_csv(b"datetime,currency,title\n2026-13-45 99:99,USD,X\n")


def test_the_feed_keeps_an_imported_actual(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'n.db'}")
    ev = {"time": "2026-03-04T13:30:00+00:00", "currency": "USD", "title": "CPI m/m", "impact": "High", "forecast": "0.3%"}
    store.calendar_put(store_rows([{**ev, "actual": "0.4%"}], source="csv"))
    store.calendar_put(store_rows([ev]))  # the weekly feed has no actual
    [row] = store.calendar_history()
    assert row["actual"] == "0.4%" and row["source"] == "feed"


FF = [{"title": "CPI m/m", "country": "USD", "date": "2099-03-04T08:30:00-05:00", "impact": "High", "forecast": "0.3%"}]


def test_api_import_and_reactions_on_the_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "fetch_events", lambda url: parse_events(FF))
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, calendar=Calendar(store), brief=BriefRunner(store))
    runtime.calendar.refresh()
    assert [r["title"] for r in store.calendar_history()] == ["CPI m/m"]  # the fetched week went into the history
    monkeypatch.setattr(runtime.engine.feed, "persist", True)
    utc = pd.Timestamp("2026-02-11 13:30", tz="UTC")
    store.save_bars("synthetic", runtime.engine.symbol,
                    bars_around(utc_to_feed(utc, runtime.settings.resolved_clock), {0: 2000, 4: 2007, 59: 2010}))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))

    assert api.post("/api/calendar/import", content=b"nope").status_code == 422
    r = api.post("/api/calendar/import", content=b"datetime,currency,title,impact\n2026-02-11 13:30,USD,CPI m/m,High\n")
    assert r.json()["imported"] == 1 and r.json()["history"]["stored"] == 2
    [t] = api.get("/api/calendar/reactions").json()["types"]
    assert t["count"] == 1 and t["stored"] == 1 and t["move"]["60"] == pytest.approx(10.0, abs=0.05)
    [e] = api.get("/api/calendar").json()["week"]
    assert e["reaction"]["summary"].startswith("CPI m/m: median 15m range") and e["reaction"]["count"] == 1
    # The brief gets the same facts for the release coming up.
    sent = {}
    monkeypatch.setattr(runtime.brief, "start", lambda ctx, confirmed=False: sent.update(ctx))
    api.post("/api/brief/generate")
    line = sent[f"{runtime.engine.symbol} after past USD CPI m/m (M1)"]
    assert line.startswith("last 1 release: median 15m range") and "60m 10.00" in line
