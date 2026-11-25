//! The chart routes on the engine's bars (#96, step 9d part 2), ported from `server.py`:
//! `GET /api/candles` (with `before`, the older candles of `history.py`), bar replay
//! (`GET /api/replay/range`, `GET /api/replay`, `replay.py`) and `GET /api/clock/feed`.
//!
//! The handlers take the query as a map and answer as FastAPI does: the same bodies, statuses and
//! messages, and FastAPI's validation list (status 422) for a query it would refuse. The work on
//! explicit bars and store ([`older_candles`], [`replay_range_of`], [`replay_view`]) is public so
//! the stored-history paths can be checked without a running TradingView feed.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, OnceLock, Weak};

use chrono::{DateTime, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Timelike};
use serde_json::{json, Value};

use super::{py_iso_naive, Answer, Engine, Refusal, Runtime};
use wednesday_base::py::py_repr;
use wednesday_scan::{scan, unix, Bar, Clock, ScanConfig, Timeframe, TIMEFRAMES};
use wednesday_store::{Arg, Store};

/// Replay views kept, so stepping back and forth doesn't rescan (`replay.CACHE_SIZE`).
pub const CACHE_SIZE: usize = 64;

// ---- FastAPI's query validation ----

/// Pydantic's lax `int` from a query string: surrounding whitespace, a sign, digits with single
/// underscores between them, and an optional `.000` part. Too big for i128 saturates (it only
/// meets range checks or the time limits after that).
pub fn py_int(s: &str) -> Option<i128> {
    let s = s.trim();
    let (neg, body) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let (int, frac) = match body.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (body, None),
    };
    if let Some(f) = frac {
        if f.is_empty() || !f.bytes().all(|b| b == b'0') {
            return None;
        }
    }
    if int.is_empty() || int.starts_with('_') || int.ends_with('_') || int.contains("__") {
        return None;
    }
    let digits: String = int.chars().filter(|&c| c != '_').collect();
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let v = digits.parse::<i128>().unwrap_or(i128::MAX);
    Some(if neg { -v } else { v })
}

/// One query parameter's validation error, as FastAPI lists it.
fn missing(name: &str) -> Value {
    json!({"type": "missing", "loc": ["query", name], "msg": "Field required", "input": null})
}

fn not_int(name: &str, raw: &str) -> Value {
    json!({"type": "int_parsing", "loc": ["query", name],
           "msg": "Input should be a valid integer, unable to parse string as an integer", "input": raw})
}

/// An optional int parameter (`Query(None)`): None when absent.
fn opt_int(q: &HashMap<String, String>, name: &str, errors: &mut Vec<Value>) -> Option<i128> {
    let raw = q.get(name)?;
    let v = py_int(raw);
    if v.is_none() {
        errors.push(not_int(name, raw));
    }
    v
}

/// A required int parameter (`Query(...)`).
fn req_int(q: &HashMap<String, String>, name: &str, errors: &mut Vec<Value>) -> Option<i128> {
    if !q.contains_key(name) {
        errors.push(missing(name));
        return None;
    }
    opt_int(q, name, errors)
}

/// `limit: int = Query(default, ge=10, le=2000)`.
fn limit(q: &HashMap<String, String>, default: i128, errors: &mut Vec<Value>) -> usize {
    let Some(raw) = q.get("limit") else { return default as usize };
    match py_int(raw) {
        None => errors.push(not_int("limit", raw)),
        Some(v) if v < 10 => errors.push(json!({"type": "greater_than_equal", "loc": ["query", "limit"],
            "msg": "Input should be greater than or equal to 10", "input": raw, "ctx": {"ge": 10}})),
        Some(v) if v > 2000 => errors.push(json!({"type": "less_than_equal", "loc": ["query", "limit"],
            "msg": "Input should be less than or equal to 2000", "input": raw, "ctx": {"le": 2000}})),
        Some(v) => return v as usize,
    }
    0
}

fn invalid(errors: Vec<Value>) -> Refusal {
    Refusal::new(422, Value::Array(errors))
}

/// An exception the Python route doesn't catch: FastAPI's 500.
fn internal() -> Refusal {
    Refusal::new(500, "Internal Server Error")
}

/// `TIMEFRAMES_BY_NAME.get(tf.upper())`, or the route's 404.
fn timeframe(tf: &str) -> Result<Timeframe, Refusal> {
    let upper = tf.to_uppercase();
    TIMEFRAMES
        .iter()
        .find(|t| t.name == upper)
        .copied()
        .ok_or_else(|| Refusal::new(404, format!("unknown timeframe {}", py_repr(tf))))
}

// ---- candles ----

/// `resample_ohlcv(m1, tf, drop_incomplete=False)`: like [`wednesday_scan::resample`], but keeping
/// the still-forming last candle, so the chart matches the live price.
pub fn resample_all(m1: &[Bar], tf: Timeframe) -> Vec<Bar> {
    let step = tf.minutes * 60;
    let mut out: Vec<Bar> = Vec::new();
    let mut bin = i64::MIN;
    for b in m1 {
        let start = unix(b.time).div_euclid(step) * step;
        if start != bin {
            bin = start;
            let time = DateTime::from_timestamp(start, 0).expect("in range").naive_utc();
            out.push(Bar { time, ..*b });
        } else {
            let c = out.last_mut().expect("a candle");
            c.high = c.high.max(b.high);
            c.low = c.low.min(b.low);
            c.close = b.close;
            c.volume += b.volume;
        }
    }
    out
}

fn tail<T>(v: &[T], n: usize) -> &[T] {
    &v[v.len().saturating_sub(n)..]
}

/// `_candle_rows`: the chart's candles (unix time on the feed clock).
pub fn candle_rows(candles: &[Bar]) -> Value {
    Value::Array(
        candles
            .iter()
            .map(|c| json!({"time": unix(c.time), "open": c.open, "high": c.high, "low": c.low, "close": c.close}))
            .collect(),
    )
}

/// The active levels and the labelled swings of `tf` in a scan (empty when it didn't scan `tf`).
fn levels_and_swings(result: &wednesday_scan::ScanResult, tf: Timeframe) -> (Value, Value) {
    match result.results.iter().find(|r| r.timeframe.name == tf.name) {
        Some(r) => (
            Value::Array(r.sets.iter().flat_map(|(_, s)| s.active.iter().map(|lv| lv.to_json())).collect()),
            Value::Array(r.swings.clone()),
        ),
        None => (json!([]), json!([])),
    }
}

/// A stored bar stream: the store with the source and symbol its bars are kept under. Only a feed
/// that persists its bars has one (TradingView; not the synthetic feed).
#[derive(Clone, Copy)]
pub struct Stream<'a> {
    pub store: &'a Store,
    pub source: &'a str,
    pub symbol: &'a str,
}

fn stream_of<'a>(rt: &'a Runtime, engine: &'a Engine) -> Option<Stream<'a>> {
    match (&rt.store, engine.persist) {
        (Some(store), true) => Some(Stream { store, source: &engine.source, symbol: &engine.symbol }),
        _ => None,
    }
}

fn bar_of(r: &serde_json::Map<String, Value>) -> Option<Bar> {
    let f = |k: &str| r.get(k).and_then(Value::as_f64).unwrap_or(f64::NAN);
    Some(Bar { time: DateTime::from_timestamp(r.get("time")?.as_i64()?, 0)?.naive_utc(), open: f("open"),
               high: f("high"), low: f("low"), close: f("close"), volume: f("volume") })
}

/// `Store.load_bar_range`: stored M1 bars opening in [start, end], oldest first.
async fn load_bar_range(s: Stream<'_>, start: i64, end: i64) -> Result<Vec<Bar>, String> {
    let sql = "SELECT time, open, high, low, close, volume FROM bars \
               WHERE source = ? AND symbol = ? AND tf = '1m' AND time >= ? AND time <= ? ORDER BY time";
    let args = vec![Arg::Str(Some(s.source.into())), Arg::Str(Some(s.symbol.into())), Arg::Int(Some(start)),
                    Arg::Int(Some(end))];
    Ok(s.store.rows("bars", sql, args).await?.iter().filter_map(bar_of).collect())
}

/// The first and last stored bar's open time (`Store.bar_bounds` without the count).
async fn bar_bounds(s: Stream<'_>) -> Result<(Option<i64>, Option<i64>), String> {
    let mut out = [None, None];
    for (slot, order) in out.iter_mut().zip(["ASC", "DESC"]) {
        let sql = format!("SELECT time FROM bars WHERE source = ? AND symbol = ? AND tf = '1m' ORDER BY time {order} LIMIT 1");
        let args = vec![Arg::Str(Some(s.source.into())), Arg::Str(Some(s.symbol.into()))];
        *slot = s.store.rows("bars", &sql, args).await?.first().and_then(|r| r.get("time")?.as_i64());
    }
    Ok((out[0], out[1]))
}

/// `history.older_candles`: up to `limit` candles opening before `before` (unix, feed clock), from
/// the engine's M1 bars and then the stored ones, and whether older ones may exist.
pub async fn older_candles(m1: &[Bar], stream: Option<Stream<'_>>, tf: Timeframe, before: i64, limit: usize)
    -> Result<(Vec<Bar>, bool), String> {
    let mut edge = before;
    let mut mem: Vec<Bar> = Vec::new();
    if !m1.is_empty() {
        // The first candle may open before the buffer's first bar: leave it to the older sources.
        let all = resample_all(m1, tf);
        let kept: Vec<Bar> = all.into_iter().skip(1).filter(|c| unix(c.time) < edge).collect();
        mem = tail(&kept, limit).to_vec();
        if let Some(first) = mem.first() {
            edge = unix(first.time);
        }
    }
    let rest = limit - mem.len();
    let mut more = true;
    let mut out = Vec::new();
    if rest > 0 {
        let (older, m) = beyond_memory(stream, tf, edge, rest).await?;
        more = m;
        out = older;
    }
    out.extend(mem);
    Ok((out, more))
}

async fn beyond_memory(stream: Option<Stream<'_>>, tf: Timeframe, edge: i64, count: usize)
    -> Result<(Vec<Bar>, bool), String> {
    let Some(s) = stream else { return Ok((Vec::new(), false)) };
    let wanted = count * tf.minutes as usize;
    let m1 = super::load_bars(s.store, s.source, s.symbol, wanted, Some(edge)).await?;
    if m1.is_empty() {
        return Ok((Vec::new(), false));
    }
    let mut candles = resample_all(&m1, tf);
    let full = m1.len() >= wanted;
    if full && candles.len() > 1 {
        candles.remove(0); // may be partial; the next page loads it whole
    }
    let kept: Vec<Bar> = candles.into_iter().filter(|c| unix(c.time) < edge).collect();
    Ok((tail(&kept, count).to_vec(), full))
}

/// `GET /api/candles?tf=1H&limit=200&before=`: the latest candles with their levels and swings;
/// with `before` (unix), older candles only, for scrolling back.
pub async fn candles(rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let mut errors = Vec::new();
    let tf_name = q.get("tf").map_or("1H", String::as_str);
    let limit = limit(q, 200, &mut errors);
    let before = opt_int(q, "before", &mut errors);
    if !errors.is_empty() {
        return Err(invalid(errors));
    }
    let tf = timeframe(tf_name)?;
    let engine = rt.engine();
    let st = engine.state();
    if let Some(native) = st.native.get(tf.name).filter(|c| !c.is_empty()) {
        return match before {
            Some(before) => native_before(native, stream_of(rt, &engine), tf, before, limit).await,
            None => latest_native(native, &st.m1, st.result.as_deref(), tf, limit),
        };
    }
    match before {
        Some(before) => candles_before(&st.m1, stream_of(rt, &engine), tf, before, limit).await,
        None => latest_candles(&st.m1, st.result.as_deref(), tf, limit),
    }
}

/// [`latest_candles`] on the source's own candles of `tf`, then the forming one built from M1 on
/// their grid, so the chart shows TradingView's candles and still ends at the live price.
pub fn latest_native(native: &[Bar], m1: &[Bar], result: Option<&wednesday_scan::ScanResult>, tf: Timeframe,
                     limit: usize) -> Answer {
    let Some(result) = result else { return Err(Refusal::new(503, "no data yet")) };
    let mut all = native.to_vec();
    all.extend(crate::ingest::forming(native, m1, tf.minutes));
    let (levels, swings) = levels_and_swings(result, tf);
    Ok(json!({
        "timeframe": tf.name,
        "price": result.price,
        "candles": candle_rows(tail(&all, limit)),
        "levels": levels,
        "swings": swings,
    }))
}

/// [`candles_before`] on the source's own candles: those in memory, then the stored ones.
pub async fn native_before(native: &[Bar], stream: Option<Stream<'_>>, tf: Timeframe, before: i128, limit: usize)
    -> Answer {
    let before = i64::try_from(before).map_err(|_| internal())?;
    let kept: Vec<Bar> = native.iter().filter(|c| unix(c.time) < before).copied().collect();
    let mut out = tail(&kept, limit).to_vec();
    let mut more = false;
    let rest = limit - out.len();
    if let (Some(s), Some(n), true) = (stream, crate::ingest::native_tf(tf.name), rest > 0) {
        let edge = out.first().map_or(before, |c| unix(c.time));
        let older = super::load_series(s.store, s.source, s.symbol, n.tf, rest, Some(edge)).await.map_err(|e| {
            log::error!("older candles: {e}");
            internal()
        })?;
        more = older.len() == rest;
        out.splice(0..0, older);
    } else if rest == 0 {
        more = true;
    }
    Ok(json!({"timeframe": tf.name, "candles": candle_rows(&out), "has_more": more}))
}

/// [`candles`] with `before`: the older candles' answer.
pub async fn candles_before(m1: &[Bar], stream: Option<Stream<'_>>, tf: Timeframe, before: i128, limit: usize)
    -> Answer {
    // pandas can't hold a time that far out: the Python route fails there.
    let before = i64::try_from(before).map_err(|_| internal())?;
    let (older, more) = older_candles(m1, stream, tf, before, limit).await.map_err(|e| {
        log::error!("older candles: {e}");
        internal()
    })?;
    Ok(json!({"timeframe": tf.name, "candles": candle_rows(&older), "has_more": more}))
}

/// [`candles`] without `before`: the latest candles, the forming one included so the chart
/// matches the live price, with the scan's levels and swings on `tf`.
pub fn latest_candles(m1: &[Bar], result: Option<&wednesday_scan::ScanResult>, tf: Timeframe, limit: usize) -> Answer {
    let Some(result) = result else { return Err(Refusal::new(503, "no data yet")) };
    let all = resample_all(m1, tf);
    let (levels, swings) = levels_and_swings(result, tf);
    Ok(json!({
        "timeframe": tf.name,
        "price": result.price,
        "candles": candle_rows(tail(&all, limit)),
        "levels": levels,
        "swings": swings, // confirmed swing points labelled HH / LH / HL / LL
    }))
}

// ---- bar replay (replay.py) ----

/// `GET /api/replay/range`: the earliest and latest minute a replay can start at (unix, feed clock).
pub async fn replay_range(rt: &Arc<Runtime>, _q: &HashMap<String, String>) -> Answer {
    let engine = rt.engine();
    let st = engine.state();
    replay_range_of(&st.m1, stream_of(rt, &engine), engine.max_bars).await
}

/// [`replay_range`] on explicit bars: the stored stream's bounds widened by the live buffer's.
pub async fn replay_range_of(m1: &[Bar], stream: Option<Stream<'_>>, max_bars: usize) -> Answer {
    let (mut first, mut last) = match stream {
        Some(s) => bar_bounds(s).await.map_err(|e| {
            log::error!("replay range: {e}");
            internal()
        })?,
        None => (None, None),
    };
    if let (Some(a), Some(b)) = (m1.first(), m1.last()) {
        // `first or 10**12`: Python reads a stored 0 as none.
        first = Some(first.filter(|f| *f != 0).unwrap_or(1_000_000_000_000).min(unix(a.time)));
        last = Some(last.filter(|l| *l != 0).unwrap_or(0).max(unix(b.time)));
    }
    let Some(first) = first else { return Err(Refusal::new(503, "no data yet")) };
    // A scan needs its lookback of history before the clock.
    Ok(json!({"first": first + (max_bars / 2 * 60) as i64, "last": last.unwrap_or(0) + 60}))
}

/// `replay_bars`: M1 history reaching back far enough for one scan before `at`: stored bars plus
/// the live buffer (whose bar wins where both have one).
async fn replay_bars(m1: &[Bar], stream: Option<Stream<'_>>, max_bars: usize, at: i64) -> Result<Vec<Bar>, Refusal> {
    let mut bars = match stream {
        Some(s) => {
            let span = (max_bars as f64 * 60.0 * 1.6) as i64 + 3 * 86400; // weekends and holidays have no bars
            load_bar_range(s, at.saturating_sub(span), at).await.map_err(|e| {
                log::error!("replay bars: {e}");
                internal()
            })?
        }
        None => Vec::new(),
    };
    if bars.is_empty() && m1.is_empty() {
        return Err(Refusal::new(503, "no data yet"));
    }
    bars.extend_from_slice(m1);
    bars.sort_by_key(|b| b.time); // stable: of equal times, the live buffer's comes last
    let mut out: Vec<Bar> = Vec::with_capacity(bars.len());
    for b in bars {
        match out.last_mut() {
            Some(prev) if prev.time == b.time => *prev = b,
            _ => out.push(b),
        }
    }
    Ok(out)
}

/// `replay.view`: the scan and `tf` candles at the replay clock `at` (unix, feed clock): only the
/// M1 bars closed by then (open + 1 minute <= at), the forming `tf` candle built from them.
pub fn view(m1: &[Bar], cfg: &ScanConfig, at: i64, tf: Timeframe, limit: usize) -> Answer {
    let closed: Vec<Bar> = m1.iter().filter(|b| unix(b.time) + 60 <= at).copied().collect();
    let bars = tail(&closed, cfg.required_m1_bars());
    let Some(last) = bars.last() else {
        return Err(Refusal::new(422, "No stored bars before that time: pick a later start"));
    };
    let result = scan(bars, cfg, None).map_err(|e| Refusal::new(422, e))?;
    let candles = resample_all(bars, tf);
    let (levels, swings) = levels_and_swings(&result, tf);
    Ok(json!({
        "at": at,
        "bar_time": unix(last.time), // the last closed minute
        "timeframe": tf.name,
        "price": result.price,
        "candles": candle_rows(tail(&candles, limit)),
        "levels": levels,
        "swings": swings,
        "scan": result.to_json(None), // today's bias isn't applied to the past
    }))
}

/// [`replay`] on explicit bars and store, without the cache.
pub async fn replay_view(m1: &[Bar], stream: Option<Stream<'_>>, cfg: &ScanConfig, at: i128, tf: Timeframe,
                         limit: usize) -> Answer {
    let at = at - at.rem_euclid(60); // whole minutes
    let Ok(at) = i64::try_from(at) else {
        // pandas can't hold the clock: the store refuses the number, or the cut fails.
        if stream.is_some() {
            return Err(internal());
        }
        if m1.is_empty() {
            return Err(Refusal::new(503, "no data yet"));
        }
        return Err(Refusal::new(422, format!("Out of bounds nanosecond timestamp {at}")));
    };
    let bars = replay_bars(m1, stream, cfg.required_m1_bars(), at).await?;
    let cfg = cfg.clone();
    tokio::task::spawn_blocking(move || view(&bars, &cfg, at, tf, limit)).await.map_err(|_| internal())?
}

/// The last few replay views by (engine, source, at, timeframe, limit) (`ReplayCache`). The
/// engine is held weakly: a key only matches while its engine is the one that made it.
struct ReplayCache {
    items: VecDeque<(ReplayKey, Value)>,
}

#[derive(Clone)]
struct ReplayKey {
    engine: Weak<Engine>,
    source: String,
    at: i128,
    tf: &'static str,
    limit: usize,
}

impl ReplayKey {
    fn matches(&self, other: &ReplayKey) -> bool {
        Weak::ptr_eq(&self.engine, &other.engine)
            && self.engine.strong_count() > 0
            && (self.source.as_str(), self.at, self.tf, self.limit) == (other.source.as_str(), other.at, other.tf, other.limit)
    }
}

fn replay_cache() -> &'static Mutex<ReplayCache> {
    static CACHE: OnceLock<Mutex<ReplayCache>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(ReplayCache { items: VecDeque::new() }))
}

/// `GET /api/replay?at=&tf=1H&limit=200`: the scan and `tf` candles at the replay clock `at`
/// (unix, feed clock). Nothing after it.
pub async fn replay(rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let mut errors = Vec::new();
    let at = req_int(q, "at", &mut errors);
    let tf_name = q.get("tf").map_or("1H", String::as_str);
    let limit = limit(q, 200, &mut errors);
    if !errors.is_empty() {
        return Err(invalid(errors));
    }
    let at = at.expect("validated");
    let tf = timeframe(tf_name)?;
    let engine = rt.engine();
    let key = ReplayKey { engine: Arc::downgrade(&engine), source: engine.source.clone(), at: at - at.rem_euclid(60),
                          tf: tf.name, limit };
    {
        let mut cache = replay_cache().lock().unwrap();
        if let Some(i) = cache.items.iter().position(|(k, _)| k.matches(&key)) {
            let item = cache.items.remove(i).expect("found");
            let value = item.1.clone();
            cache.items.push_back(item);
            return Ok(value);
        }
    }
    let st = engine.state();
    let value = replay_view(&st.m1, stream_of(rt, &engine), &rt.opts.cfg, at, tf, limit).await?;
    let mut cache = replay_cache().lock().unwrap();
    cache.items.retain(|(k, _)| k.engine.strong_count() > 0 && !k.matches(&key));
    cache.items.push_back((key, value.clone()));
    while cache.items.len() > CACHE_SIZE {
        cache.items.pop_front();
    }
    Ok(value)
}

// ---- go to date ----

/// What `pd.Timestamp(text)` makes of the dashboard's go-to-date text.
#[derive(Debug, PartialEq)]
pub enum Wall {
    Naive(NaiveDateTime),
    Aware, // it carries an offset or a zone
    NaT,   // empty, or "NaT"
}

/// `pd.Timestamp(text)` for the ISO-style forms: `2026-09-24`, `2026-9-24`, `2026/09/24`,
/// `20260924`, `2026-09`, `2026`, then `T`, `t` or a space and `09`, `9:30`, `0930`, `09:30:15`,
/// `09:30:15.5` (or `,5`), then `Z`, `+02:00`, `-0500` or ` UTC` (aware). Err for anything else
/// (pandas' wider guesses, `Sep 24 2026` or `now`, aren't taken).
pub fn parse_wall(text: &str) -> Result<Wall, ()> {
    let s = text.trim();
    if s.is_empty() || ["NaT", "nat", "NAT", "nan", "NaN", "NAN"].contains(&s) {
        return Ok(Wall::NaT);
    }
    let b = s.as_bytes();
    let mut i: usize;
    let digits = |i: &mut usize, max: usize| -> Option<u32> {
        let start = *i;
        while *i < b.len() && *i - start < max && b[*i].is_ascii_digit() {
            *i += 1;
        }
        (*i > start).then(|| s[start..*i].parse().unwrap())
    };
    // The date.
    let run = b.iter().take_while(|c| c.is_ascii_digit()).count();
    let (year, month, day) = if run == 8 {
        i = 8;
        (s[0..4].parse::<i32>().unwrap(), s[4..6].parse().unwrap(), s[6..8].parse().unwrap())
    } else if run == 4 {
        i = 4;
        let year = s[0..4].parse::<i32>().unwrap();
        let (mut month, mut day) = (1, 1);
        if i < b.len() && (b[i] == b'-' || b[i] == b'/') {
            let sep = b[i];
            i += 1;
            month = digits(&mut i, 2).ok_or(())?;
            if i < b.len() && b[i] == sep {
                i += 1;
                day = digits(&mut i, 2).ok_or(())?;
            }
        }
        (year, month, day)
    } else {
        return Err(());
    };
    let date = NaiveDate::from_ymd_opt(year, month, day).ok_or(())?;
    // The time.
    let mut time = NaiveTime::MIN;
    if i < b.len() && matches!(b[i], b'T' | b't' | b' ') && b.get(i + 1).is_some_and(u8::is_ascii_digit) {
        i += 1;
        let start = i;
        let hm = b[i..].iter().take_while(|c| c.is_ascii_digit()).count();
        let (mut minute, mut second, mut nanos) = (0u32, 0u32, 0u32);
        let hour = if hm == 4 || hm == 6 {
            minute = s[start + 2..start + 4].parse().unwrap();
            if hm == 6 {
                second = s[start + 4..start + 6].parse().unwrap();
            }
            i += hm;
            s[start..start + 2].parse().unwrap()
        } else if hm <= 2 {
            let h = digits(&mut i, 2).ok_or(())?;
            if i < b.len() && b[i] == b':' {
                i += 1;
                minute = digits(&mut i, 2).ok_or(())?;
                if i < b.len() && b[i] == b':' {
                    i += 1;
                    second = digits(&mut i, 2).ok_or(())?;
                    if i < b.len() && (b[i] == b'.' || b[i] == b',') {
                        i += 1;
                        let f0 = i;
                        while i < b.len() && b[i].is_ascii_digit() {
                            i += 1;
                        }
                        if i == f0 {
                            return Err(());
                        }
                        let frac = &s[f0..i.min(f0 + 9)];
                        nanos = frac.parse::<u32>().unwrap() * 10u32.pow(9 - frac.len() as u32);
                    }
                }
            }
            h
        } else {
            return Err(());
        };
        time = NaiveTime::from_hms_nano_opt(hour, minute, second, nanos).ok_or(())?;
    }
    // An offset or zone makes it aware.
    let rest = &s[i..];
    if rest.is_empty() {
        return Ok(Wall::Naive(date.and_time(time)));
    }
    let r = rest.trim_start();
    if ["Z", "z", "UTC", "GMT"].contains(&r) {
        return Ok(Wall::Aware);
    }
    let rb = r.as_bytes();
    if rest.len() == r.len() && matches!(rb[0], b'+' | b'-') {
        let off = &r[1..];
        let ok = match off.len() {
            2 => off.bytes().all(|c| c.is_ascii_digit()),
            4 => off.bytes().all(|c| c.is_ascii_digit()),
            5 => off.as_bytes()[2] == b':' && off.bytes().enumerate().all(|(k, c)| k == 2 || c.is_ascii_digit()),
            _ => false,
        };
        if ok && off[0..2].parse::<u32>().unwrap() < 24 {
            return Ok(Wall::Aware);
        }
    }
    Err(())
}

/// `tz_localize("America/New_York", ambiguous=True, nonexistent="shift_forward")`: a repeated
/// wall time takes its first occurrence, a skipped one the instant of the change.
fn localize_new_york(wall: NaiveDateTime) -> Option<DateTime<chrono::Utc>> {
    let tz = wednesday_scan::clock::NEW_YORK;
    match tz.from_local_datetime(&wall) {
        LocalResult::Single(d) => Some(d.with_timezone(&chrono::Utc)),
        LocalResult::Ambiguous(a, b) => Some(a.min(b).with_timezone(&chrono::Utc)),
        LocalResult::None => {
            // The change is on a whole minute: the first wall minute after the gap is its instant.
            let minute = wall.with_second(0)?.with_nanosecond(0)?;
            wednesday_scan::clock::localize(tz, minute, true)
        }
    }
}

/// `pd.Timestamp.isoformat()` of a naive time: microseconds when there are some, nanoseconds when
/// there are those.
fn iso_nanos(t: NaiveDateTime) -> String {
    let n = t.nanosecond();
    if n == 0 {
        py_iso_naive(t)
    } else if n % 1000 == 0 {
        t.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    } else {
        t.format("%Y-%m-%dT%H:%M:%S%.9f").to_string()
    }
}

/// `GET /api/clock/feed?ny=2026-09-24T09:30`: a New York wall time as the chart's feed-clock unix
/// seconds (for go to date).
pub async fn clock_feed(rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let Some(ny) = q.get("ny") else { return Err(invalid(vec![missing("ny")])) };
    let clock = Clock::parse(&rt.engine().clock).map_err(|_| internal())?;
    feed_time(ny, &clock)
}

/// [`clock_feed`] for an explicit clock.
pub fn feed_time(ny: &str, clock: &Clock) -> Answer {
    let wall = match parse_wall(ny) {
        Ok(Wall::Naive(w)) => w,
        Ok(Wall::Aware) => return Err(Refusal::new(422, "ny is a New York wall time, without an offset")),
        Ok(Wall::NaT) => return Err(internal()), // NaT has no timestamp: the Python route fails
        Err(()) => return Err(Refusal::new(422, "ny is a date and time like 2026-09-24T09:30")),
    };
    // A time the spring change skips moves on to 03:00; one the autumn change repeats takes the first.
    let aware = localize_new_york(wall).ok_or_else(internal)?;
    let feed = clock.utc_to_feed(aware);
    // int(feed.timestamp()): toward zero.
    let mut feed_unix = unix(feed);
    if feed_unix < 0 && feed.nanosecond() > 0 {
        feed_unix += 1;
    }
    Ok(json!({"ny": iso_nanos(wall), "feed": iso_nanos(feed), "feed_unix": feed_unix}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn ints_parse_as_pydantic_does() {
        for (s, v) in [("12", Some(12)), (" 12 ", Some(12)), ("+12", Some(12)), ("-12", Some(-12)),
                       ("1_000", Some(1000)), ("12.0", Some(12)), ("12.00000", Some(12)), ("0012", Some(12)),
                       ("\t12\n", Some(12)), ("12.5", None), ("1e3", None), ("0x10", None), ("", None),
                       ("abc", None), ("1.", None), (".5", None), ("+", None), ("1__0", None), ("_1", None),
                       ("１２", None)] {
            assert_eq!(py_int(s), v, "{s:?}");
        }
    }

    #[test]
    fn wall_times_parse_as_pandas_does() {
        let at = |s: &str| match parse_wall(s) {
            Ok(Wall::Naive(t)) => t.format("%Y-%m-%d %H:%M:%S%.f").to_string(),
            other => format!("{other:?}"),
        };
        assert_eq!(at("2026-09-24T09:30"), "2026-09-24 09:30:00");
        assert_eq!(at("2026-9-24"), "2026-09-24 00:00:00");
        assert_eq!(at("20260924"), "2026-09-24 00:00:00");
        assert_eq!(at("2026/09/24 09:30"), "2026-09-24 09:30:00");
        assert_eq!(at("2026-09-24t0930"), "2026-09-24 09:30:00");
        assert_eq!(at("2026-09-24T09:30:15,5"), "2026-09-24 09:30:15.500");
        assert_eq!(at("2026"), "2026-01-01 00:00:00");
        assert_eq!(at("2026-09-24T09:30Z"), "Ok(Aware)");
        assert_eq!(at("2026-09-24T09:30-0500"), "Ok(Aware)");
        assert_eq!(at("2026-09-24T09:30 UTC"), "Ok(Aware)");
        assert_eq!(at(""), "Ok(NaT)");
        for bad in ["abc", "2026-02-30T09:30", "2026-09-24T25:00", "2026-09-24T09:30:60", "Sep 24 2026"] {
            assert_eq!(parse_wall(bad), Err(()), "{bad}");
        }
    }

    #[test]
    fn the_forming_candle_is_kept() {
        let t = |m: i64| DateTime::from_timestamp(1_790_000_000 / 3600 * 3600 + m * 60, 0).unwrap().naive_utc();
        let m1: Vec<Bar> = (0..7).map(|m| Bar { time: t(m), open: 1.0, high: 2.0 + m as f64, low: 0.5, close: 1.5,
                                                 volume: 1.0 }).collect();
        let five = resample_all(&m1, TIMEFRAMES[4]);
        assert_eq!(five.len(), 2);
        assert_eq!((five[1].high, five[1].volume), (8.0, 2.0));
        assert_eq!(Duration::minutes(5), five[1].time - five[0].time);
    }
}
