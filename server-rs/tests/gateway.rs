//! The Rust server in front of a stand-in for the Python one: ported routes answered here, the
//! rest passed through unchanged (streams included), and the API contract kept (#96).

use std::net::SocketAddr;
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::Request;
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{any, get};
use axum::Router;
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use tokio_stream::wrappers::ReceiverStream;
use wednesday_rs::store::Store;

async fn listen(app: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    addr
}

/// A stand-in Python server: echoes what it got, and streams events slowly.
fn python() -> Router {
    async fn echo(headers: HeaderMap, req: Request) -> (StatusCode, [(&'static str, &'static str); 2], String) {
        let get = |n: &str| headers.get(n).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let line = format!("{} {}", req.method(), req.uri());
        let (host, cookie, hop) = (get("host"), get("cookie"), headers.contains_key("x-hop"));
        let body = String::from_utf8(req.into_body().collect().await.unwrap().to_bytes().to_vec()).unwrap();
        let text = serde_json::json!({"line": line, "host": host, "cookie": cookie, "hop": hop, "body": body});
        (StatusCode::CREATED, [("set-cookie", "wed=1; HttpOnly"), ("content-type", "application/json")], text.to_string())
    }
    async fn stream() -> axum::response::Response {
        let (tx, rx) = tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(4);
        tokio::spawn(async move {
            tx.send(Ok(Bytes::from("data: first\n\n"))).await.unwrap();
            tokio::time::sleep(Duration::from_secs(5)).await;
            let _ = tx.send(Ok(Bytes::from("data: later\n\n"))).await;
        });
        axum::response::Response::builder()
            .header("content-type", "text/event-stream")
            .body(Body::from_stream(ReceiverStream::new(rx)))
            .unwrap()
    }
    Router::new()
        .route("/api/health", get(|| async { "python answered" }))
        .route("/api/stream", get(stream))
        .fallback(any(echo))
}

async fn front(backend: SocketAddr) -> SocketAddr {
    front_with(backend, None).await
}

async fn front_with(backend: SocketAddr, store: Option<Store>) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve(l, backend, store));
    addr
}

async fn temp_store(name: &str) -> Store {
    let dir = std::env::temp_dir().join(format!("wed-gateway-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    Store::open("sqlite:///data/xau.db", &dir).await.unwrap()
}

fn req(method: &str, addr: SocketAddr, path: &str, cookie: &str, body: Option<serde_json::Value>) -> axum::http::Request<Body> {
    let mut r = axum::http::Request::builder().method(method).uri(format!("http://{addr}{path}"));
    if !cookie.is_empty() {
        r = r.header("cookie", cookie);
    }
    match body {
        Some(b) => r.header("content-type", "application/json").body(Body::from(b.to_string())).unwrap(),
        None => r.body(Body::empty()).unwrap(),
    }
}

async fn send(req: axum::http::Request<Body>) -> axum::http::Response<hyper::body::Incoming> {
    Client::builder(TokioExecutor::new()).build_http().request(req).await.unwrap()
}

async fn text(res: axum::http::Response<hyper::body::Incoming>) -> String {
    String::from_utf8(res.into_body().collect().await.unwrap().to_bytes().to_vec()).unwrap()
}

#[tokio::test]
async fn a_ported_route_is_answered_in_rust() {
    let addr = front(listen(python()).await).await;
    let res = send(axum::http::Request::get(format!("http://{addr}/api/health")).body(Body::empty()).unwrap()).await;
    assert_eq!(res.status(), 200);
    assert_eq!(res.headers()["x-wednesday-server"], "rust");
    let body: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!(body, serde_json::json!({"ok": true, "version": wednesday_rs::VERSION}));
}

#[tokio::test]
async fn the_rest_reaches_python_unchanged() {
    let addr = front(listen(python()).await).await;
    let req = axum::http::Request::post(format!("http://{addr}/api/licence?x=1&y=two"))
        .header("cookie", "wed_session=abc")
        .header("connection", "keep-alive, x-hop")
        .header("x-hop", "1")
        .body(Body::from(r#"{"bias":"bullish"}"#))
        .unwrap();
    let res = send(req).await;
    assert_eq!(res.status(), 201);
    assert_eq!(res.headers()["set-cookie"], "wed=1; HttpOnly");
    let got: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!(got["line"], "POST /api/licence?x=1&y=two");
    assert_eq!(got["host"], addr.to_string(), "the page's address, for the login's same-origin check");
    assert_eq!(got["cookie"], "wed_session=abc");
    assert_eq!(got["hop"], false, "a header the Connection header names stays with this hop");
    assert_eq!(got["body"], r#"{"bias":"bullish"}"#);
}

#[tokio::test]
async fn the_event_stream_is_passed_on_as_it_comes() {
    let addr = front(listen(python()).await).await;
    let res = send(axum::http::Request::get(format!("http://{addr}/api/stream")).body(Body::empty()).unwrap()).await;
    assert_eq!(res.headers()["content-type"], "text/event-stream");
    let mut body = res.into_body();
    let first = tokio::time::timeout(Duration::from_secs(2), body.frame()).await.expect("the first event waited for the rest");
    let chunk = first.unwrap().unwrap().into_data().unwrap();
    assert_eq!(&chunk[..], b"data: first\n\n");
}

#[tokio::test]
async fn python_not_answering_is_a_502_saying_what_to_do() {
    let gone = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap(); // closed again
    let addr = front(gone).await;
    let res = send(axum::http::Request::get(format!("http://{addr}/api/scan")).body(Body::empty()).unwrap()).await;
    assert_eq!(res.status(), 502);
    assert!(text(res).await.contains("Restart Wednesday"));
}

// ---- no login; no cross-site writes ----

#[tokio::test]
async fn cross_site_writes_are_refused() {
    let addr = front_with(listen(python()).await, Some(temp_store("xsite").await)).await;
    let mut r = req("DELETE", addr, "/api/workspaces/x", "", None);
    r.headers_mut().insert("origin", "http://evil.example".parse().unwrap());
    let res = send(r).await;
    assert_eq!(res.status(), 403);
    assert_eq!(text(res).await, r#"{"detail":"Cross-site request refused"}"#);
    let mut r = req("PUT", addr, "/api/drawing-templates/line/x", "", Some(serde_json::json!({"style": {}})));
    r.headers_mut().insert("sec-fetch-site", "cross-site".parse().unwrap());
    assert_eq!(send(r).await.status(), 403);
    let mut r = req("DELETE", addr, "/api/workspaces/x", "", None);
    r.headers_mut().insert("origin", format!("http://{addr}").parse().unwrap());
    assert_eq!(send(r).await.status(), 200, "same origin");
    assert_eq!(send(req("DELETE", addr, "/api/workspaces/x", "", None)).await.status(), 200, "scripts send no Origin");
    let mut r = req("GET", addr, "/api/workspaces", "", None);
    r.headers_mut().insert("origin", "http://evil.example".parse().unwrap());
    assert_eq!(send(r).await.status(), 200, "reads pass");
}

#[tokio::test]
async fn without_a_database_python_serves_them() {
    let addr = front(listen(python()).await).await;
    let res = send(req("GET", addr, "/api/workspaces", "", None)).await;
    assert_eq!(res.headers()["x-wednesday-server"], "python");
    let got: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!(got["line"], "GET /api/workspaces", "the stand-in Python echoed it");
}

#[tokio::test]
async fn calendar_settings_are_saved_here_and_answered_by_python() {
    let store = temp_store("calendar").await;
    let addr = front_with(listen(python()).await, Some(store.clone())).await;
    let body = serde_json::json!({"currencies": ["usd", "eur"], "url": "https://x.test/c.json"});
    let res = send(req("PUT", addr, "/api/calendar", "wed_session=abc", Some(body))).await;
    assert_eq!(res.headers()["x-wednesday-server"], "rust");
    let got: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!((got["line"].as_str(), got["body"].as_str(), got["cookie"].as_str()),
               (Some("GET /api/calendar"), Some(""), Some("wed_session=abc")), "Python's calendar, read after saving");
    assert_eq!(store.get_setting("calendar").await.unwrap().unwrap()["currencies"], serde_json::json!(["USD", "EUR"]));

    let res = send(req("PUT", addr, "/api/calendar", "", Some(serde_json::json!({"impacts": ["Huge"]})))).await;
    assert_eq!(res.status(), 422);
    assert_eq!(text(res).await, r#"{"detail":"Unknown impact 'Huge'"}"#);
    assert_eq!(store.get_setting("calendar").await.unwrap().unwrap()["impacts"], serde_json::json!(["High"]));

    let res = send(req("GET", addr, "/api/calendar", "", None)).await;
    assert_eq!(res.headers()["x-wednesday-server"], "python", "reading the calendar is still Python's");
}

// ---- the contract (tests/api_contract.json) ----

fn repo(path: &str) -> String {
    std::fs::read_to_string(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(path)).unwrap()
}

#[test]
fn ported_routes_are_in_the_contract() {
    let contract: serde_json::Value = serde_json::from_str(&repo("tests/api_contract.json")).unwrap();
    let routes: Vec<&str> = contract["routes"].as_array().unwrap().iter().map(|r| r.as_str().unwrap()).collect();
    for route in wednesday_rs::PORTED {
        assert!(routes.contains(route), "{route} isn't in tests/api_contract.json");
    }
}

#[tokio::test]
async fn ported_answers_have_the_contract_shape() {
    let contract: serde_json::Value = serde_json::from_str(&repo("tests/api_contract.json")).unwrap();
    let addr = front_with(listen(python()).await, Some(temp_store("contract").await)).await;
    // What the Python contract test puts in the lists: a workspace "Contract", and a template.
    let window = serde_json::json!({"route": "#", "x": 0, "y": 0, "width": 1440, "height": 900, "maximized": true});
    let ws = serde_json::json!({"layout": {"xau.tf": "15M"}, "windows": [window]});
    assert_eq!(send(req("PUT", addr, "/api/workspaces/Contract", "", Some(ws))).await.status(), 200);
    let tpl = serde_json::json!({"style": {"color": "#3b82f6", "width": 1, "dash": "dotted"}, "text": "H1 OB"});
    assert_eq!(send(req("PUT", addr, "/api/drawing-templates/line/H1", "", Some(tpl))).await.status(), 200);
    // A journal's own routes need an imported report and its prices: conformance.rs checks those
    // against the real Python server.
    for route in wednesday_rs::PORTED.iter().filter(|r| r.starts_with("GET ") && !r.contains("{journal_id}") && !r.contains("{snapshot_id}")) {
        let path = route[4..].replace("{name}", "Contract");
        let res = send(req("GET", addr, &path, "", None)).await;
        assert_eq!(res.status(), 200, "{route}");
        let body: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
        let want = &contract["responses"][*route];
        assert!(!want.is_null(), "{route} has no response in the contract");
        let mut problems = Vec::new();
        differences(want, &shape(&body), "", &mut problems);
        assert!(problems.is_empty(), "{route}: {problems:?}");
    }
}

#[tokio::test]
async fn engine_routes_have_the_contract_shape() {
    use wednesday_rs::engine::{DataSettings, Options, Runtime};
    let contract: serde_json::Value = serde_json::from_str(&repo("tests/api_contract.json")).unwrap();
    let routes: Vec<&str> = contract["routes"].as_array().unwrap().iter().map(|r| r.as_str().unwrap()).collect();
    let store = temp_store("engine").await;
    let settings = DataSettings { source: "synthetic".into(), ..Default::default() };
    let rt = Runtime::start(settings, Options { poll: false, delay: 0.0, ..Default::default() }, Some(store.clone())).unwrap();
    // The alerts without the OS credential store (no keychain prompt in a test) or Telegram.
    struct NoKeyring;
    impl wednesday_rs::alerts::secrets::Keyring for NoKeyring {
        fn get(&self, _: &str, _: &str) -> Result<Option<String>, String> { Ok(None) }
        fn set(&self, _: &str, _: &str, _: &str) -> Result<(), String> { Err("none".into()) }
        fn delete(&self, _: &str, _: &str) -> Result<(), String> { Ok(()) }
    }
    let secrets = wednesday_rs::alerts::secrets::Secrets::with(Box::new(NoKeyring), |_| None);
    let alerts = wednesday_rs::alerts::Alerts::new(Some(store.clone()), secrets, std::sync::Arc::new(|_: &str| None),
                                                   "http://127.0.0.1:9", Default::default());
    let _ = rt.alerts.set(std::sync::Arc::new(alerts));
    // The calendar without its fetch task (no network in a test).
    let _ = rt.calendar.set(wednesday_rs::calendar::Calendar::new(Some(store.clone())).await);
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve_with_engine(l, listen(python()).await, Some(store), rt.clone()));
    let start = std::time::Instant::now();
    while rt.engine().state().version == 0 {
        assert!(start.elapsed() < std::time::Duration::from_secs(30), "no scan");
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    for route in wednesday_rs::ENGINE_ROUTES {
        assert!(routes.contains(route), "{route} isn't in tests/api_contract.json");
        // The stream is read below; other markets need a TradingView helper (tests/engine_markets.rs).
        if !route.starts_with("GET ") || route.ends_with("/stream") || route.contains("/markets/")
            || route.ends_with("/clock/feed") || route.ends_with("/api/replay") || route.ends_with("/alerts/chats") {
            continue; // need a query (tests/engine_charts.rs covers them)
        }
        let res = send(req("GET", addr, &route[4..], "", None)).await;
        assert_eq!(res.headers()["x-wednesday-server"], "rust", "{route}");
        let body: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
        let mut problems = Vec::new();
        differences(&contract["responses"][*route], &shape(&body), "", &mut problems);
        assert!(problems.is_empty(), "{route}: {problems:?}");
    }
    let res = send(req("GET", addr, "/api/stream?limit=1", "", None)).await;
    assert_eq!(res.headers()["content-type"], "text/event-stream; charset=utf-8");
    let events = text(res).await;
    assert!(events.starts_with("retry: 2000\n\nevent: tick\ndata: {\"version\": 1, "), "{events}");
    let res = send(req("PUT", addr, "/api/settings", "", Some(serde_json::json!({"source": "nope"})))).await;
    assert_eq!(res.status(), 422);
}

// ---- shapes, as tests/test_api_contract.py makes and compares them ----

fn data_key(k: &str) -> bool {
    let b = k.as_bytes();
    let number = !k.is_empty() && k.trim_start_matches('-').split('.').all(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_digit()))
        && k.matches('.').count() <= 1;
    let date = b.len() >= 10 && b[..4].iter().all(u8::is_ascii_digit) && b[4] == b'-' && b[7] == b'-';
    number || date
}

fn shape(v: &serde_json::Value) -> serde_json::Value {
    use serde_json::Value::*;
    match v {
        Null => "null".into(),
        Bool(_) => "bool".into(),
        Number(_) => "number".into(),
        String(_) => "string".into(),
        Array(items) => {
            let mut it = items.iter().map(shape);
            match it.next() {
                None => Array(vec![]),
                Some(first) => Array(vec![it.fold(first, |a, b| merge(&a, &b))]),
            }
        }
        Object(m) if !m.is_empty() && m.keys().all(|k| data_key(k)) => {
            let merged = m.values().map(shape).reduce(|a, b| merge(&a, &b)).unwrap();
            serde_json::json!({"*": merged})
        }
        Object(m) => {
            let mut keys: Vec<&std::string::String> = m.keys().collect();
            keys.sort();
            Object(keys.into_iter().map(|k| (k.clone(), shape(&m[k]))).collect())
        }
    }
}

fn types(s: &str) -> std::collections::BTreeSet<&str> {
    s.split('|').collect()
}

fn merge(a: &serde_json::Value, b: &serde_json::Value) -> serde_json::Value {
    use serde_json::Value::*;
    if a == b {
        return a.clone();
    }
    match (a, b) {
        (Object(x), Object(y)) => {
            let names: std::collections::BTreeSet<std::string::String> =
                x.keys().chain(y.keys()).map(|k| k.trim_end_matches('?').to_string()).collect();
            fn find<'a>(m: &'a serde_json::Map<std::string::String, serde_json::Value>, n: &str) -> Option<(bool, &'a serde_json::Value)> {
                m.get(n).map(|v| (false, v)).or_else(|| m.get(&format!("{n}?")).map(|v| (true, v)))
            }
            Object(names.into_iter().map(|n| match (find(x, &n), find(y, &n)) {
                (Some((oa, va)), Some((ob, vb))) => (if oa || ob { format!("{n}?") } else { n.clone() }, merge(va, vb)),
                (Some((_, v)), None) | (None, Some((_, v))) => (format!("{n}?"), v.clone()),
                (None, None) => unreachable!(),
            }).collect())
        }
        (Array(x), Array(y)) => match (x.first(), y.first()) {
            (Some(p), Some(q)) => Array(vec![merge(p, q)]),
            _ => if x.is_empty() { b.clone() } else { a.clone() },
        },
        (String(x), _) if x == "null" => b.clone(),
        (_, String(y)) if y == "null" => a.clone(),
        (String(x), String(y)) => {
            let all: std::collections::BTreeSet<&str> = types(x).union(&types(y)).copied().collect();
            all.into_iter().collect::<Vec<_>>().join("|").into()
        }
        _ => "any".into(),
    }
}

/// What differs, ignoring what the clock decides (nulls, empty lists), as the Python test does.
fn differences(e: &serde_json::Value, a: &serde_json::Value, path: &str, out: &mut Vec<String>) {
    use serde_json::Value::*;
    let is = |v: &serde_json::Value, s: &str| v.as_str() == Some(s);
    if e == a || is(e, "null") || is(a, "null") || is(e, "any") || is(a, "any") {
        return;
    }
    match (e, a) {
        (Array(x), Array(y)) => {
            if let (Some(p), Some(q)) = (x.first(), y.first()) {
                differences(p, q, &format!("{path}[]"), out);
            }
        }
        (Object(x), Object(y)) => {
            let strip = |m: &serde_json::Map<std::string::String, serde_json::Value>| -> std::collections::BTreeMap<std::string::String, (bool, serde_json::Value)> {
                m.iter().map(|(k, v)| (k.trim_end_matches('?').to_string(), (k.ends_with('?'), v.clone()))).collect()
            };
            let (ex, ac) = (strip(x), strip(y));
            if let (Some(p), Some(q)) = (ex.get("*"), ac.get("*")) {
                return differences(&p.1, &q.1, &format!("{path}.*"), out);
            }
            let names: std::collections::BTreeSet<&std::string::String> = ex.keys().chain(ac.keys()).collect();
            for n in names {
                match (ex.get(n), ac.get(n)) {
                    (Some((optional, _)), None) => if !optional { out.push(format!("{path}.{n}: missing")) },
                    // A level's `meta` holds what its detector adds, which depends on the bars
                    // (an inducement's `bos_time`): the sample may not have had every detector.
                    (None, Some(_)) if path.ends_with(".meta") => {}
                    (None, Some(_)) => out.push(format!("{path}.{n}: new")),
                    (Some(p), Some(q)) => differences(&p.1, &q.1, &format!("{path}.{n}"), out),
                    (None, None) => {}
                }
            }
        }
        (String(x), String(y)) => {
            let mut tx = types(x);
            let mut ty = types(y);
            tx.insert("null");
            ty.insert("null");
            if tx != ty {
                out.push(format!("{path}: {x} became {y}"));
            }
        }
        _ => out.push(format!("{path}: {e} became {a}")),
    }
}
