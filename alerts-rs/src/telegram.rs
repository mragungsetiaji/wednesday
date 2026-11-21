//! The Telegram Bot API client (`TelegramClient` in alerts.py): send a message, list the chats
//! that wrote to the bot. Same messages as Python's `TelegramError`.
//!
//! The token is part of every request's URL, so neither the URL nor an error that could carry it
//! is ever logged or returned as is: errors are built without the URL and scrubbed of the token.

use std::time::Duration;

use serde_json::{json, Map, Value};

pub const API: &str = "https://api.telegram.org";
const TIMEOUT: Duration = Duration::from_secs(10);

/// What went wrong, worded for the trader (`TelegramError`).
#[derive(Clone, Debug, PartialEq)]
pub struct TelegramError(pub String);

impl std::fmt::Display for TelegramError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Clone)]
pub struct TelegramClient {
    pub token: String,
    pub chat_id: Option<String>,
    /// The API's address: Telegram's, or a fake server in tests.
    pub api: String,
    http: reqwest::Client,
}

impl std::fmt::Debug for TelegramClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TelegramClient").field("chat_id", &self.chat_id).field("api", &self.api).finish_non_exhaustive()
    }
}

impl TelegramClient {
    pub fn new(token: impl Into<String>, chat_id: Option<String>, api: impl Into<String>) -> TelegramClient {
        let http = reqwest::Client::builder().timeout(TIMEOUT).build().unwrap_or_default();
        TelegramClient { token: token.into(), chat_id, api: api.into(), http }
    }

    fn scrub(&self, text: String) -> String {
        if self.token.is_empty() { text } else { text.replace(&self.token, "***") }
    }

    async fn call(&self, method: &str, params: &[(&str, &str)]) -> Result<Value, TelegramError> {
        let url = format!("{}/bot{}/{method}", self.api.trim_end_matches('/'), self.token);
        let unreachable = |e: reqwest::Error| TelegramError(self.scrub(format!("Can't reach Telegram: {}", reason(e))));
        let resp = self.http.post(url).form(params).send().await.map_err(unreachable)?;
        let status = resp.status();
        let bytes = resp.bytes().await.map_err(unreachable)?;
        let body: Option<Value> = serde_json::from_slice(&bytes).ok();
        if !status.is_success() {
            // urllib's HTTPError: the description from the body, else "HTTP Error 404: Not Found".
            let detail = match body.as_ref().and_then(Value::as_object) {
                Some(o) => o.get("description").map(py_str).unwrap_or_else(|| http_error(status)),
                None => http_error(status),
            };
            return Err(TelegramError(self.scrub(format!("Telegram {method} failed: {detail}"))));
        }
        let Some(body) = body.filter(Value::is_object) else {
            return Err(TelegramError("Can't reach Telegram: the answer wasn't JSON".into()));
        };
        if !super::py_truthy(body.get("ok")) {
            let detail = body.get("description").map(py_str).unwrap_or_else(|| super::py_repr_value(&body));
            return Err(TelegramError(self.scrub(format!("Telegram {method} failed: {detail}"))));
        }
        Ok(body)
    }

    pub async fn send(&self, text: &str) -> Result<(), TelegramError> {
        let Some(chat) = self.chat_id.as_deref().filter(|c| !c.is_empty()) else {
            return Err(TelegramError("TELEGRAM_CHAT_ID is not set".into()));
        };
        self.call("sendMessage", &[("chat_id", chat), ("text", text), ("parse_mode", "HTML"),
                                    ("disable_web_page_preview", "true")])
            .await
            .map(|_| ())
    }

    /// Chats that recently messaged the bot, for finding the chat id.
    pub async fn chats(&self) -> Result<Vec<Value>, TelegramError> {
        let body = self.call("getUpdates", &[]).await?;
        let mut seen: Vec<(Value, Value)> = Vec::new(); // id -> chat, first-seen order, last wins
        let empty = Map::new();
        for u in body.get("result").and_then(Value::as_array).into_iter().flatten() {
            let pick = |k: &str| u.get(k).filter(|v| super::py_truthy(Some(v)));
            let msg = pick("message").or_else(|| pick("channel_post")).or_else(|| pick("my_chat_member"));
            let msg = msg.and_then(Value::as_object).unwrap_or(&empty);
            let Some(chat) = msg.get("chat").filter(|c| super::py_truthy(Some(c))) else { continue };
            let id = chat.get("id").cloned().unwrap_or(Value::Null);
            let name = ["title", "username", "first_name"]
                .iter()
                .find_map(|k| chat.get(*k).filter(|v| super::py_truthy(Some(v))).cloned())
                .unwrap_or_else(|| chat.get("first_name").cloned().unwrap_or(Value::Null));
            let entry = json!({"id": id, "type": chat.get("type").cloned().unwrap_or(Value::Null), "name": name});
            match seen.iter_mut().find(|(k, _)| *k == id) {
                Some(slot) => slot.1 = entry,
                None => seen.push((id, entry)),
            }
        }
        Ok(seen.into_iter().map(|(_, v)| v).collect())
    }
}

/// Why a request didn't get through, without the URL: "timed out" as Python says it, else the
/// error and its causes.
fn reason(e: reqwest::Error) -> String {
    if e.is_timeout() {
        return "timed out".into();
    }
    let e = e.without_url();
    let mut out = e.to_string();
    let mut src = std::error::Error::source(&e);
    while let Some(s) = src {
        let text = s.to_string();
        if !out.contains(&text) {
            out.push_str(": ");
            out.push_str(&text);
        }
        src = s.source();
    }
    out
}

fn http_error(status: reqwest::StatusCode) -> String {
    format!("HTTP Error {}: {}", status.as_u16(), status.canonical_reason().unwrap_or(""))
}

/// Python's `str()` of a JSON value: strings as they are, the rest as their repr.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => super::py_repr_value(other),
    }
}
