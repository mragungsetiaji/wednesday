//! The plugin routes: `/api/plugins` and `/api/licence` for the dashboard (as `server.py` answers
//! them), `/api/ee/<name>/...` forwarded to a plugin, and `/api/plugin-host/settings/<key>` for the
//! plugins themselves. The server checks cross-site writes before calling these.
//!
//! Without a [`Host`] (a server that doesn't run plugins) they answer as Python did without any
//! plugin: an empty list, no licence screen.

use std::sync::Arc;

use axum::body::Body;
use axum::extract::Request;
use axum::http::{header, HeaderValue, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};

use super::{catalog, short_name, Host, API, ROUTE_PREFIX};

fn detail(status: StatusCode, detail: impl Into<Value>) -> Response {
    let mut res = Json(json!({"detail": detail.into()})).into_response();
    *res.status_mut() = status;
    res
}

/// `GET /api/plugins`: installed plugins and the features they provide (the dashboard unlocks
/// screens from this).
pub fn list(host: Option<&Arc<Host>>) -> Response {
    let (plugins, features) = match host {
        Some(h) => (h.plugins().iter().map(|p| p.to_json()).collect(), h.features()),
        None => (vec![], vec![]),
    };
    Json(json!({"api": API, "plugins": plugins, "features": features, "catalog": catalog::catalog(&features)}))
        .into_response()
}

/// The features the running plugins provide (the journal's `journal.multi`).
pub fn features(host: Option<&Arc<Host>>) -> Vec<String> {
    host.map(|h| h.features()).unwrap_or_default()
}

/// A licence call to the provider: `{"available": true, ...status}`, or its refusal. The plugin's
/// features are read again afterwards, whatever it answered.
async fn licence_call(host: &Arc<Host>, name: &str, method: Method, body: Option<Value>) -> Response {
    let res = match host.conn_of(name) {
        Ok(conn) => host.call(&conn, method, "/licence", body.as_ref()).await,
        Err(why) => Err(why),
    };
    host.refresh(name).await;
    match res {
        Ok((StatusCode::OK, Value::Object(status))) => {
            let mut out = Map::new();
            out.insert("available".into(), Value::Bool(true));
            out.extend(status);
            Json(Value::Object(out)).into_response()
        }
        Ok((StatusCode::UNPROCESSABLE_ENTITY, v)) => {
            let msg = match &v["detail"] {
                Value::String(s) => s.clone(),
                Value::Null => "That licence key wasn't accepted".into(),
                other => other.to_string(),
            };
            detail(StatusCode::UNPROCESSABLE_ENTITY, msg)
        }
        Ok((StatusCode::NOT_FOUND, _)) => detail(StatusCode::CONFLICT, "No plugin handles licences"),
        Ok((status, _)) => {
            log::warn!("plugin {name} answered its licence call with {status}");
            detail(StatusCode::BAD_GATEWAY, format!("{name} couldn't handle the licence ({}). Restart Wednesday if this stays.",
                                                    status.as_u16()))
        }
        Err(e) => {
            log::warn!("plugin {name} didn't answer its licence call: {e}");
            detail(StatusCode::BAD_GATEWAY, format!("{name} isn't answering. Restart Wednesday if this stays."))
        }
    }
}

/// `GET /api/licence`: the licence, when a plugin handles licences; otherwise just that none does.
pub async fn licence_get(host: Option<&Arc<Host>>) -> Response {
    match host.and_then(|h| Some((h, h.licence_provider()?))) {
        Some((h, name)) => licence_call(h, &name, Method::GET, None).await,
        None => Json(json!({"available": false})).into_response(),
    }
}

/// `PUT /api/licence {"key"}`: hand the key to the plugin that handles licences. Without one, a
/// key from the licence server (`WEDK-...`) downloads Wednesday EE first, starts it, then hands
/// it the key.
pub async fn licence_put(host: Option<&Arc<Host>>, body: &Map<String, Value>) -> Response {
    let key = match body.get("key") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::String(s)) => s.trim().to_string(),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => String::new(),
        Some(other) => other.to_string().trim().to_string(),
    };
    if key.is_empty() {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, "Paste a licence key");
    }
    let Some(host) = host else {
        return detail(StatusCode::CONFLICT, "The licence needs the server running with --serve");
    };
    let provider = match host.licence_provider() {
        Some(p) => p,
        None => match download(host, &key).await {
            Ok(p) => p,
            Err(res) => return res,
        },
    };
    licence_call(host, &provider, Method::PUT, Some(json!({"key": key}))).await
}

/// No plugin handles licences yet: a licence key from the licence server fetches Wednesday EE.
async fn download(host: &Arc<Host>, key: &str) -> Result<String, Response> {
    if !key.to_uppercase().starts_with("WEDK-") {
        return Err(detail(StatusCode::CONFLICT,
                          "Wednesday EE isn't installed. Enter a licence key (WEDK-...) and Wednesday downloads it."));
    }
    let (manifest, info) = host.install(key).await.map_err(|e| detail(StatusCode::UNPROCESSABLE_ENTITY, e.0))?;
    if !info.loaded {
        return Err(detail(StatusCode::INTERNAL_SERVER_ERROR, format!(
            "Wednesday EE {} was downloaded but didn't load: {}", manifest.version, info.error.unwrap_or_default())));
    }
    host.licence_provider()
        .ok_or_else(|| detail(StatusCode::INTERNAL_SERVER_ERROR, "Wednesday EE was downloaded; restart Wednesday to finish"))
}

/// `DELETE /api/licence`: remove the licence from this install.
pub async fn licence_delete(host: Option<&Arc<Host>>) -> Response {
    match host.and_then(|h| Some((h, h.licence_provider()?))) {
        Some((h, name)) => licence_call(h, &name, Method::DELETE, None).await,
        None => detail(StatusCode::CONFLICT, "No plugin handles licences"),
    }
}

// Headers that belong to one connection, not the request (RFC 9110 7.6.1).
const HOP_BY_HOP: &[&str] = &[
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
    "trailer", "transfer-encoding", "upgrade",
];
/// What the dashboard's request carries on to the plugin besides its method, query and body.
const PASSED: &[header::HeaderName] = &[header::CONTENT_TYPE, header::CONTENT_LENGTH, header::ACCEPT,
                                       header::ACCEPT_LANGUAGE, header::IF_NONE_MATCH];

/// `ANY /api/ee/<short name>/<path>`: the plugin's `/routes/<path>`, its answer streamed back.
pub async fn forward(host: Option<&Arc<Host>>, req: Request) -> Response {
    let not_found = || detail(StatusCode::NOT_FOUND, "Not Found");
    let Some(host) = host else { return not_found() };
    let path = req.uri().path().to_string();
    let Some(rest) = path.strip_prefix(ROUTE_PREFIX).and_then(|p| p.strip_prefix('/')) else { return not_found() };
    let Some((short, sub)) = rest.split_once('/') else { return not_found() };
    let Some(name) = host.by_short_name(short) else { return not_found() };
    let name = name.as_str();
    let conn = match host.conn_of(name) {
        Ok(c) => c,
        Err(why) if why.is_empty() => return not_found(),
        Err(why) => return detail(StatusCode::SERVICE_UNAVAILABLE,
                                  format!("{name} isn't running ({why}). Restart Wednesday if this stays.")),
    };
    let query = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
    let Ok(uri) = format!("http://127.0.0.1:{}/routes/{sub}{query}", conn.port).parse::<Uri>() else {
        return detail(StatusCode::BAD_REQUEST, "bad request path");
    };
    let (parts, body) = req.into_parts();
    let mut out = axum::http::Request::builder().method(parts.method).uri(uri);
    for name in PASSED {
        for v in parts.headers.get_all(name) {
            out = out.header(name, v);
        }
    }
    out = out.header(header::AUTHORIZATION, format!("Bearer {}", conn.token));
    let Ok(out) = out.body(body) else { return detail(StatusCode::BAD_REQUEST, "bad request") };
    match host.client.request(out).await {
        Ok(res) => {
            let (mut parts, body) = res.into_parts();
            for h in HOP_BY_HOP {
                parts.headers.remove(*h);
            }
            parts.headers.remove(header::SET_COOKIE);
            parts.headers.insert("x-wednesday-server", HeaderValue::from_static("rust"));
            Response::from_parts(parts, Body::new(body))
        }
        Err(e) => {
            log::warn!("plugin {name} didn't answer {}: {e}", path);
            detail(StatusCode::BAD_GATEWAY, format!("{name} isn't answering. Restart Wednesday if this stays."))
        }
    }
}

/// `GET / PUT / DELETE /api/plugin-host/settings/<key>`: the server's settings for the plugin
/// whose token the request carries, only keys starting with its name without `wednesday_` and an
/// underscore (Wednesday EE: `ee_...`). Values are never logged.
pub async fn settings(host: Option<&Arc<Host>>, key: &str, req: Request) -> Response {
    let refused = || detail(StatusCode::UNAUTHORIZED, "Not authenticated");
    let Some(host) = host else { return refused() };
    let Some(name) = host.authorize(req.headers()) else { return refused() };
    let prefix = format!("{}_", short_name(&name));
    if !key.starts_with(&prefix) || key.len() == prefix.len() {
        return detail(StatusCode::FORBIDDEN, format!("A plugin can only use its own settings ({prefix}...)"));
    }
    let Some(store) = host.opts.store.as_ref() else {
        return detail(StatusCode::CONFLICT, "Plugin settings need a database");
    };
    let stored = |r: Result<Value, String>| match r {
        Ok(v) => Json(v).into_response(),
        Err(e) => {
            log::error!("database: {e}");
            detail(StatusCode::INTERNAL_SERVER_ERROR, "The database couldn't be read or written")
        }
    };
    match req.method().clone() {
        Method::GET => stored(store.get_setting(key).await.map(|v| json!({"value": v}))),
        Method::PUT => {
            let Ok(bytes) = axum::body::to_bytes(req.into_body(), 8 * 1024 * 1024).await else {
                return detail(StatusCode::PAYLOAD_TOO_LARGE, "The request is too large");
            };
            let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
                return detail(StatusCode::UNPROCESSABLE_ENTITY, "The body must be JSON");
            };
            log::debug!("plugin {name} saved setting {key}");
            stored(store.set_setting(key, &value).await.map(|()| json!({"ok": true})))
        }
        Method::DELETE => stored(store.delete_setting(key).await.map(|()| json!({"ok": true}))),
        _ => detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed"),
    }
}
