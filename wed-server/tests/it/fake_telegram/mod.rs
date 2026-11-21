//! A fake Telegram Bot API on a local port, for the alert tests: records each request (path and
//! form fields) and answers with what the test set.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::http::{StatusCode, Uri};
use axum::response::IntoResponse;
use axum::Router;

#[derive(Clone, Debug)]
pub struct Request {
    pub path: String,
    pub form: Vec<(String, String)>,
}

impl Request {
    pub fn field(&self, k: &str) -> Option<&str> {
        self.form.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
    }
}

#[derive(Clone)]
pub struct Fake {
    pub url: String,
    pub requests: Arc<Mutex<Vec<Request>>>,
    /// Answer per method name (`sendMessage`, `getUpdates`): status and body.
    pub replies: Arc<Mutex<HashMap<String, (u16, String)>>>,
}

impl Fake {
    pub fn reply(&self, method: &str, status: u16, body: &str) {
        self.replies.lock().unwrap().insert(method.into(), (status, body.into()));
    }

    pub fn sent(&self) -> Vec<String> {
        self.requests.lock().unwrap().iter().filter_map(|r| r.field("text").map(String::from)).collect()
    }
}

fn decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() => {
                out.push(u8::from_str_radix(&s[i + 1..i + 3], 16).unwrap());
                i += 2;
            }
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8(out).unwrap()
}

pub async fn start() -> Fake {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let fake = Fake { url, requests: Arc::default(), replies: Arc::default() };
    let f = fake.clone();
    let app = Router::new().fallback(move |uri: Uri, body: Bytes| {
        let f = f.clone();
        async move {
            let text = String::from_utf8_lossy(&body).to_string();
            let form = text
                .split('&')
                .filter(|p| !p.is_empty())
                .map(|p| {
                    let (k, v) = p.split_once('=').unwrap_or((p, ""));
                    (decode(k), decode(v))
                })
                .collect();
            let path = uri.path().to_string();
            f.requests.lock().unwrap().push(Request { path: path.clone(), form });
            let method = path.rsplit('/').next().unwrap_or("").to_string();
            let (status, body) = f.replies.lock().unwrap().get(&method).cloned()
                .unwrap_or((200, r#"{"ok": true, "result": {}}"#.into()));
            (StatusCode::from_u16(status).unwrap(), [("content-type", "application/json")], body).into_response()
        }
    });
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    fake
}
