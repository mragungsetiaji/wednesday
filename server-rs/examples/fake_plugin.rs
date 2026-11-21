//! A plugin API 2 plugin for the tests (`tests/plugins_host.rs`): the handshake, the token, a
//! licence that unlocks `journal.multi` for keys starting with `WEDK-GOOD`, kept in the server's
//! settings, and a few routes. `fake.json` in its folder changes how it behaves:
//! `{"handshake": false}` never says it listens, `{"api": 1}` claims another API,
//! `{"licence": false}` doesn't handle licences.
//!
//! Not part of Wednesday; `cargo test` builds it.

use std::sync::{Arc, Mutex};

use axum::extract::{Query, Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get};
use axum::{Json, Router};
use serde_json::{json, Value};
use tokio::io::AsyncReadExt;

#[derive(Clone)]
struct Plugin {
    name: String,
    api: i64,
    licence: bool,
    core: String,
    core_token: String,
    features: Arc<Mutex<Vec<String>>>,
    plan: Arc<Mutex<Option<String>>>,
    http: reqwest::Client,
}

impl Plugin {
    fn prefix(&self) -> String {
        format!("{}_", self.name.strip_prefix("wednesday_").unwrap_or(&self.name))
    }

    async fn core(&self, method: reqwest::Method, key: &str, body: Option<Value>) -> (u16, Value) {
        let mut req = self
            .http
            .request(method, format!("{}/api/plugin-host/settings/{key}", self.core))
            .bearer_auth(&self.core_token);
        if let Some(b) = body {
            req = req.header("content-type", "application/json").body(b.to_string());
        }
        match req.send().await {
            Ok(res) => {
                let status = res.status().as_u16();
                (status, serde_json::from_str(&res.text().await.unwrap_or_default()).unwrap_or(Value::Null))
            }
            Err(e) => (0, json!(e.to_string())),
        }
    }

    fn status(&self) -> Value {
        let plan = self.plan.lock().unwrap().clone();
        json!({"valid": plan.is_some(), "plan": plan, "features": *self.features.lock().unwrap()})
    }

    fn set_plan(&self, plan: Option<String>) {
        *self.features.lock().unwrap() = if plan.is_some() { vec!["journal.multi".into()] } else { vec![] };
        *self.plan.lock().unwrap() = plan;
    }
}

#[tokio::main]
async fn main() {
    let var = |k: &str| std::env::var(k).unwrap_or_default();
    let token = var("WEDNESDAY_PLUGIN_TOKEN");
    let config: Value = std::fs::read("fake.json").ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or(json!({}));
    let manifest: Value = serde_json::from_slice(&std::fs::read("plugin.json").unwrap()).unwrap();
    let plugin = Plugin {
        name: manifest["name"].as_str().unwrap().to_string(),
        api: config["api"].as_i64().unwrap_or(2),
        licence: config["licence"].as_bool().unwrap_or(true),
        core: var("WEDNESDAY_CORE_URL"),
        core_token: var("WEDNESDAY_CORE_TOKEN"),
        features: Default::default(),
        plan: Default::default(),
        http: reqwest::Client::new(),
    };
    // Exit when the server goes: its end of standard input closes.
    tokio::spawn(async {
        let mut stdin = tokio::io::stdin();
        let mut buf = [0u8; 64];
        while let Ok(n) = stdin.read(&mut buf).await {
            if n == 0 {
                break;
            }
        }
        std::process::exit(0);
    });
    // A licence kept from an earlier run, in the server's settings.
    let key = format!("{}licence", plugin.prefix());
    if let (200, saved) = plugin.core(reqwest::Method::GET, &key, None).await {
        if let Some(plan) = saved["value"]["plan"].as_str() {
            plugin.set_plan(Some(plan.to_string()));
        }
    }
    if config["handshake"] == json!(false) {
        tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let data = std::path::PathBuf::from(var("WEDNESDAY_DATA_DIR"));
    std::fs::write(data.join("port"), port.to_string()).unwrap();
    std::fs::write(data.join("env.json"), json!({
        "device": var("WEDNESDAY_DEVICE_ID"), "core": var("WEDNESDAY_CORE_URL"),
        "licence_server": std::env::var("WEDNESDAY_LICENCE_SERVER").ok(),
        "telegram": std::env::var("TELEGRAM_BOT_TOKEN").ok(),
        "cwd": std::env::current_dir().unwrap(),
    }).to_string()).unwrap();
    let app = Router::new()
        .route("/plugin", get(about))
        .route("/licence", get(licence_get).put(licence_put).delete(licence_delete))
        .route("/routes/echo", any(echo))
        .route("/routes/settings", get(settings))
        .route("/routes/crash", get(|| async { std::process::exit(3) }))
        .layer(axum::middleware::from_fn_with_state(token, auth))
        .with_state(plugin);
    println!("starting");
    println!("WEDNESDAY-PLUGIN-LISTEN {port}");
    axum::serve(listener, app).await.unwrap();
}

async fn auth(State(token): State<String>, req: Request, next: Next) -> Response {
    let given = req.headers().get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("");
    if given != format!("Bearer {token}") {
        return (StatusCode::UNAUTHORIZED, Json(json!({"detail": "Not authenticated"}))).into_response();
    }
    next.run(req).await
}

async fn about(State(p): State<Plugin>) -> Json<Value> {
    Json(json!({"name": p.name, "version": "0.2.0", "api": p.api, "features": *p.features.lock().unwrap()}))
}

async fn licence_get(State(p): State<Plugin>) -> Response {
    if !p.licence {
        return StatusCode::NOT_FOUND.into_response();
    }
    Json(p.status()).into_response()
}

async fn licence_put(State(p): State<Plugin>, Json(body): Json<Value>) -> Response {
    if !p.licence {
        return StatusCode::NOT_FOUND.into_response();
    }
    let key = body["key"].as_str().unwrap_or("");
    if !key.starts_with("WEDK-GOOD") {
        return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": "That licence key isn't known"}))).into_response();
    }
    p.set_plan(Some("pro".into()));
    let (status, _) = p.core(reqwest::Method::PUT, &format!("{}licence", p.prefix()), Some(json!({"plan": "pro"}))).await;
    assert_eq!(status, 200, "the server kept the licence");
    Json(p.status()).into_response()
}

async fn licence_delete(State(p): State<Plugin>) -> Response {
    if !p.licence {
        return StatusCode::NOT_FOUND.into_response();
    }
    p.set_plan(None);
    p.core(reqwest::Method::DELETE, &format!("{}licence", p.prefix()), None).await;
    Json(p.status()).into_response()
}

async fn echo(req: Request) -> Json<Value> {
    let (parts, body) = req.into_parts();
    let body = axum::body::to_bytes(body, 1 << 20).await.unwrap();
    let header = |n: &str| parts.headers.get(n).and_then(|v| v.to_str().ok()).map(String::from);
    Json(json!({
        "method": parts.method.as_str(), "path": parts.uri.path(), "query": parts.uri.query(),
        "content_type": header("content-type"), "origin": header("origin"), "cookie": header("cookie"),
        "body": String::from_utf8_lossy(&body),
    }))
}

/// Read one of the server's settings through the plugin host (the tests check the prefix rule).
async fn settings(State(p): State<Plugin>, Query(q): Query<std::collections::HashMap<String, String>>) -> Json<Value> {
    let (status, body) = p.core(reqwest::Method::GET, q.get("key").map(String::as_str).unwrap_or(""), None).await;
    Json(json!({"status": status, "body": body}))
}
