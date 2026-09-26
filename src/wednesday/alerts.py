"""Telegram alerts when price trades into an active order block.

After every scan, the M1 bars that arrived since the previous check are
compared with each active order block: if their high/low range overlaps the
zone, the order block was entered (a wick inside the minute counts, not just
the last price). Each order block alerts once; the log lives in the store so a
restart doesn't repeat alerts. Bars that printed before the order block was
confirmed (its break candle closed) never trigger it.

The bot token is entered in Settings and kept in the OS credential store
(``secret_store``; ``TELEGRAM_BOT_TOKEN`` still works as a fallback). The chat
id, and which timeframes and priorities alert, are settings the dashboard saves
(``TELEGRAM_CHAT_ID`` is the fallback for the chat id).
"""

from __future__ import annotations

import html
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .bias import RISK_TEXT, TradeBias, setup_risk
from .levels import Level
from .secret_store import get_secret
from .scanner import ScanResult
from .storage import Store
from .timeframes import TIMEFRAMES, Timeframe

log = logging.getLogger(__name__)

ALERTS_KEY = "alerts"
PRIORITIES = ("extreme", "middle")


@dataclass
class AlertSettings:
    enabled: bool = True
    timeframes: list[str] = field(default_factory=lambda: [tf.name for tf in TIMEFRAMES])
    priorities: list[str] = field(default_factory=lambda: list(PRIORITIES))
    neutral_alerts: bool = False  # with a neutral bias (no trading) alerts are held back unless this is on
    chat_id: str | None = None  # None = TELEGRAM_CHAT_ID from the environment

    @property
    def resolved_chat_id(self) -> str | None:
        return self.chat_id or os.environ.get("TELEGRAM_CHAT_ID") or None

    def validate(self) -> list[str]:
        known = {tf.name for tf in TIMEFRAMES}
        errors = [f"Unknown timeframe {t!r}" for t in self.timeframes if t not in known]
        if self.chat_id and not self.chat_id.lstrip("-").isdigit() and not self.chat_id.startswith("@"):
            errors.append("The chat id is a number like 123456789 (or -100… for a group), or @channelname")
        errors += [f"Unknown priority {p!r}" for p in self.priorities if p not in PRIORITIES]
        return errors

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> AlertSettings:
        d = d or {}
        base = cls()
        return cls(
            enabled=bool(d.get("enabled", base.enabled)),
            timeframes=list(d.get("timeframes", base.timeframes)),
            priorities=list(d.get("priorities", base.priorities)),
            neutral_alerts=bool(d.get("neutral_alerts", base.neutral_alerts)),
            chat_id=str(d.get("chat_id") or "").strip() or None,
        )


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str | None, timeout: float = 10):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def _call(self, method: str, params: dict) -> dict:
        url = f"{self.API}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("description", str(exc))
            except Exception:  # noqa: BLE001 - keep the HTTP error if the body isn't JSON
                detail = str(exc)
            raise TelegramError(f"Telegram {method} failed: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"Can't reach Telegram: {exc}") from exc
        if not body.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {body.get('description', body)}")
        return body

    def send(self, text: str) -> None:
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID is not set")
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})

    def chats(self) -> list[dict]:
        """Chats that recently messaged the bot, for finding TELEGRAM_CHAT_ID."""
        updates = self._call("getUpdates", {}).get("result", [])
        seen: dict[int, dict] = {}
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = msg.get("chat")
            if chat:
                seen[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                    "name": chat.get("title") or chat.get("username") or chat.get("first_name")}
        return list(seen.values())


def ob_key(source: str, symbol: str, tf: str, ob: Level) -> str:
    return f"{source}|{symbol}|{tf}|{ob.kind}|{ob.time.isoformat()}|{ob.top:.5f}"


def format_alert(symbol: str, tf: str, ob: Level, price: float, bias_text: str | None, digits: int = 2,
                 trade_bias: TradeBias | None = None) -> str:
    m = ob.meta
    side = "Buy" if ob.kind == "bullish" else "Sell"
    prio = "extreme" if m.get("priority") == "extreme" else "mid"
    swing = f" at {m['swing']}" if m.get("swing") else ""
    f = lambda v: f"{v:,.{digits}f}"  # noqa: E731
    risk = setup_risk(side.lower(), trade_bias)
    lines = [
        f"<b>{html.escape(symbol)} entered {tf} {'bullish' if ob.kind == 'bullish' else 'bearish'} OB</b> ({prio}{swing})",
        f"{side} limit <b>{f(m['entry'])}</b> · SL {f(m['sl'])} · risk {f(m['risk'])}{' (capped)' if m.get('sl_capped') else ''}",
        f"Zone {f(ob.bottom)} – {f(ob.top)} · price {f(price)}",
    ]
    if ob.touches:
        lines.append(f"Entry tested {ob.touches}× before")
    if bias_text:
        lines.append(f"{tf} structure: {bias_text}")
    if risk:
        verb = {"on": "with", "off": "against"}.get(risk)
        why = f" ({verb} {trade_bias.direction} bias)" if verb else " (neutral bias)"
        lines.append(f"<b>{RISK_TEXT[risk]}</b>{why}")
    return "\n".join(lines)


def telegram_client(settings: AlertSettings) -> TelegramClient | None:
    """A client from the saved token and the settings' chat id; None without a token."""
    token, _ = get_secret("telegram_bot_token")
    return TelegramClient(token, settings.resolved_chat_id) if token else None


class AlertManager:
    def __init__(self, store: Store | None, client: TelegramClient | None, settings: AlertSettings | None = None):
        self.store = store
        self.client = client
        self.settings = settings or AlertSettings()
        self.last_error: str | None = None
        self._last_bar: dict[tuple[str, str], pd.Timestamp] = {}
        self._sent_memory: set[str] = set()  # when there is no store

    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.token and self.client.chat_id)

    def _already(self, key: str) -> bool:
        return self.store.alert_sent(key) if self.store else key in self._sent_memory

    def _record(self, key: str, row: dict) -> None:
        if self.store:
            self.store.log_alert({"key": key, **row})
        elif row["status"] == "sent":
            self._sent_memory.add(key)

    def check(self, source: str, symbol: str, result: ScanResult, m1: pd.DataFrame,
              trade_bias: TradeBias | None = None) -> list[str]:
        """Alert for order blocks entered by bars since the last check. Returns the alert keys sent.

        ``trade_bias`` (the active one) labels each alert RISK ON / OFF; with a
        neutral bias alerts are held back unless ``neutral_alerts`` is on.
        """
        if m1 is None or m1.empty:
            return []
        stream = (source, symbol)
        last = self._last_bar.get(stream)
        self._last_bar[stream] = m1.index[-1]
        if last is None:
            return []  # first scan of this stream: only react to bars that arrive from now on
        new = m1[m1.index > last]
        if new.empty or not (self.configured and self.settings.enabled):
            return []
        if trade_bias and trade_bias.direction == "neutral" and not self.settings.neutral_alerts:
            return []  # neutral = not trading; the OBs can still alert once a direction is set

        sent = []
        for r in result.results:
            tf: Timeframe = r.timeframe
            if tf.name not in self.settings.timeframes or "ob" not in r.sets:
                continue
            bias = r.bias
            bias_text = f"{bias.direction} {bias.event} at {bias.level:,.2f}" if bias else None
            for ob in r.sets["ob"].active:
                if ob.meta.get("priority") not in self.settings.priorities:
                    continue
                # Only bars after the order block was confirmed (its break candle closed).
                bars = new[new.index >= ob.confirmed_time + tf.delta]
                if bars.empty or bars["low"].min() > ob.top or bars["high"].max() < ob.bottom:
                    continue
                key = ob_key(source, symbol, tf.name, ob)
                if self._already(key):
                    continue
                row = {
                    "source": source, "symbol": symbol, "timeframe": tf.name, "kind": ob.kind,
                    "priority": ob.meta.get("priority", ""), "entry": float(ob.meta.get("entry", ob.top)),
                    "price": float(result.price), "sent_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    self.client.send(format_alert(symbol, tf.name, ob, result.price, bias_text, trade_bias=trade_bias))
                except TelegramError as exc:
                    self.last_error = str(exc)
                    log.warning("alert not sent: %s", exc)
                    self._record(key, {**row, "status": "failed", "error": str(exc)})
                    continue
                self.last_error = None
                self._record(key, {**row, "status": "sent", "error": None})
                sent.append(key)
        return sent

    def send_test(self, symbol: str) -> None:
        if not self.configured:
            raise TelegramError("Add the bot token and chat id in Settings > Telegram alerts first")
        self.client.send(f"<b>{html.escape(symbol)} screener</b>\nTest message: alerts are connected.")
