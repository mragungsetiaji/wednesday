//! Session shading and reference levels for the price chart (`sessions.py`): killzones, the
//! Asia range, previous day / week / month high and low, and the day, week, midnight and quarter
//! opens. Set in New York time, handed to the chart as feed-clock unix seconds.

use chrono::{Datelike, Duration, NaiveDate, NaiveTime, Timelike};
use serde_json::{json, Value};

use super::candles::Bar;
use super::clock::{unix, Clock, Time};
use super::quarters::{day_shift, trading_bars};

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap()
}

/// name, start and end in New York wall time (the evening before when at or after 18:00)
pub fn killzones() -> [(&'static str, NaiveTime, NaiveTime); 4] {
    [("Asia", hm(20, 0), hm(0, 0)), ("London", hm(2, 0), hm(5, 0)), ("NY AM", hm(7, 0), hm(10, 0)),
     ("NY PM", hm(13, 30), hm(16, 0))]
}

pub const MAX_DAYS: usize = 30; // trading days of shading and daily levels sent to the chart

fn label(kind: &str) -> &'static str {
    match kind {
        "asia_high" => "Asia H",
        "asia_low" => "Asia L",
        "pdh" => "PDH",
        "pdl" => "PDL",
        "pwh" => "PWH",
        "pwl" => "PWL",
        "pmh" => "PMH",
        "pml" => "PML",
        "day_open" => "Day open",
        "week_open" => "Week open",
        "midnight_open" => "00:00 open",
        _ => "Q open",
    }
}

/// The New York wall time `at` of trading day `day` (18:00 and later: the evening before).
fn ny_wall(day: NaiveDate, at: NaiveTime) -> Time {
    let date = if at >= hm(18, 0) { day - Duration::days(1) } else { day };
    date.and_time(at)
}

#[derive(Clone, Copy, PartialEq)]
enum Freq {
    Day,
    Week,
    Month,
}

fn period_of(day: NaiveDate, f: Freq) -> NaiveDate {
    match f {
        Freq::Day => day,
        Freq::Week => day - Duration::days(day.weekday().num_days_from_monday() as i64),
        Freq::Month => day.with_day(1).unwrap(),
    }
}

fn weekend(d: NaiveDate) -> bool {
    d.weekday().num_days_from_monday() >= 5
}

/// The last weekday of the day / week / month starting at `p`.
fn period_last_day(p: NaiveDate, f: Freq) -> NaiveDate {
    match f {
        Freq::Day => p,
        Freq::Week => p + Duration::days(4),
        Freq::Month => {
            let next = if p.month() == 12 {
                NaiveDate::from_ymd_opt(p.year() + 1, 1, 1)
            } else {
                NaiveDate::from_ymd_opt(p.year(), p.month() + 1, 1)
            }
            .unwrap();
            let mut end = next - Duration::days(1);
            while weekend(end) {
                end -= Duration::days(1);
            }
            end
        }
    }
}

fn period_first_weekday(p: NaiveDate) -> NaiveDate {
    let mut d = p;
    while weekend(d) {
        d += Duration::days(1);
    }
    d
}

struct Period {
    key: NaiveDate,
    high: f64,
    low: f64,
    open: f64,
    first_t: i64,
    first_day: NaiveDate,
    last_day: NaiveDate,
}

/// Killzones and levels over the last `max_days` trading days of `m1` (closed M1 bars, feed clock).
pub fn reference_levels(m1: &[Bar], clock_name: &str, clock: &Clock, max_days: usize) -> Value {
    let (bars, shifted) = trading_bars(m1, clock);
    let mut lines: Vec<Value> = Vec::new();
    if bars.is_empty() {
        return json!({"clock": clock_name, "killzones": [], "lines": []});
    }
    let t: Vec<i64> = bars.iter().map(|b| unix(b.time)).collect();
    let day: Vec<NaiveDate> = shifted.iter().map(|s| s.date()).collect();
    let last_t = *t.last().unwrap();
    let feed = |w: Time| clock.ny_wall_to_feed_unix(w);

    // The first closed bar in [start, end) that trades through `price`.
    let first_through = |price: f64, above: bool, start: i64, end: i64| -> Option<i64> {
        let (a, b) = (t.partition_point(|&x| x < start), t.partition_point(|&x| x < end));
        (a..b).find(|&i| if above { bars[i].high > price } else { bars[i].low < price }).map(|i| t[i])
    };
    let mut line = |kind: &str, price: f64, start: i64, end: i64, sweep_from: Option<i64>, current: bool,
                    sweepable: bool| {
        let swept = sweepable
            .then(|| first_through(price, matches!(kind, "asia_high" | "pdh" | "pwh" | "pmh"),
                                   sweep_from.unwrap_or(start), end))
            .flatten();
        lines.push(json!({"kind": kind, "label": label(kind), "price": price, "start_unix": start, "end_unix": end,
                          "swept_unix": swept, "current": current}));
    };

    let mut days: Vec<NaiveDate> = day.clone();
    days.dedup();
    let shown = &days[days.len().saturating_sub(max_days)..];
    let last_day = *days.last().unwrap();

    // Killzones: every shown day's windows, including those still to come today.
    let mut zones = Vec::new();
    for d in shown {
        for (name, start, end) in killzones() {
            zones.push(json!({"name": name, "day": d.format("%Y-%m-%d").to_string(),
                              "start_unix": feed(ny_wall(*d, start)), "end_unix": feed(ny_wall(*d, end))}));
        }
    }

    let (asia_start, asia_end) = (killzones()[0].1, killzones()[0].2);
    for &d in shown {
        let (day_start, day_end, midnight) = (feed(ny_wall(d, hm(18, 0))), feed(ny_wall(d, hm(17, 0))),
                                              feed(ny_wall(d, hm(0, 0))));
        let (a_start, a_end) = (feed(ny_wall(d, asia_start)), feed(ny_wall(d, asia_end)));
        let current = d == last_day;
        let rows: Vec<usize> = (0..bars.len()).filter(|&i| day[i] == d).collect();
        // Opens: the first closed bar at or after the time, only when that bar is the one opening it.
        if let Some(&first) = rows.first() {
            if t[first] < day_start + 3600 {
                line("day_open", bars[first].open, day_start, day_end, None, current, false);
            }
        }
        if let Some(&i) = rows.iter().find(|&&i| t[i] >= midnight && t[i] < midnight + 3600) {
            line("midnight_open", bars[i].open, midnight, day_end, None, current, false);
        }
        // Asia: only once its window has closed, and only when the history covers all of it.
        if last_t + 60 >= a_end {
            let win: Vec<usize> = rows.iter().copied().filter(|&i| t[i] >= a_start && t[i] < a_end).collect();
            if let Some(&first) = win.first() {
                if t[first] < a_start + 3600 {
                    let high = win.iter().map(|&i| bars[i].high).fold(f64::MIN, f64::max);
                    let low = win.iter().map(|&i| bars[i].low).fold(f64::MAX, f64::min);
                    line("asia_high", high, a_start, day_end, Some(a_end), current, true);
                    line("asia_low", low, a_start, day_end, Some(a_end), current, true);
                }
            }
        }
    }

    // Previous day / week / month: each whole period's high and low, drawn across the next one.
    let first_shifted = shifted[0];
    for (freq, hk, lk, open_kind) in [(Freq::Day, "pdh", "pdl", None), (Freq::Week, "pwh", "pwl", Some("week_open")),
                                      (Freq::Month, "pmh", "pml", None)] {
        let mut periods: Vec<Period> = Vec::new();
        for (i, b) in bars.iter().enumerate() {
            let key = period_of(day[i], freq);
            match periods.last_mut() {
                Some(p) if p.key == key => {
                    p.high = p.high.max(b.high);
                    p.low = p.low.min(b.low);
                    p.last_day = p.last_day.max(day[i]);
                    p.first_day = p.first_day.min(day[i]);
                    p.first_t = p.first_t.min(t[i]);
                }
                _ => periods.push(Period { key, high: b.high, low: b.low, open: b.open, first_t: t[i],
                                           first_day: day[i], last_day: day[i] }),
            }
        }
        let n = periods.len();
        for (j, row) in periods.iter().enumerate() {
            let next = periods.get(j + 1);
            if row.last_day < shown[0] && next.is_some_and(|q| q.last_day < shown[0]) {
                continue; // neither it nor the period it feeds is on screen
            }
            let p_start = feed(ny_wall(row.first_day, hm(18, 0)));
            if let Some(kind) = open_kind {
                if row.first_t < p_start + 3600 && row.last_day >= shown[0] {
                    let p_end = feed(ny_wall(period_last_day(row.key, freq), hm(17, 0)));
                    line(kind, row.open, p_start, p_end, None, j == n - 1, false);
                }
            }
            let Some(q) = next else { continue };
            let whole = j > 0
                || (first_shifted <= row.first_day.and_hms_opt(1, 0, 0).unwrap()
                    && row.first_day == period_first_weekday(row.key));
            if !whole {
                continue;
            }
            let s = feed(ny_wall(q.first_day, hm(18, 0)));
            let e = feed(ny_wall(period_last_day(q.key, freq), hm(17, 0)));
            let cur = j + 1 == n - 1;
            line(hk, row.high, s, e, None, cur, true);
            line(lk, row.low, s, e, None, cur, true);
        }
    }

    // The current 90-minute quarter's open.
    let last_s = *shifted.last().unwrap();
    let minute = (last_s.hour() * 60 + last_s.minute()) as i64;
    let q_start = last_s.date().and_hms_opt(0, 0, 0).unwrap() + Duration::minutes(minute / 90 * 90);
    let q_end = q_start + Duration::minutes(90);
    if let Some(i) = (0..bars.len()).find(|&i| shifted[i] >= q_start && shifted[i] < q_end) {
        if shifted[i] < q_start + Duration::minutes(5) {
            let q_wall = q_start - day_shift();
            let (s, e) = (feed(q_wall), feed(q_wall + Duration::minutes(90)));
            line("quarter_open", bars[i].open, s, e, None, true, false);
        }
    }
    json!({"clock": clock_name, "killzones": zones, "lines": lines})
}
