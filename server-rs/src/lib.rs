//! Wednesday's server in Rust, in front of the Python one while the backend moves over (#96).
//!
//! It listens on the dashboard's address, answers the routes ported to Rust so far ([`PORTED`])
//! and passes every other request, unchanged, to the Python server behind it. The dashboard and
//! the Tauri shell can't tell the difference, so routes move over one at a time and the app works
//! at every step. When the last route is ported, the Python server goes (step 10).
//!
//! What a ported route answers must match `tests/api_contract.json` (step 1). There is no login
//! (the dashboard runs on the trader's machine); cross-site writes are refused as `same_site.py`
//! refuses them, so another web page in the browser can't drive the dashboard.
//!
//! Routes that keep their data in the database ([`store`]) are served here only when this server
//! could open the same database as Python; otherwise they go to Python like the rest.

pub mod calendar;
pub mod config;
pub mod journal;
pub mod pyjson;
pub mod scan;
pub mod settings_routes;
pub mod store;

use std::net::SocketAddr;

use axum::body::Body;
use axum::extract::{Path, Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Map, Value};

use settings_routes::Refused;
use store::Store;

/// The routes this server answers itself, as `"METHOD /path"`; everything else goes to Python.
pub const PORTED: &[&str] = &[
    "GET /api/health",
    "GET /api/workspaces",
    "GET /api/workspaces/{name}",
    "PUT /api/workspaces/{name}",
    "DELETE /api/workspaces/{name}",
    "GET /api/drawing-templates",
    "PUT /api/drawing-templates/{family}/{name}",
    "DELETE /api/drawing-templates/{family}/{name}",
    "PUT /api/calendar",
    "GET /api/journals",
    "POST /api/journals",
    "GET /api/journals/{journal_id}",
    "PATCH /api/journals/{journal_id}",
    "DELETE /api/journals/{journal_id}",
    "POST /api/journals/{journal_id}/import",
    "PUT /api/journals/{journal_id}/trades/{trade_id}/note",
    "POST /api/journals/{journal_id}/trades/{trade_id}/images",
    "DELETE /api/journals/{journal_id}/trades/{trade_id}/images/{snapshot_id}",
    "GET /api/journals/{journal_id}/entries",
    "PATCH /api/journals/{journal_id}/entries/{entry_id}",
    "DELETE /api/journals/{journal_id}/entries/{entry_id}",
    "GET /api/journals/{journal_id}/trades.csv",
];

/// The version the dashboard and `/api/health` report; `scripts/set_version.py` keeps it in step.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone)]
struct App {
    backend: SocketAddr,
    client: Client<HttpConnector, Body>,
    store: Option<Store>,
    journals: journal::service::Cache,
}

/// The server: ported routes, then the Python server at `backend` for the rest. Without a
/// `store`, the routes that need the database go to Python too.
pub fn app(backend: SocketAddr, store: Option<Store>) -> Router {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client = Client::builder(TokioExecutor::new()).build(connector);
    Router::new()
        .route("/api/health", get(health))
        .route("/api/workspaces", get(workspaces_list))
        .route("/api/workspaces/{name}", get(workspace_get).put(workspace_put).delete(workspace_delete))
        .route("/api/drawing-templates", get(templates_list))
        .route("/api/drawing-templates/{family}/{name}", axum::routing::put(template_put).delete(template_delete))
        .route("/api/calendar", axum::routing::put(calendar_put).fallback(forward))
        .route("/api/journals", get(journals_list).post(journal_create))
        .route("/api/journals/{id}", get(journal_stats).patch(journal_update).delete(journal_delete))
        .route("/api/journals/{id}/import", axum::routing::post(journal_import))
        .route("/api/journals/{id}/trades.csv", get(journal_csv))
        .route("/api/journals/{id}/trades/{trade}/note", axum::routing::put(journal_note))
        .route("/api/journals/{id}/trades/{trade}/images", axum::routing::post(journal_image_attach))
        .route("/api/journals/{id}/trades/{trade}/images/{sid}", axum::routing::delete(journal_image_detach))
        // Writing an entry freezes the market of the moment, which the Python engine still holds.
        .route("/api/journals/{id}/entries", get(journal_entries).fallback(forward))
        .route("/api/journals/{id}/entries/{entry}", axum::routing::patch(journal_entry_update).delete(journal_entry_delete))
        .route_layer(axum::middleware::map_response(served_here))
        .fallback(forward)
        .with_state(App { backend, client, store, journals: Default::default() })
}

/// Which server answered, for anyone debugging the move: `x-wednesday-server: rust` or `python`.
const SERVED_BY: &str = "x-wednesday-server";

async fn served_here(mut res: Response) -> Response {
    if !res.headers().contains_key(SERVED_BY) {
        res.headers_mut().insert(SERVED_BY, HeaderValue::from_static("rust"));
    }
    res
}

/// Serve on `listener` until the process ends.
pub async fn serve(listener: tokio::net::TcpListener, backend: SocketAddr, store: Option<Store>) -> std::io::Result<()> {
    axum::serve(listener, app(backend, store)).await
}

/// Serve on a listener the caller already bound (the Tauri shell binds the dashboard's port
/// before the Python server is up, so nothing else takes it meanwhile). Needs a Tokio runtime.
pub async fn serve_std(listener: std::net::TcpListener, backend: SocketAddr, store: Option<Store>) -> std::io::Result<()> {
    listener.set_nonblocking(true)?;
    serve(tokio::net::TcpListener::from_std(listener)?, backend, store).await
}

/// The database the Python server in `home` uses, opened; None (with a log line) when there is
/// none (`XAU_DB_URL=none`) or it can't be opened, and then Python serves those routes.
pub async fn open_store(home: &std::path::Path) -> Option<Store> {
    let url = config::database_url(home)?;
    match Store::open(&url, home).await {
        Ok(s) => Some(s),
        Err(e) => {
            log::warn!("the Rust server can't use the database, Python keeps serving it: {e}");
            None
        }
    }
}

// ---- cross-site writes (same_site.py) ----

const UNSAFE: [Method; 4] = [Method::POST, Method::PUT, Method::PATCH, Method::DELETE];

/// A state-changing browser request that came from another site (`same_site.cross_site`).
pub fn cross_site(method: &Method, headers: &HeaderMap) -> bool {
    if !UNSAFE.contains(method) {
        return false;
    }
    let get = |n: &str| headers.get(n).and_then(|v| v.to_str().ok());
    if let Some(origin) = get("origin") {
        let host = get("x-forwarded-host").or(get("host")).unwrap_or("");
        let host = host.split(',').next().unwrap_or("").trim().to_lowercase();
        // urlsplit(origin).netloc: after "//", up to the path, query or fragment
        let netloc = origin
            .split_once("//")
            .map(|(_, rest)| rest.split(['/', '?', '#']).next().unwrap_or(""))
            .unwrap_or("");
        return origin == "null" || netloc.to_lowercase() != host;
    }
    matches!(get("sec-fetch-site"), Some("cross-site") | Some("same-site"))
}

fn detail(status: StatusCode, detail: impl Into<Value>) -> Response {
    let mut res = Json(json!({"detail": detail.into()})).into_response();
    *res.status_mut() = status;
    res
}

impl App {
    /// None when the request may go on, else the 403 `same_site.py` gives a cross-site write.
    fn guard(&self, method: &Method, headers: &HeaderMap) -> Option<Response> {
        cross_site(method, headers).then(|| detail(StatusCode::FORBIDDEN, "Cross-site request refused"))
    }

    /// The store and a cleared request, or the answer to send instead (Python's, without a store).
    async fn admit(&self, req: Request) -> Result<(Store, Request), Response> {
        let Some(store) = self.store.clone() else { return Err(forward(State(self.clone()), req).await) };
        match self.guard(req.method(), req.headers()) {
            Some(refused) => Err(refused),
            None => Ok((store, req)),
        }
    }
}

/// The request's JSON object body (FastAPI's `body: dict = Body(...)`), or its 422.
async fn object_body(req: Request) -> Result<Map<String, Value>, Response> {
    let bytes = axum::body::to_bytes(req.into_body(), 8 * 1024 * 1024)
        .await
        .map_err(|_| detail(StatusCode::PAYLOAD_TOO_LARGE, "The request is too large"))?;
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(Value::Object(m)) => Ok(m),
        Ok(_) => Err(detail(StatusCode::UNPROCESSABLE_ENTITY,
                            json!([{"type": "dict_type", "loc": ["body"], "msg": "Input should be a valid dictionary"}]))),
        Err(_) => Err(detail(StatusCode::UNPROCESSABLE_ENTITY,
                             json!([{"type": "json_invalid", "loc": ["body"], "msg": "JSON decode error"}]))),
    }
}

fn answer(result: Result<Value, Refused>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(Refused::Invalid(msg)) => detail(StatusCode::UNPROCESSABLE_ENTITY, msg),
        Err(Refused::Store(e)) => {
            log::error!("database: {e}");
            detail(StatusCode::INTERNAL_SERVER_ERROR,
                   "The database couldn't be read or written. Try again, and restart Wednesday if this stays.")
        }
    }
}

// ---- workspaces and drawing templates (settings_routes.rs) ----

async fn workspaces_list(State(app): State<App>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => answer(settings_routes::workspaces_list(&store).await),
        Err(res) => res,
    }
}

async fn workspace_get(State(app): State<App>, Path(name): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => match settings_routes::workspace_get(&store, &name).await {
            Ok(Some(w)) => Json(w).into_response(),
            Ok(None) => detail(StatusCode::NOT_FOUND, "No such workspace"),
            Err(e) => answer(Err(e)),
        },
        Err(res) => res,
    }
}

async fn workspace_put(State(app): State<App>, Path(name): Path<String>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => answer(settings_routes::workspace_save(&store, &name, &body).await),
        Err(res) => res,
    }
}

async fn workspace_delete(State(app): State<App>, Path(name): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => answer(settings_routes::workspace_delete(&store, &name).await.map(|d| json!({"deleted": d}))),
        Err(res) => res,
    }
}

async fn templates_list(State(app): State<App>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => answer(settings_routes::templates_list(&store).await),
        Err(res) => res,
    }
}

async fn template_put(State(app): State<App>, Path((family, name)): Path<(String, String)>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => answer(settings_routes::template_save(&store, &family, &name, &body).await),
        Err(res) => res,
    }
}

async fn template_delete(State(app): State<App>, Path((family, name)): Path<(String, String)>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => match settings_routes::template_delete(&store, &family, &name).await {
            Ok(true) => Json(json!({"deleted": name})).into_response(),
            Ok(false) => detail(StatusCode::NOT_FOUND, "No such template"),
            Err(e) => answer(Err(e)),
        },
        Err(res) => res,
    }
}

/// Liveness for proxies and monitors; the only route open without a login.
// ---- calendar settings (calendar.rs) ----

/// Check and save the settings here, then answer with Python's calendar, which reads them from
/// the same database: the events, the chart times and the past reactions are still Python's.
async fn calendar_put(State(app): State<App>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    let mut headers = req.headers().clone();
    let body = match object_body(req).await {
        Ok(body) => body,
        Err(res) => return res,
    };
    if let Err(e) = calendar::save(&store, &body).await {
        return answer(Err(e));
    }
    headers.remove(header::CONTENT_LENGTH);
    headers.remove(header::CONTENT_TYPE);
    let mut get = Request::new(Body::empty());
    *get.uri_mut() = Uri::from_static("/api/calendar");
    *get.headers_mut() = headers;
    let mut res = forward(State(app), get).await;
    if res.status().is_success() {
        res.headers_mut().insert(SERVED_BY, HeaderValue::from_static("rust"));
    }
    res
}

// ---- the journal (journal/service.rs) ----

/// A journal answer: the value, or Python's status and detail.
fn journal_answer(result: Result<Value, journal::service::Fail>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(journal::service::Fail::Status(code, msg)) => {
            detail(StatusCode::from_u16(code).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY), msg)
        }
        Err(journal::service::Fail::Store(e)) => answer(Err(Refused::Store(e))),
    }
}

/// The features the plugins provide, as Python reports them (a second journal needs one).
async fn plugin_features(app: &App, headers: &HeaderMap) -> Result<Vec<String>, Response> {
    let mut req = Request::new(Body::empty());
    *req.uri_mut() = Uri::from_static("/api/plugins");
    for name in [header::HOST, header::USER_AGENT] {
        if let Some(v) = headers.get(&name) {
            req.headers_mut().insert(name, v.clone());
        }
    }
    let res = forward(State(app.clone()), req).await;
    if !res.status().is_success() {
        return Err(res);
    }
    let bytes = axum::body::to_bytes(res.into_body(), 4 * 1024 * 1024)
        .await
        .map_err(|_| detail(StatusCode::BAD_GATEWAY, "The screener isn't answering. Restart Wednesday if this stays."))?;
    let v: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    Ok(v["features"].as_array().map(|a| a.iter().filter_map(|f| f.as_str().map(String::from)).collect()).unwrap_or_default())
}

async fn journals_list(State(app): State<App>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    let features = match plugin_features(&app, req.headers()).await {
        Ok(f) => f,
        Err(res) => return res,
    };
    journal_answer(journal::service::list(&store, &features).await)
}

async fn journal_create(State(app): State<App>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    let features = match plugin_features(&app, req.headers()).await {
        Ok(f) => f,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => journal_answer(journal::service::create(&store, &body, &features).await),
        Err(res) => res,
    }
}

async fn journal_stats(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => journal_answer(journal::service::stats(&store, &app.journals, &id).await),
        Err(res) => res,
    }
}

async fn journal_update(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => journal_answer(journal::service::update(&store, &app.journals, &id, &body).await),
        Err(res) => res,
    }
}

async fn journal_delete(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => journal_answer(journal::service::delete(&store, &app.journals, &id).await),
        Err(res) => res,
    }
}

async fn journal_import(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    // The journal and whether it takes a report, before reading a large body.
    match journal::service::get(&store, &id).await {
        Ok(j) => {
            if let Err(e) = journal::service::check_writable(&j) {
                return journal_answer(Err(e));
            }
        }
        Err(e) => return journal_answer(Err(e)),
    }
    let data = match axum::body::to_bytes(req.into_body(), journal::service::MAX_REPORT_BYTES).await {
        Ok(d) => d,
        Err(_) => return detail(StatusCode::PAYLOAD_TOO_LARGE, "The report is larger than 50 MB"),
    };
    journal_answer(journal::service::import_report(&store, &app.journals, &id, &data).await)
}

async fn journal_note(State(app): State<App>, Path((id, trade)): Path<(String, String)>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => journal_answer(journal::service::set_note(&store, &app.journals, &id, &trade, &body).await),
        Err(res) => res,
    }
}

async fn journal_image_attach(State(app): State<App>, Path((id, trade)): Path<(String, String)>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => journal_answer(journal::service::attach_image(&store, &app.journals, &id, &trade, &body).await),
        Err(res) => res,
    }
}

async fn journal_image_detach(State(app): State<App>, Path((id, trade, sid)): Path<(String, String, String)>,
                              req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => journal_answer(journal::service::detach_image(&store, &app.journals, &id, &trade, &sid).await),
        Err(res) => res,
    }
}

async fn journal_entries(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => journal_answer(journal::service::list_entries(&store, &id).await),
        Err(res) => res,
    }
}

async fn journal_entry_update(State(app): State<App>, Path((id, entry)): Path<(String, String)>, req: Request) -> Response {
    let (store, req) = match app.admit(req).await {
        Ok(ok) => ok,
        Err(res) => return res,
    };
    match object_body(req).await {
        Ok(body) => journal_answer(journal::service::update_entry(&store, &id, &entry, &body).await),
        Err(res) => res,
    }
}

async fn journal_entry_delete(State(app): State<App>, Path((id, entry)): Path<(String, String)>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => journal_answer(journal::service::delete_entry(&store, &id, &entry).await),
        Err(res) => res,
    }
}

async fn journal_csv(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    match app.admit(req).await {
        Ok((store, _)) => match journal::service::trades_csv(&store, &app.journals, &id).await {
            Ok((csv, name)) => {
                let mut res = csv.into_response();
                res.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static("text/csv; charset=utf-8"));
                if let Ok(v) = HeaderValue::from_str(&format!("attachment; filename=\"{name}\"")) {
                    res.headers_mut().insert(header::CONTENT_DISPOSITION, v);
                }
                res
            }
            Err(e) => journal_answer(Err(e)),
        },
        Err(res) => res,
    }
}

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
async fn forward(State(backend): State<App>, mut req: Request) -> Response {
    let path = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let uri: Uri = match format!("http://{}{}", backend.backend, path).parse() {
        Ok(u) => u,
        Err(_) => return (StatusCode::BAD_REQUEST, "bad request path").into_response(),
    };
    *req.uri_mut() = uri;
    strip_hop_by_hop(req.headers_mut());
    match backend.client.request(req).await {
        Ok(res) => {
            let (mut parts, body) = res.into_parts();
            strip_hop_by_hop(&mut parts.headers);
            parts.headers.insert(SERVED_BY, HeaderValue::from_static("python"));
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
