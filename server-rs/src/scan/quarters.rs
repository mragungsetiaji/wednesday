//! Quarterly theory blocks (`quarters.py`): the week, each day's four sessions and each
//! session's four 90-minute quarters, in New York time with the trading day starting at 18:00.

use chrono::{Datelike, Duration, NaiveDate, Timelike};
use serde_json::{json, Map, Value};

use super::candles::Bar;
use super::clock::{unix, Clock, Time};

pub const WEEKDAYS: [&str; 5] = ["Mon", "Tue", "Wed", "Thu", "Fri"];
pub const SESSIONS: [&str; 4] = ["Tokyo", "London", "NY AM", "NY PM"];
pub const QUARTERS: [&str; 4] = ["Q1", "Q2", "Q3", "Q4"];
pub const ROWS: [&str; 3] = ["week", "session", "q90"];

/// 18:00 New York + 6 h = 00:00 of the trading day.
pub fn day_shift() -> Duration {
    Duration::hours(6)
}

/// The bars of trading days with their shifted New York time (the trading day runs 00:00-24:00
/// in it): weekend bars and times the clock repeats are dropped, as in Python.
pub fn trading_bars<'a>(m1: &'a [Bar], clock: &Clock) -> (Vec<&'a Bar>, Vec<Time>) {
    let mut bars = Vec::new();
    let mut shifted = Vec::new();
    for b in m1 {
        let Some(ny) = clock.to_new_york(b.time) else { continue };
        let s = ny + day_shift();
        if s.weekday().num_days_from_monday() < 5 {
            bars.push(b);
            shifted.push(s);
        }
    }
    (bars, shifted)
}

pub struct Block {
    pub row: &'static str,
    pub label: &'static str,
    pub day: NaiveDate,
    pub start: Time, // first M1 bar (feed clock)
    pub end: Time,   // last M1 bar + 1 minute
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub live: bool,
}

impl Block {
    fn to_json(&self) -> Value {
        json!({
            "row": self.row,
            "label": self.label,
            "day": self.day.format("%Y-%m-%d").to_string(),
            "start_unix": unix(self.start),
            "end_unix": unix(self.end),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "change": super::round_to(self.close - self.open, 5),
            "live": self.live,
        })
    }
}

/// Blocks per row, oldest first.
pub fn quarter_blocks(m1: &[Bar], clock: &Clock) -> Vec<(&'static str, Vec<Block>)> {
    let (bars, shifted) = trading_bars(m1, clock);
    let Some(&last) = shifted.last() else { return ROWS.iter().map(|r| (*r, Vec::new())).collect() };
    let now = last + Duration::minutes(1);
    // Per bar and row: the block's key, its label and when its period ends (shifted time).
    let per_bar: Vec<[((NaiveDate, usize, usize), &'static str, Time); 3]> = shifted
        .iter()
        .map(|s| {
            let day = s.date();
            let minute = (s.hour() * 60 + s.minute()) as i64;
            let (session, q90) = ((minute / 360) as usize, ((minute % 360) / 90) as usize);
            let midnight = day.and_hms_opt(0, 0, 0).unwrap();
            let session_start = midnight + Duration::hours(6 * session as i64);
            [
                ((day, 9, 9), WEEKDAYS[day.weekday().num_days_from_monday() as usize], midnight + Duration::days(1)),
                ((day, session, 9), SESSIONS[session], session_start + Duration::hours(6)),
                ((day, session, q90), QUARTERS[q90], session_start + Duration::minutes(90 * (q90 as i64 + 1))),
            ]
        })
        .collect();
    ROWS.iter()
        .enumerate()
        .map(|(row, name)| {
            let mut blocks: Vec<Block> = Vec::new();
            let mut s = 0;
            while s < bars.len() {
                // A new block starts wherever the key changes between consecutive bars.
                let key = per_bar[s][row].0;
                let mut e = s + 1;
                while e < bars.len() && per_bar[e][row].0 == key {
                    e += 1;
                }
                let span = &bars[s..e];
                blocks.push(Block {
                    row: name,
                    label: per_bar[s][row].1,
                    day: key.0,
                    start: bars[s].time,
                    end: bars[e - 1].time + Duration::minutes(1),
                    open: bars[s].open,
                    high: span.iter().map(|b| b.high).fold(f64::MIN, f64::max),
                    low: span.iter().map(|b| b.low).fold(f64::MAX, f64::min),
                    close: bars[e - 1].close,
                    live: e == bars.len() && now < per_bar[s][row].2,
                });
                s = e;
            }
            (*name, blocks)
        })
        .collect()
}

pub fn quarter_stats(rows: &[(&'static str, Vec<Block>)]) -> Map<String, Value> {
    let labels: [&[&str]; 3] = [&WEEKDAYS, &SESSIONS, &QUARTERS];
    rows.iter()
        .zip(labels)
        .map(|((row, blocks), labels)| {
            let stats: Vec<Value> = labels
                .iter()
                .map(|label| {
                    let moves: Vec<f64> =
                        blocks.iter().filter(|b| !b.live && b.label == *label).map(|b| b.close - b.open).collect();
                    let avg = (!moves.is_empty())
                        .then(|| super::round_to(moves.iter().sum::<f64>() / moves.len() as f64, 5));
                    json!({"label": label, "count": moves.len(), "green": moves.iter().filter(|m| **m > 0.0).count(),
                           "avg_change": avg})
                })
                .collect();
            (row.to_string(), Value::from(stats))
        })
        .collect()
}

/// What `/api/quarters` answers: the blocks and their stats.
pub fn quarters_payload(m1: &[Bar], clock_name: &str, clock: &Clock) -> Value {
    let rows = quarter_blocks(m1, clock);
    let blocks: Map<String, Value> = rows
        .iter()
        .map(|(row, bs)| (row.to_string(), Value::from(bs.iter().map(Block::to_json).collect::<Vec<_>>())))
        .collect();
    json!({"clock": clock_name, "rows": blocks, "stats": quarter_stats(&rows)})
}
