//! How gold moved after past releases, per event type (`news_stats.py`): for each stored release
//! with M1 bars around it, on the feed clock, the move 5, 15 and 60 minutes after it (in price and
//! in ATRs of the 15M bars before it), the first 15 minutes' range, whether the first move
//! reversed, and whether the actual came in above or below the forecast.
//!
//! These describe past moves; they are not a forecast and nothing here trades.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Mutex;

use chrono::{DateTime, Duration};
use serde_json::{json, Map, Value};

use super::events::{event_key, py_digit, py_space, py_strip, CalRow};
use crate::engine::analysis::np_mean;
use crate::scan::{round_to, unix, Bar, Clock, Time};
use crate::store::Store;

pub const WINDOWS: [i64; 3] = [5, 15, 60]; // minutes after the release
pub const LIMIT: usize = 24; // most recent covered releases per event type
pub const MIN_AFTER: usize = 50; // of the 60 M1 bars after the release
pub const ATR_BARS: i64 = 14; // 15M bars before the release for the ATR
pub const CACHE_TTL: f64 = 600.0; // seconds

fn minutes(n: i64) -> Duration {
    Duration::minutes(n)
}

// ---- figures ----

/// `number`: "0.3%" -> 0.3, "-12K" -> -12000, "1.2M" -> 1200000; None when it isn't a figure
/// (`^[<>~]?\s*([+\-−]?\d+(?:[.,]\d+)?)\s*([KMBT%]?)`, any case; `\d` is any script's digit).
pub fn number(text: Option<&str>) -> Option<f64> {
    let s: Vec<char> = py_strip(text.unwrap_or("")).chars().collect();
    let mut i = 0;
    if matches!(s.first(), Some('<' | '>' | '~')) {
        i += 1;
    }
    while i < s.len() && py_space(s[i]) {
        i += 1;
    }
    let mut lit = String::new();
    if let Some(&c @ ('+' | '-' | '−')) = s.get(i) {
        lit.push(if c == '+' { '+' } else { '-' });
        i += 1;
    }
    let start = i;
    let digit = |c: char| py_digit(c).and_then(|d| char::from_digit(d, 10));
    while let Some(d) = s.get(i).and_then(|&c| digit(c)) {
        lit.push(d);
        i += 1;
    }
    if i == start {
        return None;
    }
    if matches!(s.get(i), Some('.' | ',')) && s.get(i + 1).is_some_and(|&c| digit(c).is_some()) {
        lit.push('.');
        i += 1;
        while let Some(d) = s.get(i).and_then(|&c| digit(c)) {
            lit.push(d);
            i += 1;
        }
    }
    let v: f64 = lit.parse().ok()?;
    while i < s.len() && py_space(s[i]) {
        i += 1;
    }
    let scale = match s.get(i).map(|c| c.to_ascii_uppercase()) {
        Some('K') => 1e3,
        Some('M') => 1e6,
        Some('B') => 1e9,
        Some('T') => 1e12,
        _ => return Some(v),
    };
    Some(v * scale)
}

/// `surprise`: "above", "below" or "inline" the forecast; None without both figures.
pub fn surprise(actual: Option<&str>, forecast: Option<&str>) -> Option<&'static str> {
    let (a, f) = (number(actual)?, number(forecast)?);
    Some(if a > f {
        "above"
    } else if a < f {
        "below"
    } else {
        "inline"
    })
}

// ---- one release ----

/// Gold's move after one release.
#[derive(Clone, Debug, PartialEq)]
pub struct Reaction {
    pub moves: [f64; 3], // after 5, 15, 60 minutes
    pub range15: f64,
    pub atr: Option<f64>,
    pub reversed: bool,
}

impl Reaction {
    /// As `reaction()` gives it (the moves keyed by minutes).
    pub fn to_json(&self) -> Value {
        json!({"moves": {"5": self.moves[0], "15": self.moves[1], "60": self.moves[2]}, "range15": self.range15,
               "atr": self.atr, "reversed": self.reversed})
    }
}

/// `_atr`: ATR(14) of the 15M bars (left-labelled, left-closed) made from these M1 bars.
fn atr(before: &[&Bar]) -> Option<f64> {
    let mut m15: Vec<(i64, f64, f64, f64)> = Vec::new(); // bin, high, low, close
    for b in before {
        let bin = unix(b.time).div_euclid(900);
        match m15.last_mut() {
            Some(last) if last.0 == bin => {
                last.1 = last.1.max(b.high);
                last.2 = last.2.min(b.low);
                last.3 = b.close;
            }
            _ => m15.push((bin, b.high, b.low, b.close)),
        }
    }
    if m15.len() < (ATR_BARS / 2 + 1) as usize {
        return None;
    }
    let tr: Vec<f64> = m15
        .windows(2)
        .map(|w| {
            let (prev, (_, h, l, _)) = (w[0].3, w[1]);
            (h - l).max((h - prev).abs()).max((l - prev).abs())
        })
        .collect();
    let tail = &tr[tr.len().saturating_sub(ATR_BARS as usize)..];
    let atr = np_mean(tail);
    (atr > 0.0).then_some(atr)
}

fn sign(x: f64) -> f64 {
    if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    }
}

/// `reaction`: gold's move after a release at `t0` (feed clock), or None without the bars for it:
/// a bar in the 5 minutes before, and at least 50 of the 60 after, from 2 minutes in to 55.
pub fn reaction(m1: &[Bar], t0: Time) -> Option<Reaction> {
    let before: Vec<&Bar> = m1.iter().filter(|b| b.time < t0).collect();
    let after: Vec<&Bar> = m1.iter().filter(|b| b.time >= t0 && b.time < t0 + minutes(60)).collect();
    let last = before.last()?;
    if last.time < t0 - minutes(5) {
        return None;
    }
    if after.len() < MIN_AFTER || after[0].time > t0 + minutes(2) || after[after.len() - 1].time < t0 + minutes(55) {
        return None;
    }
    let reference = last.close;
    let mut moves = [0.0; 3];
    for (i, n) in WINDOWS.iter().enumerate() {
        let close = after.iter().filter(|b| b.time < t0 + minutes(*n)).last()?.close;
        moves[i] = round_to(close - reference, 2);
    }
    let first: Vec<&&Bar> = after.iter().filter(|b| b.time < t0 + minutes(15)).collect();
    let high = first.iter().map(|b| b.high).fold(f64::NAN, f64::max);
    let low = first.iter().map(|b| b.low).fold(f64::NAN, f64::min);
    let window: Vec<&Bar> = before.into_iter().filter(|b| b.time >= t0 - minutes(15 * (ATR_BARS + 1))).collect();
    let atr = atr(&window);
    Some(Reaction {
        moves,
        range15: round_to(high - low, 2),
        atr: atr.map(|a| round_to(a, 2)),
        reversed: moves[0] != 0.0 && sign(moves[2]) == -sign(moves[0]),
    })
}

// ---- per event type ----

/// One covered release: its reaction, time and surprise.
#[derive(Clone, Debug)]
pub struct Covered {
    pub reaction: Reaction,
    pub time: i64,
    pub surprise: Option<&'static str>,
}

/// `_med`: the median, rounded to cents; None for no values.
fn med(mut values: Vec<f64>) -> Value {
    if values.is_empty() {
        return Value::Null;
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = values.len();
    let m = if n % 2 == 1 { values[n / 2] } else { (values[n / 2 - 1] + values[n / 2]) / 2.0 };
    json!(round_to(m, 2))
}

/// `summary_line`: "CPI m/m: median 15m range 9.40 (last 12)".
pub fn summary_line(title: &str, count: usize, range15: &Value) -> String {
    match range15.as_f64() {
        Some(r) if count > 0 => format!("{title}: median 15m range {r:.2} (last {count})"),
        _ => format!("{title}: no M1 bars around past releases yet"),
    }
}

/// `summarise`: one event type from its covered releases (newest first).
pub fn summarise(title: &str, currency: &str, rows: &[Covered], stored: usize) -> Value {
    let atr_rows: Vec<(&Covered, f64)> =
        rows.iter().filter_map(|r| r.reaction.atr.filter(|a| *a != 0.0).map(|a| (r, a))).collect();
    let per_window = |f: &dyn Fn(usize) -> Value| {
        let mut m = Map::new();
        for (i, w) in WINDOWS.iter().enumerate() {
            m.insert(w.to_string(), f(i));
        }
        Value::Object(m)
    };
    let side = |s: &str| {
        let v: Vec<&Covered> = rows.iter().filter(|r| r.surprise == Some(s)).collect();
        json!({"count": v.len(), "move15": med(v.iter().map(|r| r.reaction.moves[1]).collect())})
    };
    let range15 = med(rows.iter().map(|r| r.reaction.range15).collect());
    let summary = summary_line(title, rows.len(), &range15);
    json!({
        "title": title,
        "currency": currency,
        "count": rows.len(),
        "stored": stored,
        "last": rows.first().map(|r| r.time),
        "move": per_window(&|i| med(rows.iter().map(|r| r.reaction.moves[i].abs()).collect())),
        "move_atr": per_window(&|i| med(atr_rows.iter().map(|(r, a)| r.reaction.moves[i].abs() / a).collect())),
        "range15": range15,
        "range15_atr": med(atr_rows.iter().map(|(r, a)| r.reaction.range15 / a).collect()),
        "reversed": rows.iter().filter(|r| r.reaction.reversed).count(),
        "up15": rows.iter().filter(|r| r.reaction.moves[1] > 0.0).count(),
        "surprise": {"above": side("above"), "below": side("below")},
        "summary": summary,
    })
}

/// What the stats are cached by: clock, currencies, impacts, the bars' (source, symbol), and the
/// stored calendar's count, first and last time.
type Key = (String, Vec<String>, Vec<String>, (String, String), (i64, Option<i64>, Option<i64>));

/// Per event type stats over the stored calendar, cached for a few minutes (`NewsReactions`).
#[derive(Default)]
pub struct NewsReactions {
    cache: Mutex<HashMap<Key, (f64, Map<String, Value>)>>,
}

impl NewsReactions {
    pub fn invalidate(&self) {
        self.cache.lock().unwrap().clear();
    }

    /// Event key -> summary, over releases at least an hour old (newest event type first).
    /// `bars(start, end)` gives the M1 bars (feed clock) with open times in [start, end]; `key`
    /// names them (source, symbol); `now` is unix seconds.
    pub async fn stats<F, Fut>(&self, store: &Store, bars: F, clock: &str, currencies: &[String], impacts: &[String],
                               key: (String, String), now: f64) -> Result<Map<String, Value>, String>
    where
        F: Fn(Time, Time) -> Fut,
        Fut: Future<Output = Result<Vec<Bar>, String>>,
    {
        let bounds = store.calendar_bounds().await?;
        let ck: Key = (clock.to_string(), currencies.to_vec(), impacts.to_vec(), key, bounds);
        if let Some((at, value)) = self.cache.lock().unwrap().get(&ck) {
            if now - at < CACHE_TTL {
                return Ok(value.clone());
            }
        }
        let parsed = Clock::parse(clock)?;
        let history = store.calendar_history(currencies, impacts, Some(now as i64 - 61 * 60)).await?;
        let mut order: Vec<String> = Vec::new();
        let mut groups: HashMap<String, Vec<CalRow>> = HashMap::new();
        for e in history {
            let k = event_key(&e.currency, &e.title);
            if !groups.contains_key(&k) {
                order.push(k.clone());
            }
            groups.entry(k).or_default().push(e);
        }
        let mut out = Map::new();
        for k in order {
            let events = &groups[&k];
            let mut rows = Vec::new();
            for e in events {
                if rows.len() >= LIMIT {
                    break;
                }
                let Some(utc) = DateTime::from_timestamp(e.time, 0) else { continue };
                let t0 = parsed.utc_to_feed(utc);
                let m1 = bars(t0 - minutes(15 * (ATR_BARS + 2)), t0 + minutes(61)).await?;
                if let Some(r) = reaction(&m1, t0) {
                    rows.push(Covered { reaction: r, time: e.time,
                                        surprise: surprise(e.actual.as_deref(), e.forecast.as_deref()) });
                }
            }
            out.insert(k, summarise(&events[0].title, &events[0].currency, &rows, events.len()));
        }
        let mut cache = self.cache.lock().unwrap();
        cache.retain(|_, (at, _)| now - *at < CACHE_TTL); // expired entries would be recomputed anyway
        cache.insert(ck, (now, out.clone()));
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn figures_and_surprise() {
        assert_eq!(number(Some("0.3%")), Some(0.3));
        assert_eq!(number(Some("-12K")), Some(-12_000.0));
        assert_eq!(number(Some("1.2M")), Some(1_200_000.0));
        assert_eq!(number(Some("<0.1%")), Some(0.1));
        assert_eq!(number(Some("−0.2%")), Some(-0.2));
        assert_eq!(number(Some("1,5 k")), Some(1500.0));
        assert_eq!(number(Some("3.")), Some(3.0));
        assert_eq!(number(Some("")), None);
        assert_eq!(number(Some("n/a")), None);
        assert_eq!(number(None), None);
        assert_eq!(surprise(Some("0.4%"), Some("0.3%")), Some("above"));
        assert_eq!(surprise(Some("150K"), Some("180K")), Some("below"));
        assert_eq!(surprise(Some("0.3%"), Some("0.3%")), Some("inline"));
        assert_eq!(surprise(None, Some("0.3%")), None);
    }
}
