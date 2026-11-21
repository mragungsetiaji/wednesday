"""TradingView market data over its websocket, spoken directly (no Node helper).

The protocol is unofficial: it is what the TradingView web chart sends, as the community library
@mathieuc/tradingview (ISC) documents it. Every websocket message holds frames
``~m~<length>~m~<payload>``, the length in UTF-16 code units. A payload is a JSON packet
``{"m": method, "p": [params]}``, a server greeting, or a heartbeat ``~h~<id>`` that the client
echoes. Chart sessions (``cs_...``) resolve a symbol and load its bars; one quote session
(``qs_...``) streams the live price.

:class:`Connection` answers the three questions the feed and the other-symbol panes ask, with the
answers the old helper (``tradingview/feed.mjs``) gave:

- ``info`` ``{symbol}``: ``{full_name, description, exchange, timezone, session, pricescale}``
- ``candles`` ``{symbol, timeframe, count, to?}``: rows ``[time, open, high, low, close, volume]``,
  oldest first, UTC unix seconds; ``to`` is the newest bar time when scrolling back
- ``quote`` ``{symbol}``: ``{price, time}`` or None before the first price

One websocket stays open between calls and is reopened when TradingView drops it. The Rust server
has the same client (``wed-engine/src/tradingview.rs``).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time

log = logging.getLogger(__name__)

URL = "wss://data.tradingview.com/socket.io/websocket?from=chart&type=chart"
ORIGIN = "https://www.tradingview.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
MAX_CHUNK = 5_000  # the most bars one request asks for; deeper history comes in more requests
MAX_COUNT = 20_000  # the most bars one candles call loads
IDLE = 60.0  # the server sends a heartbeat every few seconds; this long without anything, the line is dead
CONNECT_TIMEOUT = 20.0


class TradingViewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"TradingView: {message}")
        self.code = code


class _Timeout(Exception):
    """Out of time: the caller words it."""


# ---- framing ----

def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def encode_frame(body: str) -> str:
    """``~m~<length>~m~<body>``, the length in UTF-16 code units as JavaScript counts it."""
    return f"~m~{_utf16_len(body)}~m~{body}"


def encode_packet(method: str, params: list) -> str:
    return encode_frame(json.dumps({"m": method, "p": params}, separators=(",", ":"), ensure_ascii=False))


def _utf16_end(s: str, units: int) -> int:
    """The index ``units`` UTF-16 code units into ``s`` (its end when shorter)."""
    seen = 0
    for i, c in enumerate(s):
        if seen >= units:
            return i
        seen += 2 if ord(c) > 0xFFFF else 1
    return len(s)


_HEADER = re.compile(r"~m~[0-9]+~m~")


def _decode_payload(raw: str) -> tuple[str, object]:
    if raw.startswith("~h~"):
        hb = raw[3:]
        try:
            float(hb)
        except ValueError:
            return "invalid", raw
        return "heartbeat", hb
    try:
        data = json.loads(raw)
    except ValueError:
        return "invalid", raw
    if isinstance(data, dict) and isinstance(data.get("m"), str):
        p = data.get("p")
        return "packet", {"m": data["m"], "p": p if isinstance(p, list) else []}
    return "data", data


def decode_frames(text: str) -> list[tuple[str, object]]:
    """The frames of one websocket message: (kind, value), kind heartbeat, packet, data or invalid."""
    payloads: list[str] = []
    pos = 0
    while pos < len(text):
        m = re.match(r"~m~([0-9]+)~m~", text[pos:])
        if m is None:
            # Lenient fallback for malformed input: the parts between frame headers.
            payloads.extend(p for p in _HEADER.split(text[pos:]) if p)
            break
        body = text[pos + m.end():]
        end = _utf16_end(body, int(m.group(1)))
        payloads.append(body[:end])
        pos += m.end() + end
    return [_decode_payload(p) for p in payloads]


# ---- sessions ----

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def session_id(prefix: str) -> str:
    """A random session id such as ``cs_Ab12Cd34Ef56``."""
    return prefix + "_" + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def symbol_init(symbol: str) -> str:
    return "=" + json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"), ensure_ascii=False)


def quote_key(symbol: str) -> str:
    return "=" + json.dumps({"session": "regular", "symbol": symbol}, separators=(",", ":"), ensure_ascii=False)


def _js_string(v: object, missing: bool = False) -> str:
    """JavaScript's ``String(value)`` for the error texts TradingView sends."""
    if missing:
        return "undefined"
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return "[object Object]"
    if isinstance(v, list):
        return ",".join("" if x is None else _js_string(x) for x in v)
    return json.dumps(v)


def _arg(p: list, i: int) -> str:
    return _js_string(p[i]) if i < len(p) else "undefined"


def chart_error(packet: dict) -> TradingViewError | None:
    """The error a chart session packet carries, if it is one."""
    p, m = packet["p"], packet["m"]
    if m == "symbol_error":
        return TradingViewError("SYMBOL_ERROR", f"({_arg(p, 1)}) Symbol error: {_arg(p, 2)}")
    if m == "series_error":
        return TradingViewError("SERIES_ERROR", f"Series error: {_arg(p, 3)}")
    if m == "critical_error":
        return TradingViewError("CRITICAL_ERROR", f"Critical error: {_arg(p, 1)}")
    return None


class Bars:
    """Bars a chart session has loaded, by time."""

    def __init__(self):
        self.rows: dict[int, list] = {}

    def update(self, packet: dict) -> None:
        """A ``timescale_update``/``du`` packet's ``$prices`` bars."""
        p = packet["p"]
        data = p[1] if len(p) > 1 and isinstance(p[1], dict) else {}
        prices = data.get("$prices")
        for bar in (prices.get("s") or []) if isinstance(prices, dict) else []:
            v = bar.get("v") if isinstance(bar, dict) else None
            if not isinstance(v, list) or not v or not isinstance(v[0], (int, float)):
                continue
            row = [v[i] if i < len(v) else None for i in range(5)]
            row.append(v[5] if len(v) > 5 and v[5] is not None else 0)
            self.rows[int(v[0])] = row

    def __len__(self) -> int:
        return len(self.rows)

    def in_range(self, to: int | None) -> int:
        return len(self.rows) if to is None else sum(1 for t in self.rows if t <= to)

    def select(self, count: int, to: int | None) -> list[list]:
        """The last ``count`` bars up to ``to``, oldest first."""
        times = sorted(t for t in self.rows if to is None or t <= to)
        return [self.rows[t] for t in times[-count:]]


def next_step(bars: Bars, count: int, to: int | None, data_completed: bool, previous: int | None) -> int | None:
    """After each ``series_completed``: None when done, else how many older bars to ask for."""
    in_range = bars.in_range(to)
    exhausted = data_completed or (previous is not None and len(bars) <= previous)
    if in_range >= count or exhausted or len(bars) >= MAX_COUNT:
        return None
    return max(1, min(count - in_range, MAX_CHUNK, MAX_COUNT - len(bars)))


def apply_quote(quotes: dict[str, dict], packet: dict) -> None:
    """Quote packets (``qsd``, ``quote_completed``) into the quotes they're about."""
    p = packet["p"]
    if packet["m"] == "quote_completed":
        q = quotes.get(p[1]) if len(p) > 1 and isinstance(p[1], str) else None
        if q is not None:
            q["loaded"] = True
    elif packet["m"] == "qsd" and len(p) > 1 and isinstance(p[1], dict):
        payload = p[1]
        q = quotes.get(payload.get("n")) if isinstance(payload.get("n"), str) else None
        if q is None:
            return
        if payload.get("s") == "ok" and isinstance(payload.get("v"), dict):
            q["data"].update(payload["v"])
        elif payload.get("s") == "error":
            q["error"] = payload.get("errmsg") or "unknown"


def _new_quote() -> dict:
    return {"data": {}, "loaded": False, "error": None}


# ---- the websocket ----

class Client:
    """One open websocket: a reader thread answers heartbeats, keeps the quotes and hands chart
    packets over."""

    def __init__(self, timeout: float = CONNECT_TIMEOUT, url: str = URL):
        from websockets.sync.client import connect

        try:
            self._ws = connect(url, origin=ORIGIN, user_agent_header=USER_AGENT, open_timeout=timeout,
                               additional_headers={"Accept-Language": "en-US,en;q=0.9",
                                                   "Cache-Control": "no-cache", "Pragma": "no-cache"},
                               compression=None, max_size=None).__enter__()  # websockets 17 wants it entered
        except TimeoutError as exc:
            raise TradingViewError("TIMEOUT", f"Connection not ready after {int(timeout * 1000)} ms") from exc
        except Exception as exc:  # noqa: BLE001 - websockets' handshake errors, OSError, ...
            raise TradingViewError("CONNECTION_ERROR", f"WebSocket error: {exc}") from exc
        self._packets: queue.Queue = queue.Queue()
        self._cond = threading.Condition()
        self._quotes: dict[str, dict] = {}
        self._quote_session: str | None = None
        self.closed: str | None = None  # why the line is gone
        threading.Thread(target=self._read, daemon=True, name="tradingview-ws").start()
        self._send("set_auth_token", ["unauthorized_user_token"])

    def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        why = "the server closed the connection"
        try:
            while True:
                try:
                    msg = self._ws.recv(timeout=IDLE)
                except TimeoutError:
                    why = f"No data from the server for {int(IDLE * 1000)} ms"
                    break
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                stop = None
                for kind, value in decode_frames(msg):
                    if kind == "heartbeat":
                        self._raw(encode_frame(f"~h~{value}"))
                    elif kind == "packet" and value["m"] == "protocol_error":
                        stop = f"Protocol error: {_arg(value['p'], 0) if value['p'] else 'undefined'}"
                        break
                    elif kind == "packet" and value["m"] in ("qsd", "quote_completed"):
                        with self._cond:
                            apply_quote(self._quotes, value)
                            self._cond.notify_all()
                    elif kind == "packet":
                        self._packets.put(value)
                    elif kind == "invalid":
                        log.debug("tradingview: invalid frame %s", value)
                if stop:
                    why = stop
                    break
        except ConnectionClosed as exc:
            reason = getattr(exc.rcvd, "reason", "") if exc.rcvd else ""
            why = reason or why
        except Exception as exc:  # noqa: BLE001 - the line broke
            why = f"WebSocket error: {exc}"
        log.info("tradingview: connection closed: %s", why)
        with self._cond:
            self.closed = why
            self._cond.notify_all()
        self._packets.put(None)  # a call waiting for packets learns the line is gone
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _raw(self, text: str) -> None:
        try:
            self._ws.send(text)
        except Exception as exc:  # noqa: BLE001
            raise TradingViewError("DISCONNECTED", f"Connection closed: {self.closed or exc}") from exc

    def _send(self, method: str, params: list) -> None:
        if self.closed is not None:
            raise TradingViewError("DISCONNECTED", f"Connection closed: {self.closed}")
        self._raw(encode_packet(method, params))

    def _next_packet(self, cs: str, deadline: float, what: str) -> dict:
        """The next packet for chart session ``cs`` (others are late answers to earlier calls)."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Timeout
            try:
                packet = self._packets.get(timeout=left)
            except queue.Empty:
                raise _Timeout from None
            if packet is None:
                self._packets.put(None)  # for the next call too
                raise TradingViewError("DISCONNECTED", f"Connection closed during {what}: {self.closed}")
            if packet["p"][:1] == [cs]:
                return packet

    def _open_chart(self, symbol: str, timeframe: str, range_) -> str:
        cs = session_id("cs")
        self._send("chart_create_session", [cs])
        self._send("resolve_symbol", [cs, "ser_1", symbol_init(symbol)])
        self._send("create_series", [cs, "$prices", "s1", "ser_1", timeframe, range_])
        return cs

    def _close_chart(self, cs: str) -> None:
        try:
            self._send("chart_delete_session", [cs])
        except TradingViewError:
            pass

    def info(self, symbol: str, deadline: float) -> dict:
        cs = self._open_chart(symbol, "D", 1)
        try:
            while True:
                packet = self._next_packet(cs, deadline, f"getSymbolInfo({symbol})")
                error = chart_error(packet)
                if error:
                    raise error
                if packet["m"] == "symbol_resolved":
                    i = packet["p"][2] if len(packet["p"]) > 2 and isinstance(packet["p"][2], dict) else {}
                    return {k: i.get(k) for k in ("full_name", "description", "exchange", "timezone", "session",
                                                  "pricescale")}
        finally:
            self._close_chart(cs)

    def candles(self, symbol: str, timeframe: str, count: int, to: int | None, deadline: float) -> list[list]:
        if count < 1:
            raise TradingViewError("INVALID_ARGUMENT", "count must be a positive integer")
        first = min(count, MAX_CHUNK)
        cs = self._open_chart(symbol, timeframe, first if to is None else ["bar_count", to, first])
        bars, previous, completed = Bars(), None, None
        try:
            while True:
                packet = self._next_packet(cs, deadline, f"getCandles({symbol})")
                error = chart_error(packet)
                if error:
                    raise error
                if packet["m"] in ("timescale_update", "du"):
                    bars.update(packet)
                elif packet["m"] == "series_completed":
                    p = packet["p"]
                    details = p[4] if len(p) > 4 and isinstance(p[4], dict) else {}
                    completed = details.get("data_completed")
                    more = next_step(bars, count, to, "data_completed" in details, previous)
                    if more is None:
                        break
                    previous = len(bars)
                    self._send("request_more_data", [cs, "$prices", more])
        finally:
            self._close_chart(cs)
        rows = bars.select(count, to)
        if not rows:
            limit = (" (the server reported an access limit: this range or timeframe may need an account with "
                     "access)") if completed == "limit" else ""
            raise TradingViewError("NO_DATA", f"No candles available for {symbol} in the requested range{limit}")
        return rows

    def quote(self, symbol: str, deadline: float) -> dict | None:
        """The live price and its time, None before the first price. The first call for a symbol
        subscribes it and waits for its first values; later calls read the latest."""
        if self._quote_session is None:
            qs = session_id("qs")
            self._send("quote_create_session", [qs])
            self._send("quote_set_fields", [qs, "lp", "lp_time"])
            self._quote_session = qs
        qs, key = self._quote_session, quote_key(symbol)
        with self._cond:
            new = key not in self._quotes
            if new:
                self._quotes[key] = _new_quote()
        if new:
            self._send("quote_add_symbols", [qs, key])
        with self._cond:
            while True:
                q = self._quotes.get(key) or _new_quote()
                if q["loaded"]:
                    price = q["data"].get("lp")
                    return None if price is None else {"price": price, "time": q["data"].get("lp_time")}
                error: Exception | None = None
                if q["error"] is not None:
                    error = TradingViewError("QUOTE_ERROR", f"Quote error ({symbol}): {q['error']}")
                elif self.closed is not None:
                    error = TradingViewError("DISCONNECTED",
                                             f"Connection closed during watchQuotes({symbol}): {self.closed}")
                elif time.monotonic() >= deadline:
                    error = _Timeout()
                if error is not None:
                    self._quotes.pop(key, None)  # the next call subscribes again
                    break
                self._cond.wait(timeout=max(0.0, min(0.5, deadline - time.monotonic())))
        try:
            self._send("quote_remove_symbols", [qs, key])
        except TradingViewError:
            pass
        raise error

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class Connection:
    """TradingView for the feed and the other-symbol panes: one websocket, opened on the first
    call and again after it drops, answering ``info``, ``candles`` and ``quote`` as ``feed.mjs``
    did."""

    def __init__(self, url: str = URL):
        self.url = url  # another server: tests
        self._client: Client | None = None

    def call(self, op: str, timeout: float, **args):
        """Answer ``op`` within ``timeout`` seconds; TradingView's errors as :class:`TradingViewError`,
        running out of time as a RuntimeError."""
        deadline = time.monotonic() + timeout
        try:
            return self._answer(op, args, deadline)
        except _Timeout:
            raise RuntimeError(f"TradingView didn't answer within {timeout:.0f} s. "
                               "Check the internet connection.") from None

    def _answer(self, op: str, args: dict, deadline: float):
        symbol = str(args.get("symbol") or "").strip()
        if not symbol:
            raise TradingViewError("INVALID_ARGUMENT", "symbol is required")
        if op not in ("info", "candles", "quote"):
            raise TradingViewError("BAD_REQUEST", f"Unknown op {op}")
        if self._client is None or self._client.closed is not None:
            self.close()
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Timeout
            self._client = Client(min(CONNECT_TIMEOUT, left), self.url)
        client = self._client
        if op == "info":
            return client.info(symbol, deadline)
        if op == "candles":
            timeframe = args.get("timeframe")
            count = args.get("count")
            to = args.get("to")
            return client.candles(symbol, "1" if timeframe is None else str(timeframe),
                                  100 if count is None else int(count),
                                  None if to is None else int(to // 1), deadline)
        return client.quote(symbol, deadline)

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()
