"""Following the Rust server's engine (#96): bars, price and ticks come from it, nothing else."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wednesday import feeds
from wednesday.feeds import FollowFeed, build_feed, followed_settings
from wednesday.settings import DataSettings

BARS = [[1772409600 + 60 * i, 2000.0 + i, 2001.0 + i, 1999.0 + i, 2000.5 + i, 5.0] for i in range(5)]


@pytest.fixture
def rust():
    state = {"version": 3, "tick": 7}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/engine/m1"):
                count = int(self.path.split("count=")[1])
                body = {"source": "tradingview", "symbol": "FX:XAUUSD", "clock": "UTC", "version": state["version"],
                        "settings": {"source": "tradingview", "symbol": None, "clock": None, "tick_seconds": None},
                        "price": 2004.75, "bars": BARS[len(BARS) - count:] if count else [], "conn": "connected",
                        "error": None}
            elif self.path == "/api/tick":
                body = {"version": state["version"], "tick": state["tick"], "price": 2004.9, "time": 1772409900, "bar": None}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def test_the_follower_reads_the_rust_engine(rust, monkeypatch):
    base, state = rust
    monkeypatch.setenv(feeds.FOLLOW_ENV, base)
    feed = build_feed(DataSettings(source="tradingview"))
    assert isinstance(feed, FollowFeed) and feed.name == "tradingview" and not feed.persist
    feed.connect()
    m1 = feed.fetch_m1(3)
    assert list(m1["close"]) == [2002.5, 2003.5, 2004.5] and str(m1.index[-1]) == "2026-03-02 00:04:00"
    assert feed.last_price() == 2004.75
    [(at, price)] = feed.new_ticks()
    assert price == 2004.9 and str(at) == "2026-03-02 00:05:00"
    assert feed.new_ticks() == []  # the same tick isn't folded twice
    # The next fetch waits for the Rust engine's next scan, then takes what is there.
    monkeypatch.setattr(FollowFeed, "WAIT", 0.3)
    assert len(feed.fetch_m1(5)) == 5
    assert followed_settings(base).source == "tradingview"


def test_without_the_rust_server_it_says_so(monkeypatch):
    feed = FollowFeed("http://127.0.0.1:9", "tradingview")
    with pytest.raises(RuntimeError, match="The Rust server isn't answering"):
        feed.connect()


def test_following_rust_sends_no_alerts_and_tells_plugins_what_rust_sent(rust, monkeypatch, tmp_path):
    from wednesday.alerts import AlertManager
    from wednesday.engine import Runtime
    from wednesday.scanner import ScanConfig
    from wednesday.storage import Store

    base, _ = rust
    monkeypatch.setenv(feeds.FOLLOW_ENV, base)
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    runtime = Runtime(ScanConfig(), DataSettings(source="tradingview"), store, alerts=AlertManager(store, None))
    assert runtime.alerts is None and runtime.drawing_alerts is None  # Rust sends them
    heard = []
    runtime.hooks.on_alert.append(lambda keys, result: heard.append(keys))
    row = {"source": "tradingview", "symbol": "FX:XAUUSD", "timeframe": "M15", "priority": "extreme",
           "entry": 2000.0, "price": 2000.5, "status": "sent", "error": None}
    store.log_alert({**row, "key": "old", "kind": "bullish", "sent_at": "2000-01-01T00:00:00+00:00"})
    store.log_alert({**row, "key": "ob", "kind": "bullish", "sent_at": "2999-01-01T00:00:00+00:00"})
    store.log_alert({**row, "key": "line", "kind": "drawing", "sent_at": "2999-01-01T00:00:01+00:00"})
    store.log_alert({**row, "key": "failed", "kind": "bearish", "sent_at": "2999-01-01T00:00:02+00:00",
                     "status": "failed", "error": "x"})
    runtime._followed_alerts(runtime.engine, None)
    runtime._followed_alerts(runtime.engine, None)  # heard once
    assert heard == [["ob"]]


def test_following_rust_reads_the_bias_from_the_database(rust, monkeypatch, tmp_path):
    """A bias set through the Rust server (it writes the setting) is the one Python's plugins see."""
    from wednesday.bias import BIAS_KEY, TradeBias
    from wednesday.engine import Runtime
    from wednesday.scanner import ScanConfig
    from wednesday.storage import Store

    base, _ = rust
    monkeypatch.setenv(feeds.FOLLOW_ENV, base)
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    runtime = Runtime(ScanConfig(), DataSettings(source="tradingview"), store)
    assert runtime.following and runtime.bias is None and runtime.active_bias() is None
    store.set_setting(BIAS_KEY, TradeBias("bearish", "CPI hot", "none").stamped().to_dict())  # as Rust saves it
    assert runtime.bias.direction == "bearish" and runtime.active_bias().note == "CPI hot"
    store.delete_setting(BIAS_KEY)
    assert runtime.bias is None


def test_not_following_keeps_the_bias_it_was_given(monkeypatch, tmp_path):
    from wednesday.bias import BIAS_KEY, TradeBias
    from wednesday.engine import Runtime
    from wednesday.scanner import ScanConfig
    from wednesday.storage import Store

    monkeypatch.delenv(feeds.FOLLOW_ENV, raising=False)
    store = Store(f"sqlite:///{tmp_path / 'w.db'}")
    runtime = Runtime(ScanConfig(), DataSettings(source="synthetic"), store)
    runtime.set_bias(TradeBias("bullish").stamped())
    store.delete_setting(BIAS_KEY)
    assert runtime.bias.direction == "bullish"
