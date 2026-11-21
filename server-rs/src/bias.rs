//! The trader's directional bias (`bias.py`), #96 step 9f: `GET /api/bias` and `PUT /api/bias`.
//!
//! The bias is set by hand, through this route only; nothing here sets it on its own. It is kept
//! in the database (setting `bias`), where the engine ([`crate::engine::bias`]) and the Python
//! follower read it. A bias can expire at the New York close (17:00 NY) today or on Friday.

use chrono::{DateTime, Datelike, Duration, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::America::New_York;
use serde_json::{json, Map, Value};

use crate::calendar::py_repr;
use crate::engine::{self, py_iso_utc, Answer, Refusal, BIAS_KEY};
use crate::journal::service::py_str;
use crate::store::Store;

pub const DIRECTIONS: [&str; 3] = ["bullish", "bearish", "neutral"];
pub const EXPIRY: [&str; 3] = ["day", "week", "none"];
const NY_CLOSE_HOUR: u32 = 17;
const MAX_NOTE: usize = 500;

fn store_failed(e: String) -> Refusal {
    log::error!("database: {e}");
    Refusal::new(500, "The database couldn't be read or written. Try again, and restart Wednesday if this stays.")
}

/// Python's truthiness of a JSON value.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

/// `str(v or default)`
fn text_or(v: Option<&Value>, default: &str) -> String {
    if truthy(v) { py_str(v.unwrap()) } else { default.to_string() }
}

/// When a bias set at `now` runs out (`expiry_time`): the next 17:00 NY close, or Friday's; a
/// close on the weekend rolls to Monday. None for `none`. Wall-clock arithmetic, as Python's.
pub fn expiry_time(expiry: &str, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
    if expiry == "none" {
        return None;
    }
    let ny: NaiveDateTime = now.with_timezone(&New_York).naive_local();
    let mut close = ny.date().and_time(NaiveTime::from_hms_opt(NY_CLOSE_HOUR, 0, 0).unwrap());
    if ny >= close {
        close += Duration::days(1);
    }
    if expiry == "week" {
        let wd = close.weekday().num_days_from_monday() as i64;
        close += Duration::days((4 - wd).rem_euclid(7)); // Friday
    }
    while close.weekday().num_days_from_monday() >= 5 {
        close += Duration::days(1);
    }
    // 17:00 is never in a daylight saving gap or overlap in New York.
    New_York.from_local_datetime(&close).earliest().map(|t| t.with_timezone(&Utc))
}

/// `TradeBias.validate()`: the same messages, in the same order.
pub fn validate(direction: &str, note: &str, expiry: &str) -> Vec<String> {
    let mut errors = Vec::new();
    if !DIRECTIONS.contains(&direction) {
        errors.push(format!("Unknown bias {}", py_repr(direction)));
    }
    if !EXPIRY.contains(&expiry) {
        errors.push(format!("Unknown expiry {}", py_repr(expiry)));
    }
    if note.chars().count() > MAX_NOTE {
        errors.push("Keep the note under 500 characters".into());
    }
    errors
}

/// What `PUT /api/bias` saves for `body` at `now` (`TradeBias(...).stamped().to_dict()`): None
/// to clear it (no direction), or the 422 message.
pub fn stamp(body: &Map<String, Value>, now: DateTime<Utc>) -> Result<Option<Value>, String> {
    if !truthy(body.get("direction")) {
        return Ok(None);
    }
    let direction = py_str(&body["direction"]);
    let note = text_or(body.get("note"), "");
    let expiry = text_or(body.get("expiry"), "day");
    let errors = validate(&direction, &note, &expiry);
    if !errors.is_empty() {
        return Err(errors.join("; "));
    }
    let until = expiry_time(&expiry, now);
    let expired = until.is_some_and(|u| now >= u);
    Ok(Some(json!({
        "direction": direction,
        "note": note.trim(),
        "expiry": expiry,
        "set_at": py_iso_utc(now),
        "expires_at": until.map(py_iso_utc),
        "expired": expired,
    })))
}

/// `GET /api/bias`: the saved bias with `expired`, or null.
pub async fn get(store: Option<&Store>) -> Answer {
    let (bias, _) = engine::bias(store).await;
    Ok(json!({"bias": bias}))
}

/// `PUT /api/bias` ({direction, note, expiry}; a null direction clears it). Needs the database:
/// without one, Python answers (409 there without `--serve`).
pub async fn put(store: &Store, body: &Map<String, Value>) -> Answer {
    match stamp(body, Utc::now()).map_err(|e| Refusal::new(422, e))? {
        None => {
            store.delete_setting(BIAS_KEY).await.map_err(store_failed)?;
            Ok(json!({"bias": null}))
        }
        Some(saved) => {
            store.set_setting(BIAS_KEY, &saved).await.map_err(store_failed)?;
            get(Some(store)).await
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn body(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn expiry_rolls_to_the_next_close() {
        // Wednesday 2026-03-04 15:00 UTC = 10:00 NY (EST): today 17:00 NY = 22:00 UTC.
        assert_eq!(expiry_time("day", at("2026-03-04T15:00:00Z")).map(py_iso_utc).unwrap(), "2026-03-04T22:00:00+00:00");
        assert_eq!(expiry_time("week", at("2026-03-04T15:00:00Z")).map(py_iso_utc).unwrap(), "2026-03-06T22:00:00+00:00");
        // Friday after the close: Monday's.
        assert_eq!(expiry_time("day", at("2026-03-06T23:00:00Z")).map(py_iso_utc).unwrap(), "2026-03-09T21:00:00+00:00");
        assert_eq!(expiry_time("none", at("2026-03-06T23:00:00Z")), None);
    }

    #[test]
    fn messages_are_pythons() {
        let now = at("2026-03-04T15:00:00Z");
        assert_eq!(stamp(&body(json!({"direction": "up"})), now), Err("Unknown bias 'up'".to_string()));
        assert_eq!(stamp(&body(json!({"direction": "up", "expiry": "month", "note": "x".repeat(501)})), now),
                   Err("Unknown bias 'up'; Unknown expiry 'month'; Keep the note under 500 characters".to_string()));
        assert_eq!(stamp(&body(json!({"direction": ""})), now), Ok(None));
        assert_eq!(stamp(&body(json!({"direction": null})), now), Ok(None));
        let saved = stamp(&body(json!({"direction": "bearish", "note": "  CPI hot ", "expiry": null})), now).unwrap().unwrap();
        assert_eq!(saved, json!({"direction": "bearish", "note": "CPI hot", "expiry": "day",
                                 "set_at": "2026-03-04T15:00:00+00:00", "expires_at": "2026-03-04T22:00:00+00:00",
                                 "expired": false}));
    }
}
