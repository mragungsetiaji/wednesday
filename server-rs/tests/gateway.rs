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
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve(l, backend));
    addr
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
    let body: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!(body, serde_json::json!({"ok": true, "version": wednesday_rs::VERSION}));
}

#[tokio::test]
async fn the_rest_reaches_python_unchanged() {
    let addr = front(listen(python()).await).await;
    let req = axum::http::Request::post(format!("http://{addr}/api/bias?x=1&y=two"))
        .header("cookie", "wed_session=abc")
        .header("connection", "keep-alive, x-hop")
        .header("x-hop", "1")
        .body(Body::from(r#"{"bias":"bullish"}"#))
        .unwrap();
    let res = send(req).await;
    assert_eq!(res.status(), 201);
    assert_eq!(res.headers()["set-cookie"], "wed=1; HttpOnly");
    let got: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
    assert_eq!(got["line"], "POST /api/bias?x=1&y=two");
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

// ---- the contract (tests/api_contract.json, src/wednesday/auth.py) ----

fn repo(path: &str) -> String {
    std::fs::read_to_string(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(path)).unwrap()
}

#[test]
fn ported_routes_are_in_the_contract_and_open_without_the_login() {
    let contract: serde_json::Value = serde_json::from_str(&repo("tests/api_contract.json")).unwrap();
    let routes: Vec<&str> = contract["routes"].as_array().unwrap().iter().map(|r| r.as_str().unwrap()).collect();
    let auth = repo("src/wednesday/auth.py");
    let public = auth.lines().find(|l| l.starts_with("PUBLIC = ")).expect("auth.PUBLIC");
    for route in wednesday_rs::PORTED {
        assert!(routes.contains(route), "{route} isn't in tests/api_contract.json");
        let path = route.split_once(' ').unwrap().1;
        // The login is still Python's: a route behind it can't move until the login does.
        assert!(public.contains(&format!("\"{path}\"")), "{route} needs the login, which isn't ported yet");
    }
}

#[tokio::test]
async fn ported_answers_have_the_contract_shape() {
    let contract: serde_json::Value = serde_json::from_str(&repo("tests/api_contract.json")).unwrap();
    let addr = front(listen(python()).await).await;
    for route in wednesday_rs::PORTED.iter().filter(|r| r.starts_with("GET ")) {
        let path = &route[4..];
        let res = send(axum::http::Request::get(format!("http://{addr}{path}")).body(Body::empty()).unwrap()).await;
        let body: serde_json::Value = serde_json::from_str(&text(res).await).unwrap();
        assert_eq!(shape(&body), contract["responses"][*route], "{route}");
    }
}

/// The contract's shape of a value (see `shape` in tests/test_api_contract.py), for flat answers.
fn shape(v: &serde_json::Value) -> serde_json::Value {
    use serde_json::Value::*;
    match v {
        Null => "null".into(),
        Bool(_) => "bool".into(),
        Number(_) => "number".into(),
        String(_) => "string".into(),
        Array(items) => Array(items.first().map(shape).into_iter().collect()),
        Object(m) => Object(m.iter().map(|(k, v)| (k.clone(), shape(v))).collect()),
    }
}
