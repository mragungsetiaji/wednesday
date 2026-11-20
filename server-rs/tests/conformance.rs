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
