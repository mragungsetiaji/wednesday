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

/// `wednesday_guest=<token>` from a Set-Cookie header.
fn guest(a: &Answer) -> String {
    let c = a.cookie.as_deref().unwrap_or_else(|| panic!("no guest cookie: {} {}", a.status, a.body));
    assert!(c.starts_with("wednesday_guest="), "{c}");
    c.split(';').next().unwrap().to_string()
}

async fn log_in_as(addr: SocketAddr, username: &str, password: &str) -> Answer {
    call(addr, "POST", "/api/auth/login", "", Some(json!({"username": username, "password": password}))).await
}

#[tokio::test]
async fn a_preview_reads_the_market_and_keeps_nothing_on_the_server() {
    let (addr, auth) = server("guest", &[]).await;
    auth.create_user("trader", "the password").await.unwrap();
    let status = call(addr, "GET", "/api/auth/status", "", None).await.body;
    assert_eq!(status["guest"], Value::Null);
    assert_eq!(status["guest_available"], json!(true));

    let started = call(addr, "POST", "/api/auth/guest", "", None).await;
    assert_eq!(started.status, 200, "{}", started.body);
    let pass = guest(&started);
    let until = started.body["guest"]["until"].clone();
    assert!(until.is_string(), "{}", started.body);
    // Asked again from the same address, the same pass: clearing the cookie doesn't restart it.
    let again = call(addr, "POST", "/api/auth/guest", "", None).await;
    assert_eq!(again.status, 200);
    assert_eq!(again.body["guest"]["until"], until);

    assert_eq!(call(addr, "GET", "/api/bias", &pass, None).await.status, 200);
    assert_eq!(call(addr, "GET", "/api/drawings", &pass, None).await.status, 200);
    for (method, path) in [("PUT", "/api/bias"), ("PUT", "/api/settings"), ("PUT", "/api/drawings/abcd1234"),
                           ("POST", "/api/journals"), ("GET", "/api/users")] {
        let a = call(addr, method, path, &pass, Some(json!({}))).await;
        assert_eq!(a.status, 403, "{method} {path}: {}", a.body);
        assert_eq!(a.body, json!({"detail": "Log in or sign up for this"}), "{method} {path}");
    }
    assert_eq!(call(addr, "GET", "/api/bias", "wednesday_guest=forged", None).await.status, 401);
}

#[tokio::test]
async fn a_sign_up_waits_for_the_admin() {
    let (addr, auth) = server("signup", &[]).await;
    let early = call(addr, "POST", "/api/auth/signup", "", Some(json!({"username": "ana", "password": "ana password"}))).await;
    assert_eq!(early.status, 409, "no account yet: the setup code makes the first one");
    auth.create_user("trader", "the password").await.unwrap();
    let admin = session(&log_in(addr, "the password").await);

    let up = call(addr, "POST", "/api/auth/signup", "",
                  Some(json!({"username": "ana", "password": "ana password", "email": "ana@example.com"}))).await;
    assert_eq!(up.status, 200, "{}", up.body);
    assert_eq!(up.cookie, None, "a sign-up doesn't log in");
    let taken = call(addr, "POST", "/api/auth/signup", "", Some(json!({"username": "ana", "password": "other password"}))).await;
    assert_eq!(taken.status, 409);

    let waiting = log_in_as(addr, "ana", "ana password").await;
    assert_eq!(waiting.status, 403);
    assert!(waiting.body["detail"].as_str().unwrap().contains("approval"), "{}", waiting.body);

    let users = call(addr, "GET", "/api/users", &admin, None).await.body["users"].clone();
    let ana = users.as_array().unwrap().iter().find(|u| u["username"] == json!("ana")).unwrap().clone();
    assert_eq!(ana["role"], json!("pending"));
    assert_eq!(ana["email"], json!("ana@example.com"));
    let id = ana["id"].as_str().unwrap();
    let ok = call(addr, "PATCH", &format!("/api/users/{id}"), &admin, Some(json!({"role": "user"}))).await;
    assert_eq!(ok.status, 200, "{}", ok.body);

    let ana_in = log_in_as(addr, "ana", "ana password").await;
    assert_eq!(ana_in.status, 200, "{}", ana_in.body);
    let ana_cookie = session(&ana_in);
    assert_eq!(call(addr, "GET", "/api/bias", &ana_cookie, None).await.status, 200);
    for (method, path) in [("PUT", "/api/bias"), ("GET", "/api/users"), ("PUT", "/api/settings")] {
        let a = call(addr, method, path, &ana_cookie, Some(json!({}))).await;
        assert_eq!(a.status, 403, "{method} {path}: {}", a.body);
        assert_eq!(a.body, json!({"detail": "Only the admin can change this"}));
    }

    // Disabled, the account is logged out and can't log in.
    let off = call(addr, "PATCH", &format!("/api/users/{id}"), &admin, Some(json!({"disabled": true}))).await;
    assert_eq!(off.status, 200, "{}", off.body);
    assert_eq!(call(addr, "GET", "/api/bias", &ana_cookie, None).await.status, 401);
    assert_ne!(log_in_as(addr, "ana", "ana password").await.status, 200);
}

#[tokio::test]
async fn the_last_admin_stays_an_admin() {
    let (addr, auth) = server("last-admin", &[]).await;
    auth.create_user("trader", "the password").await.unwrap();
    let admin = session(&log_in(addr, "the password").await);
    let users = call(addr, "GET", "/api/users", &admin, None).await.body["users"].clone();
    let me = users[0]["id"].as_str().unwrap().to_string();
    for body in [json!({"role": "user"}), json!({"disabled": true})] {
        let a = call(addr, "PATCH", &format!("/api/users/{me}"), &admin, Some(body.clone())).await;
        assert!(a.status == 400 || a.status == 409 || a.status == 422, "{body}: {} {}", a.status, a.body);
    }
    assert_ne!(call(addr, "DELETE", &format!("/api/users/{me}"), &admin, None).await.status, 200);
    assert_eq!(call(addr, "GET", "/api/bias", &admin, None).await.status, 200);
}

#[tokio::test]
async fn each_account_keeps_its_own_drawings_journals_and_preferences() {
    let (addr, auth) = server("mine", &[]).await;
    auth.create_user("trader", "the password").await.unwrap();
    let admin = session(&log_in(addr, "the password").await);
    call(addr, "POST", "/api/auth/signup", "", Some(json!({"username": "ana", "password": "ana password"}))).await;
    let users = call(addr, "GET", "/api/users", &admin, None).await.body["users"].clone();
    let id = users.as_array().unwrap().iter().find(|u| u["username"] == json!("ana")).unwrap()["id"]
        .as_str().unwrap().to_string();
    call(addr, "PATCH", &format!("/api/users/{id}"), &admin, Some(json!({"role": "user"}))).await;
    let ana = session(&log_in_as(addr, "ana", "ana password").await);

    let line = json!({"kind": "hline", "points": [{"t": 1_700_000_000, "p": 2000.0}]});
    let put = call(addr, "PUT", "/api/drawings/admin-0001", &admin, Some(line.clone())).await;
    assert_eq!(put.status, 200, "{}", put.body);
    let ids = |v: &Value| v["drawings"].as_array().unwrap().iter().map(|d| d["id"].as_str().unwrap().to_string()).collect::<Vec<_>>();
    assert_eq!(ids(&call(addr, "GET", "/api/drawings", &ana, None).await.body), Vec::<String>::new());
    // Someone else's drawing can't be overwritten or deleted.
    assert_ne!(call(addr, "PUT", "/api/drawings/admin-0001", &ana, Some(line.clone())).await.status, 200);
    assert_ne!(call(addr, "DELETE", "/api/drawings/admin-0001", &ana, None).await.status, 200);
    assert_eq!(ids(&call(addr, "GET", "/api/drawings", &admin, None).await.body), vec!["admin-0001"]);

    let journal = call(addr, "POST", "/api/journals", &admin, Some(json!({"name": "Main"}))).await;
    assert_eq!(journal.status, 200, "{}", journal.body);
    let jid = journal.body["id"].as_str().unwrap().to_string();
    assert_eq!(call(addr, "GET", "/api/journals", &ana, None).await.body["journals"], json!([]));
    assert_eq!(call(addr, "GET", &format!("/api/journals/{jid}/stats"), &ana, None).await.status, 404);

    assert_eq!(call(addr, "PUT", "/api/prefs", &ana, Some(json!({"other": 1}))).await.status, 422);
    let prefs = call(addr, "PUT", "/api/prefs", &ana, Some(json!({"wed.tf": "H1"}))).await;
    assert_eq!(prefs.status, 200, "{}", prefs.body);
    assert_eq!(call(addr, "GET", "/api/prefs", &ana, None).await.body["prefs"], json!({"wed.tf": "H1"}));
    assert_eq!(call(addr, "GET", "/api/prefs", &admin, None).await.body["prefs"], json!({}));

    // What a preview kept goes into the account at login; another account's drawing id is skipped.
    let import = call(addr, "POST", "/api/me/import", &ana, Some(json!({
        "drawings": [{"drawing": {"id": "ana-00001", "kind": "hline", "points": [{"t": 1_700_000_000, "p": 2010.0}]}},
                     {"drawing": {"id": "admin-0001", "kind": "hline", "points": []}}],
        "templates": [{"family": "line", "name": "H1 OB", "style": {"color": "#ff0000"}}],
        "workspaces": [{"name": "London", "layout": {}, "windows": []}],
        "prefs": {"wed.tf": "M15", "wed.theme": "dark"},
    }))).await;
    assert_eq!(import.status, 200, "{}", import.body);
    assert_eq!(import.body["drawings"], json!(1));
    assert_eq!(import.body["templates"], json!(1));
    assert_eq!(import.body["workspaces"], json!(1));
    assert_eq!(ids(&call(addr, "GET", "/api/drawings", &ana, None).await.body), vec!["ana-00001"]);
    assert_eq!(ids(&call(addr, "GET", "/api/drawings", &admin, None).await.body), vec!["admin-0001"]);
    let mine = call(addr, "GET", "/api/prefs", &ana, None).await.body["prefs"].clone();
    assert_eq!(mine["wed.tf"], json!("H1"), "the account's own preference stays");
    assert_eq!(mine["wed.theme"], json!("dark"));
    assert_eq!(call(addr, "GET", "/api/workspaces", &admin, None).await.body["workspaces"], json!([]));
}
