from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from wednesday import news
from wednesday.engine import Runtime
from wednesday.news import Calendar, CalendarSettings, parse_events
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

NOW = datetime(2026, 3, 4, 13, 0, tzinfo=timezone.utc)
FF = [  # ForexFactory's weekly JSON shape
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-04T08:30:00-05:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Core CPI m/m", "country": "USD", "date": "2026-03-04T08:30:00-05:00", "impact": "High", "forecast": "", "previous": "0.3%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-03-04T10:30:00-05:00", "impact": "Medium"},
    {"title": "ECB Speech", "country": "EUR", "date": "2026-03-04T09:00:00-05:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-03-04", "impact": "Holiday"},
    {"title": "FOMC Minutes", "country": "USD", "date": "2026-03-03T14:00:00-05:00", "impact": "High"},
]


def test_parse_events_to_utc():
    events = parse_events(FF)
    assert [e["title"] for e in events][:2] == ["FOMC Minutes", "CPI m/m"]  # sorted, the dateless holiday dropped
    cpi = events[1]
    assert cpi["time"] == "2026-03-04T13:30:00+00:00" and cpi["currency"] == "USD" and cpi["forecast"] == "0.3%"
    assert events[2]["forecast"] is None  # empty string -> None


def test_upcoming_filters_currency_impact_and_past():
    cal = Calendar(None)
    cal.events = parse_events(FF)
    titles = [e["title"] for e in cal.upcoming(NOW)]
    assert titles == ["CPI m/m", "Core CPI m/m"]  # USD, High, not yesterday's
    cal.settings = CalendarSettings(currencies=["USD", "EUR"], impacts=["High", "Medium"])
    assert [e["title"] for e in cal.upcoming(NOW)] == ["CPI m/m", "Core CPI m/m", "ECB Speech", "Crude Oil Inventories"]
    # A release stays listed for 30 minutes.
    assert cal.upcoming(NOW + timedelta(minutes=59))[0]["title"] == "CPI m/m"
    assert "CPI m/m" not in [e["title"] for e in cal.upcoming(NOW + timedelta(minutes=61))]


def test_refresh_caches_and_keeps_old_data_on_error(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'n.db'}")
    monkeypatch.setattr(news, "fetch_events", lambda url: parse_events(FF))
    cal = Calendar(store)
    assert cal.stale(NOW)
    cal.refresh()
    assert len(cal.events) == 5 and cal.error is None
    again = Calendar(store)  # restart: loads the cache instead of refetching
    assert len(again.events) == 5 and not again.stale(datetime.fromisoformat(again.fetched_at) + timedelta(minutes=5))

    def boom(url):
        raise RuntimeError("Can't reach the calendar source: blocked")

    monkeypatch.setattr(news, "fetch_events", boom)
    again.refresh()
    assert again.error.endswith("blocked") and len(again.events) == 5
    again.settings = CalendarSettings(url="https://other.example/cal.json")
    assert again.stale(NOW) and again.snapshot()["events"] == []  # cache from another source isn't shown


def test_settings_validation():
    assert CalendarSettings(url="file:///x").validate()
    assert CalendarSettings(impacts=["Huge"]).validate()
    assert CalendarSettings.from_dict({"currencies": [" usd ", ""]}).currencies == ["USD"]


def test_calendar_api(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "fetch_events", lambda url: parse_events(FF))
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, calendar=Calendar(store))
    runtime.calendar.refresh()
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    body = api.get("/api/calendar").json()
    assert body["editable"] and body["fetched_at"] and body["settings"]["currencies"] == ["USD"]
    assert api.put("/api/calendar", json={"impacts": ["Huge"]}).status_code == 422
    saved = api.put("/api/calendar", json={"currencies": ["USD", "EUR"], "impacts": ["High"]}).json()
    assert saved["settings"]["currencies"] == ["USD", "EUR"] and store.get_setting("calendar")["currencies"] == ["USD", "EUR"]
