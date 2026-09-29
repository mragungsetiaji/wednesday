import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.quarters import NEW_YORK, utc_index_to_feed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.sessions import GROUPS, LABELS, reference_levels


def ny_bars(start: str, end: str, clock: str, spikes: dict[str, dict] | None = None) -> pd.DataFrame:
    """Flat M1 bars (2000 ± 0.5) over New York wall times, trading hours only, indexed in the feed clock.
    ``spikes`` sets fields of the bar at a New York wall time."""
    ny = pd.date_range(start, end, freq="1min", inclusive="left")
    trading = ~((ny.hour == 17) | ((ny.dayofweek == 4) & (ny.hour >= 17)) | (ny.dayofweek == 5) |
                ((ny.dayofweek == 6) & (ny.hour < 18)))
    ny = ny[trading]
    df = pd.DataFrame({"open": 2000.0, "high": 2000.5, "low": 1999.5, "close": 2000.0, "volume": 1.0}, index=ny)
    for at, fields in (spikes or {}).items():
        if pd.Timestamp(at) not in df.index:
            continue
        for k, v in fields.items():
            df.loc[pd.Timestamp(at), k] = v
    df.index = utc_index_to_feed(ny.tz_localize(NEW_YORK), clock)
    return df


def feed_unix(ny: str, clock: str) -> int:
    return int(utc_index_to_feed(pd.DatetimeIndex([ny]).tz_localize(NEW_YORK), clock).as_unit("s").asi8[0])


SPIKES = {
    "2026-03-09 10:00": {"high": 2020.0},  # Monday's high: Tuesday's PDH
    "2026-03-09 21:00": {"high": 2010.0},  # Tuesday's Asia high (Monday evening, New York)
    "2026-03-09 22:00": {"low": 1990.0},  # Tuesday's Asia low; takes Monday's low (Tuesday's PDL) out
    "2026-03-10 00:00": {"open": 2005.0, "high": 2005.0},  # midnight open
    "2026-03-10 03:00": {"high": 2011.0},  # London takes the Asia high
}


def lines_of(out: dict, day_start_unix: int) -> dict[str, dict]:
    """The lines drawn across the trading day that starts at ``day_start_unix`` (Asia ones from 20:00)."""
    return {ln["kind"]: ln for ln in out["lines"] if day_start_unix <= ln["start_unix"] < day_start_unix + 6 * 3600}


@pytest.mark.parametrize("clock", ["UTC", "NY+7", "Europe/London"])
def test_levels_match_a_hand_check_through_dst(clock):
    # Monday 2 to Wednesday 11 March 2026: New York moves to EDT on Sunday 8 March.
    out = reference_levels(ny_bars("2026-03-01 18:00", "2026-03-11 12:00", clock, SPIKES), clock)
    tue = lines_of(out, feed_unix("2026-03-09 18:00", clock))
    assert tue["pdh"]["price"] == 2020.0 and tue["pdh"]["swept_unix"] is None
    assert tue["pdl"]["price"] == 1999.5 and tue["pdl"]["swept_unix"] == feed_unix("2026-03-09 22:00", clock)
    assert tue["day_open"]["price"] == 2000.0 and tue["day_open"]["end_unix"] == feed_unix("2026-03-10 17:00", clock)
    mid = next(ln for ln in out["lines"] if ln["kind"] == "midnight_open" and ln["start_unix"] == feed_unix("2026-03-10 00:00", clock))
    assert mid["price"] == 2005.0
    asia = {ln["kind"]: ln for ln in out["lines"] if ln["kind"].startswith("asia") and ln["start_unix"] == feed_unix("2026-03-09 20:00", clock)}
    assert asia["asia_high"]["price"] == 2010.0 and asia["asia_high"]["swept_unix"] == feed_unix("2026-03-10 03:00", clock)
    assert asia["asia_low"]["price"] == 1990.0 and asia["asia_low"]["swept_unix"] is None  # nothing after 00:00 went lower

    # Killzones sit at the same New York hours on both sides of the change.
    kz = {(k["day"], k["name"]): k for k in out["killzones"]}
    for day, before in (("2026-03-05", "2026-03-05"), ("2026-03-10", "2026-03-10")):
        assert kz[(day, "London")]["start_unix"] == feed_unix(f"{before} 02:00", clock)
        assert kz[(day, "NY PM")]["end_unix"] == feed_unix(f"{before} 16:00", clock)
    assert kz[("2026-03-10", "Asia")]["start_unix"] == feed_unix("2026-03-09 20:00", clock)
    if clock == "UTC":
        assert kz[("2026-03-05", "London")]["start_unix"] - kz[("2026-03-10", "London")]["start_unix"] != 5 * 86400

    # The week of 9 March carries the previous week's range and its own open.
    pw = {ln["kind"]: ln for ln in out["lines"] if ln["kind"] in ("pwh", "pwl") and ln["current"]}
    assert pw["pwh"]["price"] == 2000.5 and pw["pwh"]["start_unix"] == feed_unix("2026-03-08 18:00", clock)
    assert pw["pwh"]["swept_unix"] == feed_unix("2026-03-09 10:00", clock)
    assert pw["pwh"]["end_unix"] == feed_unix("2026-03-13 17:00", clock)


def test_asia_range_waits_for_its_window_to_close():
    clock = "UTC"
    during = reference_levels(ny_bars("2026-03-08 18:00", "2026-03-09 23:00", clock, SPIKES), clock)
    assert not [ln for ln in during["lines"] if ln["kind"].startswith("asia") and ln["start_unix"] == feed_unix("2026-03-09 20:00", clock)]
    after = reference_levels(ny_bars("2026-03-08 18:00", "2026-03-10 00:01", clock, SPIKES), clock)
    assert [ln for ln in after["lines"] if ln["kind"] == "asia_high" and ln["start_unix"] == feed_unix("2026-03-09 20:00", clock)]


def test_a_period_only_gives_levels_when_the_history_covers_all_of_it():
    clock = "UTC"
    # History from Wednesday noon: no previous week, and Wednesday is not a whole day.
    out = reference_levels(ny_bars("2026-03-04 12:00", "2026-03-11 12:00", clock), clock)
    kinds = [ln["kind"] for ln in out["lines"]]
    assert "pmh" not in kinds
    pwh = [ln for ln in out["lines"] if ln["kind"] == "pwh"]
    assert pwh == []  # the week of 2 March is partial, so the week of 9 March has no PWH
    first_pdh = min((ln for ln in out["lines"] if ln["kind"] == "pdh"), key=lambda ln: ln["start_unix"])
    assert first_pdh["start_unix"] == feed_unix("2026-03-05 18:00", clock)  # Friday's, from a whole Thursday


def test_groups_cover_every_kind():
    assert {k for ks in GROUPS.values() for k in ks} == set(LABELS)


def test_sessions_endpoint(tmp_path):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    engine = Engine(SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-12 12:00")),
                    cfg, "XAUUSD")
    api = TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path))
    assert api.get("/api/sessions").status_code == 503
    engine.step()
    body = api.get("/api/sessions").json()
    assert body["clock"] == "UTC" and len(body["killzones"]) % 4 == 0 and body["lines"]
    last = int(engine.snapshot()[2].index[-1].timestamp())
    assert all(ln["swept_unix"] is None or ln["swept_unix"] <= last for ln in body["lines"])
    assert np.all([ln["start_unix"] < ln["end_unix"] for ln in body["lines"]])
