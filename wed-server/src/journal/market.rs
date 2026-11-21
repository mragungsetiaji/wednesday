//! The market as it stood when a journal entry was made (`journal/market.py`, #14), frozen into
//! the entry so a later review sees what the trader saw, not what the scan says now.

use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};

use crate::engine::{self, py_iso_utc, Runtime};
use crate::store::Store;

pub const TOP_SETUPS: usize = 3;
pub const NEXT_NEWS: usize = 3;

fn key<'a>(v: &'a Value, k: &str) -> &'a Value {
    v.get(k).unwrap_or(&Value::Null)
}

/// Python's truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

fn setup(s: &Value) -> Value {
    let empty = Value::Object(Map::new());
    let meta = if truthy(key(s, "meta")) { key(s, "meta") } else { &empty };
    let entry = meta.get("entry").unwrap_or_else(|| key(s, "top")); // meta.get("entry", s.get("top"))
    json!({"timeframe": key(s, "timeframe"), "entry": entry, "sl": key(meta, "sl"), "risk": key(s, "risk"),
           "priority": key(meta, "priority"), "swing": key(meta, "swing"), "distance": key(s, "distance")})
}

/// An event's time as the string Python compares and sorts by.
fn time_of(e: &Value) -> String {
    match key(e, "time") {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// `freeze`: a small copy of the scan (price, bias, the nearest setups each side, each
/// timeframe's last break) and the next high-impact releases. `scan` is the scan result's JSON
/// with the bias applied (`ScanResult::to_json(active)`), `bias` the active bias's dict, `news`
/// the upcoming events (`Calendar.upcoming()`), `price` the live price.
pub fn freeze(scan: Option<&Value>, bias: Option<&Value>, news: &[Value], symbol: &str, source: &str,
              price: Option<f64>, now: DateTime<Utc>) -> Value {
    let at = py_iso_utc(now);
    let empty = Value::Object(Map::new());
    let scan = scan.filter(|s| truthy(s)).unwrap_or(&empty);
    let setups = if truthy(key(scan, "setups")) { key(scan, "setups") } else { &empty };
    let mut sides = Map::new();
    for side in ["sell", "buy"] {
        let list = key(setups, side).as_array().map(|a| a.iter().take(TOP_SETUPS).map(setup).collect::<Vec<_>>());
        sides.insert(side.into(), json!(list.unwrap_or_default()));
    }
    let structure: Vec<Value> = key(scan, "timeframes")
        .as_array()
        .map(|tfs| {
            tfs.iter()
                .filter(|t| truthy(key(t, "bias")))
                .map(|t| {
                    let b = key(t, "bias");
                    json!({"timeframe": key(t, "timeframe"), "direction": key(b, "direction"), "event": key(b, "event"),
                           "level": key(b, "level")})
                })
                .collect()
        })
        .unwrap_or_default();
    let mut upcoming: Vec<&Value> = news.iter().collect();
    upcoming.sort_by_key(|e| time_of(e)); // stable, as sorted()
    let upcoming: Vec<Value> = upcoming
        .into_iter()
        .filter(|e| time_of(e) >= at)
        .take(NEXT_NEWS)
        .map(|e| json!({"time": key(e, "time"), "currency": key(e, "currency"), "title": key(e, "title"),
                        "impact": key(e, "impact")}))
        .collect();
    let bias = match bias.filter(|b| truthy(b)) {
        Some(b) => {
            let mut out = Map::new();
            for k in ["direction", "note", "expires_at"] {
                if let Some(v) = b.get(k) {
                    out.insert(k.into(), v.clone());
                }
            }
            Value::Object(out)
        }
        None => Value::Null,
    };
    json!({
        "at": at,
        "symbol": symbol,
        "source": source,
        "bar_time": key(scan, "time"),
        "price": price.map_or_else(|| key(scan, "price").clone(), |p| json!(p)),
        "bias": bias,
        "setups": sides,
        "structure": structure,
        "news": upcoming,
    })
}

/// The market now (`market_now` in server.py): the engine's last scan with the active bias, the
/// live price, and `news` (the calendar's upcoming events, which the caller gives).
pub async fn market_now(rt: &Arc<Runtime>, store: Option<&Store>, news: &[Value]) -> Value {
    let e = rt.engine();
    let st = e.state();
    let (payload, active) = engine::bias(store).await;
    let bias = if active.is_some() { payload } else { None }; // trade_bias(): None once expired
    let scan = st.result.as_ref().map(|r| r.to_json(active.as_deref()));
    freeze(scan.as_ref(), bias.as_ref(), news, &e.symbol, &e.source, st.live.as_ref().map(|l| l.price), Utc::now())
}
