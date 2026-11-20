//! Calendar settings (`news.CalendarSettings`): which news the risk-time card and the chart show.
//! Read and checked as the Python server does, with its messages. The calendar itself (the weekly
//! fetch, past reactions) is still Python's: it reads these settings from the store.

use serde_json::{json, Map, Value};

use crate::settings_routes::Refused;
use crate::store::Store;

pub const CALENDAR_KEY: &str = "calendar";
pub const DEFAULT_URL: &str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";
pub const IMPACTS: [&str; 3] = ["High", "Medium", "Low"];

/// Python's `bool(value)` for a JSON value.
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

/// Python's `repr(s)` for a string.
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() || (c.is_whitespace() && c != ' ') => {
                let n = c as u32;
                if n < 0x100 {
                    out.push_str(&format!("\\x{n:02x}"));
                } else if n < 0x10000 {
                    out.push_str(&format!("\\u{n:04x}"));
                } else {
                    out.push_str(&format!("\\U{n:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// A list of strings, or None for anything else.
fn strings(v: &Value) -> Option<Vec<&str>> {
    v.as_array()?.iter().map(Value::as_str).collect()
}

/// `CalendarSettings.from_dict(body)` then `.validate()`: the settings as `to_dict()` gives them.
pub fn settings(body: &Map<String, Value>) -> Result<Value, Refused> {
    let invalid = |m: &str| Err(Refused::Invalid(m.into()));
    let url = match body.get("url") {
        None | Some(Value::Null) => DEFAULT_URL.to_string(),
        Some(Value::String(s)) if s.is_empty() => DEFAULT_URL.to_string(),
        Some(Value::String(s)) => s.trim().to_string(),
        Some(_) => return invalid("The calendar URL must be text"),
    };
    let currencies = match body.get("currencies") {
        None => vec!["USD".to_string()],
        Some(v) => match strings(v) {
            Some(c) => c.iter().map(|c| c.trim()).filter(|c| !c.is_empty()).map(str::to_uppercase).collect(),
            None => return invalid("Currencies must be a list of currency codes, like USD"),
        },
    };
    let impacts: Vec<String> = match body.get("impacts") {
        None => vec!["High".to_string()],
        Some(v) => match strings(v) {
            Some(i) => i.into_iter().map(String::from).collect(),
            None => return invalid("Impacts must be a list of High, Medium or Low"),
        },
    };
    let enabled = body.get("enabled").map_or(true, truthy);

    let mut errors = Vec::new();
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        errors.push("The calendar URL must start with http:// or https://".to_string());
    }
    for i in impacts.iter().filter(|i| !IMPACTS.contains(&i.as_str())) {
        errors.push(format!("Unknown impact {}", py_repr(i)));
    }
    if currencies.is_empty() {
        errors.push("Pick at least one currency".to_string());
    }
    if !errors.is_empty() {
        return Err(Refused::Invalid(errors.join("; ")));
    }
    Ok(json!({"enabled": enabled, "url": url, "currencies": currencies, "impacts": impacts}))
}

/// Check and save the settings (PUT /api/calendar); the answer comes from Python's calendar.
pub async fn save(store: &Store, body: &Map<String, Value>) -> Result<Value, Refused> {
    let new = settings(body)?;
    store.set_setting(CALENDAR_KEY, &new).await?;
    Ok(new)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(body: Value) -> Result<Value, String> {
        settings(body.as_object().unwrap()).map_err(|e| match e {
            Refused::Invalid(m) | Refused::Store(m) => m,
        })
    }

    #[test]
    fn defaults_and_cleaning() {
        assert_eq!(check(json!({})).unwrap(),
                   json!({"enabled": true, "url": DEFAULT_URL, "currencies": ["USD"], "impacts": ["High"]}));
        let s = check(json!({"enabled": 0, "url": " https://x.test/c.json ", "currencies": [" usd ", "", "eur"]})).unwrap();
        assert_eq!(s, json!({"enabled": false, "url": "https://x.test/c.json", "currencies": ["USD", "EUR"],
                             "impacts": ["High"]}));
        assert_eq!(check(json!({"enabled": "no", "url": null})).unwrap()["enabled"], json!(true)); // bool("no")
        assert_eq!(check(json!({"enabled": null})).unwrap()["enabled"], json!(false));
    }

    #[test]
    fn errors_as_python_words_them() {
        assert_eq!(check(json!({"url": "ftp://x", "impacts": ["Huge", "it's"], "currencies": [" "]})).unwrap_err(),
                   "The calendar URL must start with http:// or https://; Unknown impact 'Huge'; \
                    Unknown impact \"it's\"; Pick at least one currency");
        assert_eq!(check(json!({"url": 5})).unwrap_err(), "The calendar URL must be text");
        assert_eq!(check(json!({"currencies": "USD"})).unwrap_err(),
                   "Currencies must be a list of currency codes, like USD");
        assert_eq!(check(json!({"impacts": null})).unwrap_err(), "Impacts must be a list of High, Medium or Low");
        assert_eq!(py_repr("a\tb\\'\u{a0}"), "\"a\\tb\\\\'\\xa0\"");
    }
}
