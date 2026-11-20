//! Wednesday's server in Rust, step 6 of the move off Python (#96).
//!
//! It listens on the dashboard's address, answers the routes ported to Rust so far ([`PORTED`])
//! and passes every other request, unchanged, to the Python server behind it. The dashboard and
//! the Tauri shell can't tell the difference, so routes move over one at a time and the app works
//! at every step. When the last route is ported, the Python server goes (step 10).
//!
//! What a ported route answers must match `tests/api_contract.json` (step 1), and one that needs
//! the login can only move once the login itself is ported: until then, only routes the Python
//! server leaves open (`auth.PUBLIC`) are served here.

pub mod scan;

use std::net::SocketAddr;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderName, HeaderValue, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::json;

/// The routes this server answers itself, as `"METHOD /path"`; everything else goes to Python.
pub const PORTED: &[&str] = &["GET /api/health"];

/// The version the dashboard and `/api/health` report; `scripts/set_version.py` keeps it in step.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone)]
struct Backend {
    addr: SocketAddr,
    client: Client<HttpConnector, Body>,
}

/// The server: ported routes, then the Python server at `backend` for the rest.
pub fn app(backend: SocketAddr) -> Router {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client = Client::builder(TokioExecutor::new()).build(connector);
    Router::new()
        .route("/api/health", get(health))
        .fallback(forward)
        .with_state(Backend { addr: backend, client })
}

/// Serve on `listener` until the process ends.
pub async fn serve(listener: tokio::net::TcpListener, backend: SocketAddr) -> std::io::Result<()> {
    axum::serve(listener, app(backend)).await
}

/// Serve on a listener the caller already bound (the Tauri shell binds the dashboard's port
/// before the Python server is up, so nothing else takes it meanwhile). Needs a Tokio runtime.
pub async fn serve_std(listener: std::net::TcpListener, backend: SocketAddr) -> std::io::Result<()> {
    listener.set_nonblocking(true)?;
    serve(tokio::net::TcpListener::from_std(listener)?, backend).await
}

/// Liveness for proxies and monitors; the only route open without a login.
async fn health() -> Json<serde_json::Value> {
    Json(json!({"ok": true, "version": VERSION}))
}

// Headers that belong to one connection, not the request (RFC 9110 7.6.1).
const HOP_BY_HOP: &[&str] = &[
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
    "trailer", "transfer-encoding", "upgrade",
];

fn strip_hop_by_hop(headers: &mut axum::http::HeaderMap) {
    // Plus any header the Connection header names.
    let named: Vec<HeaderName> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .filter_map(|n| HeaderName::try_from(n.trim()).ok())
        .collect();
    for name in named {
        headers.remove(name);
    }
    for name in HOP_BY_HOP {
        headers.remove(*name);
    }
}

/// Pass the request to the Python server and stream its answer back (the event stream included).
/// The Host header is kept, so the login's same-origin check sees the address the page came from.
async fn forward(State(backend): State<Backend>, mut req: Request) -> Response {
    let path = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let uri: Uri = match format!("http://{}{}", backend.addr, path).parse() {
        Ok(u) => u,
        Err(_) => return (StatusCode::BAD_REQUEST, "bad request path").into_response(),
    };
    *req.uri_mut() = uri;
    strip_hop_by_hop(req.headers_mut());
    match backend.client.request(req).await {
        Ok(res) => {
            let (mut parts, body) = res.into_parts();
            strip_hop_by_hop(&mut parts.headers);
            Response::from_parts(parts, Body::new(body))
        }
        Err(e) => {
            log::warn!("the Python server didn't answer: {e}");
            let mut res = Json(json!({"detail": "The screener isn't answering. Restart Wednesday if this stays."}))
                .into_response();
            *res.status_mut() = StatusCode::BAD_GATEWAY;
            res.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
            res
        }
    }
}
