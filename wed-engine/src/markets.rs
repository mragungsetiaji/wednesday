//! Another market in a chart pane (`markets.py`): candles and live price only, from TradingView.
//!
//! The scan, levels, alerts, bias and drawings follow the data source's symbol. A full-screen
//! pane can show another symbol beside it (`FX:XAGUSD` next to gold) through its own TradingView
//! helper (`tradingview/feed.mjs`); nothing about it is scanned or stored. Times are converted to
//! the feed's clock so the panes line up.
//!
//! Routes: `GET /api/markets/candles` ([`candles`]) and `GET /api/markets/tick` ([`tick`]), with
//! FastAPI's query validation, statuses and messages.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration as StdDuration, Instant};

use chrono::{DateTime, Duration, NaiveDateTime, Utc};
use serde_json::{json, Map, Value};

use super::feed::{tradingview_command, FeedError, NodeHelper};
use super::{Answer, Refusal, Runtime};
use wednesday_base::py::py_repr;
use wednesday_scan::clock::{localize, NEW_YORK};
use wednesday_scan::{Clock, Time, TIMEFRAMES};

/// Panes on the same symbol and timeframe within this share one request.
pub const CACHE_SECONDS: f64 = 5.0;
/// Every pane's live price poll within this reads one quote.
pub const TICK_SECONDS: f64 = 0.5;
/// A symbol's info (its price scale) is kept this long.
const INFO_SECONDS: f64 = 3600.0;
const CACHE_MAX: usize = 64;
const CACHE_DROP: usize = 16;

const LIMIT_MIN: i128 = 10;
const LIMIT_MAX: i128 = 2000;
const LIMIT_DEFAULT: i64 = 300;

/// How the helper is started: (node, script), or why it can't run here.
type Command = Box<dyn Fn() -> Result<(PathBuf, PathBuf), String> + Send + Sync>;

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
enum Key {
    Info(String),
    Candles(String, &'static str, i64, Option<i64>),
    Quote(String),
}

/// One TradingView helper for every pane's own symbol, started on first use (`Markets`).
pub struct Markets {
    command: Command,
    helper: tokio::sync::Mutex<Option<NodeHelper>>,
    cache: Mutex<HashMap<Key, (Instant, Value)>>,
}

impl Default for Markets {
    fn default() -> Self {
        Markets::new()
    }
}

impl Markets {
    /// Markets on the TradingView helper `tradingview_command` finds.
    pub fn new() -> Markets {
        Markets::with_command(tradingview_command)
    }

    /// Markets on another helper command (tests), as Python's `Markets(command=...)`.
    pub fn with_command(command: impl Fn() -> Result<(PathBuf, PathBuf), String> + Send + Sync + 'static) -> Markets {
        Markets { command: Box::new(command), helper: tokio::sync::Mutex::new(None), cache: Mutex::new(HashMap::new()) }
    }

    /// One helper call at a time; its failures as the statuses the dashboard shows.
    async fn call(&self, op: &str, timeout: f64, args: Map<String, Value>) -> Result<Value, Refusal> {
        let mut helper = self.helper.lock().await;
        if helper.is_none() {
            let (node, script) = (self.command)()
                .map_err(|why| Refusal::new(503, format!("Other symbols come from TradingView. {why}")))?;
            *helper = Some(NodeHelper::new(node, script));
        }
        let symbol = args.get("symbol").and_then(Value::as_str).unwrap_or("None").to_string();
        let h = helper.as_mut().expect("started");
        h.call(op, timeout, args).await.map_err(|e| market_error(e, &symbol))
    }

    /// `ttl` seconds of sharing per key; failures aren't kept.
    async fn cached<F>(&self, key: Key, ttl: f64, fetch: F) -> Result<Value, Refusal>
    where
        F: std::future::Future<Output = Result<Value, Refusal>>,
    {
        let now = Instant::now();
        if let Some((at, value)) = self.cache.lock().unwrap().get(&key) {
            if now.duration_since(*at) < StdDuration::from_secs_f64(ttl) {
                return Ok(value.clone());
            }
        }
        let value = fetch.await?;
        let mut cache = self.cache.lock().unwrap();
        cache.insert(key, (now, value.clone()));
        if cache.len() > CACHE_MAX {
            // Forget the oldest, e.g. symbols typed and left.
            let mut by_age: Vec<(Key, Instant)> = cache.iter().map(|(k, v)| (k.clone(), v.0)).collect();
            by_age.sort_by_key(|(_, at)| *at);
            for (k, _) in by_age.into_iter().take(CACHE_DROP) {
                cache.remove(&k);
            }
        }
        Ok(value)
    }

    /// Decimals the symbol is quoted with (2 for gold, 3 for silver on FXCM).
    pub async fn precision(&self, symbol: &str) -> Result<i64, Refusal> {
        let args = obj(json!({"symbol": symbol}));
        let info = self.cached(Key::Info(symbol.into()), INFO_SECONDS, self.call("info", 30.0, args)).await?;
        let scale = match info.get("pricescale").and_then(Value::as_f64) {
            Some(s) if s != 0.0 => s,
            _ => 100.0,
        };
        let digits = scale.log10().round_ties_even();
        Ok(if digits.is_nan() { 0 } else { digits.clamp(0.0, 8.0) as i64 })
    }

    /// The latest `limit` candles of `minutes` (the forming one included, as the main chart does),
    /// or the ones opening before `before` (unix, feed clock), as rows on the feed's clock.
    async fn candles(&self, symbol: &str, tf: (&'static str, i64), limit: i64, clock: &str, before: Option<i64>)
        -> Result<Vec<Candle>, Refusal> {
        let mut args = obj(json!({"symbol": symbol, "timeframe": tf.1.to_string(), "count": limit}));
        let before_t = match before {
            Some(b) => Some(DateTime::from_timestamp(b, 0).ok_or_else(internal)?.naive_utc()),
            None => None,
        };
        let parsed = Clock::parse(clock);
        if let (Some(b), Some(t)) = (before, before_t) {
            let utc = to_utc(parsed.as_ref().map_err(|_| internal())?, t);
            // None: an hour a DST change repeats.
            args.insert("to".into(), json!(utc.map(|u| u.timestamp()).unwrap_or(b) - 1));
        }
        let key = Key::Candles(symbol.into(), tf.0, limit, before);
        let rows = self.cached(key, CACHE_SECONDS, self.call("candles", 60.0, args)).await?;
        let rows: &[Value] = rows.as_array().map(Vec::as_slice).unwrap_or(&[]);
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let clock = parsed.map_err(|_| internal())?;
        // pandas gives a column of whole numbers int64 (shown as 4), any other float64 (4.0).
        let whole: Vec<bool> = (1..5).map(|c| rows.iter().all(|r| r.get(c).is_some_and(Value::is_i64))).collect();
        let mut out = Vec::new();
        for r in rows {
            let secs = r.get(0).and_then(Value::as_f64).ok_or_else(internal)?;
            let utc = from_unix_f64(secs).ok_or_else(internal)?;
            let at = clock.utc_to_feed(utc);
            if before_t.is_some_and(|b| at >= b) {
                continue;
            }
            let field = |c: usize| {
                let v = r.get(c).cloned().unwrap_or(Value::Null);
                if whole[c - 1] { v } else { json!(v.as_f64().unwrap_or(f64::NAN)) }
            };
            out.push(Candle { time: at, values: [field(1), field(2), field(3), field(4)] });
        }
        Ok(out)
    }

    /// The live price and when it traded (unix, feed clock); price None when there is none yet.
    async fn tick(&self, symbol: &str, clock: &str) -> Answer {
        let args = obj(json!({"symbol": symbol}));
        let q = self.cached(Key::Quote(symbol.into()), TICK_SECONDS, self.call("quote", 15.0, args)).await?;
        let price = match q.get("price") {
            None | Some(Value::Null) => None,
            Some(p) => Some(py_float(p).ok_or_else(internal)?),
        };
        let Some(price) = price.filter(|_| truthy(&q)) else {
            return Ok(json!({"symbol": symbol, "price": null, "time": null}));
        };
        let at = match q.get("time") {
            Some(t) if truthy(t) => DateTime::from_timestamp(py_int(t).ok_or_else(internal)?, 0).ok_or_else(internal)?,
            _ => Utc::now(),
        };
        let clock = Clock::parse(clock).map_err(|_| internal())?;
        Ok(json!({"symbol": symbol, "price": price, "time": py_unix(clock.utc_to_feed(at))}))
    }

    /// Stop the helper (the next call starts another).
    pub async fn close(&self) {
        if let Some(mut h) = self.helper.lock().await.take() {
            h.close().await;
        }
    }
}

struct Candle {
    time: Time,
    values: [Value; 4], // open, high, low, close
}

// ---- the routes ----

/// `GET /api/markets/candles?symbol=&tf=1H&limit=300&before=`: a pane's own symbol (e.g.
/// FX:XAGUSD). No levels: only the source's symbol is scanned.
pub async fn candles(m: &Markets, rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let mut errors = Vec::new();
    let symbol = required(q, "symbol", &mut errors);
    let tf = q.get("tf").cloned().unwrap_or_else(|| "1H".into());
    let limit = int_query(q, "limit", Some((LIMIT_MIN, LIMIT_MAX)), &mut errors).map(|l| l as i64);
    let before = int_query(q, "before", None, &mut errors);
    if !errors.is_empty() {
        return Err(Refusal::new(422, Value::Array(errors)));
    }
    let (symbol, limit) = (symbol.unwrap_or_default(), limit.unwrap_or(LIMIT_DEFAULT));
    let before = match before {
        Some(b) => Some(i64::try_from(b).map_err(|_| internal())?),
        None => None,
    };

    let upper = tf.to_uppercase();
    let Some(timeframe) = TIMEFRAMES.iter().find(|t| t.name == upper) else {
        return Err(Refusal::new(404, format!("unknown timeframe {}", py_repr(&tf))));
    };
    let sym = clean_symbol(&symbol)?;
    let clock = rt.engine().clock.clone();
    let rows = m.candles(&sym, (timeframe.name, timeframe.minutes), limit, &clock, before).await?;
    let precision = m.precision(&sym).await?;
    let price = rows.last().map(|c| json!(c.values[3].as_f64().unwrap_or(f64::NAN)));
    let has_more = !rows.is_empty();
    let candles: Vec<Value> = rows
        .into_iter()
        .map(|c| {
            let [open, high, low, close] = c.values;
            json!({"time": py_unix(c.time), "open": open, "high": high, "low": low, "close": close})
        })
        .collect();
    Ok(json!({
        "symbol": sym, "timeframe": timeframe.name, "precision": precision, "price": price,
        "candles": candles, "has_more": has_more, "levels": [], "swings": [],
    }))
}

/// `GET /api/markets/tick?symbol=`: the live price of a pane's own symbol (unix time on the
/// feed's clock).
pub async fn tick(m: &Markets, rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let mut errors = Vec::new();
    let symbol = required(q, "symbol", &mut errors);
    if !errors.is_empty() {
        return Err(Refusal::new(422, Value::Array(errors)));
    }
    let sym = clean_symbol(&symbol.unwrap_or_default())?;
    let clock = rt.engine().clock.clone();
    m.tick(&sym, &clock).await
}

// ---- symbols and errors ----

/// `FX:XAGUSD` from ` fx:xagusd `; TradingView symbols carry an exchange prefix (FX:XAGUSD,
/// OANDA:XAUUSD, TVC:GOLD). Anything else is a 400 the dashboard shows as is.
pub fn clean_symbol(symbol: &str) -> Result<String, Refusal> {
    let s = symbol.trim_matches(py_space).to_uppercase();
    let part = |p: &str, extra: &str| {
        (1..=32).contains(&p.len())
            && p.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b"_.".contains(&b) || extra.as_bytes().contains(&b))
    };
    match s.split_once(':') {
        Some((exchange, name)) if part(exchange, "") && part(name, "!&-") => Ok(s),
        _ => Err(Refusal::new(400, format!(
            "{} isn't a TradingView symbol. Use the exchange prefix, e.g. FX:XAGUSD.", py_repr(symbol)))),
    }
}

/// `MarketError`: an unknown symbol is a 404, another TradingView error a 502, a helper that
/// can't run or answer a 503.
fn market_error(e: FeedError, symbol: &str) -> Refusal {
    if e.kind != "TradingViewError" {
        return Refusal::new(503, e.message);
    }
    if e.code.as_deref() == Some("SYMBOL_ERROR") {
        return Refusal::new(404, format!("TradingView has no symbol {symbol}. Use the exchange prefix, e.g. \
                                          FX:XAGUSD (FXCM silver) or OANDA:XAUUSD."));
    }
    Refusal::new(502, e.message)
}

/// What Python answers when something it didn't expect fails.
fn internal() -> Refusal {
    Refusal::new(500, "Internal Server Error")
}

// ---- FastAPI's query validation ----

fn required(q: &HashMap<String, String>, name: &str, errors: &mut Vec<Value>) -> Option<String> {
    let v = q.get(name).cloned();
    if v.is_none() {
        errors.push(json!({"type": "missing", "loc": ["query", name], "msg": "Field required", "input": null}));
    }
    v
}

/// An int query parameter as pydantic reads it, with its `ge`/`le` bounds.
fn int_query(q: &HashMap<String, String>, name: &str, bounds: Option<(i128, i128)>, errors: &mut Vec<Value>)
    -> Option<i128> {
    let raw = q.get(name)?;
    let Some(n) = pydantic_int(raw) else {
        errors.push(json!({"type": "int_parsing", "loc": ["query", name],
                           "msg": "Input should be a valid integer, unable to parse string as an integer", "input": raw}));
        return None;
    };
    if let Some((ge, le)) = bounds {
        if n < ge {
            errors.push(json!({"type": "greater_than_equal", "loc": ["query", name],
                               "msg": format!("Input should be greater than or equal to {ge}"), "input": raw,
                               "ctx": {"ge": ge as i64}}));
            return None;
        }
        if n > le {
            errors.push(json!({"type": "less_than_equal", "loc": ["query", name],
                               "msg": format!("Input should be less than or equal to {le}"), "input": raw,
                               "ctx": {"le": le as i64}}));
            return None;
        }
    }
    Some(n)
}

/// pydantic's lax int from a string: surrounding whitespace, a sign, digits with single
/// underscores between them, and an optional fraction of zeros (`20.0`). Saturates past i128.
fn pydantic_int(raw: &str) -> Option<i128> {
    let s = raw.trim();
    let (neg, s) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let (whole, frac) = match s.split_once('.') {
        Some((w, f)) => (w, Some(f)),
        None => (s, None),
    };
    if frac.is_some_and(|f| f.is_empty() || !f.bytes().all(|b| b == b'0')) {
        return None;
    }
    if whole.is_empty() || whole.split('_').any(|g| g.is_empty() || !g.bytes().all(|b| b.is_ascii_digit())) {
        return None;
    }
    let n = whole.bytes().filter(u8::is_ascii_digit).fold(0i128, |n, d| n.saturating_mul(10).saturating_add((d - b'0') as i128));
    Some(if neg { -n } else { n })
}

// ---- small Python equivalents ----

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

/// `str.strip()`'s whitespace: Unicode's, plus the ASCII separators \x1c-\x1f.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// Python's truthiness for a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `float(x)` for a number or a numeric string.
fn py_float(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => s.trim().replace('_', "").parse().ok(),
        _ => None,
    }
}

/// `int(x)` for a number (truncated) or an integer string.
fn py_int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().filter(|f| f.is_finite()).map(|f| f.trunc() as i64)),
        Value::Bool(b) => Some(*b as i64),
        Value::String(s) => s.trim().replace('_', "").parse().ok(),
        _ => None,
    }
}

/// `pd.Timestamp(secs, unit="s", tz="UTC")` for a float number of seconds.
fn from_unix_f64(secs: f64) -> Option<DateTime<Utc>> {
    if !secs.is_finite() {
        return None;
    }
    let whole = secs.floor();
    let nanos = ((secs - whole) * 1e9).round() as u32;
    DateTime::from_timestamp(whole as i64, 0).map(|t| t + Duration::nanoseconds(nanos as i64))
}

/// `int(ts.timestamp())` for a naive feed time read as UTC (truncated toward zero).
fn py_unix(t: NaiveDateTime) -> i64 {
    let u = t.and_utc();
    let s = u.timestamp();
    if s < 0 && u.timestamp_subsec_nanos() > 0 { s + 1 } else { s }
}

/// `quarters.to_utc` for one naive feed time: None for a time the clock's zone repeats (DST
/// fall-back); a skipped one moves on to the change.
fn to_utc(clock: &Clock, t: Time) -> Option<DateTime<Utc>> {
    match *clock {
        Clock::Offset { ny: false, minutes } => Some((t - Duration::minutes(minutes)).and_utc()),
        Clock::Offset { ny: true, minutes } => localize(NEW_YORK, t - Duration::minutes(minutes), false),
        Clock::Zone(tz) => localize(tz, t, false),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ints_read_as_pydantic_does() {
        for (s, n) in [("20", 20), (" +1_0 ", 10), ("20.00", 20), ("-0", 0), ("020", 20), ("1_0.0", 10), ("\t15\n", 15)] {
            assert_eq!(pydantic_int(s), Some(n), "{s:?}");
        }
        for s in ["", "x", "20.", ".0", "20.5", "1__0", "_10", "10_", "1e3", "0x10", "- 5", "+-5", "20.0_0", "١٢"] {
            assert_eq!(pydantic_int(s), None, "{s:?}");
        }
        assert!(pydantic_int("99999999999999999999999").unwrap() > LIMIT_MAX);
    }

    #[test]
    fn symbols_need_the_exchange_prefix() {
        assert_eq!(clean_symbol(" fx:xagusd ").unwrap(), "FX:XAGUSD");
        assert_eq!(clean_symbol("OANDA:X_A.G!&-1").unwrap(), "OANDA:X_A.G!&-1");
        for bad in ["XAGUSD", "", "FX:XAG USD", "../etc:passwd", "FX:A:B", "FX!:A", &format!("{}:A", "A".repeat(33))] {
            let r = clean_symbol(bad).unwrap_err();
            assert_eq!(r.status, 400, "{bad:?}");
        }
        assert_eq!(clean_symbol("it's").unwrap_err().detail,
                   "\"it's\" isn't a TradingView symbol. Use the exchange prefix, e.g. FX:XAGUSD.");
    }
}
