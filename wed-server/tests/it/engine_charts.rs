//! The chart routes in Rust (`engine/charts.rs`: candles, replay, go to date) against the Python
//! ones (#96, step 9d part 2).
//!
//! 1. Python follows a Rust engine on the synthetic feed, so both hold the same M1 bars; the same
//!    queries (valid and not) go to Python over HTTP and to the Rust handlers, and the answers
//!    must match: status and JSON, floats within 1e-9.
//! 2. The stored-history paths (older candles past the buffer, replay reaching into the stored
//!    bars, the replay range) need a feed that stores its bars, which the synthetic one doesn't:
//!    Rust seeds a TradingView-named stream in a database, and a Python script runs the real
//!    routes on an engine holding the same buffer over the same database.
//!
//! Both start Python through uv from this checkout (`make install`); without uv they pass.

use std::collections::HashMap;
use std::net::{SocketAddr, TcpListener as StdListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use axum::body::Body;
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};
use wednesday_rs::engine::charts::{self, Stream};
use wednesday_rs::engine::{self, Answer, DataSettings, Options, Runtime};
use wednesday_rs::scan::{scan, Bar, Clock, ScanConfig, TIMEFRAMES};
use wednesday_rs::store::Store;

struct Python(Child);

impl Drop for Python {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_port() -> u16 {
    StdListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").canonicalize().unwrap()
}

fn have_uv() -> bool {
    let ok = Command::new("uv").arg("--version").stdout(Stdio::null()).status().is_ok();
    if !ok {
        eprintln!("uv isn't installed: the chart conformance test is skipped");
    }
    ok
}

fn start_python(dir: &Path, port: u16, env: &[(&str, String)]) -> Python {
    let db = format!("sqlite:///{}", dir.join("python.db").display());
    let child = Command::new("uv")
        .args(["run", "--project"]).arg(repo())
        .args(["wednesday", "--serve", "--source", "synthetic", "--lookback", "50", "--host", "127.0.0.1", "--no-poll"])
        .args(["--port", &port.to_string(), "--db", &db, "--env-file"]).arg(dir.join("none.env"))
        .current_dir(dir)
        .envs(env.iter().map(|(k, v)| (*k, v.as_str())))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start the Python server");
    let python = Python(child);
    let start = Instant::now();
    while TcpStream::connect(("127.0.0.1", port)).is_err() {
        assert!(start.elapsed() < Duration::from_secs(120), "the Python server didn't start");
        std::thread::sleep(Duration::from_millis(200));
    }
    python
}

/// Percent-encode a query value.
fn encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// (status, body) of a GET; a body that isn't JSON (FastAPI's plain 500) reads as null.
async fn get(addr: SocketAddr, path: &str, q: &[(String, String)]) -> (u16, Value) {
    let query: Vec<String> = q.iter().map(|(k, v)| format!("{}={}", encode(k), encode(v))).collect();
    let uri = format!("http://{addr}{path}?{}", query.join("&"));
    let r = axum::http::Request::builder().uri(uri).body(Body::empty()).unwrap();
    let res = Client::builder(TokioExecutor::new()).build_http().request(r).await.unwrap();
    let status = res.status().as_u16();
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// A handler's answer as the wired route would send it.
fn answered(a: Answer) -> (u16, Value) {
    match a {
        Ok(v) => (200, v),
        Err(r) if r.status == 500 => (500, Value::Null),
        Err(r) => (r.status, json!({"detail": r.detail})),
    }
}

/// Equal JSON, numbers within 1e-9 (the scan's floats, as tests/golden allows).
fn close_enough(a: &Value, b: &Value, path: &str, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > 1e-9 * x.abs().max(1.0) {
                out.push(format!("{path}: {x} vs {y}"));
            }
        }
        (Value::Object(x), Value::Object(y)) => {
            let (kx, ky): (Vec<_>, Vec<_>) = (x.keys().collect(), y.keys().collect());
            if kx != ky {
                out.push(format!("{path}: keys {kx:?} vs {ky:?}"));
                return;
            }
            for (k, v) in x {
                close_enough(v, &y[k], &format!("{path}.{k}"), out);
            }
        }
        (Value::Array(x), Value::Array(y)) => {
            if x.len() != y.len() {
                out.push(format!("{path}: {} items vs {}", x.len(), y.len()));
                return;
            }
            for (i, (v, w)) in x.iter().zip(y).enumerate() {
                close_enough(v, w, &format!("{path}[{i}]"), out);
            }
        }
        _ if a != b => out.push(format!("{path}: {a} vs {b}")),
        _ => {}
    }
}

fn compare(label: &str, python: &(u16, Value), rust: &(u16, Value), out: &mut Vec<String>) {
    if python.0 != rust.0 {
        out.push(format!("{label}: status {} vs {} ({} / {})", python.0, rust.0, python.1, rust.1));
        return;
    }
    close_enough(&python.1, &rust.1, label, out);
}

fn q(pairs: &[(&str, String)]) -> Vec<(String, String)> {
    pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
}

fn map(q: &[(String, String)]) -> HashMap<String, String> {
    q.iter().cloned().collect()
}

/// Queries for every route, valid and not, around the bars both servers hold.
fn queries(first: i64, last: i64) -> Vec<(&'static str, Vec<(String, String)>)> {
    let s = |v: i64| v.to_string();
    let mid = first + (last - first) / 2;
    let mut out: Vec<(&'static str, Vec<(String, String)>)> = Vec::new();
    // GET /api/candles
    out.push(("/api/candles", vec![]));
    for tf in ["4H", "1H", "1h", "30M", "15m", "5M", "2H", "", "ß", "1H ", "M1"] {
        out.push(("/api/candles", q(&[("tf", tf.into())])));
    }
    for limit in ["10", "2000", "9", "2001", "abc", "12.0", " 50 ", "1_000", "", "-5", "99999999999999999999999", "12.5"] {
        out.push(("/api/candles", q(&[("tf", "5M".into()), ("limit", limit.into())])));
    }
    out.push(("/api/candles", q(&[("tf", "2H".into()), ("limit", "5".into())])));
    out.push(("/api/candles", q(&[("limit", "5".into()), ("before", "x".into())])));
    for tf in ["4H", "1H", "15M", "5M"] {
        for limit in ["10", "37", "2000"] {
            for before in [last + 60, last, mid, mid + 17, first + 3600, first, 0, 9_999_999_999, -100] {
                out.push(("/api/candles", q(&[("tf", tf.into()), ("limit", limit.into()), ("before", s(before))])));
            }
        }
    }
    for before in ["", "abc", "1.5", "100000000000000000000"] {
        out.push(("/api/candles", q(&[("before", before.into())])));
    }
    // GET /api/replay/range
    out.push(("/api/replay/range", vec![]));
    // GET /api/replay
    out.push(("/api/replay", vec![]));
    for at in ["", "abc", "1.5"] {
        out.push(("/api/replay", q(&[("at", at.into())])));
    }
    out.push(("/api/replay", q(&[("at", s(mid)), ("tf", "2H".into())])));
    out.push(("/api/replay", q(&[("tf", "2H".into()), ("limit", "1".into())])));
    out.push(("/api/replay", q(&[("at", s(mid)), ("limit", "3000".into())])));
    for at in [last + 60, last + 120, last, mid, mid + 59, mid + 61, first + 60, first + 59, first, 0, -61,
               9_999_999_999] {
        out.push(("/api/replay", q(&[("at", s(at))])));
    }
    for tf in ["4H", "30m", "5M"] {
        for limit in ["10", "2000"] {
            out.push(("/api/replay", q(&[("at", s(mid + 7)), ("tf", tf.into()), ("limit", limit.into())])));
        }
    }
    out.push(("/api/replay", q(&[("at", s(mid)), ("tf", "15M".into()), ("limit", "10".into())]))); // cached
    out.push(("/api/replay", q(&[("at", "100000000000000000000".into())])));
    // GET /api/clock/feed
    out.push(("/api/clock/feed", vec![]));
    for ny in ["2026-09-24T09:30", "2026-09-24 09:30", "2026-09-24", "2026-09-24T09", "2026-09-24T09:30:15",
               "2026-09-24T09:30:15.5", "2026-09-24T09:30:15.123456789", "2026-09-24T09:30Z",
               "2026-09-24T09:30+02:00", "2026-09-24T09:30-0500", "2026-09-24T09:30 UTC", "2026-09-24T9:30",
               "2026-9-24", "20260924", "2026/09/24 09:30", "2026-09-24t0930", " 2026-09-24T09:30 ", "2026-09",
               "2026", "abc", "", "NaT", "2026-02-30T09:30", "2026-09-24T25:00", "2026-09-24T09:30:60",
               "2026-03-08T02:30", "2026-03-08T02:30:45", "2026-03-08T03:00", "2026-11-01T01:30",
               "2026-11-01T02:30", "2026-01-15T12:00", "1969-12-31T19:00:00.5", "1969-12-31T18:59:59.5", "1960-06-01T00:00",
               "9999-01-01", "2026-09-24T09:30:15,25"] {
        out.push(("/api/clock/feed", q(&[("ny", ny.into())])));
    }
    out
}

async fn rust_answer(rt: &std::sync::Arc<Runtime>, path: &str, q: &[(String, String)]) -> (u16, Value) {
    let q = map(q);
    answered(match path {
        "/api/candles" => charts::candles(rt, &q).await,
        "/api/replay/range" => charts::replay_range(rt, &q).await,
        "/api/replay" => charts::replay(rt, &q).await,
        "/api/clock/feed" => charts::clock_feed(rt, &q).await,
        _ => unreachable!(),
    })
}

#[tokio::test(flavor = "multi_thread")]
async fn charts_answer_as_python_does() {
    if !have_uv() {
        return;
    }
    let dir = std::env::temp_dir().join(format!("wed-charts-follow-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let port = free_port();
    let python: SocketAddr = ([127, 0, 0, 1], port).into();

    let rust_store = Store::open(&format!("sqlite:///{}", dir.join("rust.db").display()), &dir).await.unwrap();
    let mut cfg = ScanConfig::default();
    cfg.lookback = 50;
    let settings = DataSettings { source: "synthetic".into(), ..Default::default() };
    let rt = Runtime::start(settings, Options { cfg, poll: false, delay: 0.0 }, Some(rust_store.clone())).unwrap();
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let rust = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve_with_engine(l, python, Some(rust_store.clone()), rt.clone()));
    let _python = start_python(&dir, port, &[("WEDNESDAY_FOLLOW", format!("http://{rust}"))]);

    let start = Instant::now();
    loop {
        let p = get(python, "/api/status", &[]).await.1;
        if p["version"] == 1 && rt.engine().state().version == 1 {
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(90), "no scan: python {p}");
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
    let m1 = rt.engine().state().m1.clone();
    let (first, last) = (m1[0].time.and_utc().timestamp(), m1.last().unwrap().time.and_utc().timestamp());

    let mut failures = Vec::new();
    let mut statuses: HashMap<u16, usize> = HashMap::new();
    for (path, query) in queries(first, last) {
        let p = get(python, path, &query).await;
        let r = rust_answer(&rt, path, &query).await;
        *statuses.entry(p.0).or_default() += 1;
        compare(&format!("{path} {query:?}"), &p, &r, &mut failures);
    }
    eprintln!("statuses compared: {statuses:?}");
    assert!(failures.is_empty(), "Rust answered differently:\n{}", failures.join("\n"));
}

// ---- stored history: a TradingView-named stream in a database both read ----

const PY_SCRIPT: &str = r#"
import json, sys
import pandas as pd
from fastapi import HTTPException
from wednesday.engine import Engine
from wednesday.feeds import DataFeed
from wednesday.scanner import ScanConfig, scan
from wednesday.server import create_app
from wednesday.storage import Store

db, memory, queries = sys.argv[1:4]

class Stored(DataFeed):
    name = "tradingview"
    persist = True
    def fetch_m1(self, count):
        raise RuntimeError("not used")

cfg = ScanConfig(lookback=50)
store = Store(db)
engine = Engine(Stored(), cfg, "FX:XAUUSD", store)
rows = json.load(open(memory))
if rows:
    m1 = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close", "volume"],
                      index=pd.to_datetime([r[0] for r in rows], unit="s")).astype(float)
    engine.state.m1, engine.state.result, engine.state.version = m1, scan(m1, cfg), 1
apps = {}
out = []
for path, params, clock in json.load(open(queries)):
    if clock not in apps:
        apps[clock] = create_app(engine, source="tradingview", clock=clock)
    route = next(r for r in apps[clock].routes if getattr(r, "path", None) == path and "GET" in r.methods)
    try:
        out.append([200, route.endpoint(**params)])
    except HTTPException as exc:
        out.append([exc.status_code, {"detail": exc.detail}])
print(json.dumps(out))
"#;

/// A deterministic random walk of M1 bars over `days` weekdays (no weekend bars, an hour's gap
/// each day, two holidays), from 2026-03-02.
fn walk(days: i64) -> Vec<Bar> {
    let start = chrono::NaiveDate::from_ymd_opt(2026, 3, 2).unwrap().and_hms_opt(0, 0, 0).unwrap();
    let mut seed: u64 = 7;
    let mut rand = || {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((seed >> 11) as f64 / (1u64 << 53) as f64) - 0.5
    };
    let mut price = 2300.0;
    let mut out = Vec::new();
    for d in 0..days {
        let day = start + chrono::Duration::days(d);
        if chrono::Datelike::weekday(&day).number_from_monday() > 5 || [14, 18].contains(&d) {
            continue;
        }
        for m in 0..24 * 60 {
            if (21 * 60..22 * 60).contains(&m) {
                continue; // the daily break
            }
            let open = price;
            price += rand() * 2.0;
            let (hi, lo) = (open.max(price) + rand().abs(), open.min(price) - rand().abs());
            out.push(Bar { time: day + chrono::Duration::minutes(m), open, high: hi, low: lo, close: price,
                           volume: 1.0 });
        }
    }
    out
}

async fn python_answers(dir: &Path, db: &str, memory: &[Bar], asks: &[(String, Value, String)]) -> Vec<(u16, Value)> {
    let rows: Vec<Value> = memory
        .iter()
        .map(|b| json!([b.time.and_utc().timestamp(), b.open, b.high, b.low, b.close, b.volume]))
        .collect();
    std::fs::write(dir.join("memory.json"), Value::from(rows).to_string()).unwrap();
    let asks: Vec<Value> = asks.iter().map(|(p, q, c)| json!([p, q, c])).collect();
    std::fs::write(dir.join("queries.json"), Value::from(asks).to_string()).unwrap();
    std::fs::write(dir.join("charts_parity.py"), PY_SCRIPT).unwrap();
    let out = Command::new("uv")
        .args(["run", "--project"]).arg(repo())
        .args(["python", "charts_parity.py", db, "memory.json", "queries.json"])
        .current_dir(dir)
        .output()
        .expect("run the Python script");
    assert!(out.status.success(), "the Python script failed:\n{}", String::from_utf8_lossy(&out.stderr));
    let v: Vec<Value> = serde_json::from_slice(&out.stdout).expect("JSON from the Python script");
    v.into_iter().map(|a| (a[0].as_u64().unwrap() as u16, a[1].clone())).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn stored_history_answers_as_python_does() {
    if !have_uv() {
        return;
    }
    let dir = std::env::temp_dir().join(format!("wed-charts-stored-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let db = format!("sqlite:///{}", dir.join("bars.db").display());
    let store = Store::open(&db, &dir).await.unwrap();
    let (source, symbol) = ("tradingview", "FX:XAUUSD");
    let all = walk(40);
    let mut cfg = ScanConfig::default();
    cfg.lookback = 50;
    let max_bars = cfg.required_m1_bars();
    // The buffer: the last max_bars bars, the newest 200 not stored yet and 100 stored ones
    // changed since (the buffer's bar wins).
    let stored_upto = all.len() - 200;
    engine::save_bars(&store, source, symbol, &all[..stored_upto]).await.unwrap();
    let mut memory = all[all.len() - max_bars..].to_vec();
    let changed = memory.len() - 300;
    for b in &mut memory[changed..changed + 100] {
        b.high += 0.25;
        b.close += 0.1;
    }
    let stream = Stream { store: &store, source, symbol };
    let result = scan(&memory, &cfg, None).unwrap();

    let t = |b: &Bar| b.time.and_utc().timestamp();
    let (first, last, mem_first) = (t(&all[0]), t(all.last().unwrap()), t(&memory[0]));
    let mut asks: Vec<(String, Value, String)> = Vec::new();
    for tf in TIMEFRAMES {
        for limit in [10, 37, 200, 2000] {
            asks.push(("/api/candles".into(), json!({"tf": tf.name, "limit": limit, "before": null}), "UTC".into()));
            for before in [last + 60, mem_first + 3 * 3600 + 120, mem_first + 60, mem_first, mem_first - 7 * 60,
                           first + 86400 * 3 + 300, first + 600, first, 0] {
                asks.push(("/api/candles".into(), json!({"tf": tf.name, "limit": limit, "before": before}),
                           "UTC".into()));
            }
        }
    }
    asks.push(("/api/replay/range".into(), json!({}), "UTC".into()));
    for at in [last + 60, last - 600 + 13, mem_first + 60, mem_first - 86400, first + 86400 * 12 + 600,
               first + 86400 * 21 + 600, first + 86400 * 22 + 43200, first + 86400 * 2, first + 60, first, 0] {
        for (tf, limit) in [("1H", 200), ("4H", 10), ("5M", 2000)] {
            asks.push(("/api/replay".into(), json!({"at": at, "tf": tf, "limit": limit}), "UTC".into()));
        }
    }
    for clock in ["UTC", "NY+7", "UTC+3", "UTC-3.5", "Europe/London", "Asia/Tokyo", "NY"] {
        for ny in ["2026-09-24T09:30", "2026-03-08T02:30", "2026-11-01T01:30", "2026-01-15T23:59:59.25",
                   "2026-03-29T01:30", "1969-12-31T18:59:59.5"] {
            asks.push(("/api/clock/feed".into(), json!({"ny": ny}), clock.into()));
        }
    }
    let python = python_answers(&dir, &db, &memory, &asks).await;

    let mut failures = Vec::new();
    for ((path, query, clock), p) in asks.iter().zip(&python) {
        let tf = TIMEFRAMES.iter().copied().find(|t| Some(t.name) == query["tf"].as_str()).unwrap_or(TIMEFRAMES[1]);
        let limit = query["limit"].as_u64().unwrap_or(200) as usize;
        let r = answered(match path.as_str() {
            "/api/candles" => match query["before"].as_i64() {
                Some(before) => charts::candles_before(&memory, Some(stream), tf, before.into(), limit).await,
                None => charts::latest_candles(&memory, Some(&result), tf, limit),
            },
            "/api/replay/range" => charts::replay_range_of(&memory, Some(stream), max_bars).await,
            "/api/replay" => {
                charts::replay_view(&memory, Some(stream), &cfg, query["at"].as_i64().unwrap().into(), tf, limit).await
            }
            "/api/clock/feed" => charts::feed_time(query["ny"].as_str().unwrap(), &Clock::parse(clock).unwrap()),
            _ => unreachable!(),
        });
        compare(&format!("{path} {query} {clock}"), p, &r, &mut failures);
    }
    let ok = python.iter().filter(|p| p.0 == 200).count();
    eprintln!("{} answers compared, {ok} of them 200", python.len());
    assert!(failures.is_empty(), "Rust answered differently:\n{}", failures.join("\n"));
}
