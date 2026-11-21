"""TradingView's websocket protocol (tradingview.py): framing, sessions, and a whole call against a
fake TradingView server on 127.0.0.1. The Rust client (wed-engine/src/tradingview.rs) has the
same unit tests; neither touches the network.
"""

import json
import threading
import time

import pytest

from wednesday import tradingview as tv
from wednesday.tradingview import Bars, Connection, TradingViewError

# ---- framing and sessions ----


def test_frames_round_trip():
    a = tv.encode_packet("set_auth_token", ["unauthorized_user_token"])
    assert a == '~m~54~m~{"m":"set_auth_token","p":["unauthorized_user_token"]}'
    frames = tv.decode_frames(a + tv.encode_frame("~h~12"))
    assert frames == [("packet", {"m": "set_auth_token", "p": ["unauthorized_user_token"]}), ("heartbeat", "12")]
    assert tv.decode_frames('~m~16~m~{"session_id":1}')[0][0] == "data"
    assert tv.decode_frames("~m~3~m~abc")[0][0] == "invalid"


def test_frame_lengths_count_utf16_units():
    body = '{"m":"x","p":["é😀"]}'
    framed = tv.encode_frame(body)
    assert framed == f"~m~21~m~{body}"
    assert tv.decode_frames(framed * 2) == [("packet", {"m": "x", "p": ["é😀"]})] * 2


def test_malformed_input_splits_on_headers():
    frames = tv.decode_frames('~m~x~m~{"m":"a","p":[]}~m~5~m~~h~42')
    assert frames[0][0] == "invalid" and frames[1] == ("heartbeat", "42")
    assert tv.decode_frames("junk~m~5~m~~h~42") == [("invalid", "junk"), ("heartbeat", "42")]


def test_keys_keep_javascripts_order():
    assert tv.symbol_init("FX:XAUUSD") == '={"symbol":"FX:XAUUSD","adjustment":"splits"}'
    assert tv.quote_key("FX:XAUUSD") == '={"session":"regular","symbol":"FX:XAUUSD"}'
    sid = tv.session_id("cs")
    assert sid.startswith("cs_") and len(sid) == 15 and sid != tv.session_id("cs")


def _packet(m, p):
    return {"m": m, "p": p}


def test_bars_merge_by_time_and_select_the_latest():
    bars = Bars()
    bars.update(_packet("timescale_update", ["cs_1", {"$prices": {"s": [
        {"i": 0, "v": [120.0, 1, 2, 0.5, 1.5, 10]}, {"i": 1, "v": [60.0, 1, 2, 0.5, 1.5]}]}}]))
    bars.update(_packet("du", ["cs_1", {"$prices": {"s": [{"i": 1, "v": [120.0, 9, 9, 9, 9, 3]}]}}]))
    assert len(bars) == 2
    assert bars.select(5, None) == [[60.0, 1, 2, 0.5, 1.5, 0], [120.0, 9, 9, 9, 9, 3]]
    assert bars.select(1, None) == [[120.0, 9, 9, 9, 9, 3]]
    assert bars.select(5, 100) == [[60.0, 1, 2, 0.5, 1.5, 0]]


def test_history_asks_for_more_until_enough_or_exhausted():
    bars = Bars()
    bars.update(_packet("du", ["cs", {"$prices": {"s": [{"i": i, "v": [i * 60, 1, 1, 1, 1, 0]}
                                                           for i in range(5000)]}}]))
    assert tv.next_step(bars, 7001, None, False, None) == 2001
    assert tv.next_step(bars, 12000, None, False, None) == 5000
    assert tv.next_step(bars, 5000, None, False, None) is None
    assert tv.next_step(bars, 7001, None, True, None) is None
    assert tv.next_step(bars, 7001, None, False, 5000) is None  # nothing new came
    assert tv.next_step(bars, 100, 59 * 60, False, None) == 40  # bars after `to` don't count


def test_quotes_merge_and_complete():
    key, bad = tv.quote_key("FX:XAUUSD"), tv.quote_key("FX:NOPE")
    quotes = {key: tv._new_quote(), bad: tv._new_quote()}
    tv.apply_quote(quotes, _packet("qsd", ["qs_1", {"n": key, "s": "ok", "v": {"lp": 2650.5}}]))
    tv.apply_quote(quotes, _packet("qsd", ["qs_1", {"n": key, "s": "ok", "v": {"lp_time": 1763632800}}]))
    assert not quotes[key]["loaded"]
    tv.apply_quote(quotes, _packet("quote_completed", ["qs_1", key]))
    assert quotes[key]["loaded"] and quotes[key]["data"] == {"lp": 2650.5, "lp_time": 1763632800}
    tv.apply_quote(quotes, _packet("qsd", ["qs_1", {"n": bad, "s": "error", "errmsg": "invalid symbol"}]))
    assert quotes[bad]["error"] == "invalid symbol"
    tv.apply_quote(quotes, _packet("qsd", ["qs_1", {"n": "=other", "s": "ok", "v": {"lp": 1}}]))
    assert len(quotes) == 2


def test_chart_errors_read_as_the_library_words_them():
    e = tv.chart_error(_packet("symbol_error", ["cs_1", "ser_1", "invalid symbol"]))
    assert (e.code, str(e)) == ("SYMBOL_ERROR", "TradingView: (ser_1) Symbol error: invalid symbol")
    e = tv.chart_error(_packet("series_error", ["cs_1", "$prices", "s1", "no data"]))
    assert (e.code, str(e)) == ("SERIES_ERROR", "TradingView: Series error: no data")
    assert tv.chart_error(_packet("du", ["cs_1"])) is None


# ---- a fake TradingView ----

NOW = 1763632800  # 2025-11-20 10:00 UTC
HISTORY = 12_000  # M1 bars the fake has


class FakeTradingView:
    """Answers the packets the client sends as TradingView does, from a fixed history."""

    def __init__(self, silent_quotes=False):
        from websockets.sync.server import serve

        self.received: list[dict] = []
        self.heartbeats: list[str] = []
        self.silent_quotes = silent_quotes
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def handle(self, ws):
        def send(*packets):
            ws.send("".join(p if isinstance(p, str) else tv.encode_frame(json.dumps(p)) for p in packets))

        send({"session_id": "fake", "timestamp": NOW}, tv.encode_frame("~h~1"))
        charts: dict[str, dict] = {}
        for message in ws:
            for kind, value in tv.decode_frames(message):
                if kind == "heartbeat":
                    self.heartbeats.append(value)
                    continue
                self.received.append(value)
                m, p = value["m"], value["p"]
                if m == "resolve_symbol":
                    symbol = json.loads(p[2][1:])["symbol"]
                    charts[p[0]] = {"symbol": symbol, "sent": 0}
                    if symbol == "FX:DROP":
                        ws.close()
                        return
                    if symbol == "FX:NOPE":
                        send({"m": "symbol_error", "p": [p[0], p[1], "invalid symbol"]})
                    else:
                        send({"m": "symbol_resolved", "p": [p[0], p[1], {
                            "full_name": symbol, "description": "Gold Spot", "exchange": "FXCM",
                            "timezone": "Etc/UTC", "session": "24x7", "pricescale": 100, "type": "forex"}]})
                elif m == "create_series" and charts[p[0]]["symbol"] != "FX:NOPE":
                    chart = charts[p[0]]
                    chart["step"] = int(p[4]) * 60 if p[4].isdigit() else 86400
                    chart["to"] = p[5][1] if isinstance(p[5], list) else None
                    n = p[5][2] if isinstance(p[5], list) else p[5]
                    send(*self.bars(p[0], chart, n))
                elif m == "request_more_data":
                    send(*self.bars(p[0], charts[p[0]], p[2]))
                elif m == "quote_add_symbols" and not self.silent_quotes:
                    symbol = json.loads(p[1][1:])["symbol"]
                    if symbol == "FX:NOPE":
                        send({"m": "qsd", "p": [p[0], {"n": p[1], "s": "error", "errmsg": "invalid symbol"}]})
                    else:
                        send({"m": "qsd", "p": [p[0], {"n": p[1], "s": "ok", "v": {"lp": 4010.25}}]},
                             {"m": "qsd", "p": [p[0], {"n": p[1], "s": "ok", "v": {"lp_time": NOW + 5}}]},
                             {"m": "quote_completed", "p": [p[0], p[1]]})

    def bars(self, cs, chart, n):
        """The next ``n`` older bars (the newest first), then series_completed."""
        step, newest = chart["step"], (chart["to"] or NOW) // chart["step"] * chart["step"]
        start, chart["sent"] = chart["sent"], min(HISTORY, chart["sent"] + n)
        rows = [{"i": i, "v": [newest - step * i, 4000 + i, 4001 + i, 3999 + i, 4000.5 + i]}
                for i in range(start, chart["sent"])]
        done = {"data_completed": "end"} if chart["sent"] >= HISTORY else {}
        return ({"m": "timescale_update", "p": [cs, {"$prices": {"s": rows}}]},
                {"m": "series_completed", "p": [cs, "$prices", "streaming", "s1", done]})


@pytest.fixture
def fake():
    server = FakeTradingView()
    yield server
    server.close()


def test_a_whole_session_against_the_fake(fake):
    c = Connection(fake.url)
    try:
        info = c.call("info", 10, symbol="FX:XAUUSD")
        assert info == {"full_name": "FX:XAUUSD", "description": "Gold Spot", "exchange": "FXCM",
                        "timezone": "Etc/UTC", "session": "24x7", "pricescale": 100}
        rows = c.call("candles", 10, symbol="FX:XAUUSD", timeframe="1", count=7001, timeoutMs=1)
        assert len(rows) == 7001 and rows[-1][0] == NOW and rows[0][0] == NOW - 7000 * 60
        assert all(a[0] < b[0] for a, b in zip(rows, rows[1:]))
        assert rows[-1] == [NOW, 4000, 4001, 3999, 4000.5, 0]  # no volume: 0
        # Scrolling back: bars up to `to`.
        older = c.call("candles", 10, symbol="FX:XAUUSD", timeframe="60", count=10, to=NOW - 3600 * 5)
        assert [r[0] for r in older] == [NOW - 3600 * k for k in range(14, 4, -1)]
        # More than the server has: what it has.
        assert len(c.call("candles", 10, symbol="FX:XAUUSD", timeframe="1", count=15000)) == HISTORY
        assert c.call("quote", 10, symbol="FX:XAUUSD") == {"price": 4010.25, "time": NOW + 5}
        assert c.call("quote", 10, symbol="FX:XAUUSD") == {"price": 4010.25, "time": NOW + 5}
        with pytest.raises(TradingViewError) as exc:
            c.call("info", 10, symbol="FX:NOPE")
        assert exc.value.code == "SYMBOL_ERROR"
        with pytest.raises(TradingViewError) as exc:
            c.call("candles", 10, symbol="FX:NOPE", timeframe="1", count=5)
        assert exc.value.code == "SYMBOL_ERROR"
        with pytest.raises(TradingViewError) as exc:
            c.call("quote", 10, symbol="FX:NOPE")
        assert exc.value.code == "QUOTE_ERROR" and "invalid symbol" in str(exc.value)
    finally:
        c.close()
    sent = [p["m"] for p in fake.received]
    assert sent[0] == "set_auth_token" and fake.received[0]["p"] == ["unauthorized_user_token"]
    assert "request_more_data" in sent
    assert sent.count("quote_create_session") == 1 and sent.count("chart_delete_session") == 6
    assert fake.heartbeats == ["1"]  # echoed


def test_no_quote_in_time_is_a_timeout():
    server = FakeTradingView(silent_quotes=True)
    c = Connection(server.url)
    try:
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="didn't answer within 1 s"):
            c.call("quote", 0.6, symbol="FX:XAUUSD")
        assert time.monotonic() - start < 3
        assert c.call("info", 5, symbol="FX:XAUUSD")["exchange"] == "FXCM"  # the line still works
    finally:
        c.close()
        server.close()


def test_a_dropped_line_is_reopened(fake):
    c = Connection(fake.url)
    try:
        with pytest.raises(TradingViewError) as exc:
            c.call("info", 5, symbol="FX:DROP")
        assert exc.value.code == "DISCONNECTED" and "getSymbolInfo(FX:DROP)" in str(exc.value)
        assert c.call("info", 5, symbol="FX:XAUUSD")["exchange"] == "FXCM"
    finally:
        c.close()
    assert [p["m"] for p in fake.received].count("set_auth_token") == 2


def test_bad_calls():
    c = Connection("ws://127.0.0.1:9")
    with pytest.raises(TradingViewError, match="symbol is required"):
        c.call("info", 1, symbol=" ")
    with pytest.raises(TradingViewError, match="Unknown op"):
        c.call("nope", 1, symbol="FX:XAUUSD")
    with pytest.raises(TradingViewError) as exc:
        c.call("info", 5, symbol="FX:XAUUSD")  # nothing listens there
    assert exc.value.code == "CONNECTION_ERROR"
