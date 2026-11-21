//! The weekly feed's events (`news.parse_events`) and the history's rows (`news_stats.store_rows`),
//! with the bits of Python's string and time handling they lean on.

use chrono::{DateTime, Duration, FixedOffset, Local, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use serde_json::{json, Map, Value};

// ---- Python's strings ----

/// `str.isspace()` for one character: Unicode's white space plus the four separators Python adds.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// The zero of each run of ten Unicode decimal digits (category Nd, Unicode 14 as Python 3.11 has it).
const DIGIT_ZEROS: [u32; 66] = [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6, 0xe50, 0xed0,
    0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620,
    0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0,
    0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60,
    0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
];

/// The value of a decimal digit as Python's `\d` and `float()` read it (any script's digits).
pub fn py_digit(c: char) -> Option<u32> {
    let n = c as u32;
    DIGIT_ZEROS.iter().find(|&&z| (z..z + 10).contains(&n)).map(|z| n - z)
}

/// `s.strip()`
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// `" ".join(s.split())`
pub fn py_squash(s: &str) -> String {
    s.split(py_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
}

/// `s[:n]` (characters, not bytes).
pub fn py_take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `s.capitalize()`: the first character upper case, the rest lower case.
pub fn py_capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().chain(chars.as_str().to_lowercase().chars()).collect(),
        None => String::new(),
    }
}

/// `str(v)` of a JSON value as Python holds it.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => crate::drawings::py::repr(other),
    }
}

/// Python's `bool(value)` for a JSON value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

// ---- times ----

/// `datetime.fromisoformat(s)`: aware when the text has an offset.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum IsoTime {
    Aware(DateTime<FixedOffset>),
    Naive(NaiveDateTime),
}

impl IsoTime {
    /// The instant; a naive time is read in the machine's zone, as `datetime.timestamp()` does.
    pub fn instant(self) -> Option<DateTime<Utc>> {
        match self {
            IsoTime::Aware(d) => Some(d.with_timezone(&Utc)),
            IsoTime::Naive(n) => Local.from_local_datetime(&n).earliest().map(|d| d.with_timezone(&Utc)),
        }
    }

    pub fn aware(self) -> Option<DateTime<Utc>> {
        match self {
            IsoTime::Aware(d) => Some(d.with_timezone(&Utc)),
            IsoTime::Naive(_) => None,
        }
    }
}

fn digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn num(s: &str) -> Option<u32> {
    digits(s).then(|| s.parse().ok()).flatten()
}

fn iso_date(s: &str) -> Option<NaiveDate> {
    let (y, m, d) = match s.len() {
        10 if &s[4..5] == "-" && &s[7..8] == "-" => (&s[..4], &s[5..7], &s[8..]),
        8 => (&s[..4], &s[4..6], &s[6..]),
        _ => return None,
    };
    NaiveDate::from_ymd_opt(num(y)? as i32, num(m)?, num(d)?)
}

/// `HH[:MM[:SS[.f]]]` or `HHMM[SS[.f]]`, fractions cut to microseconds.
fn iso_clock(s: &str) -> Option<NaiveTime> {
    let (main, frac) = match s.find(['.', ',']) {
        Some(i) => (&s[..i], Some(&s[i + 1..])),
        None => (s, None),
    };
    let parts: Vec<&str> = if main.contains(':') {
        main.split(':').collect()
    } else {
        if main.len() % 2 != 0 {
            return None;
        }
        (0..main.len()).step_by(2).map(|i| &main[i..i + 2]).collect()
    };
    if parts.is_empty() || parts.len() > 3 || parts.iter().any(|p| p.len() != 2) {
        return None;
    }
    let mut v = [0u32; 3];
    for (i, p) in parts.iter().enumerate() {
        v[i] = num(p)?;
    }
    let micro = match frac {
        None => 0,
        Some(f) if parts.len() == 3 && (f.is_empty() || digits(f)) => {
            let f6: String = f.chars().chain(std::iter::repeat('0')).take(6).collect();
            f6.parse().ok()?
        }
        Some(_) => return None,
    };
    NaiveTime::from_hms_micro_opt(v[0], v[1], v[2], micro)
}

/// Python 3.11's `datetime.fromisoformat` for the usual forms: `YYYY-MM-DD` or `YYYYMMDD`, then
/// any one character and a time, then `Z` or an offset. Week dates aren't read.
pub fn fromisoformat(s: &str) -> Option<IsoTime> {
    if !s.is_ascii() {
        // A non-ASCII separator is allowed by Python; nothing else is.
        let mut chars: Vec<char> = s.chars().collect();
        let at = if chars.len() > 4 && chars[4] == '-' { 10 } else { 8 };
        if chars.len() > at && !chars[at].is_ascii() {
            chars[at] = 'T';
            let fixed: String = chars.into_iter().collect();
            if fixed.is_ascii() {
                return fromisoformat(&fixed);
            }
        }
        return None;
    }
    let date_len = if s.len() > 4 && &s[4..5] == "-" { 10 } else { 8 };
    if s.len() <= date_len {
        return iso_date(s).map(|d| IsoTime::Naive(d.and_time(NaiveTime::MIN)));
    }
    let date = iso_date(&s[..date_len])?;
    let rest = &s[date_len + 1..];
    let (clock, tz) = match rest.find(['Z', '+', '-']) {
        Some(i) => (&rest[..i], Some(&rest[i..])),
        None => (rest, None),
    };
    let time = iso_clock(clock)?;
    let naive = date.and_time(time);
    let Some(tz) = tz else { return Some(IsoTime::Naive(naive)) };
    let seconds = if tz == "Z" {
        0
    } else {
        let sign = if tz.starts_with('-') { -1 } else { 1 };
        let body = &tz[1..];
        let t = iso_clock(body).filter(|_| body.len() >= 2)?;
        sign * (t.signed_duration_since(NaiveTime::MIN).num_seconds() as i32)
    };
    let off = FixedOffset::east_opt(seconds)?;
    off.from_local_datetime(&naive).single().map(IsoTime::Aware)
}

/// `datetime.isoformat()` of an aware UTC time (microseconds only when there are some).
pub fn iso_utc(t: DateTime<Utc>) -> String {
    crate::engine::py_iso_utc(t)
}

/// `int(dt.timestamp())`: seconds, cut toward zero.
pub fn int_timestamp(t: DateTime<Utc>) -> i64 {
    let s = t.timestamp();
    if s < 0 && t.timestamp_subsec_nanos() > 0 {
        s + 1
    } else {
        s
    }
}

/// The aware time of an ISO string, as `datetime.fromisoformat` reads it; None for a naive or
/// unreadable one.
pub fn aware_time(v: Option<&Value>) -> Option<DateTime<Utc>> {
    fromisoformat(v?.as_str()?)?.aware()
}

pub const MINUTE: Duration = Duration::minutes(1);

// ---- the weekly feed ----

/// ForexFactory rows -> events with a UTC time, oldest first (`parse_events`). Rows without a
/// time of day with an offset (all day, tentative) are skipped.
pub fn parse_events(payload: &[Value]) -> Vec<Value> {
    let mut out: Vec<(String, Value)> = Vec::new();
    for row in payload {
        let Some(row) = row.as_object() else { continue };
        let Some(date) = row.get("date") else { continue };
        let Some(IsoTime::Aware(when)) = fromisoformat(&py_str(date)) else { continue };
        let text = |k: &str| row.get(k).map(py_str).unwrap_or_default();
        let figure = |k: &str| row.get(k).filter(|v| truthy(v)).cloned().unwrap_or(Value::Null);
        let time = iso_utc(when.with_timezone(&Utc));
        let e = json!({
            "title": py_strip(&text("title")),
            "currency": py_strip(&text("country")).to_uppercase(),
            "impact": py_capitalize(py_strip(&text("impact"))),
            "time": time,
            "forecast": figure("forecast"),
            "previous": figure("previous"),
        });
        out.push((time, e));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0)); // stable, as sorted()
    out.into_iter().map(|(_, e)| e).collect()
}

// ---- the history's rows ----

/// A `calendar_events` row.
#[derive(Clone, Debug, PartialEq)]
pub struct CalRow {
    pub time: i64, // the release, unix seconds UTC
    pub currency: String,
    pub title: String,
    pub impact: Option<String>,
    pub forecast: Option<String>,
    pub previous: Option<String>,
    pub actual: Option<String>,
    pub source: String,
}

impl CalRow {
    pub fn to_json(&self) -> Value {
        json!({"time": self.time, "currency": self.currency, "title": self.title, "impact": self.impact,
               "forecast": self.forecast, "previous": self.previous, "actual": self.actual, "source": self.source})
    }

    /// A row as the store gives it back.
    pub fn from_json(r: &Map<String, Value>) -> Option<CalRow> {
        let s = |k: &str| r.get(k).and_then(Value::as_str).map(String::from);
        Some(CalRow { time: r.get("time")?.as_i64()?, currency: s("currency")?, title: s("title")?, impact: s("impact"),
                      forecast: s("forecast"), previous: s("previous"), actual: s("actual"),
                      source: s("source").unwrap_or_default() })
    }
}

/// `_figure`: the text, stripped and cut to 40 characters; None when empty.
fn figure(v: Option<&Value>) -> Option<String> {
    let text = match v {
        None | Some(Value::Null) => String::new(),
        Some(v) => py_strip(&py_str(v)).to_string(),
    };
    Some(py_take(&text, 40)).filter(|t| !t.is_empty())
}

/// Calendar events (ISO UTC `time`) -> `calendar_events` rows (`store_rows`).
pub fn store_rows(events: &[Value], source: &str) -> Vec<CalRow> {
    let mut out = Vec::new();
    for e in events {
        let Some(e) = e.as_object() else { continue };
        let Some(when) = e.get("time").and_then(|t| fromisoformat(&py_str(t))).and_then(IsoTime::instant) else {
            continue;
        };
        let or_empty = |k: &str| e.get(k).filter(|v| truthy(v)).map(py_str).unwrap_or_default();
        let row = CalRow {
            time: int_timestamp(when),
            currency: py_take(&or_empty("currency").to_uppercase(), 8),
            title: py_take(&py_squash(&or_empty("title")), 200),
            impact: e.get("impact").filter(|v| truthy(v)).map(py_str),
            forecast: figure(e.get("forecast")),
            previous: figure(e.get("previous")),
            actual: figure(e.get("actual")),
            source: source.to_string(),
        };
        if !row.currency.is_empty() && !row.title.is_empty() {
            out.push(row);
        }
    }
    out
}

/// `event_key`: one event type, "usd cpi m/m".
pub fn event_key(currency: &str, title: &str) -> String {
    format!("{} {}", py_strip(currency).to_uppercase(), py_squash(title)).to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_times_as_python_reads_them() {
        let utc = |s: &str| fromisoformat(s).and_then(IsoTime::aware).map(iso_utc);
        assert_eq!(utc("2026-11-16T08:30:00-05:00").as_deref(), Some("2026-11-16T13:30:00+00:00"));
        assert_eq!(utc("2026-11-16 08:30-05:00").as_deref(), Some("2026-11-16T13:30:00+00:00"));
        assert_eq!(utc("2026-11-16T08:30:00.1234567-05:00").as_deref(), Some("2026-11-16T13:30:00.123456+00:00"));
        assert_eq!(utc("2026-11-16T08-05:00").as_deref(), Some("2026-11-16T13:00:00+00:00"));
        assert_eq!(utc("2026-11-16T0830-0500").as_deref(), Some("2026-11-16T13:30:00+00:00"));
        assert_eq!(utc("20261116T083000Z").as_deref(), Some("2026-11-16T08:30:00+00:00"));
        assert_eq!(utc("2026-11-16x08:30Z").as_deref(), Some("2026-11-16T08:30:00+00:00"));
        assert_eq!(utc("2026-11-16T08:30:00,5Z").as_deref(), Some("2026-11-16T08:30:00.500000+00:00"));
        assert_eq!(utc("2026-11-16T08:30:00+05:30:15").as_deref(), Some("2026-11-16T02:59:45+00:00"));
        assert_eq!(utc("2026-11-16T08:30:00+05").as_deref(), Some("2026-11-16T03:30:00+00:00"));
        assert!(matches!(fromisoformat("2026-11-16"), Some(IsoTime::Naive(_))));
        for bad in ["2026-11-16T24:00:00Z", "2026-11-16T08:30:00z", "2026-11-16T08:30Z ", "None", "", "2026-13-01"] {
            assert_eq!(utc(bad), None, "{bad}");
        }
    }

    #[test]
    fn strings_as_python_handles_them() {
        assert_eq!(py_strip("\x1c a b \u{3000}"), "a b");
        assert_eq!(py_squash("  CPI \t m/m \n"), "CPI m/m");
        assert_eq!(py_capitalize("hIGH"), "High");
        assert_eq!(py_str(&json!(null)), "None");
        assert_eq!(py_str(&json!(0.5)), "0.5");
        assert_eq!(py_str(&json!(2.0)), "2.0");
        assert_eq!(event_key(" usd ", "CPI   m/m"), "usd cpi m/m");
    }
}
