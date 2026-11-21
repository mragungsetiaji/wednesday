//! `DrawingAlerts`: checks the armed drawing alerts of the running source and symbol after each
//! scan (on the closed M1 bars that arrived since the last check) and on each live tick, and sends
//! what fires. Each firing is logged in `alert_log` under its own key before anything else, so a
//! bar never fires twice, even across restarts.

use std::collections::HashMap;
use std::sync::Mutex;

use chrono::Utc;
use serde_json::{json, Map, Value};

use super::clean::{conditions, timestamp, Stamp};
use super::events::{bar_events, describe, format_message, tick_event, Drawing};
use crate::notify::Sender;
use crate::scan::{unix, Bar, Time};
use crate::store::{Arg, Store};

type Stream = (String, String);

/// An armed alert with its drawing.
#[derive(Clone, Debug)]
struct Armed {
    drawing: Drawing,
    alert: Map<String, Value>,
}

impl Armed {
    fn str(&self, k: &str) -> Option<&str> {
        self.alert.get(k).and_then(Value::as_str)
    }
    fn flag(&self, k: &str) -> bool {
        self.alert.get(k).and_then(Value::as_bool).unwrap_or(false)
    }
}

#[derive(Default)]
struct Memory {
    last_bar: HashMap<Stream, Time>,
    last_tick: HashMap<Stream, f64>,
}

/// The checker the engine runs after each scan (`check`) and on each tick (`check_tick`).
#[derive(Default)]
pub struct DrawingAlerts {
    memory: tokio::sync::Mutex<Memory>, // one check at a time
    cache: Mutex<HashMap<Stream, Vec<Armed>>>,
    last_error: Mutex<Option<String>>,
}

/// What a firing is about.
struct Firing<'a> {
    level: f64,
    what: &'a str,
    price: f64,
    when_key: String,
    bar_time: Option<String>,
    intrabar: bool,
}

impl DrawingAlerts {
    pub fn new() -> DrawingAlerts {
        DrawingAlerts::default()
    }

    /// A drawing or an alert changed: read them again on the next check.
    pub fn invalidate(&self) {
        self.cache.lock().unwrap().clear();
    }

    /// The last send's error (None once a message went out again).
    pub fn last_error(&self) -> Option<String> {
        self.last_error.lock().unwrap().clone()
    }

    /// Alerts fired by the closed M1 bars since the last check. Returns the keys logged.
    pub async fn check(&self, store: &Store, source: &str, symbol: &str, m1: &[Bar], send: Option<&Sender>) -> Vec<String> {
        let Some(newest) = m1.last() else { return vec![] };
        let mut memory = self.memory.lock().await;
        let stream = (source.to_string(), symbol.to_string());
        let last = memory.last_bar.insert(stream, newest.time);
        let Some(last) = last else { return vec![] }; // first scan of this stream: only bars from now on
        let new: Vec<Bar> = m1.iter().filter(|b| b.time > last).cloned().collect();
        if new.is_empty() {
            return vec![];
        }
        let prev_close = m1.iter().rev().find(|b| b.time <= last).map(|b| b.close);
        let mut fired = Vec::new();
        let armed = match self.armed(store, source, symbol).await {
            Ok(a) => a,
            Err(e) => {
                log::warn!("drawing alerts: {e}");
                return fired;
            }
        };
        for mut a in armed {
            let (mut bars, mut before) = (new.as_slice(), prev_close);
            let after: Vec<Bar>;
            if let Some(armed_bar) = a.alert.get("armed_bar").and_then(Value::as_i64) {
                // Only bars after the one that closed last when it was armed.
                let Some(first) = new.iter().position(|b| unix(b.time) > armed_bar) else { continue };
                before = if first > 0 { Some(new[first - 1].close) } else { prev_close };
                after = new.iter().filter(|b| unix(b.time) > armed_bar).cloned().collect();
                bars = &after;
            }
            let condition = a.str("condition").unwrap_or("").to_string();
            for (t, level, what) in bar_events(&a.drawing, &condition, bars, before) {
                let bar = bars.iter().find(|b| unix(b.time) == t).expect("an event's bar");
                let firing = Firing {
                    level,
                    what,
                    price: bar.close,
                    when_key: format!("bar{t}"),
                    bar_time: Some(bar.time.format("%Y-%m-%d %H:%M").to_string()),
                    intrabar: false,
                };
                match self.fire(store, source, symbol, &mut a, firing, send).await {
                    Ok(Some(key)) => {
                        fired.push(key);
                        if a.str("mode") == Some("once") {
                            break;
                        }
                    }
                    Ok(None) => {}
                    Err(e) => {
                        log::warn!("drawing alerts: {e}");
                        return fired;
                    }
                }
            }
        }
        fired
    }

    /// Intrabar alerts: the live price moved through a line or into / out of a box since the last tick.
    pub async fn check_tick(&self, store: &Store, source: &str, symbol: &str, price: f64, at: Time,
                            send: Option<&Sender>) -> Vec<String> {
        let mut memory = self.memory.lock().await;
        let before = memory.last_tick.insert((source.to_string(), symbol.to_string()), price);
        let Some(before) = before else { return vec![] };
        if before == price || !price.is_finite() {
            return vec![];
        }
        let t = unix(at);
        let mut fired = Vec::new();
        let armed = match self.armed(store, source, symbol).await {
            Ok(a) => a,
            Err(e) => {
                log::warn!("drawing alerts: {e}");
                return fired;
            }
        };
        for mut a in armed {
            if !a.flag("intrabar") {
                continue;
            }
            let condition = a.str("condition").unwrap_or("").to_string();
            if let Some((level, what)) = tick_event(&a.drawing, &condition, before, price, t) {
                let firing = Firing { level, what, price, when_key: format!("tick{}", t.div_euclid(60)), bar_time: None,
                                      intrabar: true };
                match self.fire(store, source, symbol, &mut a, firing, send).await {
                    Ok(Some(key)) => fired.push(key),
                    Ok(None) => {}
                    Err(e) => {
                        log::warn!("drawing alerts: {e}");
                        return fired;
                    }
                }
            }
        }
        drop(memory);
        fired
    }

    /// The armed alerts of a stream (read once, then from the cache); an expired one is disarmed.
    async fn armed(&self, store: &Store, source: &str, symbol: &str) -> Result<Vec<Armed>, String> {
        let stream = (source.to_string(), symbol.to_string());
        let cached = self.cache.lock().unwrap().get(&stream).cloned();
        let cached = match cached {
            Some(c) => c,
            None => {
                let loaded = load(store, source, symbol).await?;
                self.cache.lock().unwrap().insert(stream.clone(), loaded.clone());
                loaded
            }
        };
        let now = Utc::now();
        let mut out = Vec::new();
        for a in cached {
            if !a.flag("armed") {
                continue;
            }
            let expired = match a.alert.get("expires_at") {
                Some(Value::String(s)) if !s.is_empty() => matches!(timestamp(&json!(s)), Stamp::At(t) if t <= now),
                _ => false,
            };
            if expired {
                self.patch_cache(&stream, &a.drawing.id, &json!({"armed": false}));
                store.drawing_alert_update(&a.drawing.id, vec![("armed", Arg::Bool(false))]).await?;
                continue;
            }
            out.push(a);
        }
        Ok(out)
    }

    fn patch_cache(&self, stream: &Stream, drawing_id: &str, patch: &Value) {
        if let Some(list) = self.cache.lock().unwrap().get_mut(stream) {
            for a in list.iter_mut().filter(|a| a.drawing.id == drawing_id) {
                for (k, v) in patch.as_object().unwrap() {
                    a.alert.insert(k.clone(), v.clone());
                }
            }
        }
    }

    /// Log the firing under its key, send it, and record it on the alert; None when this bar (or
    /// minute of ticks) fired it already.
    async fn fire(&self, store: &Store, source: &str, symbol: &str, a: &mut Armed, f: Firing<'_>, send: Option<&Sender>)
        -> Result<Option<String>, String> {
        let d = &a.drawing;
        let base = format!("drawing|{source}|{symbol}|{}|", d.id);
        let key = format!("{base}{}", f.when_key);
        if store.alert_logged(&key).await? {
            return Ok(None); // this bar (or minute of ticks) already fired it
        }
        if let Some(t) = f.when_key.strip_prefix("bar") {
            let minute = t.parse::<i64>().map_err(|e| e.to_string())?.div_euclid(60);
            if store.alert_logged(&format!("{base}tick{minute}")).await? {
                return Ok(None); // a live tick in this minute fired it already
            }
        }
        let note = a.str("note").filter(|n| !n.is_empty());
        let mode = a.str("mode").unwrap_or("").to_string();
        let sent_at = crate::engine::py_iso_utc(Utc::now());
        let mut what = f.what.to_string();
        if let Some(first) = what.get(..1) {
            what = first.to_uppercase() + &what[1..];
        }
        let summary = format!("{what} your {}{}{}", describe(&d.kind), note.map(|n| format!(" '{n}'")).unwrap_or_default(),
                              if f.intrabar { " (intrabar)" } else { "" });
        let (mut status, mut error) = ("logged", Value::Null);
        if let Some(send) = send {
            let text = format_message(symbol, d, note, &mode, f.level, f.what, f.price, f.bar_time.as_deref(), f.intrabar);
            match send(text).await {
                Ok(()) => {
                    status = "sent";
                    *self.last_error.lock().unwrap() = None;
                }
                Err(e) => {
                    log::warn!("drawing alert not sent: {e}");
                    status = "failed";
                    error = e.clone().into();
                    *self.last_error.lock().unwrap() = Some(e);
                }
            }
        }
        let mut row = Map::new();
        for (k, v) in [
            ("key", json!(key)), ("source", json!(source)), ("symbol", json!(symbol)),
            ("timeframe", json!(if f.intrabar { "tick" } else { "M1" })), ("kind", json!("drawing")),
            ("priority", json!(a.str("condition"))), ("entry", json!(f.level)), ("price", json!(f.price)),
            ("sent_at", json!(sent_at)), ("summary", json!(summary)), ("status", json!(status)), ("error", error),
        ] {
            row.insert(k.into(), v);
        }
        store.log_alert(&row).await?;
        let fires = a.alert.get("fires").and_then(Value::as_i64).unwrap_or(0) + 1;
        let mut patch = json!({"fired_at": sent_at, "fired_price": f.price, "fires": fires});
        let mut args = vec![("fired_at", Arg::Str(Some(sent_at.clone()))), ("fired_price", Arg::Float(Some(f.price))),
                            ("fires", Arg::Int(Some(fires)))];
        if mode == "once" {
            patch["armed"] = json!(false);
            args.push(("armed", Arg::Bool(false)));
        }
        for (k, v) in patch.as_object().unwrap() {
            a.alert.insert(k.clone(), v.clone());
        }
        self.patch_cache(&(source.to_string(), symbol.to_string()), &a.drawing.id, &patch);
        store.drawing_alert_update(&a.drawing.id, args).await?;
        Ok(Some(key))
    }
}

/// The armed alerts whose drawing is there and can carry one (a deleted drawing keeps its alert, for undo).
async fn load(store: &Store, source: &str, symbol: &str) -> Result<Vec<Armed>, String> {
    let drawings: HashMap<String, Drawing> = store
        .drawings(source, symbol)
        .await?
        .iter()
        .filter_map(Drawing::from_row)
        .map(|d| (d.id.clone(), d))
        .collect();
    let mut out = Vec::new();
    for alert in store.drawing_alerts(source, symbol).await? {
        let id = alert.get("drawing_id").and_then(Value::as_str).unwrap_or("");
        let armed = alert.get("armed").and_then(Value::as_bool).unwrap_or(false);
        if let Some(d) = drawings.get(id) {
            if armed && conditions(&d.kind).is_some() {
                out.push(Armed { drawing: d.clone(), alert });
            }
        }
    }
    Ok(out)
}
