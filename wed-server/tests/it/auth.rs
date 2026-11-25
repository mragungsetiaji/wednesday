//! The login (`auth.rs`, Python's `auth.py`) through the standalone server: which routes need a
//! session, the cookie, devices, a password change and the account from `XAU_AUTH_PASSWORD`.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Body;
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};
use wednesday_rs::auth::{Auth, PUBLIC};
use wednesday_rs::engine::{DataSettings, Options, Runtime};
use wednesday_rs::store::Store;

const CHROME: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36";

async fn server(name: &str, env: &[(&str, &str)]) -> (SocketAddr, Arc<Auth>) {
    let dir = std::env::temp_dir().join(format!("wed-auth-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let store = Store::open("sqlite:///data/xau.db", &dir).await.unwrap();
    let settings = DataSettings { source: "synthetic".into(), ..Default::default() };
    let runtime = Runtime::start(settings, Options { poll: false, delay: 0.0, ..Default::default() }, Some(store.clone()))
        .unwrap();
    let auth = Arc::new(Auth::new(Some(store.clone())).unwrap());
    let env: Vec<(String, String)> = env.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    auth.start(&|k| env.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone())).await.unwrap();
    let app = wednesday_rs::standalone_with(dir.join("ui"), Some(store), runtime, auth.clone());
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(l, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap()
    });
    (addr, auth)
}

struct Answer {
    status: u16,
    cookie: Option<String>,
    body: Value,
}

async fn call(addr: SocketAddr, method: &str, path: &str, cookie: &str, body: Option<Value>) -> Answer {
    let mut r = axum::http::Request::builder().method(method).uri(format!("http://{addr}{path}"))
        .header("user-agent", CHROME);
    if !cookie.is_empty() {
        r = r.header("cookie", cookie);
    }
    let req = match body {
        Some(b) => r.header("content-type", "application/json").body(Body::from(b.to_string())).unwrap(),
        None => r.body(Body::empty()).unwrap(),
    };
    let res = Client::builder(TokioExecutor::new()).build_http().request(req).await.unwrap();
    let status = res.status().as_u16();
    let cookie = res.headers().get("set-cookie").map(|v| v.to_str().unwrap().to_string());
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    Answer { status, cookie, body }
}

/// `wednesday_session=<token>` from a Set-Cookie header.
fn session(a: &Answer) -> String {
    a.cookie.as_deref().unwrap_or_else(|| panic!("no cookie: {} {}", a.status, a.body)).split(';').next().unwrap().to_string()
}

async fn log_in(addr: SocketAddr, password: &str) -> Answer {
    call(addr, "POST", "/api/auth/login", "", Some(json!({"username": "trader", "password": password}))).await
}

#[tokio::test]
async fn every_api_route_but_the_public_ones_needs_a_session() {
    let (addr, _) = server("walk", &[]).await;
    for route in wednesday_rs::PORTED {
        let (method, path) = route.split_once(' ').unwrap();
        let path: String = path.split('/')
            .map(|part| if part.starts_with('{') { "x" } else { part })
            .collect::<Vec<_>>().join("/");
        let a = call(addr, method, &path, "", None).await;
        if PUBLIC.contains(&path.as_str()) {
            assert_ne!(a.status, 401, "{route} is public");
        } else {
            assert_eq!(a.status, 401, "{route} needs a session");
            assert_eq!(a.body, json!({"detail": "Log in first"}), "{route}");
        }
    }
    // The dashboard itself loads, to show the login form.
    assert_ne!(call(addr, "GET", "/", "", None).await.status, 401);
}

#[tokio::test]
async fn a_login_sets_a_cookie_that_opens_the_api() {
    let (addr, auth) = server("login", &[]).await;
    let status = call(addr, "GET", "/api/auth/status", "", None).await.body;
    assert_eq!(status["login"], json!(true));
    assert_eq!(status["logged_in"], json!(false));
    assert_eq!(status["setup"], json!(true), "no account yet, so a setup code was logged");

    assert!(auth.create_user("trader", "correct horse").await.unwrap());
    assert_eq!(log_in(addr, "wrong password").await.status, 401);
    let ok = log_in(addr, "correct horse").await;
    assert_eq!(ok.status, 200, "{}", ok.body);
    let set = ok.cookie.clone().unwrap();
    assert!(set.contains("HttpOnly") && set.to_lowercase().contains("samesite=strict"), "{set}");
    assert!(!set.contains("Secure"), "plain http on loopback: {set}");
    let cookie = session(&ok);

    let status = call(addr, "GET", "/api/auth/status", &cookie, None).await.body;
    assert_eq!(status["logged_in"], json!(true));
    assert_eq!(status["setup"], json!(false));
    assert_eq!(status["user"]["username"], json!("trader"));
    assert_eq!(call(addr, "GET", "/api/bias", &cookie, None).await.status, 200);

    let devices = call(addr, "GET", "/api/auth/sessions", &cookie, None).await.body["sessions"].clone();
    assert_eq!(devices.as_array().unwrap().len(), 1);
    assert_eq!(devices[0]["device"], json!("Chrome on Windows"));
    assert_eq!(devices[0]["current"], json!(true));

    assert_eq!(call(addr, "POST", "/api/auth/logout", &cookie, None).await.status, 200);
    assert_eq!(call(addr, "GET", "/api/bias", &cookie, None).await.status, 401);
}

#[tokio::test]
async fn a_password_change_logs_the_other_devices_out() {
    let (addr, auth) = server("password", &[]).await;
    auth.create_user("trader", "first password").await.unwrap();
    let pc = session(&log_in(addr, "first password").await);
    let laptop = session(&log_in(addr, "first password").await);
    let devices = call(addr, "GET", "/api/auth/sessions", &pc, None).await.body["sessions"].clone();
    assert_eq!(devices.as_array().unwrap().len(), 2);

    let wrong = call(addr, "PUT", "/api/auth/password", &pc,
                     Some(json!({"current": "not it", "new": "second password"}))).await;
    assert_eq!(wrong.status, 403);
    let changed = call(addr, "PUT", "/api/auth/password", &pc,
                       Some(json!({"current": "first password", "new": "second password"}))).await;
    assert_eq!(changed.status, 200, "{}", changed.body);
    assert_eq!(changed.body["sessions"].as_array().unwrap().len(), 1);
    assert_eq!(call(addr, "GET", "/api/bias", &pc, None).await.status, 200);
    assert_eq!(call(addr, "GET", "/api/bias", &laptop, None).await.status, 401);
    assert_eq!(log_in(addr, "first password").await.status, 401);
    assert_eq!(log_in(addr, "second password").await.status, 200);
}

#[tokio::test]
async fn another_device_can_be_logged_out() {
    let (addr, auth) = server("devices", &[]).await;
    auth.create_user("trader", "the password").await.unwrap();
    let pc = session(&log_in(addr, "the password").await);
    let laptop = session(&log_in(addr, "the password").await);
    let devices = call(addr, "GET", "/api/auth/sessions", &pc, None).await.body["sessions"].clone();
    let other = devices.as_array().unwrap().iter().find(|d| d["current"] == json!(false)).unwrap();
    let id = other["id"].as_str().unwrap();
    assert_eq!(call(addr, "DELETE", &format!("/api/auth/sessions/{id}"), &pc, None).await.status, 200);
    assert_eq!(call(addr, "DELETE", &format!("/api/auth/sessions/{id}"), &pc, None).await.status, 404);
    assert_eq!(call(addr, "GET", "/api/bias", &laptop, None).await.status, 401);
    assert_eq!(call(addr, "GET", "/api/bias", &pc, None).await.status, 200);
}

#[tokio::test]
async fn the_account_comes_from_the_password_in_the_environment() {
    let (addr, _) = server("env", &[("XAU_AUTH_PASSWORD", "from the env file")]).await;
    let status = call(addr, "GET", "/api/auth/status", "", None).await.body;
    assert_eq!(status["setup"], json!(false));
    let ok = call(addr, "POST", "/api/auth/login", "",
                  Some(json!({"username": "admin", "password": "from the env file"}))).await;
    assert_eq!(ok.status, 200, "{}", ok.body);
}

#[tokio::test]
async fn a_cross_site_login_is_refused() {
    let (addr, auth) = server("cross", &[]).await;
    auth.create_user("trader", "the password").await.unwrap();
    let req = axum::http::Request::builder().method("POST").uri(format!("http://{addr}/api/auth/login"))
        .header("origin", "https://evil.example").header("sec-fetch-site", "cross-site")
        .header("content-type", "application/json")
        .body(Body::from(json!({"username": "trader", "password": "the password"}).to_string())).unwrap();
    let res = Client::builder(TokioExecutor::new()).build_http().request(req).await.unwrap();
    assert_eq!(res.status().as_u16(), 403);
}
