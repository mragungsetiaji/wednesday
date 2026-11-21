//! TradingView market data over its websocket, spoken directly (no Node helper).
//!
//! The protocol is unofficial: it is what the TradingView web chart sends, as the community
//! library @mathieuc/tradingview (ISC) documents it. Every websocket message holds frames
//! `~m~<length>~m~<payload>`, the length in UTF-16 code units. A payload is a JSON packet
//! `{"m": method, "p": [params]}`, a server greeting, or a heartbeat `~h~<id>` that the client
//! echoes. Chart sessions (`cs_...`) resolve a symbol and load its bars; one quote session
//! (`qs_...`) streams the live price.
//!
//! [`Connection`] answers the three questions the feed and the other-symbol panes ask, with the
//! answers the old helper (`tradingview/feed.mjs`) gave:
//! - `info` `{symbol}`: `{full_name, description, exchange, timezone, session, pricescale}`
//! - `candles` `{symbol, timeframe, count, to?}`: rows `[time, open, high, low, close, volume]`,
//!   oldest first, UTC unix seconds; `to` is the newest bar time when scrolling back
//! - `quote` `{symbol}`: `{price, time}` or null before the first price
//!
//! One websocket stays open between calls and is reopened when TradingView drops it.

use std::collections::{BTreeMap, HashMap};
use std::hash::{BuildHasher, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use crate::feed::FeedError;

const URL: &str = "wss://data.tradingview.com/socket.io/websocket?from=chart&type=chart";
const ORIGIN: &str = "https://www.tradingview.com";
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) \
                          Chrome/131.0.0.0 Safari/537.36";
/// The most bars one request asks for; deeper history comes in more requests.
const MAX_CHUNK: usize = 5_000;
/// The most bars one `candles` call loads.
const MAX_COUNT: usize = 20_000;
/// The server sends a heartbeat every few seconds; this long without anything, the line is dead.
const IDLE: Duration = Duration::from_secs(60);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(20);

// ---------------------------------------------------------------------------------------------
// Framing

/// One payload of a websocket message.
#[derive(Clone, Debug, PartialEq)]
pub enum Frame {
    /// `~h~<id>`: echo it back.
    Heartbeat(String),
    /// `{"m": method, "p": params}`.
    Packet(Packet),
    /// Any other JSON (the server's greeting).
    Data(Value),
    /// Not JSON.
    Invalid(String),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Packet {
    pub m: String,
    pub p: Vec<Value>,
}

/// `~m~<length>~m~<body>`, the length in UTF-16 code units as JavaScript counts it.
pub fn encode_frame(body: &str) -> String {
    format!("~m~{}~m~{body}", body.encode_utf16().count())
}

/// A client packet.
pub fn encode_packet(method: &str, params: Value) -> String {
    encode_frame(&json!({"m": method, "p": params}).to_string())
}

/// The byte index `units` UTF-16 code units into `s` (its end when shorter).
fn utf16_end(s: &str, units: usize) -> usize {
    let mut seen = 0;
    for (i, c) in s.char_indices() {
        if seen >= units {
            return i;
        }
        seen += c.len_utf16();
    }
    s.len()
}

/// Lenient fallback for malformed input: the parts between frame headers (`~m~<digits>~m~`).
fn split_frames(input: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    let (mut start, mut search) = (0, 0);
    while let Some(off) = input[search..].find("~m~") {
        let at = search + off;
        let after = &input[at + 3..];
        let digits = after.bytes().take_while(u8::is_ascii_digit).count();
        if digits > 0 && after[digits..].starts_with("~m~") {
            if at > start {
                parts.push(&input[start..at]);
            }
            start = at + 3 + digits + 3;
            search = start;
        } else {
            search = at + 1;
        }
    }
    if start < input.len() {
        parts.push(&input[start..]);
    }
    parts
}

fn decode_payload(raw: &str) -> Frame {
    if let Some(id) = raw.strip_prefix("~h~") {
        return if !id.is_empty() && id.parse::<f64>().is_ok() {
            Frame::Heartbeat(id.to_string())
        } else {
            Frame::Invalid(raw.to_string())
        };
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(mut o)) if o.get("m").is_some_and(Value::is_string) => {
            let m = o.remove("m").and_then(|m| m.as_str().map(str::to_string)).unwrap_or_default();
            let p = match o.remove("p") {
                Some(Value::Array(p)) => p,
                _ => Vec::new(),
            };
            Frame::Packet(Packet { m, p })
        }
        Ok(data) => Frame::Data(data),
        Err(_) => Frame::Invalid(raw.to_string()),
    }
}

/// The frames of one websocket message.
pub fn decode_frames(input: &str) -> Vec<Frame> {
    let mut payloads: Vec<&str> = Vec::new();
    let mut pos = 0;
    while pos < input.len() {
        let rest = &input[pos..];
        let header = rest.strip_prefix("~m~").and_then(|after| {
            let end = after.find("~m~")?;
            let len: usize = after[..end].parse().ok()?;
            Some((3 + end + 3, len))
        });
        let Some((skip, len)) = header else {
            payloads.extend(split_frames(rest));
            break;
        };
        let body = &rest[skip..];
        let end = utf16_end(body, len);
        payloads.push(&body[..end]);
        pos += skip + end;
    }
    payloads.into_iter().map(decode_payload).collect()
}

// ---------------------------------------------------------------------------------------------
// Errors

/// Why a call failed: out of time (the caller words it), or what TradingView or the line said.
#[derive(Clone, Debug, PartialEq)]
pub enum TvError {
    Timeout,
    Tv { code: String, message: String },
}

impl TvError {
    fn new(code: &str, message: impl Into<String>) -> TvError {
        TvError::Tv { code: code.to_string(), message: message.into() }
    }
}

/// JavaScript's `String(value)` for the error texts TradingView sends.
fn js_string(v: Option<&Value>) -> String {
    match v {
        None => "undefined".into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Object(_)) => "[object Object]".into(),
        Some(Value::Array(a)) => a.iter().map(|x| if x.is_null() { String::new() } else { js_string(Some(x)) })
            .collect::<Vec<_>>().join(","),
        Some(other) => other.to_string(),
    }
}

/// The error a chart session packet carries, if it is one.
fn chart_error(packet: &Packet) -> Option<TvError> {
    let p = &packet.p;
    match packet.m.as_str() {
        "symbol_error" => Some(TvError::new(
            "SYMBOL_ERROR",
            format!("({}) Symbol error: {}", js_string(p.get(1)), js_string(p.get(2))),
        )),
        "series_error" => Some(TvError::new("SERIES_ERROR", format!("Series error: {}", js_string(p.get(3))))),
        "critical_error" => Some(TvError::new("CRITICAL_ERROR", format!("Critical error: {}", js_string(p.get(1))))),
        _ => None,
    }
}

// ---------------------------------------------------------------------------------------------
// Sessions

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A random session id such as `cs_Ab12Cd34Ef56`.
fn session_id(prefix: &str) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
    let mut bits = h.finish();
    let mut id = String::with_capacity(prefix.len() + 13);
    id.push_str(prefix);
    id.push('_');
    for i in 0..12 {
        if i == 6 {
            let mut h = std::collections::hash_map::RandomState::new().build_hasher();
            h.write_u64(bits);
            bits = h.finish();
        }
        id.push(ALPHABET[(bits % ALPHABET.len() as u64) as usize] as char);
        bits /= ALPHABET.len() as u64;
    }
    id
}

/// The symbol as a chart resolves it (`={"symbol":...,"adjustment":"splits"}`).
fn symbol_init(symbol: &str) -> String {
    format!("={}", json!({"symbol": symbol, "adjustment": "splits"}))
}

/// A quote subscription's key (`={"session":"regular","symbol":...}`).
pub fn quote_key(symbol: &str) -> String {
    format!("={}", json!({"session": "regular", "symbol": symbol}))
}

/// Bars a chart session has loaded, by time.
#[derive(Default, Debug)]
pub struct Bars {
    rows: BTreeMap<i64, Value>,
}

impl Bars {
    /// A `timescale_update`/`du` packet's `$prices` bars: `{"s": [{"i", "v": [time, o, h, l, c, v]}]}`.
    pub fn update(&mut self, packet: &Packet) {
        let Some(bars) = packet.p.get(1).and_then(|d| d.get("$prices")).and_then(|d| d.get("s")).and_then(Value::as_array)
        else {
            return;
        };
        for bar in bars {
            let Some(v) = bar.get("v").and_then(Value::as_array) else { continue };
            let Some(time) = v.first().and_then(Value::as_f64) else { continue };
            let mut row: Vec<Value> = (0..5).map(|i| v.get(i).cloned().unwrap_or(Value::Null)).collect();
            row.push(v.get(5).filter(|x| !x.is_null()).cloned().unwrap_or(json!(0)));
            self.rows.insert(time as i64, Value::Array(row));
        }
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }

    /// Bars up to `to` (all without one).
    fn in_range(&self, to: Option<i64>) -> usize {
        match to {
            Some(to) => self.rows.range(..=to).count(),
            None => self.rows.len(),
        }
    }

    /// The last `count` bars up to `to`, oldest first.
    pub fn select(&self, count: usize, to: Option<i64>) -> Vec<Value> {
        let rows: Vec<&Value> = match to {
            Some(to) => self.rows.range(..=to).map(|(_, r)| r).collect(),
            None => self.rows.values().collect(),
        };
        rows[rows.len().saturating_sub(count)..].iter().map(|r| (*r).clone()).collect()
    }
}

/// What to do after a `series_completed`: done, or ask for this many older bars.
#[derive(Debug, PartialEq)]
pub enum Next {
    Done,
    More(usize),
}

/// After each `series_completed`: more history until `count` bars up to `to` are loaded, the
/// server has no more (`data_completed`, or nothing new came), or [`MAX_COUNT`] is reached.
pub fn next_step(bars: &Bars, count: usize, to: Option<i64>, data_completed: bool, previous: Option<usize>) -> Next {
    let in_range = bars.in_range(to);
    let exhausted = data_completed || previous.is_some_and(|p| bars.len() <= p);
    if in_range >= count || exhausted || bars.len() >= MAX_COUNT {
        return Next::Done;
    }
    Next::More((count - in_range).min(MAX_CHUNK).min(MAX_COUNT - bars.len()).max(1))
}

/// A live quote: the merged `qsd` values, whether its first values arrived, its error.
#[derive(Clone, Debug, Default)]
pub struct Quote {
    pub data: Map<String, Value>,
    pub loaded: bool,
    pub error: Option<String>,
}

/// Quote packets (`qsd`, `quote_completed`) into the quotes they're about.
pub fn apply_quote(quotes: &mut HashMap<String, Quote>, packet: &Packet) {
    match packet.m.as_str() {
        "quote_completed" => {
            if let Some(q) = packet.p.get(1).and_then(Value::as_str).and_then(|k| quotes.get_mut(k)) {
                q.loaded = true;
            }
        }
        "qsd" => {
            let Some(payload) = packet.p.get(1) else { return };
            let Some(q) = payload.get("n").and_then(Value::as_str).and_then(|k| quotes.get_mut(k)) else { return };
            match payload.get("s").and_then(Value::as_str) {
                Some("ok") => {
                    if let Some(v) = payload.get("v").and_then(Value::as_object) {
                        q.data.extend(v.iter().map(|(k, v)| (k.clone(), v.clone())));
                    }
                }
                Some("error") => {
                    q.error = Some(payload.get("errmsg").and_then(Value::as_str).unwrap_or("unknown").to_string());
                }
                _ => {}
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------------------------
// The websocket

/// One open websocket: a reader task answers heartbeats, keeps the quotes and hands chart
/// packets over; a writer task sends.
pub struct Client {
    out: mpsc::UnboundedSender<Message>,
    packets: mpsc::UnboundedReceiver<Packet>,
    quotes: Arc<Mutex<HashMap<String, Quote>>>,
    closed: Arc<Mutex<Option<String>>>,
    quote_session: Option<String>,
    tasks: Vec<JoinHandle<()>>,
}

fn tls() -> Result<Arc<rustls::ClientConfig>, TvError> {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| TvError::new("CONNECTION_ERROR", format!("TLS setup failed: {e}")))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(Arc::new(config))
}

impl Client {
    /// Open the websocket and send the anonymous auth token.
    pub async fn connect() -> Result<Client, TvError> {
        let mut request = URL
            .into_client_request()
            .map_err(|e| TvError::new("CONNECTION_ERROR", format!("WebSocket error: {e}")))?;
        let headers = request.headers_mut();
        for (name, value) in [
            ("Origin", ORIGIN),
            ("User-Agent", USER_AGENT),
            ("Accept-Language", "en-US,en;q=0.9"),
            ("Cache-Control", "no-cache"),
            ("Pragma", "no-cache"),
        ] {
            headers.insert(name, HeaderValue::from_static(value));
        }
        let connector = tokio_tungstenite::Connector::Rustls(tls()?);
        let opened = tokio::time::timeout(
            CONNECT_TIMEOUT,
            tokio_tungstenite::connect_async_tls_with_config(request, None, false, Some(connector)),
        )
        .await;
        let (ws, _) = match opened {
            Err(_) => {
                return Err(TvError::new(
                    "TIMEOUT",
                    format!("Connection not ready after {} ms", CONNECT_TIMEOUT.as_millis()),
                ))
            }
            Ok(Err(e)) => return Err(TvError::new("CONNECTION_ERROR", format!("WebSocket error: {e}"))),
            Ok(Ok(ws)) => ws,
        };
        let (mut sink, mut stream) = ws.split();

        let (out, mut outgoing) = mpsc::unbounded_channel::<Message>();
        let writer = tokio::spawn(async move {
            while let Some(msg) = outgoing.recv().await {
                if sink.send(msg).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });

        let (packet_tx, packets) = mpsc::unbounded_channel();
        let quotes: Arc<Mutex<HashMap<String, Quote>>> = Arc::default();
        let closed: Arc<Mutex<Option<String>>> = Arc::default();
        let (reader_out, reader_quotes, reader_closed) = (out.clone(), Arc::clone(&quotes), Arc::clone(&closed));
        let reader = tokio::spawn(async move {
            let why = loop {
                let msg = match tokio::time::timeout(IDLE, stream.next()).await {
                    Err(_) => break format!("No data from the server for {} ms", IDLE.as_millis()),
                    Ok(None) => break "the server closed the connection".to_string(),
                    Ok(Some(Err(e))) => break format!("WebSocket error: {e}"),
                    Ok(Some(Ok(msg))) => msg,
                };
                if let Message::Close(frame) = &msg {
                    break match frame {
                        Some(f) if !f.reason.is_empty() => f.reason.to_string(),
                        _ => "the server closed the connection".to_string(),
                    };
                }
                let Ok(text) = msg.to_text() else { continue };
                let mut stop = None;
                for frame in decode_frames(text) {
                    match frame {
                        Frame::Heartbeat(id) => {
                            let _ = reader_out.send(Message::text(encode_frame(&format!("~h~{id}"))));
                        }
                        Frame::Packet(p) if p.m == "protocol_error" => {
                            stop = Some(format!("Protocol error: {}", js_string(p.p.first())));
                            break;
                        }
                        Frame::Packet(p) if p.m == "qsd" || p.m == "quote_completed" => {
                            if let Ok(mut q) = reader_quotes.lock() {
                                apply_quote(&mut q, &p);
                            }
                        }
                        Frame::Packet(p) => {
                            let _ = packet_tx.send(p);
                        }
                        Frame::Data(_) => {}
                        Frame::Invalid(raw) => log::debug!("tradingview: invalid frame {raw}"),
                    }
                }
                if let Some(why) = stop {
                    break why;
                }
            };
            log::info!("tradingview: connection closed: {why}");
            if let Ok(mut c) = reader_closed.lock() {
                *c = Some(why);
            }
            let _ = reader_out.send(Message::Close(None));
            // packet_tx drops here: a call waiting for packets learns the line is gone.
        });

        let client =
            Client { out, packets, quotes, closed, quote_session: None, tasks: vec![writer, reader] };
        client.send("set_auth_token", json!(["unauthorized_user_token"]))?;
        Ok(client)
    }

    /// Whether the line is gone (reopen it).
    pub fn is_closed(&self) -> bool {
        self.closed.lock().map(|c| c.is_some()).unwrap_or(true) || self.out.is_closed()
    }

    fn why_closed(&self) -> String {
        self.closed.lock().ok().and_then(|c| c.clone()).unwrap_or_else(|| "connection lost".into())
    }

    fn send(&self, method: &str, params: Value) -> Result<(), TvError> {
        self.out
            .send(Message::text(encode_packet(method, params)))
            .map_err(|_| TvError::new("DISCONNECTED", format!("Connection closed: {}", self.why_closed())))
    }

    /// The next packet for chart session `cs` (others are late answers to earlier calls).
    async fn next_packet(&mut self, cs: &str, deadline: Instant, what: &str) -> Result<Packet, TvError> {
        loop {
            match tokio::time::timeout_at(deadline, self.packets.recv()).await {
                Err(_) => return Err(TvError::Timeout),
                Ok(None) => {
                    return Err(TvError::new(
                        "DISCONNECTED",
                        format!("Connection closed during {what}: {}", self.why_closed()),
                    ))
                }
                Ok(Some(p)) if p.p.first().and_then(Value::as_str) == Some(cs) => return Ok(p),
                Ok(Some(_)) => continue,
            }
        }
    }

    /// A chart session on `symbol` with a `$prices` series of `timeframe` over `range`.
    fn open_chart(&self, symbol: &str, timeframe: &str, range: Value) -> Result<String, TvError> {
        let cs = session_id("cs");
        self.send("chart_create_session", json!([cs]))?;
        self.send("resolve_symbol", json!([cs, "ser_1", symbol_init(symbol)]))?;
        self.send("create_series", json!([cs, "$prices", "s1", "ser_1", timeframe, range]))?;
        Ok(cs)
    }

    fn close_chart(&self, cs: &str) {
        let _ = self.send("chart_delete_session", json!([cs]));
    }

    /// The symbol's details, as `symbol_resolved` gives them.
    pub async fn info(&mut self, symbol: &str, deadline: Instant) -> Result<Value, TvError> {
        let what = format!("getSymbolInfo({symbol})");
        let cs = self.open_chart(symbol, "D", json!(1))?;
        let answer = loop {
            let packet = match self.next_packet(&cs, deadline, &what).await {
                Ok(p) => p,
                Err(e) => break Err(e),
            };
            if let Some(e) = chart_error(&packet) {
                break Err(e);
            }
            if packet.m == "symbol_resolved" {
                break Ok(packet.p.get(2).cloned().unwrap_or(Value::Null));
            }
        };
        self.close_chart(&cs);
        let i = answer?;
        let get = |k: &str| i.get(k).cloned().unwrap_or(Value::Null);
        Ok(json!({"full_name": get("full_name"), "description": get("description"), "exchange": get("exchange"),
                  "timezone": get("timezone"), "session": get("session"), "pricescale": get("pricescale")}))
    }

    /// The last `count` bars of `timeframe` (up to `to`, unix seconds, when given), oldest first.
    pub async fn candles(&mut self, symbol: &str, timeframe: &str, count: usize, to: Option<i64>,
                         deadline: Instant) -> Result<Value, TvError> {
        if count == 0 {
            return Err(TvError::new("INVALID_ARGUMENT", "count must be a positive integer"));
        }
        let what = format!("getCandles({symbol})");
        let first = count.min(MAX_CHUNK);
        let range = match to {
            Some(to) => json!(["bar_count", to, first]),
            None => json!(first),
        };
        let cs = self.open_chart(symbol, timeframe, range)?;
        let mut bars = Bars::default();
        let mut previous = None;
        let answer = loop {
            let packet = match self.next_packet(&cs, deadline, &what).await {
                Ok(p) => p,
                Err(e) => break Err(e),
            };
            if let Some(e) = chart_error(&packet) {
                break Err(e);
            }
            match packet.m.as_str() {
                "timescale_update" | "du" => bars.update(&packet),
                "series_completed" => {
                    let completed = packet.p.get(4).and_then(|d| d.get("data_completed")).cloned();
                    match next_step(&bars, count, to, completed.is_some(), previous) {
                        Next::Done => break Ok(completed),
                        Next::More(n) => {
                            previous = Some(bars.len());
                            if let Err(e) = self.send("request_more_data", json!([cs, "$prices", n])) {
                                break Err(e);
                            }
                        }
                    }
                }
                _ => {}
            }
        };
        self.close_chart(&cs);
        let completed = answer?;
        let rows = bars.select(count, to);
        if rows.is_empty() {
            let limit = if completed.as_ref().and_then(Value::as_str) == Some("limit") {
                " (the server reported an access limit: this range or timeframe may need an account with access)"
            } else {
                ""
            };
            return Err(TvError::new("NO_DATA", format!("No candles available for {symbol} in the requested range{limit}")));
        }
        Ok(Value::Array(rows))
    }

    /// The live price and its time: `{price, time}`, null before the first price. The first call
    /// for a symbol subscribes it and waits for its first values; later calls read the latest.
    pub async fn quote(&mut self, symbol: &str, deadline: Instant) -> Result<Value, TvError> {
        let qs = match &self.quote_session {
            Some(qs) => qs.clone(),
            None => {
                let qs = session_id("qs");
                self.send("quote_create_session", json!([qs]))?;
                self.send("quote_set_fields", json!([qs, "lp", "lp_time"]))?;
                self.quote_session.insert(qs).clone()
            }
        };
        let key = quote_key(symbol);
        let new = {
            let mut quotes = self.quotes.lock().map_err(|_| TvError::new("ERROR", "quote state poisoned"))?;
            let new = !quotes.contains_key(&key);
            if new {
                quotes.insert(key.clone(), Quote::default());
            }
            new
        };
        if new {
            self.send("quote_add_symbols", json!([qs, key]))?;
        }
        loop {
            let q = self.quotes.lock().ok().and_then(|q| q.get(&key).cloned()).unwrap_or_default();
            if q.loaded {
                let price = q.data.get("lp").cloned().unwrap_or(Value::Null);
                if price.is_null() {
                    return Ok(Value::Null);
                }
                return Ok(json!({"price": price, "time": q.data.get("lp_time").cloned().unwrap_or(Value::Null)}));
            }
            let failed = match (&q.error, self.is_closed()) {
                (Some(why), _) => Some(TvError::new("QUOTE_ERROR", format!("Quote error ({symbol}): {why}"))),
                (None, true) => Some(TvError::new(
                    "DISCONNECTED",
                    format!("Connection closed during watchQuotes({symbol}): {}", self.why_closed()),
                )),
                (None, false) if Instant::now() >= deadline => Some(TvError::Timeout),
                _ => None,
            };
            if let Some(e) = failed {
                // Forget it: the next call subscribes again.
                if let Ok(mut quotes) = self.quotes.lock() {
                    quotes.remove(&key);
                }
                let _ = self.send("quote_remove_symbols", json!([qs, key]));
                return Err(e);
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    /// Close the websocket.
    pub async fn close(mut self) {
        let _ = self.out.send(Message::Close(None));
        let tasks = std::mem::take(&mut self.tasks);
        drop(self);
        // The writer sends the close; the reader ends when the server answers it (or in 3 s).
        for task in tasks {
            let abort = task.abort_handle();
            if tokio::time::timeout(Duration::from_secs(3), task).await.is_err() {
                abort.abort();
            }
        }
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The feed's view

/// TradingView for the feed and the other-symbol panes: one websocket, opened on the first call
/// and again after it drops, answering `info`, `candles` and `quote` as `feed.mjs` did.
#[derive(Default)]
pub struct Connection {
    client: Option<Client>,
}

impl Connection {
    pub fn new() -> Connection {
        Connection::default()
    }

    /// Answer `op` with `args` within `timeout` seconds; errors as the helper reported them
    /// (`TradingViewError` with TradingView's code), running out of time as Python words it.
    pub async fn call(&mut self, op: &str, timeout: f64, args: Map<String, Value>) -> Result<Value, FeedError> {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.0));
        self.answer(op, &args, deadline).await.map_err(|e| match e {
            TvError::Timeout => FeedError::runtime(format!(
                "TradingView didn't answer within {timeout:.0} s. Check the internet connection."
            )),
            TvError::Tv { code, message } => FeedError::tradingview(&code, &message),
        })
    }

    async fn answer(&mut self, op: &str, args: &Map<String, Value>, deadline: Instant) -> Result<Value, TvError> {
        let symbol = args.get("symbol").and_then(Value::as_str).map(str::trim).unwrap_or("");
        if symbol.is_empty() {
            return Err(TvError::new("INVALID_ARGUMENT", "symbol is required"));
        }
        if !matches!(op, "info" | "candles" | "quote") {
            return Err(TvError::new("BAD_REQUEST", format!("Unknown op {op}")));
        }
        if self.client.as_ref().map_or(true, Client::is_closed) {
            if let Some(old) = self.client.take() {
                old.close().await;
            }
            let client = match tokio::time::timeout_at(deadline, Client::connect()).await {
                Err(_) => return Err(TvError::Timeout),
                Ok(c) => c?,
            };
            self.client = Some(client);
        }
        let client = self.client.as_mut().expect("connected");
        match op {
            "info" => client.info(symbol, deadline).await,
            "candles" => {
                let timeframe = match args.get("timeframe") {
                    Some(Value::String(s)) => s.clone(),
                    Some(Value::Null) | None => "1".into(),
                    Some(other) => other.to_string(),
                };
                let count = match args.get("count") {
                    None | Some(Value::Null) => 100,
                    Some(c) => c.as_u64().map(|c| c as usize).ok_or_else(|| {
                        TvError::new("INVALID_ARGUMENT", "count must be a positive integer")
                    })?,
                };
                let to = args.get("to").and_then(Value::as_f64).map(|t| t.floor() as i64);
                client.candles(symbol, &timeframe, count, to, deadline).await
            }
            _ => client.quote(symbol, deadline).await,
        }
    }

    pub async fn close(&mut self) {
        if let Some(client) = self.client.take() {
            client.close().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn packet(m: &str, p: Value) -> Packet {
        Packet { m: m.into(), p: p.as_array().cloned().unwrap_or_default() }
    }

    #[test]
    fn frames_round_trip() {
        let a = encode_packet("set_auth_token", json!(["unauthorized_user_token"]));
        assert_eq!(a, r#"~m~54~m~{"m":"set_auth_token","p":["unauthorized_user_token"]}"#);
        let msg = format!("{a}{}", encode_frame("~h~12"));
        let frames = decode_frames(&msg);
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0], Frame::Packet(packet("set_auth_token", json!(["unauthorized_user_token"]))));
        assert_eq!(frames[1], Frame::Heartbeat("12".into()));
        assert!(matches!(decode_frames(r#"~m~16~m~{"session_id":1}"#)[0], Frame::Data(_)));
        assert!(matches!(decode_frames("~m~3~m~abc")[0], Frame::Invalid(_)));
    }

    #[test]
    fn frame_lengths_count_utf16_units() {
        // "é" is one UTF-16 unit (two bytes); "😀" is two units (four bytes).
        let body = r#"{"m":"x","p":["é😀"]}"#;
        let framed = encode_frame(body);
        assert_eq!(framed, format!("~m~{}~m~{body}", 21));
        let twice = format!("{framed}{framed}");
        let frames = decode_frames(&twice);
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[1], Frame::Packet(packet("x", json!(["é😀"]))));
    }

    #[test]
    fn malformed_input_splits_on_headers() {
        let frames = decode_frames(r#"~m~x~m~{"m":"a","p":[]}~m~5~m~~h~42"#);
        assert_eq!(frames.len(), 2);
        assert!(matches!(&frames[0], Frame::Invalid(raw) if raw.starts_with("~m~x")));
        assert_eq!(frames[1], Frame::Heartbeat("42".into()));
        let frames = decode_frames(r#"junk~m~5~m~~h~42"#);
        assert_eq!(frames, vec![Frame::Invalid("junk".into()), Frame::Heartbeat("42".into())]);
    }

    #[test]
    fn keys_keep_javascripts_order() {
        assert_eq!(symbol_init("FX:XAUUSD"), r#"={"symbol":"FX:XAUUSD","adjustment":"splits"}"#);
        assert_eq!(quote_key("FX:XAUUSD"), r#"={"session":"regular","symbol":"FX:XAUUSD"}"#);
        let id = session_id("cs");
        assert!(id.starts_with("cs_") && id.len() == 15, "{id}");
        assert_ne!(session_id("cs"), session_id("cs"));
    }

    #[test]
    fn bars_merge_by_time_and_select_the_latest() {
        let mut bars = Bars::default();
        bars.update(&packet("timescale_update", json!(["cs_1", {"$prices": {"s": [
            {"i": 0, "v": [120.0, 1, 2, 0.5, 1.5, 10]},
            {"i": 1, "v": [60.0, 1, 2, 0.5, 1.5]},
        ]}}])));
        bars.update(&packet("du", json!(["cs_1", {"$prices": {"s": [{"i": 1, "v": [120.0, 9, 9, 9, 9, 3]}]}}])));
        assert_eq!(bars.len(), 2);
        assert_eq!(bars.select(5, None), vec![json!([60.0, 1, 2, 0.5, 1.5, 0]), json!([120.0, 9, 9, 9, 9, 3])]);
        assert_eq!(bars.select(1, None), vec![json!([120.0, 9, 9, 9, 9, 3])]);
        assert_eq!(bars.select(5, Some(100)), vec![json!([60.0, 1, 2, 0.5, 1.5, 0])]);
    }

    #[test]
    fn history_asks_for_more_until_enough_or_exhausted() {
        let mut bars = Bars::default();
        let rows: Vec<Value> = (0..5000).map(|i| json!({"i": i, "v": [i * 60, 1, 1, 1, 1, 0]})).collect();
        bars.update(&packet("du", json!(["cs", {"$prices": {"s": rows}}])));
        assert_eq!(next_step(&bars, 7001, None, false, None), Next::More(2001));
        assert_eq!(next_step(&bars, 12000, None, false, None), Next::More(5000));
        assert_eq!(next_step(&bars, 5000, None, false, None), Next::Done);
        assert_eq!(next_step(&bars, 7001, None, true, None), Next::Done);
        assert_eq!(next_step(&bars, 7001, None, false, Some(5000)), Next::Done, "nothing new came");
        // Bars after `to` don't count.
        assert_eq!(next_step(&bars, 100, Some(59 * 60), false, None), Next::More(40));
    }

    #[test]
    fn quotes_merge_and_complete() {
        let key = quote_key("FX:XAUUSD");
        let mut quotes = HashMap::from([(key.clone(), Quote::default())]);
        apply_quote(&mut quotes, &packet("qsd", json!(["qs_1", {"n": key, "s": "ok", "v": {"lp": 2650.5}}])));
        apply_quote(&mut quotes, &packet("qsd", json!(["qs_1", {"n": key, "s": "ok", "v": {"lp_time": 1763632800}}])));
        assert!(!quotes[&key].loaded);
        apply_quote(&mut quotes, &packet("quote_completed", json!(["qs_1", key])));
        let q = &quotes[&key];
        assert!(q.loaded);
        assert_eq!((q.data["lp"].clone(), q.data["lp_time"].clone()), (json!(2650.5), json!(1763632800)));

        let bad = quote_key("FX:NOPE");
        quotes.insert(bad.clone(), Quote::default());
        apply_quote(&mut quotes, &packet("qsd", json!(["qs_1", {"n": bad, "s": "error", "errmsg": "invalid symbol"}])));
        assert_eq!(quotes[&bad].error.as_deref(), Some("invalid symbol"));
        // Packets about symbols nobody asked for are dropped.
        apply_quote(&mut quotes, &packet("qsd", json!(["qs_1", {"n": "=other", "s": "ok", "v": {"lp": 1}}])));
        assert_eq!(quotes.len(), 2);
    }

    #[test]
    fn chart_errors_read_as_the_library_words_them() {
        let e = chart_error(&packet("symbol_error", json!(["cs_1", "ser_1", "invalid symbol"]))).unwrap();
        assert_eq!(e, TvError::new("SYMBOL_ERROR", "(ser_1) Symbol error: invalid symbol"));
        let e = chart_error(&packet("series_error", json!(["cs_1", "$prices", "s1", "no data"]))).unwrap();
        assert_eq!(e, TvError::new("SERIES_ERROR", "Series error: no data"));
        assert!(chart_error(&packet("du", json!(["cs_1"]))).is_none());
    }

    /// Real TradingView (needs the network): `cargo test -p wednesday-engine tradingview -- --ignored`.
    #[tokio::test]
    #[ignore]
    async fn live_gold() {
        let mut c = Connection::new();
        let args = |v: Value| v.as_object().cloned().unwrap();
        let info = c.call("info", 30.0, args(json!({"symbol": "FX:XAUUSD"}))).await.unwrap();
        assert_eq!(info["exchange"], "FXCM", "{info}");
        let rows = c.call("candles", 60.0, args(json!({"symbol": "FX:XAUUSD", "timeframe": "1", "count": 6000})))
            .await.unwrap();
        let rows = rows.as_array().unwrap();
        assert!(rows.len() > 5000, "{}", rows.len());
        assert!(rows.windows(2).all(|w| w[0][0].as_f64() < w[1][0].as_f64()));
        let q = c.call("quote", 15.0, args(json!({"symbol": "FX:XAUUSD"}))).await.unwrap();
        println!("{} rows, quote {q}", rows.len());
        let err = c.call("info", 30.0, args(json!({"symbol": "FX:NO_SUCH_X"}))).await.unwrap_err();
        assert_eq!(err.code.as_deref(), Some("SYMBOL_ERROR"));
        c.close().await;
    }
}
