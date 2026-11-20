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
pub mod engine;
pub mod journal;
pub mod pyjson;
pub mod scan;
pub mod settings_routes;
pub mod store;

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

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

/// The routes the scan engine answers when it runs here ([`app_with`]); the Python server follows
/// it through `GET /api/engine/m1` and `/api/engine/daily`, which only the follower uses.
pub const ENGINE_ROUTES: &[&str] = &[
    "GET /api/status",
    "GET /api/scan",
    "GET /api/tick",
    "GET /api/stream",
    "GET /api/settings",
    "PUT /api/settings",
    "POST /api/settings/reconnect",
];

/// The version the dashboard and `/api/health` report; `scripts/set_version.py` keeps it in step.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone)]
struct App {
    backend: SocketAddr,
    client: Client<HttpConnector, Body>,
    store: Option<Store>,
    journals: journal::service::Cache,
    engine: Option<Arc<engine::Runtime>>,
}

/// The server: ported routes, then the Python server at `backend` for the rest. Without a
/// `store`, the routes that need the database go to Python too.
pub fn app(backend: SocketAddr, store: Option<Store>) -> Router {
    app_with(backend, store, None)
}

/// The server with the scan engine running here (`runtime`): the engine's routes are answered by
/// it, and the Python server follows it (`WEDNESDAY_FOLLOW`).
pub fn app_with(backend: SocketAddr, store: Option<Store>, runtime: Option<Arc<engine::Runtime>>) -> Router {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client = Client::builder(TokioExecutor::new()).build(connector);
    let mut router = Router::new();
    if runtime.is_some() {
        router = router
            .route("/api/status", get(engine_status))
            .route("/api/scan", get(engine_scan))
            .route("/api/tick", get(engine_tick))
            .route("/api/stream", get(engine_stream))
            .route("/api/settings", get(engine_settings).put(engine_settings_put))
            .route("/api/settings/reconnect", axum::routing::post(engine_reconnect))
            .route("/api/engine/m1", get(engine_m1))
            .route("/api/engine/daily", get(engine_daily));
    }
    router
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
        .with_state(App { backend, client, store, journals: Default::default(), engine: runtime })
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

/// [`serve_with_engine`] on a listener the caller already bound (the Tauri shell).
pub async fn serve_std_with_engine(listener: std::net::TcpListener, backend: SocketAddr, store: Option<Store>,
                                   runtime: Arc<engine::Runtime>) -> std::io::Result<()> {
    listener.set_nonblocking(true)?;
    serve_with_engine(tokio::net::TcpListener::from_std(listener)?, backend, store, runtime).await
}

/// Serve with the scan engine running here; the Python server at `backend` follows it.
pub async fn serve_with_engine(listener: tokio::net::TcpListener, backend: SocketAddr, store: Option<Store>,
                               runtime: Arc<engine::Runtime>) -> std::io::Result<()> {
    axum::serve(listener, app_with(backend, store, Some(runtime))).await
}

/// Serve on a listener the caller already bound (the Tauri shell binds the dashboard's port
/// before the Python server is up, so nothing else takes it meanwhile). Needs a Tokio runtime.
pub async fn serve_std(listener: std::net::TcpListener, backend: SocketAddr, store: Option<Store>) -> std::io::Result<()> {
    listener.set_nonblocking(true)?;
    serve(tokio::net::TcpListener::from_std(listener)?, backend, store).await
}

/// The scan engine with the settings and scan config the Python server in `home` would run:
/// saved from the dashboard over the environment and `home/.env`.
pub async fn start_engine(home: &std::path::Path, store: Option<Store>) -> Result<Arc<engine::Runtime>, String> {
    let env_file = home.join(".env");
    let env = |k: &str| config::env_setting(k, &env_file);
    let settings = engine::resolve(store.as_ref(), &env).await;
    let cfg = engine::scan_config(&env);
    engine::Runtime::start(settings, engine::Options { cfg, ..Default::default() }, store)
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

// ---- the scan engine (engine/) ----

const STREAM_SECONDS: u64 = 600; // a stream's lifetime; EventSource reconnects by itself

fn runtime_of(app: &App) -> &Arc<engine::Runtime> {
    app.engine.as_ref().expect("engine routes are only mounted with a runtime")
}

/// `/api/status`, with the saved bias read from the database.
async fn status_json(app: &App) -> Value {
    let rt = runtime_of(app);
    let e = rt.engine();
    let st = e.state();
    let (bias, _) = engine::bias(app.store.as_ref()).await;
    let price = st.result.as_ref().map(|r| r.price).unwrap_or(0.0);
    json!({
        "symbol": e.symbol,
        "source": e.source,
        "clock": e.clock,
        "trade_bias": bias,
        "version": st.version,
        "scanned_at": st.scanned_at.map(engine::py_iso_utc),
        "error": st.error,
        "error_at": st.error_at.map(engine::py_iso_utc),
        "conn": st.conn,
        "tick_seconds": e.tick_seconds,
        "pip": journal::stats::pip_size(&e.symbol, price),
        "clock_offset": engine::clock_offset(&e.clock),
        "poll": e.poll,
        "app_version": VERSION,
        "config": engine::config_json(&rt.opts.cfg),
    })
}

async fn engine_status(State(app): State<App>) -> Response {
    Json(status_json(&app).await).into_response()
}

async fn engine_scan(State(app): State<App>) -> Response {
    let mut out = status_json(&app).await;
    let st = runtime_of(&app).engine().state();
    let (_, active) = engine::bias(app.store.as_ref()).await;
    out["scan"] = st.result.as_ref().map_or(Value::Null, |r| r.to_json(active.as_deref()));
    Json(out).into_response()
}

fn tick_json(e: &engine::Engine) -> Value {
    let st = e.state();
    let unix = |t: scan::Time| t.and_utc().timestamp();
    let bar = st.live.as_ref().and_then(|l| l.bar);
    json!({
        "version": st.version,
        "tick": st.tick,
        "price": st.live.as_ref().map(|l| l.price),
        "time": st.live.as_ref().map(|l| unix(l.time)),
        "bar": bar.map(|b| json!({"time": unix(b.time), "open": b.open, "high": b.high, "low": b.low, "close": b.close})),
    })
}

async fn engine_tick(State(app): State<App>) -> Response {
    Json(tick_json(&runtime_of(&app).engine())).into_response()
}

/// Server-sent events: `tick` (the /api/tick body) whenever the price or the scan changes, a
/// comment every 15 s, ending after 10 minutes or `limit` events.
async fn engine_stream(State(app): State<App>, axum::extract::Query(q): axum::extract::Query<HashMap<String, String>>)
    -> Response {
    let limit: u64 = match q.get("limit").map(|l| l.parse::<i64>()) {
        None => 0,
        Some(Ok(l)) if l >= 0 => l as u64,
        _ => {
            return detail(StatusCode::UNPROCESSABLE_ENTITY,
                          json!([{"type": "greater_than_equal", "loc": ["query", "limit"], "msg": "Input should be greater than or equal to 0"}]));
        }
    };
    let rt = runtime_of(&app).clone();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<axum::body::Bytes, std::convert::Infallible>>(16);
    tokio::spawn(async move {
        let send = |s: String| tx.send(Ok(axum::body::Bytes::from(s)));
        if send("retry: 2000\n\n".into()).await.is_err() {
            return;
        }
        let ends = tokio::time::Instant::now() + std::time::Duration::from_secs(STREAM_SECONDS);
        let mut engine = rt.engine();
        let mut changes = engine.subscribe();
        let (mut last, mut sent) = (None::<(u64, u64)>, 0u64);
        loop {
            // A settings change swaps the engine: follow the new one.
            let now_engine = rt.engine();
            if !Arc::ptr_eq(&now_engine, &engine) {
                engine = now_engine;
                changes = engine.subscribe();
            }
            let body = tick_json(&engine);
            let key = (body["tick"].as_u64().unwrap_or(0), body["version"].as_u64().unwrap_or(0));
            if Some(key) != last {
                last = Some(key);
                if send(format!("event: tick\ndata: {}\n\n", pyjson::dumps(&body))).await.is_err() {
                    return;
                }
                sent += 1;
                if limit > 0 && sent >= limit {
                    return;
                }
            }
            let left = ends.saturating_duration_since(tokio::time::Instant::now());
            if left.is_zero() {
                return;
            }
            let wait = left.min(std::time::Duration::from_secs(15));
            tokio::select! {
                changed = changes.changed() => {
                    if changed.is_err() {
                        tokio::time::sleep(std::time::Duration::from_millis(200)).await; // the engine stopped: a new one comes
                    }
                }
                _ = tokio::time::sleep(wait) => {
                    if send(": ping\n\n".into()).await.is_err() {
                        return;
                    }
                }
                _ = tokio::time::sleep(std::time::Duration::from_millis(250)) => {} // catch an engine swap
            }
        }
    });
    let mut res = Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx)).into_response();
    let h = res.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static("text/event-stream; charset=utf-8"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    h.insert("x-accel-buffering", HeaderValue::from_static("no"));
    res
}

/// The data sources the dashboard offers, and whether each can run here (`settings.catalog`).
fn catalog() -> Value {
    engine::SOURCES
        .iter()
        .filter(|s| s.offered)
        .map(|s| {
            let why = match s.id {
                "tradingview" => engine::feed::tradingview_command().err(),
                _ => None,
            };
            json!({"id": s.id, "title": s.title, "description": s.description, "default_symbol": s.default_symbol,
                   "default_clock": s.default_clock, "default_tick": s.default_tick, "min_tick": s.min_tick,
                   "available": why.is_none(), "unavailable_reason": why})
        })
        .collect()
}

async fn settings_json(app: &App) -> Value {
    let rt = runtime_of(app);
    let e = rt.engine();
    let st = e.state();
    let running = json!({
        "source": e.source,
        "symbol": e.symbol,
        "version": st.version,
        "scanned_at": st.scanned_at.map(engine::py_iso_utc),
        "error": st.error,
        "conn": st.conn,
        "attempt": st.attempt,
        "max_attempts": if e.retry_connect { Value::Null } else { json!(engine::RECONNECT_ATTEMPTS) },
        "bars_loaded": st.m1.len(),
        "bars_needed": e.max_bars,
        "first_bar": st.m1.first().map(|b| engine::py_iso_naive(b.time)),
        "last_bar": st.m1.last().map(|b| engine::py_iso_naive(b.time)),
    });
    let storage = match &app.store {
        Some(store) => {
            let mut d = store.describe();
            d["series"] = json!(store.bar_stats().await.unwrap_or_default());
            d
        }
        None => Value::Null,
    };
    json!({"editable": true, "settings": rt.settings().to_json(), "sources": catalog(), "running": running,
           "storage": storage})
}

async fn engine_settings(State(app): State<App>) -> Response {
    Json(settings_json(&app).await).into_response()
}

/// Tell the Python server, which follows this engine, to follow the new settings too.
async fn python_follow(app: &App) {
    let mut req = Request::new(Body::empty());
    *req.method_mut() = Method::POST;
    *req.uri_mut() = Uri::from_static("/api/settings/reconnect");
    let res = forward(State(app.clone()), req).await;
    if !res.status().is_success() {
        log::warn!("the Python server didn't follow the new settings ({})", res.status());
    }
}

async fn engine_settings_put(State(app): State<App>, req: Request) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    let body = match object_body(req).await {
        Ok(b) => b,
        Err(res) => return res,
    };
    let mut merged = engine::DataSettings::default().to_json().as_object().cloned().unwrap_or_default();
    merged.extend(body);
    let new = match engine::DataSettings::from_json(&merged) {
        Ok(s) => s,
        Err(e) => return detail(StatusCode::UNPROCESSABLE_ENTITY, format!("Invalid settings: {e}")),
    };
    let mut errors = new.validate();
    if errors.is_empty() && new.source == "tradingview" {
        if let Err(why) = engine::feed::tradingview_command() {
            errors.push(why);
        }
    }
    if !errors.is_empty() {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, errors.join("; "));
    }
    if let Some(store) = &app.store {
        if let Err(e) = store.set_setting(engine::SETTINGS_KEY, &new.to_json()).await {
            return answer(Err(Refused::Store(e)));
        }
    }
    if let Err(e) = runtime_of(&app).apply(new) {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, e);
    }
    python_follow(&app).await;
    Json(settings_json(&app).await).into_response()
}

async fn engine_reconnect(State(app): State<App>, req: Request) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    if let Err(e) = runtime_of(&app).reconnect() {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, e);
    }
    python_follow(&app).await;
    Json(settings_json(&app).await).into_response()
}

/// For the Python server that follows this engine: the closed M1 bars it scanned, with the price
/// and the scan version, and what it runs (source, symbol, clock).
async fn engine_m1(State(app): State<App>, axum::extract::Query(q): axum::extract::Query<HashMap<String, String>>)
    -> Response {
    let e = runtime_of(&app).engine();
    let st = e.state();
    let count = q.get("count").and_then(|c| c.parse::<usize>().ok()).unwrap_or(st.m1.len());
    let start = st.m1.len().saturating_sub(count);
    let bars: Vec<Value> = st.m1[start..]
        .iter()
        .map(|b| json!([b.time.and_utc().timestamp(), b.open, b.high, b.low, b.close, b.volume]))
        .collect();
    Json(json!({"source": e.source, "symbol": e.symbol, "clock": e.clock, "settings": e.settings.to_json(),
                "version": st.version, "conn": st.conn, "error": st.error,
                "price": st.result.as_ref().map(|r| r.price), "bars": bars}))
    .into_response()
}

/// For the follower: daily bars from the feed (the move distribution's long lookbacks).
async fn engine_daily(State(app): State<App>, axum::extract::Query(q): axum::extract::Query<HashMap<String, String>>)
    -> Response {
    let e = runtime_of(&app).engine();
    let clock = q.get("clock").cloned().unwrap_or_else(|| e.clock.clone());
    let count = q.get("count").and_then(|c| c.parse::<usize>().ok()).unwrap_or(3000);
    let (reply, rx) = tokio::sync::oneshot::channel();
    if let Err(err) = e.call(engine::Command::DailyHistory { clock, count, reply }).await {
        return detail(StatusCode::SERVICE_UNAVAILABLE, err);
    }
    match rx.await {
        Ok(Ok(Some(bars))) => Json(json!({"bars": bars.iter().map(|b| json!([b.time.and_utc().timestamp(), b.open, b.high, b.low, b.close, b.volume])).collect::<Vec<_>>()})).into_response(),
        Ok(Ok(None)) => Json(json!({"bars": null})).into_response(),
        Ok(Err(err)) => detail(StatusCode::BAD_GATEWAY, err),
        Err(_) => detail(StatusCode::SERVICE_UNAVAILABLE, "The scanner is stopping"),
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
