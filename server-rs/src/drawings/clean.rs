//! Checking what the dashboard sends: `drawings.clean_drawing` and `drawing_alerts.clean_alert`.

use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Timelike, Utc};
use serde_json::{json, Map, Value};

use super::py;

/// The drawing kinds (`storage.DRAWING_KINDS`) with their anchor points as (fewest, most).
pub const POINTS: [(&str, usize, usize); 75] = [
    ("trendline", 2, 2), ("hline", 1, 1), ("rect", 2, 2), ("long", 2, 2), ("short", 2, 2), ("path", 2, 200),
    ("text", 1, 1), ("ray", 2, 2), ("extendedLine", 2, 2), ("infoLine", 2, 2), ("trendAngle", 2, 2),
    ("horizontalRay", 1, 1), ("verticalLine", 1, 1), ("crossLine", 1, 1), ("parallelChannel", 3, 3),
    ("regression", 2, 2), ("disjointChannel", 3, 3), ("flatTopBottom", 3, 3), ("fibRetracement", 2, 2),
    ("fibExtension", 3, 3), ("fibChannel", 3, 3), ("fibTimeZones", 2, 2), ("fibFan", 2, 2), ("fibArcs", 2, 2),
    ("fibCircles", 2, 2), ("fibSpiral", 2, 2), ("fibWedge", 3, 3), ("pitchfork", 3, 3), ("schiffPitchfork", 3, 3),
    ("modSchiffFork", 3, 3), ("gannFan", 2, 2), ("gannBox", 2, 2), ("gannSquare", 2, 2), ("pitchfan", 3, 3),
    ("cyclicLines", 2, 2), ("timeCycles", 2, 2), ("sineLine", 2, 2), ("xabcd", 5, 5), ("abcd", 4, 4),
    ("headShoulders", 7, 7), ("cypher", 5, 5), ("threeDrives", 7, 7), ("elliottWave", 6, 6),
    ("elliottImpulse", 6, 6), ("elliottCorrect", 4, 4), ("elliottTriangle", 6, 6), ("elliottDouble", 4, 4),
    ("elliottTriple", 6, 6), ("forecast", 2, 2), ("projection", 3, 3), ("barsPattern", 2, 2),
    ("priceRange", 2, 2), ("dateRange", 2, 2), ("datePriceRange", 2, 2), ("measure", 2, 2),
    ("rotatedRect", 3, 3), ("circle", 2, 2), ("ellipse", 2, 2), ("triangle", 3, 3), ("polyline", 2, 200),
    ("curve", 3, 3), ("arc", 3, 3), ("brush", 2, 2000), ("highlighter", 2, 2000), ("priceLabel", 1, 1),
    ("arrow", 2, 2), ("note", 1, 1), ("callout", 2, 2), ("flag", 1, 1), ("arrowMark", 1, 1), ("icon", 1, 1),
    ("signpost", 1, 1), ("balloon", 1, 1), ("anchoredText", 1, 1), ("anchoredNote", 1, 1),
];
/// Tools anchored to the screen: props.ax and props.ay, shares of the chart's width and height.
const ANCHORED: [&str; 2] = ["anchoredText", "anchoredNote"];
const DASHES: [&str; 3] = ["solid", "dashed", "dotted"];
const MAX_TEXT: usize = 500;
const POSITIONS: [&str; 2] = ["long", "short"];

/// The conditions an alert can watch, per drawing kind (`CONDITIONS`).
pub const CONDITIONS: [(&str, &[&str]); 3] = [
    ("hline", &["cross_up", "cross_down", "cross"]),
    ("trendline", &["cross_up", "cross_down", "cross"]),
    ("rect", &["enter", "exit"]),
];
const MODES: [&str; 2] = ["once", "every"];
const MAX_NOTE: usize = 200;

pub fn conditions(kind: &str) -> Option<&'static [&'static str]> {
    CONDITIONS.iter().find(|(k, _)| *k == kind).map(|(_, c)| *c)
}

/// A regex `^...$` match in Python: `$` also matches before a final newline.
fn full(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

fn good_id(id: &str) -> bool {
    let s = full(id);
    (8..=36).contains(&s.len()) && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
}

fn good_color(c: &str) -> bool {
    let s = full(c);
    s.len() == 7 && s.starts_with('#') && s[1..].bytes().all(|c| c.is_ascii_hexdigit())
}

/// A storable row from the dashboard's JSON (without source and symbol, with `updated_at` now);
/// Err with the message Python's ValueError carries.
pub fn clean_drawing(drawing_id: &str, body: &Map<String, Value>) -> Result<Map<String, Value>, String> {
    let mut row = clean_drawing_at(drawing_id, body)?;
    row.insert("updated_at".into(), crate::engine::py_iso_utc(Utc::now()).into());
    Ok(row)
}

/// `clean_drawing` without `updated_at`.
pub fn clean_drawing_at(drawing_id: &str, body: &Map<String, Value>) -> Result<Map<String, Value>, String> {
    if !good_id(drawing_id) {
        return Err("Bad drawing id".into());
    }
    let kind_v = body.get("kind").unwrap_or(&Value::Null);
    let Some(&(kind, low, high)) = kind_v.as_str().and_then(|k| POINTS.iter().find(|(n, _, _)| *n == k)) else {
        return Err(format!("Unknown drawing kind {}", py::repr(kind_v)));
    };
    let raw = match body.get("points") {
        Some(Value::Array(a)) if (low..=high).contains(&a.len()) => a,
        _ => {
            return Err(if low == high {
                format!("A {kind} takes {low} point(s)")
            } else {
                format!("A {kind} takes {low} to {high} points")
            })
        }
    };
    let mut points: Vec<(i64, f64)> = Vec::with_capacity(raw.len());
    for pt in raw {
        let field = |k: &str| pt.as_object().and_then(|o| o.get(k));
        let (t, p) = match (field("t").and_then(py::int), field("p").and_then(py::float)) {
            (Some(t), Some(p)) => (t, p),
            _ => return Err("Each point needs a time t and a price p".into()),
        };
        if !p.is_finite() {
            return Err("A point's price must be a number".into());
        }
        points.push((t, p));
    }
    let props_in = body.get("props").and_then(Value::as_object);
    let text_of = || props_in.and_then(|p| p.get("text"));
    let props: Value = if POSITIONS.contains(&kind) {
        position_props(kind, &mut points, props_in)?
    } else if kind == "text" {
        match text_of() {
            Some(Value::String(t)) if !py::strip(t).is_empty() && py::len(t) <= MAX_TEXT => json!({"text": t}),
            _ => return Err(format!("A text needs props.text, 1 to {MAX_TEXT} characters")),
        }
    } else {
        // Lines, rectangles and paths may carry a short label ("H1 OB"); an empty one is dropped.
        let label = match text_of() {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) if py::len(s) <= MAX_TEXT => Some(s),
            Some(_) => return Err(format!("A label (props.text) must be text, at most {MAX_TEXT} characters")),
        };
        let mut props = match label {
            Some(s) if !py::strip(s).is_empty() => Some(json!({"text": s})),
            _ => None,
        };
        if ANCHORED.contains(&kind) {
            // `body.get("props") or {}`: a non-object that isn't empty makes Python fail (500); read as {}.
            let mut m = props.and_then(|p| p.as_object().cloned()).unwrap_or_default();
            for key in ["ax", "ay"] {
                let v = props_in.and_then(|p| p.get(key)).unwrap_or(&json!(0.1)).clone();
                let x = match py::number(&v) {
                    Some(x) if x.is_finite() => x,
                    _ => return Err(format!("props.{key} must be a number from 0 to 1")),
                };
                let x = if x > 0.0 { x } else { 0.0 };
                let x = if x < 1.0 { x } else { 1.0 };
                m.insert(key.into(), json!(x));
            }
            props = Some(Value::Object(m));
        }
        props.unwrap_or(Value::Null)
    };
    let style = match body.get("style") {
        Some(v) if py::truthy(v) => match v {
            Value::Object(m) => m.clone(),
            _ => return Err("style must be an object".into()),
        },
        _ => Map::new(),
    };
    let color = style.get("color").cloned().unwrap_or(Value::Null);
    match &color {
        Value::Null => {}
        Value::String(s) if good_color(s) => {}
        _ => return Err("style.color must be #rrggbb or null".into()),
    }
    let width = style.get("width").cloned().unwrap_or(json!(1));
    if !py::number(&width).is_some_and(|w| (1.0..=4.0).contains(&w)) {
        return Err("style.width must be 1 to 4".into());
    }
    let dash = style.get("dash").cloned().unwrap_or(json!("solid"));
    if !dash.as_str().is_some_and(|d| DASHES.contains(&d)) {
        return Err(format!("style.dash must be one of {}", DASHES.join(", ")));
    }
    let size = style.get("size").cloned().unwrap_or(Value::Null);
    if !size.is_null() && !py::number(&size).is_some_and(|s| (8.0..=48.0).contains(&s)) {
        return Err("style.size (text) must be 8 to 48".into());
    }
    let timeframes = body.get("timeframes").cloned().unwrap_or(Value::Null);
    if !timeframes.is_null() && !timeframes.as_array().is_some_and(|a| a.iter().all(Value::is_string)) {
        return Err("timeframes must be a list of names or null".into());
    }
    let mut style_out = Map::new();
    style_out.insert("color".into(), color);
    style_out.insert("width".into(), width);
    style_out.insert("dash".into(), dash);
    if !size.is_null() {
        style_out.insert("size".into(), size);
    }
    let mut row = Map::new();
    row.insert("id".into(), drawing_id.into());
    row.insert("kind".into(), kind.into());
    row.insert("points".into(), points.iter().map(|&(t, p)| json!({"t": t, "p": p})).collect());
    row.insert("style".into(), Value::Object(style_out));
    row.insert("props".into(), props);
    row.insert("timeframes".into(), timeframes);
    row.insert("locked".into(), body.get("locked").is_some_and(py::truthy).into());
    row.insert("hidden".into(), body.get("hidden").is_some_and(py::truthy).into());
    Ok(row)
}

/// Stop and target of a position, each on its own side of the entry (`_position_props`).
fn position_props(kind: &str, points: &mut [(i64, f64)], props: Option<&Map<String, Value>>) -> Result<Value, String> {
    let get = |k: &str| props.and_then(|p| p.get(k)).and_then(py::float);
    let (Some(stop), Some(target)) = (get("stop"), get("target")) else {
        return Err("A position needs props.stop and props.target prices".into());
    };
    if !(stop.is_finite() && target.is_finite()) {
        return Err("A position's stop and target must be numbers".into());
    }
    let entry = points[0].1;
    points[1].1 = entry; // both points sit at the entry; the second one only marks the end
    let (below, above) = if kind == "long" { (stop, target) } else { (target, stop) };
    if !(below < entry && entry < above) {
        let side = if kind == "long" { "below" } else { "above" };
        return Err(format!("A {kind} position's stop must be {side} the entry and its target on the other side"));
    }
    Ok(json!({"stop": stop, "target": target}))
}

/// The API shape of a stored row: without the source and symbol it's filed under.
pub fn drawing_payload(row: &Map<String, Value>) -> Value {
    Value::Object(row.iter().filter(|(k, _)| *k != "source" && *k != "symbol").map(|(k, v)| (k.clone(), v.clone())).collect())
}

// ---- alerts ----

/// A storable alert for a drawing of `kind` (`clean_alert`, with `created_at` now); `armed_bar`:
/// the feed-clock unix time of the last closed M1 bar now (only later bars count).
pub fn clean_alert(kind: &str, body: &Map<String, Value>, armed_bar: Option<i64>) -> Result<Map<String, Value>, String> {
    let mut row = clean_alert_at(kind, body, armed_bar, Utc::now())?;
    row.insert("created_at".into(), crate::engine::py_iso_utc(Utc::now()).into());
    Ok(row)
}

/// `clean_alert` at `now`, without `created_at`.
pub fn clean_alert_at(kind: &str, body: &Map<String, Value>, armed_bar: Option<i64>, now: DateTime<Utc>)
    -> Result<Map<String, Value>, String> {
    let Some(allowed) = conditions(kind) else {
        return Err("Alerts work on horizontal lines, trendlines and rectangles".into());
    };
    let condition = match body.get("condition").and_then(Value::as_str) {
        Some(c) if allowed.contains(&c) => c,
        _ => return Err(format!("A {kind} alert's condition is one of {}", allowed.join(", "))),
    };
    let mode = match body.get("mode") {
        None => "once",
        Some(Value::String(m)) if MODES.contains(&m.as_str()) => m.as_str(),
        Some(_) => return Err(format!("mode is one of {}", MODES.join(", "))),
    };
    let note = match body.get("note") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if py::len(s) <= MAX_NOTE => Some(s.as_str()),
        Some(_) => return Err(format!("The note is text of up to {MAX_NOTE} characters")),
    };
    let expires = match body.get("expires_at") {
        None | Some(Value::Null) => Value::Null,
        Some(v) => match timestamp(v) {
            Stamp::Bad => return Err("expires_at is an ISO date and time, or null".into()),
            Stamp::NaT => "NaT".into(),
            Stamp::At(ts) => {
                if ts <= now {
                    return Err("The expiry is in the past".into());
                }
                iso_ns(ts).into()
            }
        },
    };
    let note = note.map(py::strip).filter(|n| !n.is_empty());
    let mut row = Map::new();
    row.insert("condition".into(), condition.into());
    row.insert("mode".into(), mode.into());
    row.insert("note".into(), note.into());
    row.insert("expires_at".into(), expires);
    row.insert("intrabar".into(), body.get("intrabar").is_some_and(py::truthy).into());
    row.insert("armed".into(), true.into());
    row.insert("armed_bar".into(), armed_bar.into());
    row.insert("fired_at".into(), Value::Null);
    row.insert("fired_price".into(), Value::Null);
    row.insert("fires".into(), 0.into());
    Ok(row)
}

/// What `pd.Timestamp(v)` makes of a value, in UTC.
pub enum Stamp {
    At(DateTime<Utc>),
    NaT,
    Bad,
}

/// `pd.Timestamp(v)`, localized to UTC when naive. Strings: ISO dates and times (`2026-06-01`,
/// `2026-06-01 10:30`, `2026-06-01T10:30:00.5+07:00`, `...Z`); numbers are nanoseconds since 1970.
pub fn timestamp(v: &Value) -> Stamp {
    match v {
        Value::String(s) => parse_iso(s),
        Value::Number(n) => {
            let ns = match n.as_i64() {
                Some(i) => i,
                None => match n.as_f64() {
                    Some(f) if f.is_finite() && f.abs() < 9.2e18 => f as i64,
                    _ => return Stamp::Bad,
                },
            };
            Stamp::At(DateTime::from_timestamp_nanos(ns))
        }
        _ => Stamp::Bad,
    }
}

fn parse_iso(s: &str) -> Stamp {
    if matches!(s, "" | "NaT" | "nat" | "NAT" | "nan" | "NaN" | "NAN") {
        return Stamp::NaT;
    }
    parse_iso_at(s).map_or(Stamp::Bad, Stamp::At)
}

fn digits(s: &str) -> Option<u32> {
    (!s.is_empty() && s.bytes().all(|c| c.is_ascii_digit())).then(|| s.parse().ok()).flatten()
}

fn parse_iso_at(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim();
    let (date, rest) = match s.find(['T', ' ']) {
        Some(i) => (&s[..i], Some(&s[i + 1..])),
        None => (s, None),
    };
    let date = if date.len() == 8 && digits(date).is_some() {
        NaiveDate::from_ymd_opt(date[..4].parse().ok()?, date[4..6].parse().ok()?, date[6..].parse().ok()?)?
    } else {
        let mut it = date.split('-');
        let (y, m, d) = (it.next()?, it.next()?, it.next()?);
        if it.next().is_some() || y.len() != 4 || m.len() > 2 || d.len() > 2 {
            return None;
        }
        NaiveDate::from_ymd_opt(digits(y)? as i32, digits(m)?, digits(d)?)?
    };
    let (time, offset) = match rest {
        None => (NaiveTime::MIN, None),
        Some(rest) => {
            let (clock, offset) = match rest.find(['Z', 'z', '+', '-']) {
                Some(i) => (&rest[..i], Some(&rest[i..])),
                None => (rest, None),
            };
            let (hms, frac) = match clock.split_once('.') {
                Some((a, f)) => (a, Some(f)),
                None => (clock, None),
            };
            let parts: Vec<&str> = hms.split(':').collect();
            if parts.len() > 3 || parts.iter().any(|p| p.len() != 2) || (frac.is_some() && parts.len() != 3) {
                return None;
            }
            let n = |i: usize| parts.get(i).map_or(Some(0), |p| digits(p));
            let nanos = match frac {
                None => 0,
                Some(f) if (1..=9).contains(&f.len()) => digits(f)? * 10u32.pow(9 - f.len() as u32),
                Some(_) => return None,
            };
            (NaiveTime::from_hms_nano_opt(n(0)?, n(1)?, n(2)?, nanos)?, offset)
        }
    };
    let naive = NaiveDateTime::new(date, time);
    let seconds = match offset {
        None | Some("Z") | Some("z") => 0,
        Some(o) => {
            let sign = if o.starts_with('-') { -1 } else { 1 };
            let o = &o[1..];
            let (h, m) = match o.len() {
                2 => (o, "00"),
                4 => (&o[..2], &o[2..]),
                5 if &o[2..3] == ":" => (&o[..2], &o[3..]),
                _ => return None,
            };
            let (h, m) = (digits(h)?, digits(m)?);
            if h > 23 || m > 59 {
                return None;
            }
            sign * (h * 3600 + m * 60) as i32
        }
    };
    Some(Utc.from_utc_datetime(&naive) - chrono::Duration::seconds(seconds as i64))
}

/// `Timestamp.isoformat()` of a UTC time: nanoseconds when there are some, else microseconds when
/// there are some, else whole seconds.
pub fn iso_ns(t: DateTime<Utc>) -> String {
    let ns = t.nanosecond() % 1_000_000_000;
    let base = t.format("%Y-%m-%dT%H:%M:%S").to_string();
    if ns % 1000 != 0 {
        format!("{base}.{ns:09}+00:00")
    } else if ns != 0 {
        format!("{base}.{:06}+00:00", ns / 1000)
    } else {
        format!("{base}+00:00")
    }
}
