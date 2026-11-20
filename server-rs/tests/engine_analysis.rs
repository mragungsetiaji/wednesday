//! The analysis routes (`GET /api/quarters`, `/api/sessions`, `/api/distribution`) against the
//! real Python server (#96, step 9d part 2). Rust runs the engine on the synthetic feed and Python
//! follows it (`WEDNESDAY_FOLLOW`), so both hold the same M1 bars; each query goes to Python over
//! HTTP and to the Rust handler on the same runtime, and the answers must agree: the status, the
//! message, and the JSON (ints as ints, floats within 1e-9). FastAPI's validation lists are compared
//! by status only.
//!
//! It starts `uv run wednesday --serve --source synthetic` from this checkout, so it needs uv and the
//! Python environment (`make install`); without uv it says so and passes.

use std::collections::HashMap;
use std::net::{SocketAddr, TcpListener as StdListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::Value;
use wednesday_rs::engine::{analysis, Answer, DataSettings, Options, Runtime};
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

fn start_python(dir: &Path, port: u16, env: &[(&str, String)]) -> Option<Python> {
    if Command::new("uv").arg("--version").stdout(Stdio::null()).status().is_err() {
        eprintln!("uv isn't installed: the analysis conformance test is skipped");
        return None;
    }
    let db = format!("sqlite:///{}", dir.join("python.db").display());
    let child = Command::new("uv")
        .args(["run", "--project"]).arg(repo())
        .args(["wednesday", "--serve", "--source", "synthetic", "--lookback", "50", "--host", "127.0.0.1"])
        .args(["--port", &port.to_string(), "--db", &db, "--env-file"]).arg(dir.join("none.env"))
        .arg("--no-poll")
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
    Some(python)
}

/// GET `path_and_query` (already percent-encoded where it needs to be): status and JSON body.
async fn get(addr: SocketAddr, path_and_query: &str) -> (u16, Value) {
    let r = axum::http::Request::builder()
        .uri(format!("http://{addr}{path_and_query}"))
        .body(Body::empty())
        .unwrap();
    let res = Client::builder(TokioExecutor::new()).build_http().request(r).await.unwrap();
    let status = res.status().as_u16();
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// The query as the router hands it to a handler (axum's `Query<HashMap<String, String>>`).
fn query_of(path_and_query: &str) -> HashMap<String, String> {
    let uri: axum::http::Uri = path_and_query.parse().unwrap();
    axum::extract::Query::<HashMap<String, String>>::try_from_uri(&uri).map(|q| q.0).unwrap_or_default()
}

async fn rust(rt: &Arc<Runtime>, path_and_query: &str) -> (u16, Value) {
    let q = query_of(path_and_query);
    let answer: Answer = match path_and_query.split('?').next().unwrap() {
        "/api/quarters" => analysis::quarters(rt, &q).await,
        "/api/sessions" => analysis::sessions(rt, &q).await,
        "/api/distribution" => analysis::distribution(rt, &q).await,
        other => panic!("not an analysis route: {other}"),
    };
    match answer {
        Ok(v) => (200, v),
        Err(r) => (r.status, serde_json::json!({"detail": r.detail})),
    }
}

/// Equal JSON: the same keys in the same order, ints as ints, floats within 1e-9.
fn close_enough(a: &Value, b: &Value, path: &str, out: &mut Vec<String>) {
    if out.len() >= 30 {
        return;
    }
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            if x.is_f64() != y.is_f64() {
                out.push(format!("{path}: {x} vs {y} (int vs float)"));
                return;
            }
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

fn requests() -> Vec<String> {
    let mut out: Vec<String> = vec!["/api/quarters".into(), "/api/sessions".into(), "/api/distribution".into()];
    for period in ["day", "week", "session", "q90"] {
        for measure in ["change", "range_pct", "range_atr"] {
            for lookback in ["1y", "5y", "all"] {
                out.push(format!("/api/distribution?period={period}&measure={measure}&lookback={lookback}"));
            }
            for flags in ["weekday=true", "session=true", "weekday=1&session=yes", "weekday=false&session=off"] {
                out.push(format!("/api/distribution?period={period}&measure={measure}&{flags}"));
            }
        }
    }
    // Bins, as pydantic reads them, and the ones it refuses.
    for bins in ["10", "80", "13", "1_0", "10.0", "+20", "%2020%20", "0020", "9", "81", "x", "", "10.5", "_10", "1e1",
                 "-10", "99999999999999999999999"] {
        out.push(format!("/api/distribution?period=q90&bins={bins}"));
    }
    for flag in ["TRUE", "On", "t", "n", "", "maybe", "2", "%20true"] {
        out.push(format!("/api/distribution?period=session&session={flag}"));
        out.push(format!("/api/distribution?weekday={flag}"));
    }
    // Unknown values: the route's 422 with distribution.py's message.
    for bad in ["period=month", "period=", "lookback=2y", "lookback=ALL", "measure=size", "measure=", "period=Day",
                "period=month&measure=size", "lookback=2y&measure=size", "period=q90&measure=size&lookback=5y",
                "period=month&bins=5", "measure=size&weekday=maybe", "period=%E2%9C%93"] {
        out.push(format!("/api/distribution?{bad}"));
    }
    // A repeated parameter: the last one counts; unknown ones are ignored.
    out.push("/api/distribution?period=day&period=q90&bins=20&bins=30".into());
    out.push("/api/distribution?nonsense=1&measure=range_pct".into());
    out.push("/api/quarters?x=1".into());
    out.push("/api/sessions?clock=NY%2B7".into());
    out
}

#[tokio::test(flavor = "multi_thread")]
async fn the_analysis_routes_answer_as_python_does() {
    let dir = std::env::temp_dir().join(format!("wed-analysis-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let port = free_port();
    let python: SocketAddr = ([127, 0, 0, 1], port).into();

    let store = Store::open(&format!("sqlite:///{}", dir.join("rust.db").display()), &dir).await.unwrap();
    let mut cfg = wednesday_rs::scan::ScanConfig::default();
    cfg.lookback = 50;
    let settings = DataSettings { source: "synthetic".into(), ..Default::default() };
    let runtime = Runtime::start(settings, Options { cfg, poll: false, delay: 0.0 }, Some(store.clone())).unwrap();
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let rust_addr = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve_with_engine(l, python, Some(store), runtime.clone()));

    let Some(_python) = start_python(&dir, port, &[("WEDNESDAY_FOLLOW", format!("http://{rust_addr}"))]) else {
        return;
    };
    let start = Instant::now();
    loop {
        let (p, r) = (get(python, "/api/status").await.1, get(rust_addr, "/api/status").await.1);
        if p["version"] == 1 && r["version"] == 1 {
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(90), "no scan: python {p} rust {r}");
        tokio::time::sleep(Duration::from_millis(300)).await;
    }

    let mut failures = Vec::new();
    let (mut ok, mut refused, mut with_percentile) = (0, 0, 0);
    // Twice: the second round answers from the caches.
    for round in 0..2 {
        for path in requests() {
            let (ps, pb) = get(python, &path).await;
            let (rs, rb) = rust(&runtime, &path).await;
            if ps != rs {
                failures.push(format!("{path}: status {ps} vs {rs} ({pb} / {rb})"));
                continue;
            }
            if ps == 422 && pb["detail"].is_array() {
                if !rb["detail"].is_array() {
                    failures.push(format!("{path}: validation list vs {rb}"));
                }
                refused += 1;
                continue;
            }
            close_enough(&pb, &rb, &format!("{path} (round {round})"), &mut failures);
            if ps == 200 {
                ok += 1;
                with_percentile += !pb["percentile"].is_null() as usize;
            } else {
                refused += 1;
            }
        }
    }
    eprintln!("analysis routes: {ok} answers, {refused} refusals, {with_percentile} with a percentile");
    assert!(failures.is_empty(), "Rust answered differently:\n{}", failures.join("\n"));
    // The comparison covered real answers, not just refusals.
    assert!(ok > 100 && refused > 20 && with_percentile > 10, "ok {ok}, refused {refused}, with a percentile {with_percentile}");
}
