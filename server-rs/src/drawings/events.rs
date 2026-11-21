//! When a drawing's alert fires, and what it says (`drawing_alerts.py`: `line_price`, `bar_events`,
//! `tick_event`, `describe`, `format_message`).

use serde_json::{Map, Value};

use super::py;
use crate::scan::{unix, Bar};

/// The parts of a drawing an alert reads.
#[derive(Clone, Debug, PartialEq)]
pub struct Drawing {
    pub id: String,
    pub kind: String,
    pub points: Vec<(i64, f64)>, // (feed-clock unix seconds, price)
}

impl Drawing {
    /// From a drawing row (`Store::drawings`) or the same JSON.
    pub fn from_row(row: &Map<String, Value>) -> Option<Drawing> {
        let points = row.get("points")?.as_array()?.iter().map(|p| {
            let t = p.get("t")?;
            let t = t.as_i64().or_else(|| t.as_f64().map(|f| f as i64))?;
            Some((t, p.get("p")?.as_f64()?))
        }).collect::<Option<Vec<_>>>()?;
        Some(Drawing {
            id: row.get("id").and_then(Value::as_str).unwrap_or("").to_string(),
            kind: row.get("kind")?.as_str()?.to_string(),
            points,
        })
    }

    /// (bottom, top, left) of a rectangle (`_box`).
    fn bounds(&self) -> (f64, f64, i64) {
        let ps = self.points.iter().map(|p| p.1);
        let bottom = ps.clone().fold(f64::INFINITY, f64::min);
        let top = ps.fold(f64::NEG_INFINITY, f64::max);
        (bottom, top, self.points.iter().map(|p| p.0).min().unwrap_or(0))
    }
}

/// The price of a line drawing at feed-clock time `t`; None before a trendline's first point.
pub fn line_price(d: &Drawing, t: i64) -> Option<f64> {
    if d.kind == "hline" {
        return Some(d.points[0].1);
    }
    let (mut a, mut b) = (d.points[0], d.points[1]);
    if b.0 < a.0 {
        std::mem::swap(&mut a, &mut b);
    }
    if t < a.0 {
        return None;
    }
    if b.0 == a.0 {
        return Some(b.1);
    }
    Some(a.1 + (b.1 - a.1) * (t - a.0) as f64 / (b.0 - a.0) as f64)
}

/// (bar unix, level, what) for each closed bar meeting the alert's condition; `prev_close`: the
/// close of the bar before the first one (None: the first bar only sets it).
pub fn bar_events(d: &Drawing, condition: &str, bars: &[Bar], mut prev_close: Option<f64>) -> Vec<(i64, f64, &'static str)> {
    let mut out = Vec::new();
    for bar in bars {
        let (t, hi, lo, close) = (unix(bar.time), bar.high, bar.low, bar.close);
        if let Some(prev) = prev_close {
            if d.kind == "rect" {
                let (bottom, top, left) = d.bounds();
                let inside = bottom <= prev && prev <= top;
                if t >= left {
                    if condition == "enter" && !inside && hi >= bottom && lo <= top {
                        out.push((t, if prev > top { top } else { bottom }, "entered"));
                    } else if condition == "exit" && inside && !(bottom <= close && close <= top) {
                        out.push((t, if close > top { top } else { bottom }, "left"));
                    }
                }
            } else if let Some(level) = line_price(d, t) {
                if matches!(condition, "cross_up" | "cross") && prev < level && level <= hi {
                    out.push((t, level, "crossed above"));
                } else if matches!(condition, "cross_down" | "cross") && prev > level && level >= lo {
                    out.push((t, level, "crossed below"));
                }
            }
        }
        prev_close = Some(close);
    }
    out
}

/// (level, what) when the price moving from `before` to `now` at `t` meets the condition.
pub fn tick_event(d: &Drawing, condition: &str, before: f64, now: f64, t: i64) -> Option<(f64, &'static str)> {
    if d.kind == "rect" {
        let (bottom, top, left) = d.bounds();
        if t < left {
            return None;
        }
        let (was, is) = (bottom <= before && before <= top, bottom <= now && now <= top);
        if condition == "enter" && !was && is {
            return Some((if before > top { top } else { bottom }, "entered"));
        }
        if condition == "exit" && was && !is {
            return Some((if now > top { top } else { bottom }, "left"));
        }
        return None;
    }
    let level = line_price(d, t)?;
    if matches!(condition, "cross_up" | "cross") && before < level && level <= now {
        return Some((level, "crossed above"));
    }
    if matches!(condition, "cross_down" | "cross") && before > level && level >= now {
        return Some((level, "crossed below"));
    }
    None
}

pub fn describe(kind: &str) -> &str {
    match kind {
        "hline" => "line",
        "trendline" => "trendline",
        "rect" => "zone",
        k => k,
    }
}

/// The Telegram message (HTML) of a firing: what happened, never advice.
#[allow(clippy::too_many_arguments)]
pub fn format_message(symbol: &str, d: &Drawing, note: Option<&str>, mode: &str, level: f64, what: &str, price: f64,
                      bar_time: Option<&str>, intrabar: bool) -> String {
    let f = |v: f64| py::grouped(v, 2);
    let name = match note {
        Some(n) if !n.is_empty() => format!(" '{}'", py::escape(n)),
        _ => String::new(),
    };
    let head = if d.kind == "rect" {
        let (bottom, top, _) = d.bounds();
        format!("<b>{} {what} your zone{name}</b> {} – {}", py::escape(symbol), f(bottom), f(top))
    } else {
        format!("<b>{} {what} {}</b>, your {}{name}", py::escape(symbol), f(level), describe(&d.kind))
    };
    let when = if intrabar {
        "Intrabar (live tick, the bar hasn't closed)".to_string()
    } else {
        format!("On the closed M1 bar of {} (chart time)", bar_time.unwrap_or("None"))
    };
    let mut lines = vec![head, format!("Price {} · {when}", f(price))];
    if mode == "once" {
        lines.push("The alert is now off; re-arm it on the chart.".into());
    }
    lines.join("\n")
}
