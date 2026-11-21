//! 1-minute data feeds (`feeds.py`): TradingView over its websocket ([`crate::tradingview`]), and
//! synthetic data for development and tests. Every feed returns *closed* M1 bars, oldest first.
//!
//! TradingView answers three questions (`info`, `candles`, `quote`). Tests can put a stand-in
//! process in its place ([`ProcessHelper`]): it answers the same questions in JSON lines, one
//! request per line on stdin, one reply per line on stdout, logs on stderr, and exits when its
//! stdin closes.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration as StdDuration;

use chrono::{DurationRound, NaiveTime, TimeDelta, Utc};
use serde_json::{json, Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use wednesday_scan::clock::NEW_YORK;
use wednesday_scan::{Bar, Time};

/// A feed failure as Python's engine records it: `"{kind}: {message}"`, e.g.
/// `RuntimeError: TradingView didn't answer within 30 s. Check the internet connection.`
#[derive(Clone, Debug)]
pub struct FeedError {
    pub kind: &'static str, // "RuntimeError", "TradingViewError", ...
    pub code: Option<String>,
    pub message: String,
}

impl FeedError {
    /// Python's `RuntimeError(message)`.
    pub fn runtime(message: impl Into<String>) -> FeedError {
        FeedError { kind: "RuntimeError", code: None, message: message.into() }
    }

    /// Python's `TradingViewError(code, message)`: an error the helper reported.
    pub fn tradingview(code: &str, message: &str) -> FeedError {
        FeedError { kind: "TradingViewError", code: Some(code.to_string()), message: format!("TradingView: {message}") }
    }
}

impl std::fmt::Display for FeedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}

impl std::error::Error for FeedError {}

/// A live price and when it traded, on the feed's clock.
#[derive(Clone, Copy, Debug)]
pub struct Tick {
    pub time: Time,
    pub price: f64,
}

pub enum Feed {
    TradingView(Box<TradingViewFeed>),
    Synthetic(SyntheticFeed),
}

impl Feed {
    /// The feed for a data source (`build_feed`); Err for an unknown source.
    pub fn build(source: &str, symbol: &str) -> Result<Feed, String> {
        match source {
            "tradingview" => Ok(Feed::TradingView(Box::new(TradingViewFeed::new(symbol)))),
            "synthetic" => Ok(Feed::Synthetic(SyntheticFeed::default())),
            _ => Err(format!("Unknown source '{source}'")),
        }
    }

    /// "tradingview" or "synthetic".
    pub fn name(&self) -> &'static str {
        match self {
            Feed::TradingView(_) => "tradingview",
            Feed::Synthetic(_) => "synthetic",
        }
    }

    /// Whether fetched bars are stored in the database (synthetic: no).
    pub fn persist(&self) -> bool {
        // Synthetic: every run is a different random walk; storing it would splice unrelated series.
        matches!(self, Feed::TradingView(_))
    }

    /// Keep retrying a failed connection every minute (false: give up after a few tries).
    pub fn retry_connect(&self) -> bool {
        true // DataFeed's default; neither feed overrides it
    }

    pub async fn connect(&mut self) -> Result<(), FeedError> {
        match self {
            Feed::TradingView(f) => f.connect().await,
            Feed::Synthetic(_) => Ok(()),
        }
    }

    pub async fn close(&mut self) {
        if let Feed::TradingView(f) = self {
            f.close().await;
        }
    }

    /// The latest `count` closed M1 bars, oldest first, sorted and unique (`normalize_ohlcv`).
    pub async fn fetch_m1(&mut self, count: usize) -> Result<Vec<Bar>, FeedError> {
        match self {
            Feed::TradingView(f) => f.fetch_m1(count).await,
            Feed::Synthetic(f) => Ok(f.fetch_m1(count)),
        }
    }

    /// Every live price since the last call, oldest first (`new_ticks`), or the latest one
    /// (`last_tick`); empty when the feed has none.
    pub async fn new_ticks(&mut self) -> Result<Vec<Tick>, FeedError> {
        Ok(match self {
            Feed::TradingView(f) => f.last_tick().await?.into_iter().collect(),
            Feed::Synthetic(f) => vec![f.last_tick()],
        })
    }

    /// The live price, if the feed has one (the scanner prices the scan with it).
    pub async fn last_price(&mut self) -> Result<Option<f64>, FeedError> {
        match self {
            Feed::TradingView(f) => f.last_price().await,
            Feed::Synthetic(_) => Ok(None), // DataFeed's default: the scanner uses the last close
        }
    }

    /// Up to `count` closed daily bars indexed by New York trading day (`daily_history`), or None.
    pub async fn daily_history(&mut self, clock: &str, count: usize) -> Result<Option<Vec<Bar>>, FeedError> {
        match self {
            Feed::TradingView(f) => f.daily_history(clock, count).await,
            Feed::Synthetic(_) => Ok(None),
        }
    }

    /// Connection details (`describe`, for `--check`).
    pub fn describe(&self) -> Value {
        match self {
            Feed::TradingView(f) => f.describe(),
            Feed::Synthetic(_) => json!({"feed": "synthetic"}),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// The helper process

/// A running helper: its stdin, and the replies its stdout reader hands over (None: it exited).
struct Running {
    child: Child,
    stdin: ChildStdin,
    replies: mpsc::UnboundedReceiver<Option<Value>>,
    stderr_task: Option<JoinHandle<()>>,
}

/// A stand-in for TradingView in tests: a long-running process answering the same JSON-line
/// requests (`_ProcessHelper`), e.g. `node tests/golden/markets/fake_feed.mjs`.
///
/// Started on the first call and again after it dies. Replies carry the request id, so a late
/// reply to a request that already timed out is skipped instead of answering the next one.
pub struct ProcessHelper {
    node: PathBuf,
    script: PathBuf,
    proc: Option<Running>,
    id: u64,
    stderr: Arc<Mutex<Vec<String>>>, // last lines, for the error when the helper stops
}

impl ProcessHelper {
    /// `program script`, e.g. node and a fake helper script.
    pub fn new(node: PathBuf, script: PathBuf) -> ProcessHelper {
        ProcessHelper { node, script, proc: None, id: 0, stderr: Arc::new(Mutex::new(Vec::new())) }
    }

    fn start(&mut self) -> Result<(), FeedError> {
        let mut cmd = Command::new(&self.node);
        cmd.arg(&self.script).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        #[cfg(windows)]
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window from the desktop app
        let mut child = cmd.spawn().map_err(|e| {
            FeedError::runtime(format!("The TradingView helper didn't start ({e})."))
        })?;
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");

        let (tx, replies) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match serde_json::from_str::<Value>(&line) {
                    Ok(msg) => {
                        let _ = tx.send(Some(msg));
                    }
                    Err(_) => log::warn!("tradingview helper: {}", line.trim_end()),
                }
            }
            let _ = tx.send(None); // the helper exited
        });
        let kept = Arc::clone(&self.stderr);
        let stderr_task = tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let line = line.trim_end();
                if line.is_empty() {
                    continue;
                }
                if let Ok(mut kept) = kept.lock() {
                    kept.push(line.to_string());
                    let extra = kept.len().saturating_sub(5);
                    kept.drain(..extra);
                }
                log::info!("tradingview helper: {line}");
            }
        });
        self.proc = Some(Running { child, stdin, replies, stderr_task: Some(stderr_task) });
        Ok(())
    }

    /// Send `{"id", "op", ...args}` and wait up to `timeout` seconds for its reply's `result`.
    pub async fn call(&mut self, op: &str, timeout: f64, args: Map<String, Value>) -> Result<Value, FeedError> {
        let alive = match self.proc.as_mut() {
            Some(p) => matches!(p.child.try_wait(), Ok(None)),
            None => false,
        };
        if !alive {
            self.start()?;
        }
        self.id += 1;
        let rid = self.id;
        let mut req = Map::new();
        req.insert("id".into(), rid.into());
        req.insert("op".into(), op.into());
        req.extend(args);
        let mut line = Value::Object(req).to_string();
        line.push('\n');

        let proc = self.proc.as_mut().expect("started");
        let sent = match proc.stdin.write_all(line.as_bytes()).await {
            Ok(()) => proc.stdin.flush().await,
            Err(e) => Err(e),
        };
        if sent.is_err() {
            return Err(self.stopped().await);
        }

        let deadline = tokio::time::Instant::now() + StdDuration::from_secs_f64(timeout.max(0.0));
        loop {
            let proc = self.proc.as_mut().expect("started");
            let msg = match tokio::time::timeout_at(deadline, proc.replies.recv()).await {
                Err(_) => {
                    return Err(FeedError::runtime(format!(
                        "TradingView didn't answer within {timeout:.0} s. Check the internet connection."
                    )))
                }
                Ok(Some(Some(msg))) => msg,
                Ok(Some(None)) | Ok(None) => return Err(self.stopped().await),
            };
            if msg.get("id").and_then(Value::as_u64) != Some(rid) {
                continue; // a late reply to an earlier request
            }
            if let Some(err) = msg.get("error") {
                let field = |k: &str, default: &str| match err.get(k) {
                    Some(Value::String(s)) if !s.is_empty() => s.clone(),
                    Some(v) if truthy(v) => v.to_string(),
                    _ => default.to_string(),
                };
                return Err(FeedError::tradingview(&field("code", "ERROR"), &field("message", "unknown error")));
            }
            return Ok(msg.get("result").cloned().unwrap_or(Value::Null));
        }
    }

    /// The helper exited: forget it (the next call starts a new one) and say why, from the last
    /// line it wrote on stderr.
    async fn stopped(&mut self) -> FeedError {
        if let Some(mut proc) = self.proc.take() {
            // Let the stderr reader catch the helper's last words before reading them.
            if let Some(task) = proc.stderr_task.take() {
                let _ = tokio::time::timeout(StdDuration::from_secs(1), task).await;
            }
            let _ = proc.child.start_kill();
            let _ = tokio::time::timeout(StdDuration::from_secs(1), proc.child.wait()).await;
        }
        FeedError::runtime(format!("The TradingView helper stopped: {}", self.why()))
    }

    fn why(&self) -> String {
        let kept = self.stderr.lock().map(|k| k.last().cloned()).unwrap_or(None);
        kept.unwrap_or_else(|| "no output".to_string())
    }

    /// Close stdin (the helper exits when it closes), wait up to 3 s, then kill it.
    pub async fn close(&mut self) {
        let Some(proc) = self.proc.take() else { return };
        let Running { mut child, stdin, .. } = proc;
        drop(stdin);
        if tokio::time::timeout(StdDuration::from_secs(3), child.wait()).await.is_err() {
            let _ = child.kill().await;
        }
    }
}

/// Where a feed's answers come from: TradingView itself, or a stand-in process (tests).
pub enum Helper {
    Native(crate::tradingview::Connection),
    Process(ProcessHelper),
}

impl Helper {
    /// TradingView, or the stand-in `command` (program, script) when given.
    pub fn new(command: Option<(PathBuf, PathBuf)>) -> Helper {
        match command {
            Some((program, script)) => Helper::Process(ProcessHelper::new(program, script)),
            None => Helper::Native(crate::tradingview::Connection::new()),
        }
    }

    /// Answer `op` (`info`, `candles`, `quote`) within `timeout` seconds.
    pub async fn call(&mut self, op: &str, timeout: f64, args: Map<String, Value>) -> Result<Value, FeedError> {
        match self {
            Helper::Native(c) => c.call(op, timeout, args).await,
            Helper::Process(p) => p.call(op, timeout, args).await,
        }
    }

    pub async fn close(&mut self) {
        match self {
            Helper::Native(c) => c.close().await,
            Helper::Process(p) => p.close().await,
        }
    }
}

/// Python's truthiness for a JSON value (`x or default`).
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

// ---------------------------------------------------------------------------------------------
// TradingView

/// `normalize_ohlcv`: sorted by time, a repeated time keeps its last bar.
pub fn normalize(mut bars: Vec<Bar>) -> Vec<Bar> {
    bars.sort_by_key(|b| b.time); // stable: repeats stay in arrival order
    let mut out: Vec<Bar> = Vec::with_capacity(bars.len());
    for bar in bars {
        match out.last_mut() {
            Some(last) if last.time == bar.time => *last = bar,
            _ => out.push(bar),
        }
    }
    out
}

/// `df.tail(count)`.
fn tail(mut bars: Vec<Bar>, count: usize) -> Vec<Bar> {
    let extra = bars.len().saturating_sub(count);
    bars.drain(..extra);
    bars
}

/// A unix second (may be fractional) as a naive UTC time.
fn from_unix(seconds: f64) -> Option<Time> {
    let whole = seconds.floor();
    let nanos = ((seconds - whole) * 1e9).round().min(999_999_999.0) as u32;
    chrono::DateTime::from_timestamp(whole as i64, nanos).map(|d| d.naive_utc())
}

/// The helper's candle rows `[time, open, high, low, close, volume]` as bars (UTC times).
pub fn parse_rows(rows: &Value) -> Vec<Bar> {
    let num = |v: Option<&Value>| v.and_then(Value::as_f64).unwrap_or(f64::NAN);
    rows.as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|r| {
                    let time = from_unix(r.get(0)?.as_f64()?)?;
                    Some(Bar {
                        time,
                        open: num(r.get(1)),
                        high: num(r.get(2)),
                        low: num(r.get(3)),
                        close: num(r.get(4)),
                        volume: r.get(5).and_then(Value::as_f64).unwrap_or(0.0),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The New York trading day a gold daily bar opening at `open` (UTC) belongs to: it opens at
/// 18:00 New York and closes at 17:00 the next day, so +7 hours puts its open on the day it ends.
pub fn ny_trading_day(open: Time) -> Time {
    let ny = open.and_utc().with_timezone(&NEW_YORK).naive_local() + TimeDelta::hours(7);
    ny.date().and_time(NaiveTime::MIN)
}

/// Daily bars indexed by New York trading day, without the day still forming at `now` (UTC),
/// the last `count` of them.
pub fn index_daily(bars: Vec<Bar>, now: Time, count: usize) -> Vec<Bar> {
    let today = ny_trading_day(now);
    let days = bars
        .into_iter()
        .map(|b| Bar { time: ny_trading_day(b.time), ..b })
        .filter(|b| b.time < today) // today's may still be forming
        .collect();
    tail(normalize(days), count)
}

/// The closed minutes of `bars` at `now` (UTC): drops the one still forming.
pub fn closed_minutes(bars: Vec<Bar>, now: Time) -> Vec<Bar> {
    let minute = now.duration_trunc(TimeDelta::minutes(1)).unwrap_or(now);
    bars.into_iter().filter(|b| b.time < minute).collect()
}

fn now_utc() -> Time {
    Utc::now().naive_utc()
}

/// Python's `repr(float)` for the usual prices: `2650.0`, `2650.25`.
fn py_float(x: f64) -> String {
    let s = format!("{x}");
    if x.is_finite() && !s.contains(['.', 'e']) {
        format!("{s}.0")
    } else {
        s
    }
}

/// `str(pd.Timestamp)`: `2026-11-20 10:00:00`, with microseconds when it has them.
fn py_timestamp(t: Time) -> String {
    if t.and_utc().timestamp_subsec_nanos() == 0 {
        t.format("%Y-%m-%d %H:%M:%S").to_string()
    } else {
        t.format("%Y-%m-%d %H:%M:%S%.6f").to_string()
    }
}

/// TradingView market data over its websocket (unofficial protocol, see [`crate::tradingview`]).
///
/// `FX:XAUUSD` is FXCM's spot gold; any TradingView symbol with its exchange prefix works
/// (`OANDA:XAUUSD`, ...). Bar times are UTC.
pub struct TradingViewFeed {
    pub symbol: String,
    command: Option<(PathBuf, PathBuf)>,
    helper: Option<Helper>,
    info: Option<Value>,
    last: Option<Tick>, // the latest quote, for `describe`
}

impl TradingViewFeed {
    pub fn new(symbol: &str) -> TradingViewFeed {
        TradingViewFeed { symbol: symbol.to_string(), command: None, helper: None, info: None, last: None }
    }

    /// A feed on a stand-in process (program, script) instead of TradingView (tests).
    pub fn with_command(symbol: &str, node: PathBuf, script: PathBuf) -> TradingViewFeed {
        TradingViewFeed { command: Some((node, script)), ..TradingViewFeed::new(symbol) }
    }

    pub async fn connect(&mut self) -> Result<(), FeedError> {
        self.close().await;
        let helper = self.helper.insert(Helper::new(self.command.clone()));
        let mut args = Map::new();
        args.insert("symbol".into(), self.symbol.clone().into());
        match helper.call("info", 30.0, args).await {
            Ok(info) => {
                self.info = Some(info);
                Ok(())
            }
            Err(e) if e.code.as_deref() == Some("SYMBOL_ERROR") => Err(FeedError::runtime(format!(
                "TradingView has no symbol '{}'. Use the exchange prefix, e.g. FX:XAUUSD (FXCM) or OANDA:XAUUSD.",
                self.symbol
            ))),
            Err(e) => Err(e),
        }
    }

    pub async fn close(&mut self) {
        if let Some(mut helper) = self.helper.take() {
            helper.close().await;
        }
    }

    async fn call(&mut self, op: &str, timeout: f64, extra: Map<String, Value>) -> Result<Value, FeedError> {
        if self.helper.is_none() {
            self.connect().await?;
        }
        let mut args = Map::new();
        args.insert("symbol".into(), self.symbol.clone().into());
        args.extend(extra);
        self.helper.as_mut().expect("connected").call(op, timeout, args).await
    }

    async fn candles(&mut self, timeframe: &str, count: usize) -> Result<Vec<Bar>, FeedError> {
        // Deep loads come in batches of a few thousand bars; give them time.
        let timeout = 30.0 + count as f64 / 200.0;
        let mut args = Map::new();
        args.insert("timeframe".into(), timeframe.into());
        args.insert("count".into(), count.into());
        args.insert("timeoutMs".into(), ((timeout * 1000.0) as i64 - 5000).into());
        let rows = self.call("candles", timeout, args).await?;
        Ok(parse_rows(&rows))
    }

    pub async fn fetch_m1(&mut self, count: usize) -> Result<Vec<Bar>, FeedError> {
        let bars = self.candles("1", count + 1).await?; // +1: the forming minute is dropped below
        let bars = closed_minutes(bars, now_utc());
        if bars.is_empty() {
            return Err(FeedError::runtime(format!("TradingView returned no 1m data for {}", self.symbol)));
        }
        Ok(tail(normalize(bars), count))
    }

    /// Daily bars indexed by New York trading day, whatever the feed's clock.
    pub async fn daily_history(&mut self, _clock: &str, count: usize) -> Result<Option<Vec<Bar>>, FeedError> {
        let bars = self.candles("D", count + 1).await?;
        if bars.is_empty() {
            return Ok(None);
        }
        Ok(Some(index_daily(bars, now_utc(), count)))
    }

    pub async fn last_tick(&mut self) -> Result<Option<Tick>, FeedError> {
        let q = self.call("quote", 15.0, Map::new()).await?;
        let Some(price) = q.get("price").filter(|p| !p.is_null()) else { return Ok(None) };
        let price = price
            .as_f64()
            .or_else(|| price.as_str().and_then(|s| s.trim().parse().ok()))
            .ok_or_else(|| FeedError { kind: "ValueError", code: None, message: format!("could not convert to float: {price}") })?;
        let time = match q.get("time").filter(|t| truthy(t)).and_then(Value::as_f64) {
            Some(t) => from_unix(t.trunc()).unwrap_or_else(now_utc),
            None => now_utc(),
        };
        let tick = Tick { time, price };
        self.last = Some(tick);
        Ok(Some(tick))
    }

    pub async fn last_price(&mut self) -> Result<Option<f64>, FeedError> {
        Ok(self.last_tick().await?.map(|t| t.price))
    }

    /// Connection details. Python asks for a fresh quote here; this one is synchronous, so it
    /// shows the latest quote seen.
    pub fn describe(&self) -> Value {
        let info = self.info.as_ref();
        let get = |k: &str| info.and_then(|i| i.get(k)).cloned().unwrap_or(Value::Null);
        let last = match self.last {
            Some(t) => Value::String(format!("{} at {} UTC", py_float(t.price), py_timestamp(t.time))),
            None => Value::Null,
        };
        json!({"feed": "tradingview", "symbol": self.symbol, "name": get("description"),
               "exchange": get("exchange"), "last": last})
    }
}

// ---------------------------------------------------------------------------------------------
// Synthetic

/// A small seeded RNG (splitmix64) with normal draws by Box–Muller: repeatable runs without a
/// crate. Its numbers differ from numpy's.
#[derive(Clone, Debug)]
pub struct Rng {
    state: u64,
    spare: Option<f64>,
}

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng { state: seed, spare: None }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in (0, 1].
    fn unit(&mut self) -> f64 {
        ((self.next_u64() >> 11) + 1) as f64 / (1u64 << 53) as f64
    }

    pub fn normal(&mut self, mean: f64, sd: f64) -> f64 {
        let z = match self.spare.take() {
            Some(z) => z,
            None => {
                let (u1, u2) = (self.unit(), self.unit());
                let r = (-2.0 * u1.ln()).sqrt();
                let a = std::f64::consts::TAU * u2;
                self.spare = Some(r * a.sin());
                r * a.cos()
            }
        };
        mean + sd * z
    }

    /// An integer in `[low, high)`.
    pub fn integer(&mut self, low: i64, high: i64) -> i64 {
        low + (self.next_u64() % (high - low) as u64) as i64
    }
}

/// The minute the ticks are building.
#[derive(Clone, Copy, Debug)]
struct Forming {
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
}

/// Random-walk gold-like prices for development and tests (`--source synthetic`), not offered in
/// the dashboard. Each fetch advances one minute.
pub struct SyntheticFeed {
    rng: Rng,
    bars: VecDeque<Bar>,
    forming: Option<Forming>,
}

impl Default for SyntheticFeed {
    fn default() -> SyntheticFeed {
        SyntheticFeed::new(2650.0, 42, 60_000, None)
    }
}

impl SyntheticFeed {
    /// `history` bars ending at the last closed minute before `end` (default: now, UTC).
    pub fn new(start_price: f64, seed: u64, history: usize, end: Option<Time>) -> SyntheticFeed {
        let end = end.unwrap_or_else(now_utc);
        let last = end.duration_trunc(TimeDelta::minutes(1)).unwrap_or(end) - TimeDelta::minutes(1);
        let first = last - TimeDelta::minutes(history.saturating_sub(1) as i64);
        let mut feed = SyntheticFeed { rng: Rng::new(seed), bars: VecDeque::new(), forming: None };
        feed.bars = feed.generate(first, history, start_price).into();
        feed
    }

    fn generate(&mut self, first: Time, n: usize, start: f64) -> Vec<Bar> {
        let rng = &mut self.rng;
        // Regime-switching drift gives trends and pullbacks, which produce structure breaks.
        let regimes: Vec<f64> = (0..n / 240 + 1).map(|_| rng.normal(0.0, 0.05)).collect();
        let steps: Vec<f64> = (0..n).map(|i| regimes[i / 240] + rng.normal(0.0, 0.6)).collect();
        let wick_up: Vec<f64> = (0..n).map(|_| rng.normal(0.0, 0.35).abs()).collect();
        let wick_down: Vec<f64> = (0..n).map(|_| rng.normal(0.0, 0.35).abs()).collect();
        let mut open = start;
        let mut close = start;
        (0..n)
            .map(|i| {
                close += steps[i];
                let bar = Bar {
                    time: first + TimeDelta::minutes(i as i64),
                    open,
                    high: open.max(close) + wick_up[i],
                    low: open.min(close) - wick_down[i],
                    close,
                    volume: rng.integer(50, 500) as f64,
                };
                open = close;
                bar
            })
            .collect()
    }

    fn last_bar(&self) -> Bar {
        *self.bars.back().expect("synthetic history is never empty")
    }

    /// A small random step within the next minute; its ticks become that minute's bar.
    pub fn last_tick(&mut self) -> Tick {
        let last = self.last_bar();
        let time = last.time + TimeDelta::minutes(1);
        let base = self.forming.map_or(last.close, |f| f.close);
        let price = base + self.rng.normal(0.0, 0.15);
        let f = self.forming.get_or_insert(Forming {
            open: last.close,
            high: last.close,
            low: last.close,
            close: last.close,
            volume: 0.0,
        });
        f.high = f.high.max(price);
        f.low = f.low.min(price);
        f.close = price;
        f.volume += 1.0;
        Tick { time, price }
    }

    /// Each fetch closes one minute: the one the ticks built, or a generated one without ticks.
    pub fn fetch_m1(&mut self, count: usize) -> Vec<Bar> {
        let last = self.last_bar();
        let time = last.time + TimeDelta::minutes(1);
        let next = match self.forming.take() {
            Some(f) => Bar { time, open: f.open, high: f.high, low: f.low, close: f.close, volume: f.volume },
            None => self.generate(time, 1, last.close)[0],
        };
        self.bars.push_back(next);
        self.bars.pop_front();
        let skip = self.bars.len().saturating_sub(count);
        self.bars.iter().skip(skip).copied().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn t(s: &str) -> Time {
        Time::parse_from_str(s, "%Y-%m-%d %H:%M").unwrap()
    }

    fn bar(time: Time, close: f64) -> Bar {
        Bar { time, open: close, high: close, low: close, close, volume: 0.0 }
    }

    #[test]
    fn normalize_sorts_and_keeps_the_last_repeat() {
        let bars = vec![
            bar(t("2026-11-20 10:02"), 3.0),
            bar(t("2026-11-20 10:00"), 1.0),
            bar(t("2026-11-20 10:02"), 4.0),
            bar(t("2026-11-20 10:01"), 2.0),
        ];
        let out = normalize(bars);
        let closes: Vec<f64> = out.iter().map(|b| b.close).collect();
        assert_eq!(closes, vec![1.0, 2.0, 4.0]);
        assert_eq!(tail(out, 2).len(), 2);
    }

    #[test]
    fn rows_parse_with_volume_defaulting_to_zero() {
        let rows = json!([[1_763_632_800, 1.0, 2.0, 0.5, 1.5], [1_763_632_860, 1.5, 2.5, 1.0, 2.0, 7]]);
        let bars = parse_rows(&rows);
        assert_eq!(bars.len(), 2);
        assert_eq!(bars[0].time, t("2025-11-20 10:00"));
        assert_eq!(bars[0].volume, 0.0);
        assert_eq!(bars[1].volume, 7.0);
    }

    #[test]
    fn forming_minute_is_dropped() {
        let now = Time::parse_from_str("2026-11-20 10:02:30", "%Y-%m-%d %H:%M:%S").unwrap();
        let bars = vec![bar(t("2026-11-20 10:01"), 1.0), bar(t("2026-11-20 10:02"), 2.0)];
        let closed = closed_minutes(bars, now);
        assert_eq!(closed.len(), 1);
        assert_eq!(closed[0].time, t("2026-11-20 10:01"));
    }

    #[test]
    fn daily_bars_are_indexed_by_new_york_trading_day() {
        // Winter (EST): the session opens 18:00 New York = 23:00 UTC, the day before it ends.
        assert_eq!(ny_trading_day(t("2026-11-19 23:00")), t("2026-11-20 00:00"));
        // Summer (EDT): 18:00 New York = 22:00 UTC.
        assert_eq!(ny_trading_day(t("2026-07-14 22:00")), t("2026-07-15 00:00"));
        // A bar stamped at UTC midnight (some feeds): 19:00/20:00 New York, still the next day.
        assert_eq!(ny_trading_day(t("2026-11-20 00:00")), t("2026-11-20 00:00"));

        let bars = vec![
            bar(t("2026-11-17 23:00"), 1.0), // Wed 18th
            bar(t("2026-11-18 23:00"), 2.0), // Thu 19th
            bar(t("2026-11-19 23:00"), 3.0), // Fri 20th: forming at the time below
        ];
        let now = t("2026-11-20 15:00"); // 10:00 New York, Friday
        let out = index_daily(bars.clone(), now, 5);
        assert_eq!(out.iter().map(|b| b.time).collect::<Vec<_>>(), vec![t("2026-11-18 00:00"), t("2026-11-19 00:00")]);
        assert_eq!(index_daily(bars, now, 1).len(), 1);
        // After the 17:00 New York close the next trading day has begun: the 20th is closed.
        let after = t("2026-11-20 22:30");
        assert_eq!(ny_trading_day(after), NaiveDate::from_ymd_opt(2026, 11, 21).unwrap().and_time(NaiveTime::MIN));
    }

    #[test]
    fn messages_match_python() {
        assert_eq!(format!("{:.0}", 30.0 + 7001.0 / 200.0), "65");
        assert_eq!(
            FeedError::runtime("x").to_string(),
            "RuntimeError: x"
        );
        assert_eq!(
            FeedError::tradingview("SYMBOL_ERROR", "bad").to_string(),
            "TradingViewError: TradingView: bad"
        );
        assert_eq!(py_float(2650.0), "2650.0");
        assert_eq!(py_float(2650.25), "2650.25");
        assert_eq!(py_timestamp(t("2026-11-20 10:00")), "2026-11-20 10:00:00");
        assert_eq!(Feed::build("csv", "X").err().unwrap(), "Unknown source 'csv'");
    }

    #[test]
    fn build_names_and_flags() {
        let tv = Feed::build("tradingview", "FX:XAUUSD").unwrap();
        assert_eq!((tv.name(), tv.persist(), tv.retry_connect()), ("tradingview", true, true));
        let syn = Feed::Synthetic(SyntheticFeed::new(2650.0, 1, 500, None));
        assert_eq!((syn.name(), syn.persist(), syn.retry_connect()), ("synthetic", false, true));
        assert_eq!(syn.describe(), json!({"feed": "synthetic"}));
        assert_eq!(tv.describe()["last"], Value::Null);
    }

    #[test]
    fn synthetic_history_ends_at_the_last_closed_minute() {
        let end = Time::parse_from_str("2026-11-20 10:02:30", "%Y-%m-%d %H:%M:%S").unwrap();
        let mut feed = SyntheticFeed::new(2650.0, 42, 1000, Some(end));
        assert_eq!(feed.bars.len(), 1000);
        assert_eq!(feed.last_bar().time, t("2026-11-20 10:01"));
        assert_eq!(feed.bars[0].time, t("2026-11-20 10:01") - TimeDelta::minutes(999));
        for w in feed.bars.iter().collect::<Vec<_>>().windows(2) {
            assert_eq!(w[1].time - w[0].time, TimeDelta::minutes(1));
            assert_eq!(w[1].open, w[0].close);
        }
        for b in &feed.bars {
            assert!(b.high >= b.open.max(b.close) && b.low <= b.open.min(b.close));
            assert!((50.0..500.0).contains(&b.volume));
        }
        // Each fetch closes one more minute, keeping the history's length.
        let bars = feed.fetch_m1(10);
        assert_eq!(bars.len(), 10);
        assert_eq!(bars[9].time, t("2026-11-20 10:02"));
        assert_eq!(feed.bars.len(), 1000);
        assert_eq!(feed.fetch_m1(5000).len(), 1000);
    }

    #[test]
    fn synthetic_ticks_build_the_next_bar() {
        let mut feed = SyntheticFeed::new(2650.0, 7, 300, Some(t("2026-11-20 10:00")));
        let last = feed.last_bar();
        let ticks: Vec<Tick> = (0..5).map(|_| feed.last_tick()).collect();
        assert!(ticks.iter().all(|k| k.time == last.time + TimeDelta::minutes(1)));
        let bars = feed.fetch_m1(2);
        let built = bars[1];
        assert_eq!(built.time, ticks[0].time);
        assert_eq!(built.open, last.close);
        assert_eq!(built.close, ticks[4].price);
        assert_eq!(built.volume, 5.0);
        let hi = ticks.iter().map(|k| k.price).fold(last.close, f64::max);
        let lo = ticks.iter().map(|k| k.price).fold(last.close, f64::min);
        assert_eq!((built.high, built.low), (hi, lo));
        // The next tick builds the minute after.
        assert_eq!(feed.last_tick().time, built.time + TimeDelta::minutes(1));
    }

    #[test]
    fn synthetic_is_repeatable() {
        let end = Some(t("2026-11-20 10:00"));
        let (mut a, mut b) = (SyntheticFeed::new(2650.0, 42, 2000, end), SyntheticFeed::new(2650.0, 42, 2000, end));
        assert_eq!(a.fetch_m1(2000), b.fetch_m1(2000));
        assert_eq!(a.last_tick().price, b.last_tick().price);
        let mut c = SyntheticFeed::new(2650.0, 43, 2000, end);
        assert_ne!(a.fetch_m1(100), c.fetch_m1(100));
    }

    #[test]
    fn normal_draws_look_normal() {
        let mut rng = Rng::new(42);
        let n = 100_000;
        let xs: Vec<f64> = (0..n).map(|_| rng.normal(0.0, 0.6)).collect();
        let mean = xs.iter().sum::<f64>() / n as f64;
        let sd = (xs.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n as f64).sqrt();
        assert!(mean.abs() < 0.01, "{mean}");
        assert!((sd - 0.6).abs() < 0.01, "{sd}");
    }
}
