//! The scan engine (`engine.py`), #96 step 9d: the feed, a rolling buffer of closed M1 bars, a
//! scan after every minute close and the live price between scans.
//!
//! One task owns the feed (connect, the minute fetch, live ticks, queued feed work); scans run on
//! the blocking pool so a slow scan never holds up the live price. Readers take snapshots of the
//! state. The Python server follows this engine while its alerts and plugins still run there: it
//! reads the same bars through `GET /api/engine/m1`.

pub mod analysis;
pub mod charts;
pub mod feed;
pub mod markets;
pub mod tradingview;

pub use wednesday_base::{Answer, Refusal};

use std::sync::{Arc, Mutex};
use std::time::Duration as StdDuration;

use chrono::{DateTime, Duration, NaiveDateTime, Timelike, Utc};
use serde_json::{json, Map, Value};
use tokio::sync::{mpsc, oneshot, watch};

use wednesday_base::py::py_repr;
use wednesday_scan::{scan, Bar, Clock, Detector, DetectorParams, ScanConfig, ScanResult, Time};
use wednesday_store::{Arg, Store};
use feed::{Feed, FeedError, Tick};

/// After losing a connection that doesn't retry by itself, the feed gets this many tries.
pub const RECONNECT_ATTEMPTS: u32 = 3;
/// A tick starts the forming candle only this soon after the last closed bar.
const FORMING_MINUTES: i64 = 5;
/// Bars fetched each minute once the buffer is full.
const UPDATE_BARS: usize = 30;
/// Seconds after each minute close before fetching.
pub const DELAY: f64 = 2.0;

// ---- data source settings (settings.py) ----

pub const SETTINGS_KEY: &str = "data_source";
pub const DEFAULT_SOURCE: &str = "tradingview";
const MAX_TICK: f64 = 60.0;

/// A data source: title, description, defaults (`SOURCES` / `DEV_SOURCES`).
pub struct SourceInfo {
    pub id: &'static str,
    pub title: &'static str,
    pub description: &'static str,
    pub default_symbol: &'static str,
    pub default_clock: &'static str,
    pub default_tick: i64,
    pub min_tick: f64,
    pub offered: bool, // shown in the dashboard (false: development and tests only)
}

pub const SOURCES: [SourceInfo; 2] = [
    SourceInfo {
        id: "tradingview",
        title: "TradingView",
        description: "Free, no account. Spot gold from TradingView's feeds: FX:XAUUSD is FXCM, or any symbol with its \
                      exchange prefix (OANDA:XAUUSD, ...). About 5 days of 1-minute bars at first; history grows as bars \
                      are stored. Unofficial: it may stop working if TradingView changes its site.",
        default_symbol: "FX:XAUUSD",
        default_clock: "UTC",
        default_tick: 1,
        min_tick: 0.5,
        offered: true,
    },
    SourceInfo {
        id: "synthetic",
        title: "Synthetic data",
        description: "Random-walk prices for development and tests. Not real market data.",
        default_symbol: "XAUUSD",
        default_clock: "UTC",
        default_tick: 2,
        min_tick: 0.5,
        offered: false,
    },
];

pub fn source_info(id: &str) -> Option<&'static SourceInfo> {
    SOURCES.iter().find(|s| s.id == id)
}

/// What the dashboard edits (`DataSettings`): None means the source's default.
#[derive(Clone, Debug, PartialEq)]
pub struct DataSettings {
    pub source: String,
    pub symbol: Option<String>,
    pub clock: Option<String>,
    pub tick_seconds: Option<f64>,
}

impl Default for DataSettings {
    fn default() -> Self {
        DataSettings { source: DEFAULT_SOURCE.into(), symbol: None, clock: None, tick_seconds: None }
    }
}

impl DataSettings {
    fn info(&self) -> &'static SourceInfo {
        source_info(&self.source).unwrap_or(&SOURCES[0])
    }

    pub fn resolved_symbol(&self) -> String {
        self.symbol.clone().unwrap_or_else(|| self.info().default_symbol.into())
    }

    pub fn resolved_clock(&self) -> String {
        self.clock.clone().unwrap_or_else(|| self.info().default_clock.into())
    }

    /// The live price interval as Python holds it: the source's int default, or the saved float.
    pub fn resolved_tick(&self) -> Value {
        match self.tick_seconds {
            Some(t) => json!(t),
            None => json!(self.info().default_tick),
        }
    }

    pub fn validate(&self) -> Vec<String> {
        let mut errors = Vec::new();
        let known = source_info(&self.source);
        if known.is_none() {
            errors.push(format!("Unknown source {}", py_repr(&self.source)));
        }
        if self.symbol.as_deref().is_some_and(|s| s.trim().is_empty()) {
            errors.push("Symbol can't be blank".into());
        }
        if let Some(c) = &self.clock {
            if Clock::parse(c).is_err() {
                errors.push(format!("Unknown clock {}: use UTC, NY+7, UTC+3 or a time zone like Europe/London", py_repr(c)));
            }
        }
        if let (Some(t), Some(info)) = (self.tick_seconds, known) {
            if !(t == 0.0 || (info.min_tick <= t && t <= MAX_TICK)) {
                errors.push(format!("Live price interval for {}: 0 (off) or {} to {} seconds", info.title,
                                    py_g(info.min_tick), MAX_TICK as i64));
            }
        }
        errors
    }

    pub fn to_json(&self) -> Value {
        json!({"source": self.source, "symbol": self.symbol, "clock": self.clock, "tick_seconds": self.tick_seconds})
    }

    /// `DataSettings.from_dict`: unknown keys dropped, "" as None, the interval as a float.
    pub fn from_json(d: &Map<String, Value>) -> Result<DataSettings, String> {
        let text = |k: &str| -> Result<Option<String>, String> {
            match d.get(k) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(s)) if s.is_empty() => Ok(None),
                Some(Value::String(s)) => Ok(Some(s.clone())),
                Some(other) => Ok(Some(wednesday_base::py::py_str(other))),
            }
        };
        let tick = match d.get("tick_seconds") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) if s.is_empty() => None,
            Some(Value::Number(n)) => n.as_f64(),
            Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
            Some(Value::String(s)) => Some(s.trim().parse::<f64>().map_err(|_| {
                format!("could not convert string to float: {}", py_repr(s))
            })?),
            Some(Value::Array(_)) => return Err("float() argument must be a string or a real number, not 'list'".into()),
            Some(Value::Object(_)) => return Err("float() argument must be a string or a real number, not 'dict'".into()),
        };
        Ok(DataSettings {
            source: text("source")?.unwrap_or_else(|| DEFAULT_SOURCE.into()),
            symbol: text("symbol")?,
            clock: text("clock")?,
            tick_seconds: tick,
        })
    }
}

/// Python's `f"{x:g}"` for the small numbers here (0.5, 1).
fn py_g(x: f64) -> String {
    if x == x.trunc() { format!("{}", x as i64) } else { format!("{x}") }
}

/// The settings to run with (`settings.resolve` without command-line flags): saved from the
/// dashboard over the environment (`XAU_SOURCE`, `XAU_SYMBOL`, `XAU_CLOCK`, `XAU_TICK`).
pub async fn resolve(store: Option<&Store>, env: &(dyn Fn(&str) -> Option<String> + Sync)) -> DataSettings {
    let source = env("XAU_SOURCE").filter(|s| source_info(s).is_some());
    let mut base = DataSettings {
        source: source.clone().unwrap_or_else(|| DEFAULT_SOURCE.into()),
        symbol: source.as_ref().and(env("XAU_SYMBOL")),
        clock: source.as_ref().and(env("XAU_CLOCK")),
        tick_seconds: env("XAU_TICK").and_then(|t| t.parse().ok()),
    };
    let saved = match store {
        Some(s) => s.get_setting(SETTINGS_KEY).await.ok().flatten(),
        None => None,
    };
    let Some(Value::Object(saved)) = saved else { return base };
    if saved.get("source").and_then(Value::as_str).is_some_and(|s| source_info(s).is_none()) {
        return base; // e.g. Yahoo from an older version: start over on the default source
    }
    let mut merged = base.to_json().as_object().cloned().unwrap_or_default();
    for (k, v) in &saved {
        if !v.is_null() {
            merged.insert(k.clone(), v.clone());
        }
    }
    if let Ok(s) = DataSettings::from_json(&merged) {
        base = s;
    }
    if saved.get("source").is_some_and(|s| !s.is_null()) {
        for (k, slot) in [("symbol", &mut base.symbol), ("clock", &mut base.clock)] {
            if saved.get(k).is_none_or(Value::is_null) {
                *slot = None;
            }
        }
        if saved.get("tick_seconds").is_none_or(Value::is_null) {
            base.tick_seconds = None;
        }
    }
    base
}

// ---- the scan config (cli.py, from the environment) ----

/// The scan config the sidecar runs with: the defaults, `XAU_MAX_SL` and `XAU_DETECTORS`.
pub fn scan_config(env: &dyn Fn(&str) -> Option<String>) -> ScanConfig {
    let mut cfg = ScanConfig::default();
    if let Some(sl) = env("XAU_MAX_SL").and_then(|v| v.parse::<f64>().ok()).filter(|v| *v != 0.0) {
        cfg.params.max_sl = sl;
    }
    if let Some(list) = env("XAU_DETECTORS") {
        let picked: Vec<Detector> = list.split(',').filter_map(|n| Detector::from_name(n.trim())).collect();
        if !picked.is_empty() {
            cfg.detectors = picked;
        }
    }
    cfg
}

/// `ScanConfig.to_dict()`
pub fn config_json(cfg: &ScanConfig) -> Value {
    let p: &DetectorParams = &cfg.params;
    json!({
        "timeframes": cfg.timeframes.iter().map(|t| t.name).collect::<Vec<_>>(),
        "lookback": cfg.lookback,
        "detectors": cfg.detectors.iter().map(|d| json!({"name": d.name(), "title": d.title()})).collect::<Vec<_>>(),
        "recent_bars": cfg.recent_bars,
        "swing_length": p.swing_length,
        "zone": if p.zone_body { "body" } else { "wick" },
        "mitigation": if p.mitigation_close { "close" } else { "wick" },
        "max_sl": p.max_sl,
        "eq_tolerance": p.eq_tolerance,
        "idm_length": p.idm_length,
    })
}

// ---- the trader's bias (bias.py), read from the database ----

pub const BIAS_KEY: &str = "bias";

/// The saved bias as `TradeBias.to_dict()` gives it (with `expired`), and its direction while active.
pub async fn bias(store: Option<&Store>) -> (Option<Value>, Option<String>) {
    let Some(store) = store else { return (None, None) };
    let Ok(Some(Value::Object(d))) = store.get_setting(BIAS_KEY).await else { return (None, None) };
    let Some(direction) = d.get("direction").and_then(Value::as_str).filter(|s| !s.is_empty()).map(String::from) else {
        return (None, None);
    };
    let text = |k: &str, def: &str| match d.get(k) {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => def.to_string(),
    };
    let expires_at = d.get("expires_at").and_then(Value::as_str).filter(|s| !s.is_empty()).map(String::from);
    let expired = expires_at
        .as_deref()
        .and_then(|e| DateTime::parse_from_rfc3339(e).ok())
        .is_some_and(|e| Utc::now() >= e);
    let out = json!({"direction": direction, "note": text("note", ""), "expiry": text("expiry", "day"),
                     "set_at": d.get("set_at").cloned().unwrap_or(Value::Null),
                     "expires_at": expires_at, "expired": expired});
    (Some(out), (!expired).then_some(direction))
}

// ---- times as Python prints them ----

pub use wednesday_base::time::{py_iso_naive, py_iso_utc};

fn unix(t: Time) -> i64 {
    t.and_utc().timestamp()
}

// ---- stored M1 bars (storage.py) ----

fn bar_from(r: &Map<String, Value>) -> Option<Bar> {
    let f = |k: &str| r.get(k).and_then(Value::as_f64).unwrap_or(f64::NAN);
    Some(Bar { time: DateTime::from_timestamp(r.get("time")?.as_i64()?, 0)?.naive_utc(), open: f("open"),
               high: f("high"), low: f("low"), close: f("close"), volume: f("volume") })
}

/// The latest `limit` stored bars of a stream, or those opening before `before`, oldest first.
pub async fn load_bars(store: &Store, source: &str, symbol: &str, limit: usize, before: Option<i64>)
    -> Result<Vec<Bar>, String> {
    load_series(store, source, symbol, "1m", limit, before).await
}

/// The latest `limit` stored bars of one series (`Store.load_bars` with `tf`), oldest first.
pub async fn load_series(store: &Store, source: &str, symbol: &str, tf: &str, limit: usize, before: Option<i64>)
    -> Result<Vec<Bar>, String> {
    let mut args = vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into())), Arg::Str(Some(tf.into()))];
    let cut = match before {
        Some(b) => {
            args.push(Arg::Int(Some(b)));
            " AND time < ?"
        }
        None => "",
    };
    args.push(Arg::Int(Some(limit as i64)));
    let rows = store
        .rows("bars", &format!("SELECT time, open, high, low, close, volume FROM bars WHERE source = ? AND symbol = ? AND tf = ?{cut} ORDER BY time DESC LIMIT ?"), args)
        .await?;
    let mut bars: Vec<Bar> = rows.iter().filter_map(bar_from).collect();
    bars.reverse();
    Ok(bars)
}

pub async fn save_bars(store: &Store, source: &str, symbol: &str, bars: &[Bar]) -> Result<(), String> {
    save_series(store, source, symbol, "1m", bars).await
}

/// Insert or update bars of one series (`Store.save_bars` with `tf`).
pub async fn save_series(store: &Store, source: &str, symbol: &str, tf: &str, bars: &[Bar]) -> Result<(), String> {
    if bars.is_empty() {
        return Ok(());
    }
    let names = ["source", "symbol", "tf", "time", "open", "high", "low", "close", "volume"];
    let rows = bars
        .iter()
        .map(|b| vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into())), Arg::Str(Some(tf.into())),
                      Arg::Int(Some(unix(b.time))),
                      Arg::Float(Some(b.open)), Arg::Float(Some(b.high)), Arg::Float(Some(b.low)),
                      Arg::Float(Some(b.close)), Arg::Float(Some(b.volume))])
        .collect();
    store.transaction(Store::upsert_many("bars", &names, rows, &["source", "symbol", "tf", "time"])).await
}

// ---- the engine ----

/// The live price between scans and the forming M1 candle it builds.
#[derive(Clone, Debug)]
pub struct Live {
    pub price: f64,
    pub time: Time,
    pub bar: Option<Bar>,
}

/// What readers see (`EngineState`).
#[derive(Clone)]
pub struct State {
    pub version: u64,
    pub result: Option<Arc<ScanResult>>,
    pub m1: Arc<Vec<Bar>>,
    pub scanned_at: Option<DateTime<Utc>>,
    pub error: Option<String>,
    pub error_at: Option<DateTime<Utc>>,
    pub conn: &'static str, // connecting, connected, reconnecting, failed
    pub attempt: u32,
    pub live: Option<Live>,
    pub tick: u64,
}

impl Default for State {
    fn default() -> Self {
        State { version: 0, result: None, m1: Arc::new(vec![]), scanned_at: None, error: None, error_at: None,
                conn: "connecting", attempt: 0, live: None, tick: 0 }
    }
}

/// Work for the feed's task, answered on a channel.
pub enum Command {
    DailyHistory { clock: String, count: usize, reply: oneshot::Sender<Result<Option<Vec<Bar>>, String>> },
}

pub struct Engine {
    pub source: String,
    pub symbol: String,
    pub clock: String,
    pub settings: DataSettings,
    pub tick_seconds: Value, // as Python reports it (0 when off)
    pub poll: bool,
    pub persist: bool,
    pub retry_connect: bool,
    pub max_bars: usize,
    state: Arc<Mutex<State>>,
    changed: watch::Sender<u64>, // bumps on every new scan or tick (the event stream waits on it)
    stop: watch::Sender<bool>,
    commands: mpsc::Sender<Command>,
}

impl Engine {
    pub fn state(&self) -> State {
        self.state.lock().unwrap().clone()
    }

    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.changed.subscribe()
    }

    pub async fn call(&self, cmd: Command) -> Result<(), String> {
        self.commands.send(cmd).await.map_err(|_| "The scanner isn't running".to_string())
    }

    pub fn stop(&self) {
        let _ = self.stop.send(true);
    }
}

/// How the engine runs: settings, scan config, whether it polls after the first scan.
#[derive(Clone)]
pub struct Options {
    pub cfg: ScanConfig,
    pub poll: bool,
    pub delay: f64,
}

impl Default for Options {
    fn default() -> Self {
        Options { cfg: ScanConfig::default(), poll: true, delay: DELAY }
    }
}

/// Start an engine for `settings` on the runtime.
pub fn start(settings: &DataSettings, opts: &Options, store: Option<Store>) -> Result<Arc<Engine>, String> {
    let mut feed = Feed::build(&settings.source, &settings.resolved_symbol())?;
    let tick = settings.resolved_tick();
    let tick_f = tick.as_f64().unwrap_or(0.0);
    let ticking = opts.poll && 0.0 < tick_f && tick_f < 60.0;
    let (changed, _) = watch::channel(0u64);
    let (stop, stop_rx) = watch::channel(false);
    let (commands, cmd_rx) = mpsc::channel(16);
    let engine = Arc::new(Engine {
        source: settings.source.clone(),
        symbol: settings.resolved_symbol(),
        clock: settings.resolved_clock(),
        settings: settings.clone(),
        tick_seconds: if !opts.poll { json!(0) } else if 0.0 < tick_f && tick_f < 60.0 { tick } else { json!(0) },
        poll: opts.poll,
        persist: feed.persist(),
        retry_connect: feed.retry_connect(),
        max_bars: opts.cfg.required_m1_bars(),
        state: Arc::new(Mutex::new(State::default())),
        changed,
        stop,
        commands,
    });
    let runner = Runner {
        engine: engine.clone(),
        cfg: Arc::new(opts.cfg.clone()),
        store: if feed.persist() { store } else { None },
        buffer: None,
        delay: opts.delay,
        tick_every: ticking.then(|| StdDuration::from_secs_f64(tick_f)),
        stop: stop_rx,
        commands: cmd_rx,
        scans: None,
    };
    tokio::spawn(async move {
        let mut runner = runner;
        runner.run(&mut feed).await;
        feed.close().await;
    });
    Ok(engine)
}

struct Runner {
    engine: Arc<Engine>,
    cfg: Arc<ScanConfig>,
    store: Option<Store>,
    buffer: Option<Vec<Bar>>,
    delay: f64,
    tick_every: Option<StdDuration>,
    stop: watch::Receiver<bool>,
    commands: mpsc::Receiver<Command>,
    scans: Option<watch::Sender<Option<(Arc<Vec<Bar>>, Option<f64>)>>>,
}

fn record_error(engine: &Engine, e: &str) {
    let mut st = engine.state.lock().unwrap();
    st.error = Some(e.to_string());
    st.error_at = Some(Utc::now());
}

fn set_conn(engine: &Engine, conn: &'static str, attempt: u32) {
    let mut st = engine.state.lock().unwrap();
    st.conn = conn;
    st.attempt = attempt;
}

/// Seconds to the next minute close plus `delay`.
fn to_next_minute(delay: f64) -> f64 {
    let now = Utc::now();
    let into = now.timestamp().rem_euclid(60) as f64 + now.timestamp_subsec_nanos() as f64 / 1e9;
    60.0 - into + delay
}

impl Runner {
    fn stopped(&self) -> bool {
        *self.stop.borrow()
    }

    /// The scan worker: scans fetched bars one at a time, only the newest when it falls behind.
    fn start_scans(&mut self) {
        let (tx, mut rx) = watch::channel::<Option<(Arc<Vec<Bar>>, Option<f64>)>>(None);
        let engine = self.engine.clone();
        let cfg = self.cfg.clone();
        tokio::spawn(async move {
            while rx.changed().await.is_ok() {
                let Some((m1, price)) = rx.borrow_and_update().clone() else { continue };
                let (cfg2, bars) = (cfg.clone(), m1.clone());
                let result = tokio::task::spawn_blocking(move || scan(&bars, &cfg2, price)).await;
                match result {
                    Ok(Ok(result)) => {
                        let mut st = engine.state.lock().unwrap();
                        st.version += 1;
                        st.result = Some(Arc::new(result));
                        let last = m1.last().map(|b| b.time);
                        st.m1 = m1;
                        st.scanned_at = Some(Utc::now());
                        st.error = None;
                        st.error_at = None;
                        if let (Some(live), Some(last)) = (st.live.as_mut(), last) {
                            if live.bar.is_some_and(|b| b.time <= last) {
                                live.bar = None; // that minute closed: the fetched bar replaces the one ticks built
                            }
                        }
                        let v = st.version + st.tick;
                        drop(st);
                        engine.changed.send_replace(v);
                    }
                    Ok(Err(e)) => {
                        record_error(&engine, &format!("ValueError: {e}"));
                        log::error!("scan failed; retrying next minute: {e}");
                    }
                    Err(e) => log::error!("scan panicked: {e}"),
                }
            }
        });
        self.scans = Some(tx);
    }

    /// New M1 bars into the buffer (`M1Buffer.update`), stored when the feed persists.
    async fn update(&mut self, feed: &mut Feed) -> Result<Arc<Vec<Bar>>, FeedError> {
        let (source, symbol, max) = (self.engine.source.clone(), self.engine.symbol.clone(), self.engine.max_bars);
        if self.buffer.is_none() {
            if let Some(store) = &self.store {
                let stored = load_bars(store, feed.name(), &symbol, max, None).await.unwrap_or_default();
                if !stored.is_empty() {
                    self.buffer = Some(stored);
                }
            }
        }
        let last = self.buffer.as_ref().and_then(|b| b.last()).map(|b| b.time);
        let mut fresh = match last {
            None => feed.fetch_m1(max).await?,
            Some(_) => feed.fetch_m1(UPDATE_BARS).await?,
        };
        if let (Some(last), Some(first)) = (last, fresh.first()) {
            if first.time > last + Duration::minutes(1) {
                fresh = feed.fetch_m1(max).await?; // a gap bigger than the update window: all the feed has
            }
        }
        if let (Some(store), false) = (&self.store, fresh.is_empty()) {
            let new: Vec<Bar> = match last {
                None => fresh.clone(),
                Some(l) => fresh.iter().filter(|b| b.time > l - Duration::minutes(5)).copied().collect(),
            };
            if let Err(e) = save_bars(store, &source, &symbol, &new).await {
                log::warn!("storing M1 bars: {e}");
            }
        }
        let mut merged = self.buffer.take().unwrap_or_default();
        merged.extend(fresh);
        merged.sort_by_key(|b| b.time); // stable: of equal times, the newer one comes last
        let mut dedup: Vec<Bar> = Vec::with_capacity(merged.len());
        for b in merged {
            match dedup.last_mut() {
                Some(prev) if prev.time == b.time => *prev = b, // keep the last
                _ => dedup.push(b),
            }
        }
        let start = dedup.len().saturating_sub(max);
        let bars = dedup.split_off(start);
        self.buffer = Some(bars.clone());
        Ok(Arc::new(bars))
    }

    async fn fetch(&mut self, feed: &mut Feed) -> Result<(Arc<Vec<Bar>>, Option<f64>), FeedError> {
        let m1 = self.update(feed).await?;
        let price = feed.last_price().await?;
        Ok((m1, price))
    }

    /// Fold a live price into the forming M1 candle.
    fn fold(&self, t: Tick) {
        let minute = t.time.with_second(0).and_then(|x| x.with_nanosecond(0)).unwrap_or(t.time);
        let mut st = self.engine.state.lock().unwrap();
        let last = st.m1.last().map(|b| b.time);
        let prev = st.live.as_ref().and_then(|l| l.bar);
        let bar = match last {
            Some(last) if last < minute && minute <= last + Duration::minutes(FORMING_MINUTES) => match prev {
                Some(b) if b.time == minute => Some(Bar { high: b.high.max(t.price), low: b.low.min(t.price), close: t.price, ..b }),
                _ => Some(Bar { time: minute, open: t.price, high: t.price, low: t.price, close: t.price, volume: 0.0 }),
            },
            _ => None,
        };
        st.live = Some(Live { price: t.price, time: t.time, bar });
        st.tick += 1;
        let v = st.version + st.tick;
        drop(st);
        self.engine.changed.send_replace(v);
    }

    async fn tick(&self, feed: &mut Feed) {
        match feed.new_ticks().await {
            Ok(ticks) => ticks.into_iter().for_each(|t| self.fold(t)),
            Err(e) => log::debug!("tick failed: {e}"),
        }
    }

    async fn serve(&mut self, cmd: Command, feed: &mut Feed) {
        match cmd {
            Command::DailyHistory { clock, count, reply } => {
                let _ = reply.send(feed.daily_history(&clock, count).await.map_err(|e| e.to_string()));
            }
        }
    }

    /// Wait `seconds`, running queued feed work, and polling the live price when it ticks.
    async fn wait(&mut self, seconds: f64, feed: &mut Feed) {
        let deadline = tokio::time::Instant::now() + StdDuration::from_secs_f64(seconds.max(0.0));
        let mut next_tick = self.tick_every.map(|d| tokio::time::Instant::now() + d);
        loop {
            if self.stopped() {
                return;
            }
            let now = tokio::time::Instant::now();
            if now >= deadline {
                return;
            }
            let until = next_tick.map_or(deadline, |t| t.min(deadline));
            tokio::select! {
                _ = tokio::time::sleep_until(until) => {}
                Some(cmd) = self.commands.recv() => { self.serve(cmd, feed).await; continue; }
                _ = self.stop.changed() => return,
            }
            if let (Some(t), Some(every)) = (next_tick, self.tick_every) {
                if tokio::time::Instant::now() >= t {
                    // Not right before the scan, which prices it anyway.
                    if deadline.saturating_duration_since(tokio::time::Instant::now()) > StdDuration::from_millis(200) {
                        self.tick(feed).await;
                    }
                    next_tick = Some(tokio::time::Instant::now() + every);
                }
            }
        }
    }

    async fn run(&mut self, feed: &mut Feed) {
        self.start_scans();
        let mut connected = false;
        let mut failures = 0u32;
        while !self.stopped() {
            let step: Result<(), FeedError> = async {
                if !connected {
                    feed.connect().await?;
                    connected = true;
                }
                let fetched = self.fetch(feed).await?;
                if let Some(tx) = &self.scans {
                    tx.send_replace(Some(fetched));
                }
                Ok(())
            }
            .await;
            match step {
                Ok(()) => {
                    failures = 0;
                    set_conn(&self.engine, "connected", 0);
                    if !self.engine.poll {
                        log::info!("{}: polling off, keeping this scan", feed.name());
                        while !self.stopped() {
                            self.wait(3600.0, feed).await; // still runs queued feed work
                        }
                        return;
                    }
                }
                Err(e) => {
                    failures += 1;
                    record_error(&self.engine, &e.to_string());
                    if !feed.retry_connect() && (!connected || failures >= RECONNECT_ATTEMPTS) {
                        set_conn(&self.engine, "failed", failures);
                        log::error!("{}: giving up ({e}); restart the feed from Settings", feed.name());
                        return;
                    }
                    set_conn(&self.engine, if connected { "reconnecting" } else { "connecting" }, failures);
                    log::warn!("scan failed; retrying next minute: {e}");
                }
            }
            let wait = to_next_minute(self.delay);
            self.wait(wait, feed).await;
        }
    }
}

// ---- the runtime: the running engine, swapped when the settings change ----

pub struct Runtime {
    pub store: Option<Store>,
    pub opts: Options,
    current: Mutex<(DataSettings, Arc<Engine>)>,
    /// What the server runs beside the engine (its alerts, drawing alerts and calendar), kept
    /// here so it travels with the runtime; the engine doesn't look inside. See [`Runtime::extras`].
    pub extras: std::sync::OnceLock<Arc<dyn std::any::Any + Send + Sync>>,
}

impl Runtime {
    /// The server's things kept with this runtime, made by `init` the first time.
    pub fn extras<T: Send + Sync + 'static>(&self, init: impl FnOnce() -> T) -> &T {
        self.extras
            .get_or_init(|| Arc::new(init()))
            .downcast_ref::<T>()
            .expect("one type of extras per runtime")
    }

    pub fn start(settings: DataSettings, opts: Options, store: Option<Store>) -> Result<Arc<Runtime>, String> {
        let engine = start(&settings, &opts, store.clone())?;
        Ok(Arc::new(Runtime { store, opts, current: Mutex::new((settings, engine)), extras: Default::default() }))
    }

    pub fn engine(&self) -> Arc<Engine> {
        self.current.lock().unwrap().1.clone()
    }

    pub fn settings(&self) -> DataSettings {
        self.current.lock().unwrap().0.clone()
    }

    /// Run `settings` from now on: stop the engine, start a new one.
    pub fn apply(&self, settings: DataSettings) -> Result<(), String> {
        let mut cur = self.current.lock().unwrap();
        cur.1.stop();
        let engine = start(&settings, &self.opts, self.store.clone())?;
        *cur = (settings, engine);
        Ok(())
    }

    /// Restart the feed with the same settings.
    pub fn reconnect(&self) -> Result<(), String> {
        let settings = self.settings();
        self.apply(settings)
    }
}

/// Seconds the feed clock is ahead of UTC right now (`clock_offset`).
pub fn clock_offset(clock: &str) -> i64 {
    let Ok(c) = Clock::parse(clock) else { return 0 };
    let now = Utc::now();
    let feed: NaiveDateTime = c.utc_to_feed(now);
    let secs = (feed - now.naive_utc()).num_milliseconds() as f64 / 1000.0;
    (secs / 60.0).round() as i64 * 60
}
