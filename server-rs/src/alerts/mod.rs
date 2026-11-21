//! Order block alerts on Telegram, and the bot token (step 9e of #96): a port of `alerts.py`,
//! the Telegram part of `secret_store.py` and the `/api/alerts*` routes of `server.py`.
//!
//! After every scan, the M1 bars that arrived since the previous check are compared with each
//! active order block: if their high/low range overlaps the zone, the order block was entered (a
//! wick inside the minute counts). Each order block alerts once; the log lives in the store
//! (`alert_log`) so a restart doesn't repeat alerts. Bars that printed before the order block was
//! confirmed (its break candle closed) never trigger it. With a neutral bias (not trading) alerts
//! are held back unless the trader turned `neutral_alerts` on.
//!
//! The bot token lives in the OS credential store ([`secrets`]); the chat id and which
//! timeframes and priorities alert are settings the dashboard saves under `alerts`
//! (`TELEGRAM_CHAT_ID` is the chat id's fallback).

pub mod secrets;
pub mod telegram;

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};

use crate::engine::{py_iso_naive, py_iso_utc, Answer, Refusal};
use crate::notify::Sender;
use crate::scan::{Bar, Level, ScanResult, Time, TIMEFRAMES};
use crate::store::Store;
pub use secrets::{Keyring, OsKeyring, Secrets, TELEGRAM_BOT_TOKEN};
pub use telegram::{TelegramClient, TelegramError, API};

pub const ALERTS_KEY: &str = "alerts";
pub const PRIORITIES: [&str; 2] = ["extreme", "middle"];

/// `RISK_TEXT` in bias.py.
pub fn risk_text(risk: &str) -> &'static str {
    match risk {
        "on" => "RISK ON",
        "off" => "RISK OFF",
        _ => "NO TRADE",
    }
}

/// `setup_risk` in bias.py: "on" (with the bias), "off" (against it), "no_trade" (neutral), None
/// (no bias). `bias` is the active bias's direction.
pub fn setup_risk(side: &str, bias: Option<&str>) -> Option<&'static str> {
    let bias = bias?;
    if bias == "neutral" {
        return Some("no_trade");
    }
    Some(if (bias == "bearish") == (side == "sell") { "on" } else { "off" })
}

// ---- Python's view of JSON values ----

/// Python truthiness of a JSON value (a missing one is falsy).
pub(crate) fn py_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python's `repr()` of a JSON value.
pub(crate) fn py_repr_value(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => (if *b { "True" } else { "False" }).into(),
        Value::Number(_) => crate::pyjson::dumps(v),
        Value::String(s) => crate::calendar::py_repr(s),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr_value).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", crate::calendar::py_repr(k), py_repr_value(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `str(value or "")`.
pub(crate) fn py_str_or_empty(v: Option<&Value>) -> String {
    match v {
        _ if !py_truthy(v) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => py_repr_value(other),
        None => String::new(),
    }
}

/// `list(value)`: a list's items, a string's characters, an object's keys.
fn py_list(v: &Value) -> Result<Vec<Value>, String> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        Value::Object(o) => Ok(o.keys().map(|k| Value::String(k.clone())).collect()),
        other => Err(format!("{} object is not iterable", py_type(other))),
    }
}

fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "'NoneType'",
        Value::Bool(_) => "'bool'",
        Value::Number(n) if n.is_f64() => "'float'",
        Value::Number(_) => "'int'",
        Value::String(_) => "'str'",
        Value::Array(_) => "'list'",
        Value::Object(_) => "'dict'",
    }
}

/// `html.escape(s)` (quotes too).
pub fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// Python's `f"{v:,.{digits}f}"`: fixed decimals with thousands separators.
pub fn thousands(v: f64, digits: usize) -> String {
    if v.is_nan() {
        return "nan".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf".into() } else { "-inf".into() };
    }
    let s = format!("{v:.digits$}");
    let (sign, rest) = match s.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", s.as_str()),
    };
    let (int, frac) = match rest.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (rest, None),
    };
    let mut grouped = String::new();
    for (i, ch) in int.chars().enumerate() {
        if i > 0 && (int.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    match frac {
        Some(f) => format!("{sign}{grouped}.{f}"),
        None => format!("{sign}{grouped}"),
    }
}

// ---- settings ----

#[derive(Clone, Debug, PartialEq)]
pub struct AlertSettings {
    pub enabled: bool,
    pub timeframes: Vec<Value>, // names; kept as given, as Python keeps the list
    pub priorities: Vec<Value>,
    pub neutral_alerts: bool, // with a neutral bias (no trading) alerts are held back unless this is on
    pub chat_id: Option<String>, // None = TELEGRAM_CHAT_ID from the environment
}

impl Default for AlertSettings {
    fn default() -> Self {
        AlertSettings {
            enabled: true,
            timeframes: TIMEFRAMES.iter().map(|t| Value::from(t.name)).collect(),
            priorities: PRIORITIES.iter().map(|&p| Value::from(p)).collect(),
            neutral_alerts: false,
            chat_id: None,
        }
    }
}

impl AlertSettings {
    /// `AlertSettings.from_dict`; `Err` where Python raises (a list that isn't one).
    pub fn from_json(d: Option<&Value>) -> Result<AlertSettings, String> {
        let empty = Map::new();
        let d = d.and_then(Value::as_object).unwrap_or(&empty);
        let base = AlertSettings::default();
        let flag = |k: &str, def: bool| d.get(k).map_or(def, |v| py_truthy(Some(v)));
        let list = |k: &str, def: &Vec<Value>| d.get(k).map_or(Ok(def.clone()), py_list);
        let chat = py_str_or_empty(d.get("chat_id"));
        let chat = chat.trim();
        Ok(AlertSettings {
            enabled: flag("enabled", base.enabled),
            timeframes: list("timeframes", &base.timeframes)?,
            priorities: list("priorities", &base.priorities)?,
            neutral_alerts: flag("neutral_alerts", base.neutral_alerts),
            chat_id: (!chat.is_empty()).then(|| chat.to_string()),
        })
    }

    /// `asdict()`: Python's key order.
    pub fn to_json(&self) -> Value {
        json!({"enabled": self.enabled, "timeframes": self.timeframes, "priorities": self.priorities,
               "neutral_alerts": self.neutral_alerts, "chat_id": self.chat_id})
    }

    pub fn validate(&self) -> Vec<String> {
        let mut errors: Vec<String> = self
            .timeframes
            .iter()
            .filter(|t| !t.as_str().is_some_and(|t| TIMEFRAMES.iter().any(|k| k.name == t)))
            .map(|t| format!("Unknown timeframe {}", py_repr_value(t)))
            .collect();
        if let Some(c) = self.chat_id.as_deref().filter(|c| !c.is_empty()) {
            let digits = c.trim_start_matches('-');
            let isdigit = !digits.is_empty() && digits.chars().all(char::is_numeric);
            if !isdigit && !c.starts_with('@') {
                errors.push("The chat id is a number like 123456789 (or -100… for a group), or @channelname".into());
            }
        }
        errors.extend(
            self.priorities
                .iter()
                .filter(|p| !p.as_str().is_some_and(|p| PRIORITIES.contains(&p)))
                .map(|p| format!("Unknown priority {}", py_repr_value(p))),
        );
        errors
    }

    /// The chat id, else `TELEGRAM_CHAT_ID`.
    pub fn resolved_chat_id(&self, env: &dyn Fn(&str) -> Option<String>) -> Option<String> {
        self.chat_id.clone().filter(|c| !c.is_empty()).or_else(|| env("TELEGRAM_CHAT_ID").filter(|c| !c.is_empty()))
    }

    fn has_timeframe(&self, tf: &str) -> bool {
        self.timeframes.iter().any(|t| t.as_str() == Some(tf))
    }
}

// ---- the alert text ----

/// The order block's log key: `{source}|{symbol}|{tf}|{kind}|{time}|{top:.5f}`.
pub fn ob_key(source: &str, symbol: &str, tf: &str, ob: &Level) -> String {
    format!("{source}|{symbol}|{tf}|{}|{}|{:.5}", ob.kind, py_iso_naive(ob.time), ob.top)
}

/// `f"{bias.direction} {bias.event} at {bias.level:,.2f}"` of a timeframe's structure bias.
pub fn bias_text(bias: Option<&Value>) -> Option<String> {
    let b = bias.filter(|b| b.is_object())?;
    let s = |k: &str| b.get(k).map(|v| v.as_str().map(String::from).unwrap_or_else(|| py_repr_value(v))).unwrap_or_default();
    Some(format!("{} {} at {}", s("direction"), s("event"), thousands(b.get("level").and_then(Value::as_f64).unwrap_or(f64::NAN), 2)))
}

/// The Telegram message for an entered order block (`format_alert`). `trade_bias` is the active
/// bias's direction.
pub fn format_alert(symbol: &str, tf: &str, ob: &Level, price: f64, bias_text: Option<&str>, digits: usize,
                    trade_bias: Option<&str>) -> String {
    let m = &ob.meta;
    let bullish = ob.kind == "bullish";
    let side = if bullish { "Buy" } else { "Sell" };
    let prio = if ob.meta_str("priority") == Some("extreme") { "extreme" } else { "mid" };
    let swing = match m.get("swing") {
        v if !py_truthy(v) => String::new(),
        Some(Value::String(s)) => format!(" at {s}"),
        Some(v) => format!(" at {}", py_repr_value(v)),
        None => String::new(),
    };
    let f = |v: f64| thousands(v, digits);
    let num = |k: &str| ob.meta_f64(k).unwrap_or(f64::NAN);
    let risk = setup_risk(&side.to_lowercase(), trade_bias);
    let mut lines = vec![
        format!("<b>{} entered {tf} {} OB</b> ({prio}{swing})", html_escape(symbol), if bullish { "bullish" } else { "bearish" }),
        format!("{side} limit <b>{}</b> · SL {} · risk {}{}", f(num("entry")), f(num("sl")), f(num("risk")),
                if py_truthy(m.get("sl_capped")) { " (capped)" } else { "" }),
        format!("Zone {} – {} · price {}", f(ob.bottom), f(ob.top), f(price)),
    ];
    if ob.touches > 0 {
        lines.push(format!("Entry tested {}× before", ob.touches));
    }
    if let Some(b) = bias_text.filter(|b| !b.is_empty()) {
        lines.push(format!("{tf} structure: {b}"));
    }
    if let Some(risk) = risk {
        let why = match risk {
            "on" => format!(" (with {} bias)", trade_bias.unwrap_or("")),
            "off" => format!(" (against {} bias)", trade_bias.unwrap_or("")),
            _ => " (neutral bias)".to_string(),
        };
        lines.push(format!("<b>{}</b>{why}", risk_text(risk)));
    }
    lines.join("\n")
}

pub fn test_message(symbol: &str) -> String {
    format!("<b>{} screener</b>\nTest message: alerts are connected.", html_escape(symbol))
}

// ---- the manager ----

/// An alert the latest bars call for: its log key, its log row (without status and error) and
/// the message.
#[derive(Clone, Debug)]
pub struct Due {
    pub key: String,
    pub row: Map<String, Value>,
    pub text: String,
}

/// The alert state (`AlertManager`): settings, the Telegram client, the last error and what each
/// stream has seen.
pub struct AlertManager {
    pub settings: AlertSettings,
    pub client: Option<TelegramClient>,
    pub last_error: Option<String>,
    last_bar: HashMap<(String, String), Time>,
    sent_memory: HashSet<String>, // when there is no store
}

impl AlertManager {
    pub fn new(settings: AlertSettings, client: Option<TelegramClient>) -> AlertManager {
        AlertManager { settings, client, last_error: None, last_bar: HashMap::new(), sent_memory: HashSet::new() }
    }

    /// A token and a chat id.
    pub fn configured(&self) -> bool {
        self.client.as_ref().is_some_and(|c| !c.token.is_empty() && c.chat_id.as_deref().is_some_and(|c| !c.is_empty()))
    }

    /// The order blocks entered by bars since the last check of this stream (the first check only
    /// remembers where the stream is). Nothing while alerts are off or not set up, or with a
    /// neutral bias unless `neutral_alerts` is on. Doesn't look at the log: [`Alerts::check`] does.
    pub fn due(&mut self, source: &str, symbol: &str, result: &ScanResult, m1: &[Bar], trade_bias: Option<&str>) -> Vec<Due> {
        let Some(last_bar) = m1.last() else { return vec![] };
        let stream = (source.to_string(), symbol.to_string());
        let Some(last) = self.last_bar.insert(stream, last_bar.time) else {
            return vec![]; // first scan of this stream: only react to bars that arrive from now on
        };
        let new: Vec<&Bar> = m1.iter().filter(|b| b.time > last).collect();
        if new.is_empty() || !(self.configured() && self.settings.enabled) {
            return vec![];
        }
        if trade_bias == Some("neutral") && !self.settings.neutral_alerts {
            return vec![]; // neutral = not trading; the OBs can still alert once a direction is set
        }
        let mut out = Vec::new();
        for r in &result.results {
            let tf = r.timeframe;
            let Some((_, set)) = r.sets.iter().find(|(d, _)| d.name() == "ob") else { continue };
            if !self.settings.has_timeframe(tf.name) {
                continue;
            }
            let bias = bias_text(r.bias.as_ref());
            for ob in &set.active {
                let prio = ob.meta.get("priority").cloned().unwrap_or(Value::Null);
                if !self.settings.priorities.contains(&prio) {
                    continue;
                }
                // Only bars after the order block was confirmed (its break candle closed).
                let from = ob.confirmed_time + tf.delta();
                let bars: Vec<&&Bar> = new.iter().filter(|b| b.time >= from).collect();
                if bars.is_empty() {
                    continue;
                }
                let low = bars.iter().map(|b| b.low).fold(f64::INFINITY, f64::min);
                let high = bars.iter().map(|b| b.high).fold(f64::NEG_INFINITY, f64::max);
                if low > ob.top || high < ob.bottom {
                    continue;
                }
                let key = ob_key(source, symbol, tf.name, ob);
                let mut row = Map::new();
                row.insert("key".into(), key.clone().into());
                row.insert("source".into(), source.into());
                row.insert("symbol".into(), symbol.into());
                row.insert("timeframe".into(), tf.name.into());
                row.insert("kind".into(), ob.kind.into());
                row.insert("priority".into(), ob.meta.get("priority").cloned().unwrap_or_else(|| "".into()));
                row.insert("entry".into(), ob.meta_f64("entry").unwrap_or(ob.top).into());
                row.insert("price".into(), result.price.into());
                row.insert("sent_at".into(), py_iso_utc(chrono::Utc::now()).into());
                let text = format_alert(symbol, tf.name, ob, result.price, bias.as_deref(), 2, trade_bias);
                out.push(Due { key, row, text });
            }
        }
        out
    }
}

// ---- the server's alerts ----

/// A lookup of environment variables (the process, then `.env`).
pub type Env = Arc<dyn Fn(&str) -> Option<String> + Send + Sync>;

/// Alerts as the server runs them: the manager, the bot token and the routes' logic.
pub struct Alerts {
    store: Option<Store>,
    secrets: Secrets,
    env: Env,
    api: String,
    state: Mutex<AlertManager>,
}

impl Alerts {
    /// The saved settings, the token from the OS credential store, `.env` in `home` for the
    /// fallbacks, and Telegram's API.
    pub async fn load(store: Option<Store>, home: &Path) -> Alerts {
        let env_file = home.join(".env");
        let env: Env = Arc::new(move |k: &str| crate::config::env_setting(k, &env_file));
        let env2 = env.clone();
        let secrets = Secrets::os(move |k| env2(k));
        let saved = match &store {
            Some(s) => s.get_setting(ALERTS_KEY).await.ok().flatten(),
            None => None,
        };
        let settings = AlertSettings::from_json(saved.as_ref()).unwrap_or_default();
        Alerts::new(store, secrets, env, API, settings)
    }

    /// Alerts with a given credential store, environment and API address (tests).
    pub fn new(store: Option<Store>, secrets: Secrets, env: Env, api: impl Into<String>, settings: AlertSettings) -> Alerts {
        let alerts = Alerts { store, secrets, env, api: api.into(), state: Mutex::new(AlertManager::new(settings, None)) };
        let client = alerts.client_for(&alerts.settings());
        alerts.state.lock().unwrap().client = client;
        alerts
    }

    /// A client from the saved token and the settings' chat id; None without a token (`telegram_client`).
    fn client_for(&self, settings: &AlertSettings) -> Option<TelegramClient> {
        let (token, _) = self.secrets.get_secret(TELEGRAM_BOT_TOKEN);
        token.map(|t| TelegramClient::new(t, settings.resolved_chat_id(&*self.env), self.api.clone()))
    }

    pub fn secrets(&self) -> &Secrets {
        &self.secrets
    }

    pub fn settings(&self) -> AlertSettings {
        self.state.lock().unwrap().settings.clone()
    }

    pub fn configured(&self) -> bool {
        self.state.lock().unwrap().configured()
    }

    pub fn last_error(&self) -> Option<String> {
        self.state.lock().unwrap().last_error.clone()
    }

    /// Telegram's send when alerts are set up and on; None otherwise (drawing alerts are then
    /// only logged), as `Runtime.telegram_send`.
    pub fn sender(&self) -> Option<Sender> {
        let st = self.state.lock().unwrap();
        if !(st.configured() && st.settings.enabled) {
            return None;
        }
        let client = st.client.clone()?;
        Some(Arc::new(move |text: String| {
            let client = client.clone();
            Box::pin(async move { client.send(&text).await.map_err(|e| e.0) })
        }))
    }

    /// After a scan: alert the order blocks the new bars entered (`AlertManager.check`). The keys
    /// sent. `trade_bias` is the active bias's direction (`engine::bias(store).1`).
    pub async fn check(&self, source: &str, symbol: &str, result: &ScanResult, m1: &[Bar], trade_bias: Option<&str>) -> Vec<String> {
        let (due, client) = {
            let mut st = self.state.lock().unwrap();
            let due = st.due(source, symbol, result, m1, trade_bias);
            (due, st.client.clone())
        };
        let Some(client) = client else { return vec![] };
        let mut sent = Vec::new();
        for d in due {
            if self.already(&d.key).await {
                continue;
            }
            let mut row = d.row;
            match client.send(&d.text).await {
                Err(e) => {
                    log::warn!("alert not sent: {e}");
                    self.state.lock().unwrap().last_error = Some(e.0.clone());
                    row.insert("status".into(), "failed".into());
                    row.insert("error".into(), e.0.into());
                    self.record(&d.key, row).await;
                }
                Ok(()) => {
                    self.state.lock().unwrap().last_error = None;
                    row.insert("status".into(), "sent".into());
                    row.insert("error".into(), Value::Null);
                    self.record(&d.key, row).await;
                    sent.push(d.key);
                }
            }
        }
        sent
    }

    async fn already(&self, key: &str) -> bool {
        match &self.store {
            Some(s) => s.alert_sent(key).await.unwrap_or_else(|e| {
                log::warn!("alert log: {e}");
                false
            }),
            None => self.state.lock().unwrap().sent_memory.contains(key),
        }
    }

    async fn record(&self, key: &str, row: Map<String, Value>) {
        match &self.store {
            Some(s) => {
                if let Err(e) = s.log_alert(&row).await {
                    log::warn!("alert log: {e}");
                }
            }
            None if row.get("status").and_then(Value::as_str) == Some("sent") => {
                self.state.lock().unwrap().sent_memory.insert(key.to_string());
            }
            None => {}
        }
    }

    pub async fn send_test(&self, symbol: &str) -> Result<(), TelegramError> {
        let client = {
            let st = self.state.lock().unwrap();
            if !st.configured() {
                return Err(TelegramError("Add the bot token and chat id in Settings > Telegram alerts first".into()));
            }
            st.client.clone()
        };
        client.expect("configured").send(&test_message(symbol)).await
    }

    // ---- the routes ----

    /// `GET /api/alerts` (`alerts_payload`): never the token, only where it comes from.
    /// `drawing_error` is the drawing alerts' last error, shown when the order blocks have none.
    pub async fn get(&self, drawing_error: Option<String>) -> Answer {
        let (_, token_source) = self.secrets.get_secret(TELEGRAM_BOT_TOKEN);
        let (token_set, chat_id_set, configured, settings, last_error) = {
            let st = self.state.lock().unwrap();
            let c = st.client.as_ref();
            (c.is_some_and(|c| !c.token.is_empty()), c.is_some_and(|c| c.chat_id.as_deref().is_some_and(|x| !x.is_empty())),
             st.configured(), st.settings.to_json(), st.last_error.clone().filter(|e| !e.is_empty()))
        };
        let recent = match &self.store {
            Some(s) => s.recent_alerts(20).await.map_err(internal)?.into_iter().map(Value::Object).collect(),
            None => vec![],
        };
        Ok(json!({
            "editable": true,
            "token_set": token_set,
            "token_source": token_source,
            "chat_id_set": chat_id_set,
            "configured": configured,
            "settings": settings,
            "last_error": last_error.or(drawing_error),
            "recent": recent,
        }))
    }

    /// `PUT /api/alerts`: the settings, and the write-only token fields (`telegram_bot_token`,
    /// `forget_telegram_bot_token`).
    pub async fn put(&self, body: &Map<String, Value>, drawing_error: Option<String>) -> Answer {
        let new = AlertSettings::from_json(Some(&Value::Object(body.clone()))).map_err(internal)?;
        let errors = new.validate();
        if !errors.is_empty() {
            return Err(Refusal::new(422, errors.join("; ")));
        }
        let token_changed = self.secrets.update_secrets(body, &[TELEGRAM_BOT_TOKEN]);
        let old_chat = self.settings().resolved_chat_id(&*self.env);
        let chat_changed = new.resolved_chat_id(&*self.env) != old_chat;
        if let Some(s) = &self.store {
            s.set_setting(ALERTS_KEY, &new.to_json()).await.map_err(internal)?;
        }
        let client = (token_changed || chat_changed).then(|| self.client_for(&new));
        {
            let mut st = self.state.lock().unwrap();
            st.settings = new;
            if let Some(client) = client {
                st.client = client;
            }
        }
        self.get(drawing_error).await
    }

    /// `GET /api/alerts/chats`: chats that wrote to the bot, to pick the chat id.
    pub async fn chats(&self) -> Answer {
        let Some(client) = self.state.lock().unwrap().client.clone() else {
            return Err(Refusal::new(409, "Save the bot token first"));
        };
        match client.chats().await {
            Ok(chats) => Ok(json!({"chats": chats})),
            Err(e) => Err(Refusal::new(502, e.0)),
        }
    }

    /// `POST /api/alerts/test`: a test message for `symbol`.
    pub async fn test(&self, symbol: &str) -> Answer {
        self.send_test(symbol).await.map_err(|e| Refusal::new(502, e.0))?;
        Ok(json!({"ok": true}))
    }
}

fn internal(e: String) -> Refusal {
    log::error!("alerts: {e}");
    Refusal::new(500, "Internal Server Error")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_as_python_formats_them() {
        assert_eq!(thousands(2471.0, 2), "2,471.00");
        assert_eq!(thousands(1234567.891, 2), "1,234,567.89");
        assert_eq!(thousands(-1234.5, 1), "-1,234.5");
        assert_eq!(thousands(999.999, 2), "1,000.00");
        assert_eq!(thousands(-0.001, 2), "-0.00");
        assert_eq!(thousands(12.0, 0), "12");
        assert_eq!(html_escape("<a&'\">"), "&lt;a&amp;&#x27;&quot;&gt;");
    }

    #[test]
    fn risk_as_bias_py() {
        assert_eq!(setup_risk("buy", None), None);
        assert_eq!(setup_risk("buy", Some("bullish")), Some("on"));
        assert_eq!(setup_risk("sell", Some("bullish")), Some("off"));
        assert_eq!(setup_risk("sell", Some("bearish")), Some("on"));
        assert_eq!(setup_risk("sell", Some("neutral")), Some("no_trade"));
    }
}
