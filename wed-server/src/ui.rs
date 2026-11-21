//! The built dashboard (`web/dist`), served as Python serves it: Starlette's
//! `StaticFiles(directory=ui, html=True)` mounted at "/" (`server.create_app`).
//!
//! A directory answers with its `index.html` (redirected to the address with a trailing slash
//! first, as Starlette does), a file with its content type, anything else 404 with FastAPI's
//! `{"detail": "Not Found"}`. A path that would leave the folder (`..`, a symlink out) is a 404.
//! Without a build, "/" shows how to make one, as Python does.

use std::path::{Component, Path, PathBuf};

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// Where the dashboard build is: `XAU_UI_DIR` (the environment or `home/.env`), else `web/`
/// beside this program (an install), else this checkout's `web/dist` (`DEFAULT_UI_DIR`).
pub fn ui_dir(home: &Path) -> PathBuf {
    if let Some(dir) = crate::config::env_setting("XAU_UI_DIR", &home.join(".env")) {
        return PathBuf::from(dir);
    }
    let beside = std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.join("web")));
    if let Some(dir) = beside.filter(|d| d.join("index.html").is_file()) {
        return dir;
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("web").join("dist")
}

fn json_status(status: StatusCode, detail: &str) -> Response {
    let mut res = Json(json!({"detail": detail})).into_response();
    *res.status_mut() = status;
    res
}

fn not_found() -> Response {
    json_status(StatusCode::NOT_FOUND, "Not Found")
}

/// The page Python shows at "/" when the dashboard isn't built.
fn not_built(ui: &Path) -> Response {
    let ui = ui.display().to_string().replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    Html(format!(
        "<h1>XAU screener API is running</h1><p>Dashboard not built yet (looked in <code>{ui}</code>). \
         Run <code>npm install && npm run build</code> in <code>web/</code>, \
         or use the dev server: <code>npm run dev</code>.</p>"
    ))
    .into_response()
}

/// `%XX` decoded, as the ASGI path is; None when it isn't valid UTF-8.
fn percent_decode(path: &str) -> Option<String> {
    let bytes = path.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok();
            if let Some(b) = hex.and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8(out).ok()
}

/// The file or folder `path` names inside `root`, or None when it would leave it.
fn resolve(root: &Path, path: &str) -> Option<PathBuf> {
    let mut rel = PathBuf::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => return None,
            p if p.contains(['\\', '\0', ':']) => return None,
            p => rel.push(p),
        }
    }
    // Each piece must be a plain name (no drive or root sneaking in on Windows).
    if !rel.components().all(|c| matches!(c, Component::Normal(_))) {
        return None;
    }
    let full = root.join(&rel);
    // Symlinks out of the folder don't count (Starlette checks the real path too).
    let real_root = root.canonicalize().ok()?;
    let real = full.canonicalize().ok()?;
    real.starts_with(&real_root).then_some(real)
}

/// The content type Python's `mimetypes` gives, with a charset for text as Starlette adds one.
fn content_type(path: &Path) -> &'static str {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json",
        "webmanifest" => "application/manifest+json",
        "txt" => "text/plain; charset=utf-8",
        "md" => "text/markdown; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/vnd.microsoft.icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "wasm" => "application/wasm",
        _ => "application/octet-stream",
    }
}

async fn file(path: &Path, method: &Method, headers: &HeaderMap) -> Response {
    let Ok(meta) = tokio::fs::metadata(path).await else { return not_found() };
    let modified = meta.modified().ok().map(chrono::DateTime::<chrono::Utc>::from);
    let etag = format!(
        "\"{:x}-{:x}\"",
        modified.map_or(0, |m| m.timestamp_nanos_opt().unwrap_or(0)),
        meta.len()
    );
    let mut res = if headers.get(header::IF_NONE_MATCH).and_then(|v| v.to_str().ok()) == Some(etag.as_str()) {
        StatusCode::NOT_MODIFIED.into_response()
    } else if *method == Method::HEAD {
        let mut r = Response::new(Body::empty());
        r.headers_mut().insert(header::CONTENT_LENGTH, HeaderValue::from(meta.len()));
        r
    } else {
        match tokio::fs::read(path).await {
            Ok(bytes) => Response::new(Body::from(bytes)),
            Err(_) => return not_found(),
        }
    };
    let h = res.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type(path)));
    if let Ok(v) = HeaderValue::from_str(&etag) {
        h.insert(header::ETAG, v);
    }
    if let Some(m) = modified {
        if let Ok(v) = HeaderValue::from_str(&m.format("%a, %d %b %Y %H:%M:%S GMT").to_string()) {
            h.insert(header::LAST_MODIFIED, v);
        }
    }
    res
}

/// Answer a request no route took: `/api/...` is FastAPI's 404, the rest comes from `ui`.
pub async fn serve(ui: &Path, method: &Method, raw_path: &str, query: Option<&str>, headers: &HeaderMap) -> Response {
    let Some(path) = percent_decode(raw_path) else { return not_found() };
    if path == "/api" || path.starts_with("/api/") {
        return not_found();
    }
    let built = ui.join("index.html").is_file();
    if !built {
        return if path == "/" && matches!(*method, Method::GET | Method::HEAD) { not_built(ui) } else { not_found() };
    }
    if !matches!(*method, Method::GET | Method::HEAD) {
        return json_status(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    }
    let Some(found) = resolve(ui, &path) else { return not_found() };
    if found.is_dir() {
        let index = found.join("index.html");
        if !index.is_file() {
            return not_found();
        }
        if !path.ends_with('/') {
            // Starlette: a folder's address ends with "/", so its page's relative links work.
            let to = match query {
                Some(q) => format!("{raw_path}/?{q}"),
                None => format!("{raw_path}/"),
            };
            let mut res = StatusCode::TEMPORARY_REDIRECT.into_response();
            if let Ok(v) = HeaderValue::from_str(&to) {
                res.headers_mut().insert(header::LOCATION, v);
            }
            return res;
        }
        return file(&index, method, headers).await;
    }
    if found.is_file() {
        return file(&found, method, headers).await;
    }
    not_found()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("wed-ui-{}-{:?}", std::process::id(), std::thread::current().id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("dist/assets")).unwrap();
        std::fs::write(dir.join("dist/index.html"), "<html>dash</html>").unwrap();
        std::fs::write(dir.join("dist/assets/app.js"), "x()").unwrap();
        std::fs::write(dir.join("secret.txt"), "no").unwrap();
        dir
    }

    async fn get(ui: &Path, path: &str) -> (StatusCode, String, String) {
        let res = serve(ui, &Method::GET, path, None, &HeaderMap::new()).await;
        let status = res.status();
        let ct = res.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let body = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        (status, ct, String::from_utf8_lossy(&body).into_owned())
    }

    #[tokio::test]
    async fn the_dashboard_and_its_files() {
        let dir = build();
        let ui = dir.join("dist");
        assert_eq!(get(&ui, "/").await, (StatusCode::OK, "text/html; charset=utf-8".into(), "<html>dash</html>".into()));
        let (s, ct, body) = get(&ui, "/assets/app.js").await;
        assert_eq!((s, ct.as_str(), body.as_str()), (StatusCode::OK, "text/javascript; charset=utf-8", "x()"));
        let (s, _, _) = get(&ui, "/assets").await;
        assert_eq!(s, StatusCode::NOT_FOUND); // a folder without an index.html
        let (s, _, body) = get(&ui, "/api/nope").await;
        assert_eq!((s, body.as_str()), (StatusCode::NOT_FOUND, r#"{"detail":"Not Found"}"#));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn nothing_outside_the_folder() {
        let dir = build();
        let ui = dir.join("dist");
        for bad in ["/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt",
                    "/assets/..%5c..%5csecret.txt", "/missing.js"] {
            let (s, _, body) = get(&ui, bad).await;
            assert_eq!(s, StatusCode::NOT_FOUND, "{bad}");
            assert_eq!(body, r#"{"detail":"Not Found"}"#, "{bad}");
        }
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn without_a_build() {
        let dir = build();
        let ui = dir.join("nothing");
        let (s, _, body) = get(&ui, "/").await;
        assert!(s == StatusCode::OK && body.contains("Dashboard not built yet"));
        assert_eq!(get(&ui, "/assets/app.js").await.0, StatusCode::NOT_FOUND);
        let _ = std::fs::remove_dir_all(dir);
    }
}
