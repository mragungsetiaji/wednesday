//! The ported routes against the real Python server (#96): the same requests go to Python and to
//! the Rust server, and they must answer the same (status and JSON, `saved_at` aside). Rust also
//! has to store the same data Python stores, read back from both databases.
//!
//! It starts `uv run wednesday --serve --source synthetic` from this checkout, so it needs uv and
//! the Python environment (`make install`); without uv it says so and passes.

use std::net::{SocketAddr, TcpListener as StdListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use axum::body::Body;
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};
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

fn start_python(dir: &Path, port: u16) -> Option<Python> {
    if Command::new("uv").arg("--version").stdout(Stdio::null()).status().is_err() {
        eprintln!("uv isn't installed: the conformance test is skipped");
        return None;
    }
    let db = format!("sqlite:///{}", dir.join("python.db").display());
    let child = Command::new("uv")
        .args(["run", "--project"]).arg(repo())
        .args(["wednesday", "--serve", "--source", "synthetic", "--lookback", "50", "--host", "127.0.0.1"])
        .args(["--port", &port.to_string(), "--db", &db, "--env-file"]).arg(dir.join("none.env"))
        .current_dir(dir)
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

struct Answer {
    status: u16,
    body: Value,
}

/// Percent-encode what a URL path can't hold as it is (spaces, non-ASCII).
fn encode(path: &str) -> String {
    let mut out = String::new();
    for b in path.bytes() {
        if b.is_ascii_alphanumeric() || b"/-._~%".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

async fn call(addr: SocketAddr, method: &str, path: &str, cookie: &str, body: Option<&Value>) -> Answer {
    let mut r = axum::http::Request::builder().method(method).uri(format!("http://{addr}{}", encode(path)));
    if !cookie.is_empty() {
        r = r.header("cookie", cookie);
    }
    let r = match body {
        Some(b) => r.header("content-type", "application/json").body(Body::from(b.to_string())),
        None => r.body(Body::empty()),
    }
    .unwrap();
    let res = Client::builder(TokioExecutor::new()).build_http().request(r).await.unwrap();
    let status = res.status().as_u16();
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    Answer { status, body: serde_json::from_slice(&bytes).unwrap_or(Value::Null) }
}

/// `saved_at` differs by when each server saved; check it's an ISO time and drop it.
fn without_saved_at(v: &Value) -> Value {
    match v {
        Value::Object(m) => Value::Object(
            m.iter()
                .filter(|(k, v)| {
                    if *k == "saved_at" {
                        let s = v.as_str().unwrap_or("");
                        assert!(s.len() >= 25 && s.ends_with("+00:00") && s.as_bytes()[10] == b'T', "saved_at {s}");
                    }
                    *k != "saved_at"
                })
                .map(|(k, v)| (k.clone(), without_saved_at(v)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(without_saved_at).collect()),
        other => other.clone(),
    }
}

fn steps() -> Vec<(&'static str, String, Option<Value>)> {
    let win = json!([{"route": "#", "x": 0, "y": 0, "width": 1440, "height": 900, "maximized": true},
                     {"route": "#popout/4H/A", "x": 1920, "y": 0, "width": 1100, "height": 700}]);
    let h1 = json!({"style": {"color": "#3b82f6", "width": 1, "dash": "dotted"}, "text": "H1 OB"});
    let mut s: Vec<(&str, String, Option<Value>)> = vec![
        ("GET", "/api/workspaces".into(), None),
        ("PUT", "/api/workspaces/London prep".into(),
         Some(json!({"layout": {"wed.focusLayout": "4", "xau.tf": "15M", "secret.token": "x"}, "windows": win}))),
        ("PUT", "/api/workspaces/apple".into(), Some(json!({}))), // sorted without case: apple before London
        ("GET", "/api/workspaces".into(), None),
        ("GET", "/api/workspaces/London prep".into(), None),
        ("GET", "/api/workspaces/missing".into(), None),
        ("PUT", "/api/workspaces/x".into(),
         Some(json!({"windows": [{"route": "https://evil.example", "x": 0, "y": 0, "width": 800, "height": 600}]}))),
        ("PUT", "/api/workspaces/x".into(), Some(json!({"windows": [{"route": "#", "x": "a"}]}))),
        ("PUT", "/api/workspaces/%20".into(), Some(json!({}))),
        ("PUT", "/api/workspaces/x".into(), Some(json!({"layout": []}))),
        ("PUT", "/api/workspaces/y".into(), Some(json!({"layout": "s"}))),
        ("PUT", "/api/workspaces/y".into(), Some(json!({"windows": "a"}))),
        ("PUT", "/api/workspaces/  R\u{e9}sum\u{e9} \t tokyo \u{1F600} ".into(),
         Some(json!({"windows": [{"route": "popout/5M", "x": 10.7, "y": -3.2, "width": 100, "height": 2000.9,
                                  "maximized": 1}, {"route": 0, "x": 1, "y": 2, "width": 600, "height": 400}, 7],
                     "layout": {"xau.\u{e9}": [1, 2.5, null, {"a": "\u{1F600}"}]}}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!({"windows": [{"route": 5, "x": 0, "y": 0, "width": 1, "height": 1}]}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!({"windows": [{"route": true, "x": 0, "y": 0, "width": 1, "height": 1}]}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!({"windows": [{"route": "#", "x": true, "y": 0, "width": 1, "height": 1}]}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!({"windows": vec![json!({"route": "#", "x": 0, "y": 0, "width": 1, "height": 1}); 13]}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!({"layout": {"xau.big": "x".repeat(70_000)}}))),
        ("PUT", "/api/workspaces/z".into(), Some(json!([1]))),
        ("GET", "/api/workspaces".into(), None),
        ("DELETE", "/api/workspaces/x".into(), None),
        ("DELETE", "/api/workspaces/x".into(), None),
        ("GET", "/api/drawing-templates".into(), None),
        ("PUT", "/api/drawing-templates/line/m15 OB".into(), Some(h1.clone())),
        ("PUT", "/api/drawing-templates/line/H1 OB".into(), Some(h1.clone())),
        ("PUT", "/api/drawing-templates/rect/Zone".into(), Some(json!({"style": {"color": null, "width": 2}}))),
        ("PUT", "/api/drawing-templates/line/H1 OB".into(), Some(json!({"style": h1["style"], "text": "H1 OB (2)"}))),
        ("PUT", "/api/drawing-templates/text/big".into(),
         Some(json!({"style": {"width": 2.5, "size": 14, "dash": "dashed"}, "text": "  spaced  "}))),
        ("PUT", "/api/drawing-templates/line/blank".into(), Some(json!({"style": {}, "text": "  "}))),
        ("PUT", "/api/drawing-templates/line/apex".into(), Some(h1.clone())), // apex before blank before H1 OB
        ("GET", "/api/drawing-templates".into(), None),
        ("PUT", "/api/drawing-templates/circle/x".into(), Some(h1.clone())),
        ("PUT", "/api/drawing-templates/line/%20%20".into(), Some(h1.clone())),
        ("DELETE", "/api/drawing-templates/line/m15 OB".into(), None),
        ("DELETE", "/api/drawing-templates/line/m15 OB".into(), None),
        ("DELETE", "/api/drawing-templates/circle/x".into(), None),
    ];
    for bad in [json!({}), json!({"style": {"color": "blue"}}), json!({"style": {"width": 0}}), json!({"style": {"width": true}}),
                json!({"style": {"width": null}}), json!({"style": {"dash": "wavy"}}), json!({"style": {"size": 2}}),
                json!({"style": {}, "text": 5}), json!({"style": {}, "text": "x".repeat(501)})] {
        s.push(("PUT", "/api/drawing-templates/line/bad".into(), Some(bad)));
    }
    for i in 0..21 {
        s.push(("PUT", format!("/api/workspaces/w{i}"), Some(json!({}))));
    }
    // Calendar settings, on a URL nothing answers so neither server reaches the internet.
    let url = "http://127.0.0.1:9/calendar.json";
    for body in [json!({"url": url}),
                 json!({"url": format!(" {url} "), "currencies": [" usd ", "", "eur"], "impacts": ["High", "Medium"]}),
                 json!({"url": url, "enabled": 0}), json!({"url": url, "enabled": "no"}), json!({"url": url, "enabled": null}),
                 json!({"url": "ftp://x", "impacts": ["Huge", "it's", "tab\t"], "currencies": [" "]}),
                 json!({"url": 5}), json!({"url": "   "}), json!({"currencies": "USD"}), json!({"currencies": [1]}),
                 json!({"impacts": null}), json!({"impacts": "High"}), json!([1]),
                 json!({"url": url, "currencies": ["gbp"], "impacts": ["Low"], "enabled": true})] {
        s.push(("PUT", "/api/calendar".into(), Some(body)));
    }
    s
}

/// What both calendars answer alike: the rest (fetch time, errors, loading) is the live fetch's.
fn calendar_part(v: &Value) -> Value {
    match v.get("settings") {
        Some(settings) => json!({"editable": v["editable"], "settings": settings}),
        None => v.clone(),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn the_ported_routes_answer_as_python_does() {
    let dir = std::env::temp_dir().join(format!("wed-conformance-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let port = free_port();
    let Some(_python) = start_python(&dir, port) else { return };
    let python: SocketAddr = ([127, 0, 0, 1], port).into();

    // Rust in front of the same Python, on a database of its own.
    let rust_store = Store::open(&format!("sqlite:///{}", dir.join("rust.db").display()), &dir).await.unwrap();
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let rust = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve(l, python, Some(rust_store.clone())));

    let cookie = String::new();
    let mut failures = Vec::new();
    for (method, path, body) in steps() {
        let p = call(python, method, &path, &cookie, body.as_ref()).await;
        let r = call(rust, method, &path, &cookie, body.as_ref()).await;
        // FastAPI's own validation errors are a list; Rust gives the same status and its own list.
        let same_body = p.body["detail"].is_array()
            || if path == "/api/calendar" {
                calendar_part(&p.body) == calendar_part(&r.body)
            } else {
                without_saved_at(&p.body) == without_saved_at(&r.body)
            };
        if p.status != r.status || !same_body {
            failures.push(format!("{method} {path}\n    python {} {}\n    rust   {} {}", p.status, p.body, r.status, r.body));
        }
    }
    assert!(failures.is_empty(), "Rust answered differently:\n{}", failures.join("\n"));

    // And stored the same: each database's settings, read back.
    let python_store = Store::open(&format!("sqlite:///{}", dir.join("python.db").display()), &dir).await.unwrap();
    for key in ["workspaces", "drawing_templates", "calendar"] {
        let p = python_store.get_setting(key).await.unwrap();
        let r = rust_store.get_setting(key).await.unwrap();
        assert_eq!(p.as_ref().map(without_saved_at), r.as_ref().map(without_saved_at), "{key}");
    }
}

// ---- the journal (#96, step 9c) ----

const REPORT: &str = r#"<html><body><table>
<tr><th colspan="14"><div><b>Trade History Report</b></div></th></tr>
<tr><th colspan="3">Name:</th><th colspan="11"><b>Someone</b></th></tr>
<tr><th colspan="3">Account:</th><th colspan="11"><b>51234567 (USD, Broker-Live, real, Hedge)</b></th></tr>
<tr><th colspan="3">Company:</th><th colspan="11"><b>Broker Ltd</b></th></tr>
<tr><th colspan="14"><div><b>Positions</b></div></th></tr>
<tr><td>Time</td><td>Position</td><td>Symbol</td><td>Type</td><td class="hidden" colspan="8"></td><td>Volume</td><td>Price</td>
<td>S / L</td><td>T / P</td><td>Time</td><td>Price</td><td>Commission</td><td>Swap</td><td>Profit</td></tr>
<tr><td>2026.03.02 00:01:00</td><td>7</td><td>XAUUSD.m</td><td>buy</td><td class="hidden" colspan="8"></td><td>1</td><td>2 000.00</td>
<td></td><td>2 020.00</td><td>2026.03.02 00:10:00</td><td>2 010.00</td><td>-7.00</td><td>0.00</td><td>1 000.00</td></tr>
<tr><td>2026.03.02 00:12:30</td><td>9</td><td>XAUUSD.m</td><td>sell</td><td class="hidden" colspan="8"></td><td>0.5</td><td>2 009.50</td>
<td>2 015.00</td><td></td><td>2026.03.02 00:20:10</td><td>2 004.00</td><td>-3.50</td><td>-1.20</td><td>275.00</td></tr>
<tr><td>2026.03.02 00:21:00</td><td>11</td><td>EURUSD</td><td>buy</td><td class="hidden" colspan="8"></td><td>0.1</td><td>1.0850</td>
<td></td><td></td><td>2026.03.02 00:25:00</td><td>1.0840</td><td>0.00</td><td>0.00</td><td>-10.00</td></tr>
<tr><td colspan="10"></td><td>-10.50</td><td>-1.20</td><td>1 265.00</td></tr>
<tr><th colspan="14"><div><b>Deals</b></div></th></tr>
<tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td><td>Volume</td><td>Price</td><td>Order</td>
<td>Commission</td><td>Fee</td><td>Swap</td><td>Profit</td><td>Balance</td><td>Comment</td></tr>
<tr><td>2026.03.01 20:00:00</td><td>1</td><td></td><td>balance</td><td></td><td></td><td></td><td></td>
<td>0.00</td><td>0.00</td><td>0.00</td><td>10 000.00</td><td>10 000.00</td><td>Deposit</td></tr>
<tr><td>2026.03.02 00:30:00</td><td>3</td><td></td><td>credit</td><td></td><td></td><td></td><td></td>
<td>0.00</td><td>0.00</td><td>0.00</td><td>50.00</td><td>11 315.00</td><td>Bonus</td></tr>
<tr><th colspan="14"><div><b>Results</b></div></th></tr>
</table></body></html>"#;

/// The same M1 bars in a database: gold around the report's trades, on the price clock.
async fn seed_bars(store: &Store) {
    let start = 1_772_409_600; // 2026-03-02 00:00 UTC
    for m in 0..40i64 {
        let base = 2000.0 + (m as f64) * 0.5 - if m > 12 { (m - 12) as f64 * 0.9 } else { 0.0 };
        let args = vec![
            wednesday_rs::store::Arg::Str(Some("tradingview".into())), wednesday_rs::store::Arg::Str(Some("XAUUSD".into())),
            wednesday_rs::store::Arg::Int(Some(start + m * 60)), wednesday_rs::store::Arg::Float(Some(base)),
            wednesday_rs::store::Arg::Float(Some(base + 6.0)), wednesday_rs::store::Arg::Float(Some(base - 3.0)),
            wednesday_rs::store::Arg::Float(Some(base + 0.25)), wednesday_rs::store::Arg::Float(Some(10.0)),
        ];
        store.execute("INSERT INTO m1_bars (source, symbol, time, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      args).await.unwrap();
    }
}

async fn call_raw(addr: SocketAddr, method: &str, path: &str, body: &[u8]) -> (u16, String) {
    let r = axum::http::Request::builder().method(method).uri(format!("http://{addr}{}", encode(path)))
        .body(Body::from(body.to_vec())).unwrap();
    let res = Client::builder(TokioExecutor::new()).build_http().request(r).await.unwrap();
    let status = res.status().as_u16();
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// A server's answer with its own journal id as `J`, and times (which differ by when each server
/// wrote them) as `T`.
fn normal(text: &str, jid: &str) -> Value {
    fn times(v: Value) -> Value {
        match v {
            Value::String(s) if s.len() >= 25 && s.as_bytes().get(10) == Some(&b'T') && s.ends_with("+00:00") => json!("T"),
            Value::Array(a) => Value::Array(a.into_iter().map(times).collect()),
            Value::Object(m) => Value::Object(m.into_iter().map(|(k, v)| (k, times(v))).collect()),
            other => other,
        }
    }
    let text = if jid.is_empty() { text.to_string() } else { text.replace(jid, "J") };
    serde_json::from_str(&text).map(times).unwrap_or(Value::String(text))
}

#[tokio::test(flavor = "multi_thread")]
async fn the_journal_answers_as_python_does() {
    let dir = std::env::temp_dir().join(format!("wed-conformance-journal-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("data/snapshots")).unwrap();
    let port = free_port();
    let Some(_python) = start_python(&dir, port) else { return };
    let python: SocketAddr = ([127, 0, 0, 1], port).into();
    let python_store = Store::open(&format!("sqlite:///{}", dir.join("python.db").display()), &dir).await.unwrap();
    let rust_store = Store::open(&format!("sqlite:///{}", dir.join("rust.db").display()), &dir).await.unwrap();
    seed_bars(&python_store).await;
    seed_bars(&rust_store).await;
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let rust = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve(l, python, Some(rust_store.clone())));

    let shot = "0123456789abcdef0123456789abcdef";
    std::fs::write(dir.join(format!("data/snapshots/{shot}.png")), b"\x89PNG\r\n\x1a\n").unwrap();
    let other_shot = "ffffffffffffffffffffffffffffffff";

    // (method, path with {j} for the server's journal, body: JSON, raw bytes, or none)
    enum B {
        J(Value),
        Raw(Vec<u8>),
        None,
    }
    let report16: Vec<u8> = [0xFFu8, 0xFE].into_iter()
        .chain(REPORT.encode_utf16().flat_map(|u| u.to_le_bytes())).collect();
    let steps: Vec<(&str, String, B)> = vec![
        ("GET", "/api/journals".into(), B::None),
        ("POST", "/api/journals".into(), B::J(json!({"name": " "}))),
        ("POST", "/api/journals".into(), B::J(json!({"name": "Main", "login": "12a"}))),
        ("POST", "/api/journals".into(), B::J(json!([1]))),
        ("POST", "/api/journals".into(), B::J(json!({"name": format!("  {}  ", "M".repeat(130)), "login": " 51234567 "}))),
        ("POST", "/api/journals".into(), B::J(json!({"name": "Second"}))),
        ("GET", "/api/journals".into(), B::None),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"time_offset": "3"}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"time_offset": 20}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"time_offset": "x"}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"show_weekends": "yes"}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"name": " "}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"name": "Gold", "show_weekends": true, "time_offset": 0.000625}))),
        ("PATCH", "/api/journals/{j}".into(), B::J(json!({"time_offset": ""}))),
        ("PATCH", "/api/journals/nope".into(), B::J(json!({}))),
        ("PATCH", "/api/journals/nope".into(), B::J(json!([1]))),
        ("GET", "/api/journals/nope".into(), B::None),
        ("POST", "/api/journals/{j}/import".into(), B::Raw(b"<html>no table</html>".to_vec())),
        ("POST", "/api/journals/{j}/import".into(), B::Raw(REPORT.replace("51234567 (USD", "7654321 (USD").into_bytes())),
        ("POST", "/api/journals/nope/import".into(), B::Raw(REPORT.as_bytes().to_vec())),
        ("POST", "/api/journals/{j}/import".into(), B::Raw(report16)),
        ("GET", "/api/journals/{j}".into(), B::None),
        ("PUT", "/api/journals/{j}/trades/p7/note".into(), B::J(json!({"note": "  Clean break  ", "tags": [" A+ ", "a+", "", "London"]}))),
        ("PUT", "/api/journals/{j}/trades/p7/note".into(), B::J(json!({"note": "x", "tags": "a"}))),
        ("PUT", "/api/journals/{j}/trades/p9/note".into(), B::J(json!({"note": "", "tags": []}))),
        ("POST", "/api/journals/{j}/trades/p7/images".into(), B::J(json!({"snapshot_id": other_shot}))),
        ("POST", "/api/journals/{j}/trades/p99/images".into(), B::J(json!({"snapshot_id": shot}))),
        ("POST", "/api/journals/{j}/trades/p9/images".into(), B::J(json!({"snapshot_id": shot}))),
        ("POST", "/api/journals/{j}/trades/p9/images".into(), B::J(json!({"snapshot_id": shot}))),
        ("GET", "/api/journals/{j}".into(), B::None),
        ("GET", "/api/journals/{j}/trades.csv".into(), B::None),
        ("GET", "/api/journals/{j}/entries".into(), B::None),
        ("PATCH", "/api/journals/{j}/entries/e1".into(), B::J(json!({"text": "  Asia was quiet ", "tags": "ab", "mood": "4"}))),
        ("PATCH", "/api/journals/{j}/entries/e1".into(), B::J(json!({"mood": 9}))),
        ("PATCH", "/api/journals/{j}/entries/e1".into(), B::J(json!({"mood": "x"}))),
        ("PATCH", "/api/journals/{j}/entries/e9".into(), B::J(json!({}))),
        ("GET", "/api/journals/{j}/entries".into(), B::None),
        ("DELETE", "/api/journals/{j}/entries/e2".into(), B::None),
        ("DELETE", "/api/journals/{j}/entries/e2".into(), B::None),
        ("DELETE", format!("/api/journals/{{j}}/trades/p9/images/{other_shot}"), B::None),
        ("DELETE", format!("/api/journals/{{j}}/trades/p9/images/{shot}"), B::None),
        ("GET", "/api/journals/{j}".into(), B::None),
        ("DELETE", "/api/journals/nope".into(), B::None),
        ("DELETE", "/api/journals/{j}".into(), B::None),
        ("GET", "/api/journals".into(), B::None),
    ];

    let (mut pj, mut rj) = (String::new(), String::new());
    let mut failures = Vec::new();
    for (method, path, body) in steps {
        if path.contains("entries/e1") && method == "PATCH" && !pj.is_empty() {
            // Entries are written with the market of the moment (still Python's): put the same two in each.
            for (store, jid) in [(&python_store, &pj), (&rust_store, &rj)] {
                for (id, at) in [("e1", "2026-03-02T09:00:00.000000+00:00"), ("e2", "2026-03-02T10:00:00.000000+00:00")] {
                    let a = |s: &str| wednesday_rs::store::Arg::Str(Some(s.into()));
                    store.upsert("journal_entries", vec![("id", a(id)), ("journal_id", a(jid)), ("kind", a("review")),
                                                         ("created_at", a(at)), ("text", a("Asia")), ("tags", a("[]")),
                                                         ("market", a("{\"price\": 2000.5}"))], &["id"]).await.unwrap();
                }
            }
        }
        let mut answers = Vec::new();
        for (addr, jid) in [(python, &pj), (rust, &rj)] {
            let p = path.replace("{j}", jid);
            let (status, text) = match &body {
                B::J(v) => {
                    let a = call(addr, method, &p, "", Some(v)).await;
                    (a.status, a.body.to_string())
                }
                B::Raw(b) => call_raw(addr, method, &p, b).await,
                B::None => call_raw(addr, method, &p, b"").await,
            };
            answers.push((status, text));
        }
        // The new journal's id, per server.
        if method == "POST" && path == "/api/journals" && answers[0].0 == 200 {
            pj = normal(&answers[0].1, "")["id"].as_str().unwrap().to_string();
            rj = normal(&answers[1].1, "")["id"].as_str().unwrap().to_string();
        }
        let (p, r) = (normal(&answers[0].1, &pj), normal(&answers[1].1, &rj));
        let fastapi_list = p.get("detail").is_some_and(Value::is_array);
        if answers[0].0 != answers[1].0 || (!fastapi_list && p != r) {
            failures.push(format!("{method} {path}\n    python {} {}\n    rust   {} {}", answers[0].0, p, answers[1].0, r));
        }
    }
    assert!(failures.is_empty(), "Rust answered differently:\n{}", failures.join("\n"));
}
